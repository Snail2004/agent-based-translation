"""Deterministic, language-agnostic integrity checks for D2L translations."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping, Sequence

from pipeline.translate.d2l_latex_markup_line_protected_spans_v4 import (
    contains_forbidden_control,
    math_spans_in_text,
    protect_blocks,
)
from pipeline.translate.d2l_quality_gates_v2 import (
    QualityGateBlock,
    detect_quality_findings,
)


POLICY_ID = "d2l_translation_integrity_v1"
_PROTECTED_REF_RE = re.compile(
    r"\[\[(?P<kind>MATH_REF|STRUCT_REF|FORMAT_REF|LINE_REF)_[0-9]{4}"
    r"(?:\|[^|\[\]\r\n]+)?\]\]"
)


@dataclass(frozen=True)
class TranslationIntegrityFinding:
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


def inspect_translations(
    source_blocks: Sequence[Mapping[str, Any]],
    translations: Mapping[str, str],
) -> list[TranslationIntegrityFinding]:
    """Inspect exact-cover translations without making semantic judgments."""

    source_by_id: dict[str, Mapping[str, Any]] = {}
    for raw in source_blocks:
        block_id = str(raw.get("block_id") or "")
        if not block_id or block_id in source_by_id:
            raise ValueError("Integrity source block IDs must be nonempty and unique")
        source_by_id[block_id] = raw
    if set(translations) != set(source_by_id):
        missing = sorted(set(source_by_id) - set(translations))
        extra = sorted(set(translations) - set(source_by_id))
        raise ValueError(
            f"Integrity translation exact-cover mismatch; missing={missing} extra={extra}"
        )

    quality_blocks: list[QualityGateBlock] = []
    for block_id, raw in source_by_id.items():
        source = _source_text(raw)
        target = translations[block_id]
        quality_blocks.append(
            QualityGateBlock(
                block_id=block_id,
                source_text=source,
                target_text=target,
                block_type=str(raw.get("block_type") or "prose"),
                translatable=True,
            )
        )

    findings = [
        TranslationIntegrityFinding(
            block_id=row.block_id,
            issue_type=row.issue_type,
            severity=row.severity,
            evidence_source=row.evidence_source,
            evidence_target=row.evidence_target,
            details=dict(row.details),
        )
        for row in detect_quality_findings(quality_blocks)
    ]
    for block_id, raw in source_by_id.items():
        source = _source_text(raw)
        target = translations[block_id]
        findings.extend(_protected_content_findings(block_id, source, target))
    return _deduplicate(findings)


def retry_findings(
    findings: Sequence[TranslationIntegrityFinding],
) -> list[TranslationIntegrityFinding]:
    return [row for row in findings if row.severity == "major"]


def warning_findings(
    findings: Sequence[TranslationIntegrityFinding],
) -> list[TranslationIntegrityFinding]:
    return [row for row in findings if row.severity != "major"]


def render_retry_note(
    findings: Sequence[TranslationIntegrityFinding],
    *,
    block_to_slot: Mapping[str, str] | None = None,
) -> str:
    """Render bounded mechanical repair instructions without language reasoning."""

    rows = retry_findings(findings)
    samples: list[str] = []
    for row in rows[:12]:
        owner = (
            str(block_to_slot.get(row.block_id) or row.block_id)
            if block_to_slot is not None
            else row.block_id
        )
        samples.append(f"{owner}:{row.issue_type}")
    extra = "" if len(rows) <= 12 else f"; +{len(rows) - 12} more"
    return (
        "Your previous translation failed deterministic integrity checks: "
        + "; ".join(samples)
        + extra
        + ". Retranslate the same source window. Return the exact same JSON shape "
        "and IDs. Produce nonempty Vietnamese prose, preserve every formula, inline "
        "code, directive, marker, list/line boundary, and protected placeholder "
        "exactly once in source order, and do not introduce characters from a script "
        "that is absent from the source."
    )


def _protected_content_findings(
    block_id: str,
    source: str,
    target: str,
) -> list[TranslationIntegrityFinding]:
    issue_types: list[str] = []
    if contains_forbidden_control(source) or contains_forbidden_control(target):
        issue_types.append("forbidden_control_character")
    if _PROTECTED_REF_RE.search(target):
        issue_types.append("protected_reference_not_restored")
    try:
        if math_spans_in_text(source) != math_spans_in_text(target):
            issue_types.append("math_bytes_or_order_changed")
        if _structure_signature(source, block_id=block_id) != _structure_signature(
            target, block_id=block_id
        ):
            issue_types.append("protected_structure_or_order_changed")
    except Exception:
        issue_types.append("protected_content_not_parseable")
    return [
        TranslationIntegrityFinding(
            block_id=block_id,
            issue_type=issue_type,
            severity="major",
            evidence_source=_snippet(source),
            evidence_target=_snippet(target),
            details={"policy_id": POLICY_ID},
        )
        for issue_type in sorted(set(issue_types))
    ]


def _structure_signature(text: str, *, block_id: str) -> list[tuple[str, ...]]:
    plan = protect_blocks(
        [{"block_id": block_id, "source_text": text, "clean_text": text}]
    )
    base = plan.base_plan
    base_v2 = base.base_plan
    identities: dict[str, tuple[str, ...]] = {}
    for span in base_v2.spans_for_block(block_id):
        if not span.is_math:
            identities[span.placeholder] = ("base", span.kind, span.source)
    for span in base.format_spans_for_block(block_id):
        identities[span.placeholder] = (
            "format",
            span.kind,
            span.open_marker,
            span.close_marker,
        )
    for span in plan.line_spans_for_block(block_id):
        identities[span.placeholder] = ("line", span.kind, span.source)
    protected = str(plan.protected_blocks[0]["clean_text"])
    result: list[tuple[str, ...]] = []
    for match in _PROTECTED_REF_RE.finditer(protected):
        if match.group("kind") == "MATH_REF":
            continue
        placeholder = match.group(0).split("|", 1)[0]
        if not placeholder.endswith("]]"):
            placeholder += "]]"
        if placeholder not in identities:
            raise ValueError(f"Unresolved protected reference in {block_id}")
        result.extend(_canonical_structure_tokens(identities[placeholder]))
    return result


def _canonical_structure_tokens(identity: tuple[str, ...]) -> list[tuple[str, ...]]:
    """Compare adjacent Markdown delimiters by bytes, not parser grouping."""

    if identity[:2] == ("base", "markdown_marker"):
        marker = identity[2]
        if marker and all(character in "[]()*_~" for character in marker):
            return [("markdown_marker_character", character) for character in marker]
    if identity[:2] == ("format", "markdown_emphasis"):
        markers = identity[2] + identity[3]
        return [("markdown_marker_character", character) for character in markers]
    return [identity]


def _source_text(raw: Mapping[str, Any]) -> str:
    value = raw.get("clean_text") or raw.get("source_text")
    if not isinstance(value, str) or not value:
        raise ValueError(f"Integrity source text is absent: {raw.get('block_id')}")
    return value


def _snippet(value: str, limit: int = 160) -> str:
    return value if len(value) <= limit else value[:limit]


def _deduplicate(
    findings: Sequence[TranslationIntegrityFinding],
) -> list[TranslationIntegrityFinding]:
    result: list[TranslationIntegrityFinding] = []
    seen: set[tuple[str, str, str]] = set()
    for row in findings:
        key = (row.block_id, row.issue_type, row.evidence_target)
        if key in seen:
            continue
        seen.add(key)
        result.append(row)
    return result


__all__ = [
    "POLICY_ID",
    "TranslationIntegrityFinding",
    "inspect_translations",
    "render_retry_note",
    "retry_findings",
    "warning_findings",
]
