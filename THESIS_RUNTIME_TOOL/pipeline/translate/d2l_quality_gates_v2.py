"""Pure deterministic quality findings for technical translation candidates."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import re
import unicodedata
from typing import Any, Iterable, Sequence


POLICY_ID = "d2l_translation_deterministic_quality_v2"


@dataclass(frozen=True)
class DeterministicQualityPolicy:
    policy_id: str = POLICY_ID
    normalization: str = "unicode_nfkc_casefold_whitespace_collapse_v1"
    residue_min_tokens: int = 4
    residue_min_characters: int = 20
    length_min_source_alnum: int = 80
    length_min_ratio: float = 0.25
    length_max_ratio: float = 4.0

    def validate(self) -> None:
        if self.policy_id != POLICY_ID:
            raise ValueError(f"Unexpected deterministic quality policy: {self.policy_id}")
        if self.residue_min_tokens < 1 or self.residue_min_characters < 1:
            raise ValueError("Residue thresholds must be positive")
        if self.length_min_source_alnum < 1:
            raise ValueError("Length eligibility threshold must be positive")
        if not (0 < self.length_min_ratio < 1):
            raise ValueError("Minimum length ratio must be between zero and one")
        if self.length_max_ratio <= 1:
            raise ValueError("Maximum length ratio must be greater than one")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)

    def sha256(self) -> str:
        payload = json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return sha256(payload).hexdigest().upper()


DEFAULT_POLICY = DeterministicQualityPolicy()


@dataclass(frozen=True)
class QualityGateBlock:
    block_id: str
    source_text: str
    target_text: str
    block_type: str = "prose"
    translatable: bool = True
    excluded_exact_spans: tuple[str, ...] = ()

    def validate(self) -> None:
        if not self.block_id:
            raise ValueError("Quality gate block_id must be nonempty")
        if not isinstance(self.source_text, str) or not isinstance(self.target_text, str):
            raise TypeError("Quality gate source and target text must be strings")
        if not self.block_type:
            raise ValueError("Quality gate block_type must be nonempty")
        if len(set(self.excluded_exact_spans)) != len(self.excluded_exact_spans):
            raise ValueError("Excluded quality spans must be unique")
        for span in self.excluded_exact_spans:
            if not span:
                raise ValueError("Excluded quality span must be nonempty")
            source_count = self.source_text.count(span)
            target_count = self.target_text.count(span)
            if source_count == 0 or source_count != target_count:
                raise ValueError(
                    "Excluded quality spans must exact-cover source and target equally"
                )


@dataclass(frozen=True)
class QualityGateFinding:
    block_id: str
    issue_type: str
    severity: str
    evidence_source: str
    evidence_target: str
    details: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "block_id": self.block_id,
            "issue_type": self.issue_type,
            "severity": self.severity,
            "evidence_source": self.evidence_source,
            "evidence_target": self.evidence_target,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class _Token:
    normalized: str
    start: int
    end: int


def detect_quality_findings(
    blocks: Sequence[QualityGateBlock],
    *,
    policy: DeterministicQualityPolicy = DEFAULT_POLICY,
) -> list[QualityGateFinding]:
    """Return deterministic findings without making publication decisions."""

    policy.validate()
    observed: set[str] = set()
    findings: list[QualityGateFinding] = []
    for block in blocks:
        block.validate()
        if block.block_id in observed:
            raise ValueError(f"Duplicate quality gate block_id: {block.block_id}")
        observed.add(block.block_id)
        findings.extend(_find_block_issues(block, policy))
    return findings


def _find_block_issues(
    block: QualityGateBlock,
    policy: DeterministicQualityPolicy,
) -> list[QualityGateFinding]:
    source = _mask_spans(block.source_text, block.excluded_exact_spans)
    target = _mask_spans(block.target_text, block.excluded_exact_spans)
    findings: list[QualityGateFinding] = []

    for index, char in enumerate(target):
        if _is_forbidden_control(char):
            findings.append(
                QualityGateFinding(
                    block_id=block.block_id,
                    issue_type="forbidden_control_character",
                    severity="major",
                    evidence_source=_snippet(source, 0, min(len(source), 160)),
                    evidence_target=_snippet(target, index, index + 1),
                    details={"codepoint": f"U+{ord(char):04X}", "offset": index},
                )
            )

    stripped_target = target.strip()
    if block.translatable and not stripped_target:
        findings.append(
            QualityGateFinding(
                block_id=block.block_id,
                issue_type="empty_translation",
                severity="major",
                evidence_source=_snippet(source, 0, min(len(source), 160)),
                evidence_target="",
                details={},
            )
        )
        return findings

    normalized_source = _normalize_text(source)
    normalized_target = _normalize_text(target)
    equal_nonempty = bool(normalized_source) and normalized_source == normalized_target
    if block.translatable and equal_nonempty:
        issue_type = (
            "untranslated_heading"
            if block.block_type.casefold() in {"heading", "title"}
            else "target_equals_source"
        )
        findings.append(
            QualityGateFinding(
                block_id=block.block_id,
                issue_type=issue_type,
                severity="major",
                evidence_source=_snippet(source, 0, min(len(source), 160)),
                evidence_target=_snippet(target, 0, min(len(target), 160)),
                details={"normalization": policy.normalization},
            )
        )

    normalized_source_for_search = _normalize_text(source)
    for start, end, script in _non_latin_letter_spans(target):
        surface = target[start:end]
        if _normalize_text(surface) in normalized_source_for_search:
            continue
        findings.append(
            QualityGateFinding(
                block_id=block.block_id,
                issue_type="unexpected_output_script",
                severity="major",
                evidence_source=_snippet(source, 0, min(len(source), 160)),
                evidence_target=_snippet(target, start, end),
                details={
                    "script": script,
                    "surface": surface,
                    "start": start,
                    "end": end,
                },
            )
        )

    if block.translatable and not equal_nonempty:
        findings.extend(_residue_findings(block.block_id, source, target, policy))
        length_finding = _length_finding(block.block_id, source, target, policy)
        if length_finding is not None:
            findings.append(length_finding)

    return findings


def _residue_findings(
    block_id: str,
    source: str,
    target: str,
    policy: DeterministicQualityPolicy,
) -> list[QualityGateFinding]:
    source_tokens = _tokens(source)
    target_tokens = _tokens(target)
    findings: list[QualityGateFinding] = []
    target_index = 0
    while target_index < len(target_tokens):
        best_length = 0
        for source_index in range(len(source_tokens)):
            length = 0
            while (
                source_index + length < len(source_tokens)
                and target_index + length < len(target_tokens)
                and source_tokens[source_index + length].normalized
                == target_tokens[target_index + length].normalized
            ):
                length += 1
            if length > best_length:
                best_length = length
        if best_length >= policy.residue_min_tokens:
            run = target_tokens[target_index : target_index + best_length]
            character_count = sum(len(token.normalized) for token in run)
            if character_count >= policy.residue_min_characters:
                target_start = run[0].start
                target_end = run[-1].end
                target_surface = target[target_start:target_end]
                normalized_surface = [token.normalized for token in run]
                source_start, source_end = _find_token_run(source_tokens, normalized_surface)
                findings.append(
                    QualityGateFinding(
                        block_id=block_id,
                        issue_type="source_language_residue_candidate",
                        severity="candidate",
                        evidence_source=source[source_start:source_end],
                        evidence_target=target_surface,
                        details={
                            "matched_tokens": best_length,
                            "matched_characters": character_count,
                            "threshold_tokens": policy.residue_min_tokens,
                            "threshold_characters": policy.residue_min_characters,
                        },
                    )
                )
                target_index += best_length
                continue
        target_index += 1
    return findings


def _length_finding(
    block_id: str,
    source: str,
    target: str,
    policy: DeterministicQualityPolicy,
) -> QualityGateFinding | None:
    source_count = sum(char.isalnum() for char in source)
    if source_count < policy.length_min_source_alnum:
        return None
    target_count = sum(char.isalnum() for char in target)
    ratio = target_count / source_count
    if policy.length_min_ratio <= ratio <= policy.length_max_ratio:
        return None
    return QualityGateFinding(
        block_id=block_id,
        issue_type="gross_length_anomaly",
        severity="candidate",
        evidence_source=_snippet(source, 0, min(len(source), 160)),
        evidence_target=_snippet(target, 0, min(len(target), 160)),
        details={
            "source_alnum": source_count,
            "target_alnum": target_count,
            "ratio": ratio,
            "minimum_ratio": policy.length_min_ratio,
            "maximum_ratio": policy.length_max_ratio,
        },
    )


def _tokens(text: str) -> list[_Token]:
    tokens: list[_Token] = []
    start: int | None = None
    for index, char in enumerate(text):
        if char.isalnum():
            if start is None:
                start = index
            continue
        if start is not None:
            tokens.append(_make_token(text, start, index))
            start = None
    if start is not None:
        tokens.append(_make_token(text, start, len(text)))
    return tokens


def _make_token(text: str, start: int, end: int) -> _Token:
    return _Token(_normalize_text(text[start:end]), start, end)


def _find_token_run(tokens: Sequence[_Token], values: Sequence[str]) -> tuple[int, int]:
    width = len(values)
    for index in range(len(tokens) - width + 1):
        if [token.normalized for token in tokens[index : index + width]] == list(values):
            return tokens[index].start, tokens[index + width - 1].end
    raise AssertionError("Matched target token run must exist in source")


def _non_latin_letter_spans(text: str) -> Iterable[tuple[int, int, str]]:
    start: int | None = None
    active_script: str | None = None
    for index, char in enumerate(text):
        script = _non_latin_script(char)
        is_mark = unicodedata.category(char).startswith("M")
        if script is not None:
            if start is None:
                start = index
                active_script = script
            elif script != active_script:
                yield start, index, str(active_script)
                start = index
                active_script = script
            continue
        if is_mark and start is not None:
            continue
        if start is not None:
            yield start, index, str(active_script)
            start = None
            active_script = None
    if start is not None:
        yield start, len(text), str(active_script)


def _non_latin_script(char: str) -> str | None:
    if not char.isalpha():
        return None
    name = unicodedata.name(char, "")
    if not name or "LATIN" in name:
        return None
    if name.startswith(("CJK ", "HIRAGANA ", "KATAKANA ")):
        return "CJK"
    return name.split(" ", 1)[0].title()


def _normalize_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"\s+", " ", normalized).strip()


def _mask_spans(text: str, spans: Sequence[str]) -> str:
    result = str(text)
    for span in spans:
        if not span:
            raise ValueError("Excluded quality span must be nonempty")
        result = result.replace(span, " " * len(span))
    return result


def _is_forbidden_control(char: str) -> bool:
    codepoint = ord(char)
    return (
        0 <= codepoint <= 8
        or codepoint in {11, 12}
        or 14 <= codepoint <= 31
        or codepoint == 127
    )


def _snippet(text: str, start: int, end: int, radius: int = 48) -> str:
    left = max(0, start - radius)
    right = min(len(text), end + radius)
    return re.sub(r"\s+", " ", text[left:right]).strip()


__all__ = [
    "DEFAULT_POLICY",
    "POLICY_ID",
    "DeterministicQualityPolicy",
    "QualityGateBlock",
    "QualityGateFinding",
    "detect_quality_findings",
]
