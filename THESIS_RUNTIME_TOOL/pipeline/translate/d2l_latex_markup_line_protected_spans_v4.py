"""V3 opaque protection plus code-owned line-sensitive source skeletons."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re
from typing import Any, Mapping, Sequence

from pipeline.translate import d2l_latex_markup_protected_spans_v3 as v3
from pipeline.translate import d2l_latex_protected_spans_v2 as v2


POLICY_ID = "d2l_latex_markup_line_protected_spans_v4"
PROMPT_VERSION = "s1_d2l_translation_slots_v4_0_line_skeleton"
LINE_PLACEHOLDER_PREFIX = "[[LINE_REF_"

_LINE_PLACEHOLDER_RE = re.compile(r"\[\[LINE_REF_[0-9]{4}\]\]")
_ANY_COMBINED_REF_RE = re.compile(
    r"\[\[(?P<fixed>(?:MATH_REF|STRUCT_REF|LINE_REF)_[0-9]{4})\]\]"
    r"|\[\[(?P<format>FORMAT_REF_[0-9]{4})(?:\|[^|\[\]\r\n]+)?\]\]"
)
_FIXED_REF_RE = re.compile(
    r"\[\[(?:MATH_REF|STRUCT_REF|LINE_REF)_[0-9]{4}\]\]"
)
_LIST_PREFIX_RE = re.compile(
    r"(?:\A|\r?\n)[ \t]{0,3}(?:(?:[*+-])|(?:[0-9]+[.)]))[ \t]+"
)
_STANDALONE_DIRECTIVE_RE = re.compile(
    r"(?:\A|\r?\n)[ \t]*:[A-Za-z_][A-Za-z0-9_-]*:`[^`\r\n]+`[ \t]*"
    r"(?=\r?(?:\n|\Z))"
)


D2LLatexProtectionError = v2.D2LLatexProtectionError
ProtectedSpanIssue = v2.ProtectedSpanIssue


@dataclass(frozen=True)
class LineSpan:
    block_id: str
    placeholder: str
    kind: str
    source: str
    start: int
    end: int

    def identity_dict(self) -> dict[str, Any]:
        return {
            "block_id": self.block_id,
            "placeholder": self.placeholder,
            "kind": self.kind,
            "source": self.source,
            "start": self.start,
            "end": self.end,
        }


@dataclass(frozen=True)
class ProtectionPlan:
    protected_blocks: list[dict[str, Any]]
    context_blocks: list[dict[str, Any]]
    lexical_blocks: list[dict[str, Any]]
    base_plan: v3.ProtectionPlan
    line_spans: tuple[LineSpan, ...]
    plan_sha256: str

    @property
    def policy_id(self) -> str:
        return POLICY_ID

    @property
    def prompt_version(self) -> str:
        return PROMPT_VERSION

    @property
    def protected_span_count(self) -> int:
        return self.base_plan.protected_span_count + len(self.line_spans)

    @property
    def math_span_count(self) -> int:
        return self.base_plan.math_span_count

    def line_spans_for_block(self, block_id: str) -> list[LineSpan]:
        return [span for span in self.line_spans if span.block_id == block_id]

    def prompt_legend(
        self,
        block_id_aliases: Mapping[str, str] | None = None,
    ) -> str:
        base = self.base_plan.prompt_legend(block_id_aliases)
        if not self.line_spans:
            return base
        aliases = block_id_aliases or {}
        lines = [
            base,
            "",
            f"OPAQUE LINE REFERENCES ({POLICY_ID})",
            "Each LINE_REF owns source line-boundary or line-prefix bytes withheld",
            "from you. Copy every LINE_REF exactly once, in source order, and do",
            "not add line breaks inside a slot. Code restores the exact source",
            "line skeleton after local validation.",
            "LINE REFERENCE INVENTORY",
        ]
        for block_id in _ordered_line_block_ids(self.line_spans):
            refs = ", ".join(
                span.placeholder for span in self.line_spans_for_block(block_id)
            )
            lines.append(f"- {aliases.get(block_id, block_id)}: {refs}")
        return "\n".join(lines)

    def metadata(self) -> dict[str, Any]:
        base_metadata = self.base_plan.metadata()
        block_counts = dict(base_metadata.get("block_counts") or {})
        for block_id in _ordered_line_block_ids(self.line_spans):
            block_counts[block_id] = int(block_counts.get(block_id, 0)) + len(
                self.line_spans_for_block(block_id)
            )
        return {
            "policy_id": POLICY_ID,
            "prompt_version": PROMPT_VERSION,
            "plan_sha256": self.plan_sha256,
            "protected_span_count": self.protected_span_count,
            "math_span_count": self.math_span_count,
            "structural_span_count": int(
                base_metadata.get("structural_span_count") or 0
            )
            + len(self.line_spans),
            "format_span_count": int(base_metadata.get("format_span_count") or 0),
            "line_span_count": len(self.line_spans),
            "latex_visible_to_model": False,
            "block_counts": block_counts,
        }


def protect_blocks(
    blocks: Sequence[Mapping[str, Any]],
    *,
    protect_bracketed_emphasis: bool = False,
) -> ProtectionPlan:
    source_blocks = [dict(block) for block in blocks]
    context_blocks = _build_context_blocks(source_blocks)
    lexical_blocks = _build_lexical_blocks(source_blocks)
    transformed_blocks: list[dict[str, Any]] = []
    line_spans: list[LineSpan] = []
    line_counter = 1

    for raw_block in source_blocks:
        block = dict(raw_block)
        block_id = str(block.get("block_id") or "")
        if not block_id:
            raise D2LLatexProtectionError("Protected block lacks block_id")
        source = str(block.get("clean_text") or block.get("source_text") or "")
        if LINE_PLACEHOLDER_PREFIX in source:
            raise D2LLatexProtectionError(
                f"Source block contains reserved LINE_REF prefix: {block_id}"
            )
        candidates = _line_candidates(source)
        pieces: list[str] = []
        cursor = 0
        for start, end, kind in candidates:
            placeholder = f"[[LINE_REF_{line_counter:04d}]]"
            line_counter += 1
            pieces.append(source[cursor:start])
            pieces.append(placeholder)
            line_spans.append(
                LineSpan(
                    block_id=block_id,
                    placeholder=placeholder,
                    kind=kind,
                    source=source[start:end],
                    start=start,
                    end=end,
                )
            )
            cursor = end
        pieces.append(source[cursor:])
        block["clean_text"] = "".join(pieces)
        transformed_blocks.append(block)

    base_plan = v3.protect_blocks(
        transformed_blocks,
        protect_bracketed_emphasis=protect_bracketed_emphasis,
    )
    identity = {
        "policy_id": POLICY_ID,
        "base_plan_sha256": base_plan.plan_sha256,
        "line_spans": [span.identity_dict() for span in line_spans],
        "context_blocks_sha256": _blocks_sha256(context_blocks),
        "lexical_blocks_sha256": _blocks_sha256(lexical_blocks),
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
        context_blocks=context_blocks,
        lexical_blocks=lexical_blocks,
        base_plan=base_plan,
        line_spans=tuple(line_spans),
        plan_sha256=plan_sha256,
    )


def restore_translations(
    translations: Mapping[str, str],
    plan: ProtectionPlan,
) -> tuple[dict[str, str], list[ProtectedSpanIssue]]:
    valid: dict[str, str] = {}
    line_issues: list[ProtectedSpanIssue] = []
    invalid_blocks: set[str] = set()

    for raw_block_id, raw_target in translations.items():
        block_id = str(raw_block_id)
        target = str(raw_target)
        expected_spans = plan.line_spans_for_block(block_id)
        expected_refs = [span.placeholder for span in expected_spans]
        observed_refs = _LINE_PLACEHOLDER_RE.findall(target)
        if target.count(LINE_PLACEHOLDER_PREFIX) != len(observed_refs):
            line_issues.append(
                _issue(block_id, "line_placeholder_malformed", expected_refs, observed_refs)
            )
            invalid_blocks.add(block_id)
            continue
        if observed_refs != expected_refs:
            line_issues.append(
                _issue(
                    block_id,
                    "line_placeholder_sequence_mismatch",
                    expected_refs,
                    observed_refs,
                )
            )
            invalid_blocks.add(block_id)
            continue
        if expected_spans and ("\n" in target or "\r" in target):
            line_issues.append(
                _issue(block_id, "model_authored_line_break", expected_refs, [])
            )
            invalid_blocks.add(block_id)
            continue
        if expected_spans and expected_spans[0].start == 0:
            if not target.startswith(expected_spans[0].placeholder):
                line_issues.append(
                    _issue(
                        block_id,
                        "leading_line_reference_moved",
                        [expected_spans[0].placeholder],
                        observed_refs[:1],
                    )
                )
                invalid_blocks.add(block_id)
                continue
        expected_combined = _combined_refs_for_block(plan, block_id)
        observed_combined = _combined_refs(target)
        if observed_combined != expected_combined:
            line_issues.append(
                _issue(
                    block_id,
                    "combined_placeholder_sequence_mismatch",
                    expected_combined,
                    observed_combined,
                )
            )
            invalid_blocks.add(block_id)
            continue
        valid[block_id] = target

    base_restored, base_issues = v3.restore_translations(valid, plan.base_plan)
    base_issues = [
        issue
        for issue in base_issues
        if not (
            issue.issue_type == "missing_translation_block"
            and issue.block_id in invalid_blocks
        )
    ]
    final_issues = [*line_issues, *base_issues]
    restored: dict[str, str] = {}
    for block_id, target in base_restored.items():
        result = target
        failed = False
        for span in plan.line_spans_for_block(block_id):
            if result.count(span.placeholder) != 1:
                final_issues.append(
                    _issue(
                        block_id,
                        "line_restore_cardinality_mismatch",
                        [span.placeholder],
                        [str(result.count(span.placeholder))],
                    )
                )
                failed = True
                break
            result = result.replace(span.placeholder, span.source, 1)
        if failed:
            continue
        if LINE_PLACEHOLDER_PREFIX in result:
            final_issues.append(
                _issue(block_id, "line_reference_not_restored", [], [])
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
        "Your previous JSON changed protected references or line skeletons "
        f"({samples}{extra}). Retranslate the same full window. Copy every "
        "MATH_REF, STRUCT_REF, and LINE_REF exactly once and in source order. "
        "Do not add line breaks. Replace each FORMAT_REF exactly once as "
        "[[FORMAT_REF_NNNN|Vietnamese phrase]]. Return the same JSON contract "
        "with no explanation."
    )


def lexical_source_blocks(plan: ProtectionPlan) -> list[dict[str, Any]]:
    """Return source prose with math and structural identifiers blanked by code."""

    return [dict(block) for block in plan.lexical_blocks]


def context_source_blocks(plan: ProtectionPlan) -> list[dict[str, Any]]:
    """Return deterministic retrieval text while retaining inline-code terms."""

    return [dict(block) for block in plan.context_blocks]


def fixed_only_block_ids(plan: ProtectionPlan) -> set[str]:
    """Return blocks whose model-visible content contains no editable prose."""

    result: set[str] = set()
    for block in plan.protected_blocks:
        block_id = str(block.get("block_id") or "")
        text = str(block.get("clean_text") or block.get("source_text") or "")
        if block_id and _FIXED_REF_RE.sub("", text).strip() == "":
            result.add(block_id)
    return result


def fixed_source_segments(plan: ProtectionPlan) -> dict[str, list[str]]:
    """Return read-only source bytes used to police semantic-audit evidence."""

    result: dict[str, list[str]] = {}
    fixed_spans = [
        *plan.base_plan.base_plan.spans,
        *plan.line_spans,
    ]
    for span in fixed_spans:
        result.setdefault(span.block_id, []).append(span.source)
    return result


def math_spans_in_text(text: str) -> list[str]:
    return v3.math_spans_in_text(text)


def contains_forbidden_control(text: str) -> bool:
    return v3.contains_forbidden_control(text)


def _build_lexical_blocks(
    source_blocks: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    original_plan = v2.protect_blocks(source_blocks)
    result: list[dict[str, Any]] = []
    for raw_block in source_blocks:
        block = dict(raw_block)
        block_id = str(block.get("block_id") or "")
        source = str(block.get("clean_text") or block.get("source_text") or "")
        pieces: list[str] = []
        cursor = 0
        for span in original_plan.spans_for_block(block_id):
            pieces.append(source[cursor : span.start])
            pieces.append(_blank_preserving_lines(span.source))
            cursor = span.end
        pieces.append(source[cursor:])
        lexical_text = "".join(pieces)
        block["clean_text"] = lexical_text
        block["source_text"] = lexical_text
        result.append(block)
    return result


def _build_context_blocks(
    source_blocks: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    original_plan = v2.protect_blocks(source_blocks)
    result: list[dict[str, Any]] = []
    for raw_block in source_blocks:
        block = dict(raw_block)
        block_id = str(block.get("block_id") or "")
        source = str(block.get("clean_text") or block.get("source_text") or "")
        pieces: list[str] = []
        cursor = 0
        for span in original_plan.spans_for_block(block_id):
            pieces.append(source[cursor : span.start])
            pieces.append(
                span.source
                if span.kind == "inline_code"
                else _blank_preserving_lines(span.source)
            )
            cursor = span.end
        pieces.append(source[cursor:])
        context_text = "".join(pieces)
        block["clean_text"] = context_text
        block["source_text"] = context_text
        result.append(block)
    return result


def _blank_preserving_lines(value: str) -> str:
    return "".join(character if character in "\r\n" else " " for character in value)


def _line_candidates(source: str) -> list[tuple[int, int, str]]:
    candidates = [
        (match.start(), match.end(), "list_prefix")
        for match in _LIST_PREFIX_RE.finditer(source)
    ]
    candidates.extend(
        (match.start(), match.end(), "standalone_sphinx_directive")
        for match in _STANDALONE_DIRECTIVE_RE.finditer(source)
    )
    selected: list[tuple[int, int, str]] = []
    previous_end = -1
    for candidate in sorted(candidates, key=lambda row: (row[0], -(row[1] - row[0]))):
        if candidate[0] < previous_end:
            continue
        selected.append(candidate)
        previous_end = candidate[1]
    return selected


def _combined_refs(text: str) -> list[str]:
    refs: list[str] = []
    for match in _ANY_COMBINED_REF_RE.finditer(text):
        identity = match.group("fixed") or match.group("format")
        refs.append(f"[[{identity}]]")
    return refs


def _combined_refs_for_block(plan: ProtectionPlan, block_id: str) -> list[str]:
    for block in plan.protected_blocks:
        if str(block.get("block_id") or "") == block_id:
            return _combined_refs(str(block.get("clean_text") or ""))
    return []


def _ordered_line_block_ids(spans: Sequence[LineSpan]) -> list[str]:
    result: list[str] = []
    for span in spans:
        if span.block_id not in result:
            result.append(span.block_id)
    return result


def _blocks_sha256(blocks: Sequence[Mapping[str, Any]]) -> str:
    payload = [
        {
            "block_id": str(block.get("block_id") or ""),
            "clean_text": str(block.get("clean_text") or ""),
        }
        for block in blocks
    ]
    return sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


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
    "LINE_PLACEHOLDER_PREFIX",
    "POLICY_ID",
    "PROMPT_VERSION",
    "LineSpan",
    "ProtectionPlan",
    "ProtectedSpanIssue",
    "contains_forbidden_control",
    "context_source_blocks",
    "fixed_only_block_ids",
    "fixed_source_segments",
    "lexical_source_blocks",
    "math_spans_in_text",
    "protect_blocks",
    "protected_span_reask_note",
    "restore_translations",
]
