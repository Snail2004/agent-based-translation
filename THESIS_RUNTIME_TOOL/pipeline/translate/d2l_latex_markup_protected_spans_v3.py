"""Opaque LaTeX protection with code-restored simple Markdown wrappers."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re
from typing import Any, Mapping, Sequence

from pipeline.translate import d2l_latex_protected_spans_v2 as v2


POLICY_ID = "d2l_latex_markup_protected_spans_v3"
PROMPT_VERSION = "s1_d2l_translation_slots_v3_2_latex_hidden_markup_wrapped"
FORMAT_PLACEHOLDER_PREFIX = "[[FORMAT_REF_"

_EXPANDED_FORMAT_RE = re.compile(
    r"\[\[(FORMAT_REF_[0-9]{4})\|([^|\[\]\r\n]+)\]\]"
)
_ANY_COMBINED_REF_RE = re.compile(
    r"\[\[(?:MATH_REF|STRUCT_REF|FORMAT_REF)_[0-9]{4}\]\]"
)
_FORMAT_PATTERNS = (
    (
        "markdown_strong_emphasis",
        "***",
        re.compile(
            r"(?<![\\*])\*\*\*(?!\*)(?P<inner>[^*\r\n]+?)(?<!\\)\*\*\*(?!\*)"
        ),
    ),
    (
        "markdown_strong",
        "**",
        re.compile(
            r"(?<![\\*])\*\*(?!\*)(?P<inner>[^*\r\n]+?)(?<!\\)\*\*(?!\*)"
        ),
    ),
    (
        "markdown_emphasis",
        "*",
        re.compile(
            r"(?<![\\*])\*(?!\*)(?P<inner>[^*\r\n]+?)(?<!\\)\*(?!\*)"
        ),
    ),
    (
        "markdown_strikethrough",
        "~~",
        re.compile(
            r"(?<![\\~])~~(?!~)(?P<inner>[^~\r\n]+?)(?<!\\)~~(?!~)"
        ),
    ),
)
_EXCLUDED_V2_KINDS = {
    "d2l_begin",
    "d2l_end",
    "sphinx_role",
    "inline_code",
    "html_tag",
    "url",
}
_FORBIDDEN_FORMAT_PAYLOAD_CHARS = set("*~`$\\[]|")


D2LLatexProtectionError = v2.D2LLatexProtectionError
ProtectedSpanIssue = v2.ProtectedSpanIssue


@dataclass(frozen=True)
class FormatSpan:
    block_id: str
    placeholder: str
    kind: str
    source_inner: str
    open_marker: str
    close_marker: str
    start: int
    end: int

    def identity_dict(self) -> dict[str, Any]:
        return {
            "block_id": self.block_id,
            "placeholder": self.placeholder,
            "kind": self.kind,
            "source_inner": self.source_inner,
            "open_marker": self.open_marker,
            "close_marker": self.close_marker,
            "start": self.start,
            "end": self.end,
        }


@dataclass(frozen=True)
class ProtectionPlan:
    protected_blocks: list[dict[str, Any]]
    base_plan: v2.ProtectionPlan
    format_spans: tuple[FormatSpan, ...]
    plan_sha256: str

    @property
    def policy_id(self) -> str:
        return POLICY_ID

    @property
    def prompt_version(self) -> str:
        return PROMPT_VERSION

    @property
    def protected_span_count(self) -> int:
        return self.base_plan.protected_span_count + len(self.format_spans)

    @property
    def math_span_count(self) -> int:
        return self.base_plan.math_span_count

    def format_spans_for_block(self, block_id: str) -> list[FormatSpan]:
        return [span for span in self.format_spans if span.block_id == block_id]

    def prompt_legend(
        self,
        block_id_aliases: Mapping[str, str] | None = None,
    ) -> str:
        base = self.base_plan.prompt_legend(block_id_aliases)
        if not self.format_spans:
            return base
        aliases = block_id_aliases or {}
        lines = [
            base,
            "",
            f"TRANSLATABLE FORMAT REFERENCES ({POLICY_ID})",
            "Each bare FORMAT_REF stands for one source phrase whose Markdown",
            "wrapper is withheld. Replace it in place with exactly",
            "[[FORMAT_REF_NNNN|Vietnamese phrase]]. Translate only the listed",
            "phrase. Do not add Markdown, TeX, brackets, or another reference",
            "inside the phrase. Code restores the original wrapper after validation.",
            "FORMAT REFERENCE INVENTORY",
        ]
        for block_id in _ordered_format_block_ids(self.format_spans):
            alias = aliases.get(block_id, block_id)
            for span in self.format_spans_for_block(block_id):
                source = json.dumps(span.source_inner, ensure_ascii=False)
                lines.append(f"- {alias}: {span.placeholder} source phrase={source}")
        return "\n".join(lines)

    def metadata(self) -> dict[str, Any]:
        base_metadata = self.base_plan.metadata()
        block_ids = _ordered_format_block_ids(self.format_spans)
        block_counts = dict(base_metadata.get("block_counts") or {})
        for block_id in block_ids:
            block_counts[block_id] = int(block_counts.get(block_id, 0)) + len(
                self.format_spans_for_block(block_id)
            )
        return {
            "policy_id": POLICY_ID,
            "prompt_version": PROMPT_VERSION,
            "plan_sha256": self.plan_sha256,
            "protected_span_count": self.protected_span_count,
            "math_span_count": self.math_span_count,
            "structural_span_count": int(
                base_metadata.get("structural_span_count") or 0
            ),
            "format_span_count": len(self.format_spans),
            "latex_visible_to_model": False,
            "block_counts": block_counts,
        }


def protect_blocks(blocks: Sequence[Mapping[str, Any]]) -> ProtectionPlan:
    source_blocks = [dict(block) for block in blocks]
    original_plan = v2.protect_blocks(source_blocks)
    format_spans: list[FormatSpan] = []
    transformed_blocks: list[dict[str, Any]] = []
    format_counter = 1

    for raw_block in source_blocks:
        block = dict(raw_block)
        block_id = str(block.get("block_id") or "")
        source = str(block.get("clean_text") or block.get("source_text") or "")
        if FORMAT_PLACEHOLDER_PREFIX in source:
            raise D2LLatexProtectionError(
                f"Source block contains reserved FORMAT_REF prefix: {block_id}"
            )
        excluded = [
            (span.start, span.end)
            for span in original_plan.spans_for_block(block_id)
            if span.is_math or span.kind in _EXCLUDED_V2_KINDS
        ]
        candidates = _simple_format_candidates(source, excluded=excluded)
        pieces: list[str] = []
        cursor = 0
        for start, end, kind, marker, source_inner in candidates:
            placeholder = f"[[FORMAT_REF_{format_counter:04d}]]"
            format_counter += 1
            pieces.append(source[cursor:start])
            pieces.append(placeholder)
            format_spans.append(
                FormatSpan(
                    block_id=block_id,
                    placeholder=placeholder,
                    kind=kind,
                    source_inner=source_inner,
                    open_marker=marker,
                    close_marker=marker,
                    start=start,
                    end=end,
                )
            )
            cursor = end
        pieces.append(source[cursor:])
        block["clean_text"] = "".join(pieces)
        transformed_blocks.append(block)

    base_plan = v2.protect_blocks(transformed_blocks)
    identity = {
        "policy_id": POLICY_ID,
        "base_plan_sha256": base_plan.plan_sha256,
        "format_spans": [span.identity_dict() for span in format_spans],
    }
    plan_sha256 = sha256(
        json.dumps(
            identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return ProtectionPlan(
        protected_blocks=base_plan.protected_blocks,
        base_plan=base_plan,
        format_spans=tuple(format_spans),
        plan_sha256=plan_sha256,
    )


def restore_translations(
    translations: Mapping[str, str],
    plan: ProtectionPlan,
) -> tuple[dict[str, str], list[ProtectedSpanIssue]]:
    sanitized: dict[str, str] = {}
    payloads_by_block: dict[str, dict[str, str]] = {}
    format_issues: list[ProtectedSpanIssue] = []
    invalid_blocks: set[str] = set()

    for raw_block_id, raw_target in translations.items():
        block_id = str(raw_block_id)
        target = str(raw_target)
        expected = plan.format_spans_for_block(block_id)
        expected_refs = [span.placeholder for span in expected]
        matches = list(_EXPANDED_FORMAT_RE.finditer(target))
        observed_refs = [f"[[{match.group(1)}]]" for match in matches]

        if target.count(FORMAT_PLACEHOLDER_PREFIX) != len(matches):
            format_issues.append(
                _issue(
                    block_id,
                    "format_placeholder_malformed",
                    expected_refs,
                    observed_refs,
                )
            )
            invalid_blocks.add(block_id)
            continue
        if observed_refs != expected_refs:
            format_issues.append(
                _issue(
                    block_id,
                    "format_placeholder_sequence_mismatch",
                    expected_refs,
                    observed_refs,
                )
            )
            invalid_blocks.add(block_id)
            continue

        payloads: dict[str, str] = {}
        payload_bad = False
        for match, span in zip(matches, expected):
            payload = match.group(2)
            if not _valid_format_payload(payload):
                format_issues.append(
                    _issue(
                        block_id,
                        "format_payload_invalid",
                        [span.placeholder],
                        [payload],
                    )
                )
                invalid_blocks.add(block_id)
                payload_bad = True
                break
            payloads[span.placeholder] = payload
        if payload_bad:
            continue

        pieces: list[str] = []
        cursor = 0
        for match, span in zip(matches, expected):
            pieces.append(target[cursor : match.start()])
            pieces.append(span.placeholder)
            cursor = match.end()
        pieces.append(target[cursor:])
        sanitized_target = "".join(pieces)
        expected_combined = _combined_refs_for_block(plan, block_id)
        observed_combined = _ANY_COMBINED_REF_RE.findall(sanitized_target)
        if observed_combined != expected_combined:
            format_issues.append(
                _issue(
                    block_id,
                    "combined_placeholder_sequence_mismatch",
                    expected_combined,
                    observed_combined,
                )
            )
            invalid_blocks.add(block_id)
            continue
        sanitized[block_id] = sanitized_target
        payloads_by_block[block_id] = payloads

    base_restored, base_issues = v2.restore_translations(sanitized, plan.base_plan)
    base_issues = [
        issue
        for issue in base_issues
        if not (
            issue.issue_type == "missing_translation_block"
            and issue.block_id in invalid_blocks
        )
    ]

    restored: dict[str, str] = {}
    final_issues = [*format_issues, *base_issues]
    for block_id, target in base_restored.items():
        result = target
        cardinality_bad = False
        for span in plan.format_spans_for_block(block_id):
            if result.count(span.placeholder) != 1:
                final_issues.append(
                    _issue(
                        block_id,
                        "format_restore_cardinality_mismatch",
                        [span.placeholder],
                        [str(result.count(span.placeholder))],
                    )
                )
                cardinality_bad = True
                break
            payload = payloads_by_block[block_id][span.placeholder]
            replacement = f"{span.open_marker}{payload}{span.close_marker}"
            result = result.replace(span.placeholder, replacement, 1)
        if cardinality_bad:
            continue
        if FORMAT_PLACEHOLDER_PREFIX in result:
            final_issues.append(
                _issue(
                    block_id,
                    "format_reference_not_restored",
                    [],
                    [FORMAT_PLACEHOLDER_PREFIX],
                )
            )
            continue
        if v2.contains_forbidden_control(result):
            final_issues.append(
                _issue(block_id, "forbidden_control_character", [], [])
            )
            continue
        restored[block_id] = result
    return restored, final_issues


def protected_span_reask_note(
    issues: Sequence[ProtectedSpanIssue],
    block_id_aliases: Mapping[str, str] | None = None,
) -> str:
    aliases = block_id_aliases or {}
    samples = "; ".join(
        f"{aliases.get(issue.block_id, issue.block_id)}:{issue.issue_type}"
        for issue in list(issues)[:8]
    )
    extra = "" if len(issues) <= 8 else f"; +{len(issues) - 8} more"
    return (
        "Your previous JSON changed protected references or format slots "
        f"({samples}{extra}). Retranslate the same full window. Copy every "
        "MATH_REF and STRUCT_REF exactly once and in source order. Replace every "
        "FORMAT_REF exactly once in place as "
        "[[FORMAT_REF_NNNN|Vietnamese phrase]]. Do not put Markdown, TeX, "
        "brackets, or another reference inside that phrase. Return the same JSON "
        "contract with no explanation."
    )


def math_spans_in_text(text: str) -> list[str]:
    return v2.math_spans_in_text(text)


def contains_forbidden_control(text: str) -> bool:
    return v2.contains_forbidden_control(text)


def _simple_format_candidates(
    source: str,
    *,
    excluded: Sequence[tuple[int, int]],
) -> list[tuple[int, int, str, str, str]]:
    candidates: list[tuple[int, int, str, str, str]] = []
    for kind, marker, pattern in _FORMAT_PATTERNS:
        for match in pattern.finditer(source):
            start, end = match.span()
            inner = match.group("inner")
            if _overlaps(start, end, excluded) or not _eligible_source_inner(inner):
                continue
            candidates.append((start, end, kind, marker, inner))

    selected: list[tuple[int, int, str, str, str]] = []
    previous_end = -1
    for candidate in sorted(candidates, key=lambda row: (row[0], row[1])):
        if candidate[0] < previous_end:
            continue
        selected.append(candidate)
        previous_end = candidate[1]
    return selected


def _eligible_source_inner(inner: str) -> bool:
    if not inner or inner != inner.strip():
        return False
    if not any(character.isalpha() for character in inner):
        return False
    if FORMAT_PLACEHOLDER_PREFIX in inner:
        return False
    return not any(character in _FORBIDDEN_FORMAT_PAYLOAD_CHARS for character in inner)


def _valid_format_payload(payload: str) -> bool:
    if not payload or payload != payload.strip():
        return False
    if not any(character.isalpha() for character in payload):
        return False
    if v2.contains_forbidden_control(payload):
        return False
    return not any(character in _FORBIDDEN_FORMAT_PAYLOAD_CHARS for character in payload)


def _overlaps(
    start: int,
    end: int,
    excluded: Sequence[tuple[int, int]],
) -> bool:
    return any(start < other_end and other_start < end for other_start, other_end in excluded)


def _ordered_format_block_ids(spans: Sequence[FormatSpan]) -> list[str]:
    result: list[str] = []
    for span in spans:
        if span.block_id not in result:
            result.append(span.block_id)
    return result


def _combined_refs_for_block(plan: ProtectionPlan, block_id: str) -> list[str]:
    for block in plan.protected_blocks:
        if str(block.get("block_id") or "") == block_id:
            return _ANY_COMBINED_REF_RE.findall(str(block.get("clean_text") or ""))
    return []


def _issue(
    block_id: str,
    issue_type: str,
    expected: list[str],
    observed: list[str],
) -> ProtectedSpanIssue:
    return ProtectedSpanIssue(
        block_id=block_id,
        issue_type=issue_type,
        expected=expected,
        observed=observed,
    )


__all__ = [
    "D2LLatexProtectionError",
    "FORMAT_PLACEHOLDER_PREFIX",
    "POLICY_ID",
    "PROMPT_VERSION",
    "FormatSpan",
    "ProtectionPlan",
    "ProtectedSpanIssue",
    "contains_forbidden_control",
    "math_spans_in_text",
    "protect_blocks",
    "protected_span_reask_note",
    "restore_translations",
]
