"""Opaque, byte-exact LaTeX and structural protection for D2L translation."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re
from typing import Any, Mapping, Sequence


POLICY_ID = "d2l_latex_protected_spans_v2"
PROMPT_VERSION = "s1_d2l_translation_slots_v3_1_latex_hidden"
MATH_PLACEHOLDER_PREFIX = "[[MATH_REF_"
STRUCT_PLACEHOLDER_PREFIX = "[[STRUCT_REF_"

_MATH_PLACEHOLDER_RE = re.compile(r"\[\[MATH_REF_[0-9]{4}\]\]")
_STRUCT_PLACEHOLDER_RE = re.compile(r"\[\[STRUCT_REF_[0-9]{4}\]\]")
_ANY_PLACEHOLDER_RE = re.compile(
    r"\[\[(?:MATH_REF|STRUCT_REF)_[0-9]{4}\]\]"
)
_RAW_TEX_COMMAND_RE = re.compile(r"(?<!\\)\\[A-Za-z]+")
_STRUCTURAL_RE = re.compile(
    r"(?P<d2l_begin>:begin_tab:`[^`\r\n]+`)"
    r"|(?P<d2l_end>:end_tab:)"
    r"|(?P<sphinx_role>:[A-Za-z_][A-Za-z0-9_-]*:`[^`\r\n]+`)"
    r"|(?P<inline_code>`[^`\r\n]+`)"
    r"|(?P<html_tag><[^>\r\n]+>)"
    r"|(?P<url>https?://[^\s)<]+)"
    r"|(?P<markdown_marker>\[\*{1,3}|\*{1,3}\]|\(\*{1,3}|\*{1,3}\)|~~|\*{1,3}|(?m:^[#]{1,6}[ \t]+))"
)


class D2LLatexProtectionError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProtectedSpan:
    block_id: str
    placeholder: str
    kind: str
    source: str
    start: int
    end: int

    @property
    def is_math(self) -> bool:
        return self.kind.startswith("math_")

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
class ProtectedSpanIssue:
    block_id: str
    issue_type: str
    expected: list[str]
    observed: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "block_id": self.block_id,
            "issue_type": self.issue_type,
            "expected": self.expected,
            "observed": self.observed,
        }


@dataclass(frozen=True)
class ProtectionPlan:
    protected_blocks: list[dict[str, Any]]
    spans: tuple[ProtectedSpan, ...]
    plan_sha256: str

    @property
    def policy_id(self) -> str:
        return POLICY_ID

    @property
    def prompt_version(self) -> str:
        return PROMPT_VERSION

    @property
    def protected_span_count(self) -> int:
        return len(self.spans)

    @property
    def math_span_count(self) -> int:
        return sum(1 for span in self.spans if span.is_math)

    def spans_for_block(self, block_id: str) -> list[ProtectedSpan]:
        return [span for span in self.spans if span.block_id == block_id]

    def prompt_legend(
        self,
        block_id_aliases: Mapping[str, str] | None = None,
    ) -> str:
        aliases = block_id_aliases or {}
        lines = [
            f"OPAQUE PROTECTED REFERENCES ({POLICY_ID})",
            "Each MATH_REF or STRUCT_REF replaces source bytes withheld from you.",
            "Translate only the visible prose. Copy every reference exactly once and",
            "in source order within its slot. Never expand, rewrite, or invent a reference.",
            "Code restores the source bytes after local validation.",
            "REFERENCE INVENTORY",
        ]
        for block_id in _ordered_block_ids(self.spans):
            refs = ", ".join(
                span.placeholder for span in self.spans_for_block(block_id)
            )
            lines.append(f"- {aliases.get(block_id, block_id)}: {refs}")
        return "\n".join(lines)

    def metadata(self) -> dict[str, Any]:
        block_ids = sorted({span.block_id for span in self.spans})
        return {
            "policy_id": POLICY_ID,
            "prompt_version": PROMPT_VERSION,
            "plan_sha256": self.plan_sha256,
            "protected_span_count": self.protected_span_count,
            "math_span_count": self.math_span_count,
            "structural_span_count": self.protected_span_count - self.math_span_count,
            "latex_visible_to_model": False,
            "block_counts": {
                block_id: len(self.spans_for_block(block_id))
                for block_id in block_ids
            },
        }


def protect_blocks(blocks: Sequence[Mapping[str, Any]]) -> ProtectionPlan:
    protected_blocks: list[dict[str, Any]] = []
    spans: list[ProtectedSpan] = []
    math_counter = 1
    struct_counter = 1

    for raw_block in blocks:
        block = dict(raw_block)
        block_id = str(block.get("block_id") or "")
        if not block_id:
            raise D2LLatexProtectionError("Protected block lacks block_id")
        source = str(block.get("clean_text") or block.get("source_text") or "")
        _raise_for_forbidden_controls(source, owner=f"source block {block_id}")
        if (
            MATH_PLACEHOLDER_PREFIX in source
            or STRUCT_PLACEHOLDER_PREFIX in source
        ):
            raise D2LLatexProtectionError(
                f"Source block contains reserved placeholder prefix: {block_id}"
            )

        selected = _selected_spans(source, block_id=block_id)

        pieces: list[str] = []
        cursor = 0
        for start, end, kind in selected:
            pieces.append(source[cursor:start])
            if kind.startswith("math_"):
                placeholder = f"[[MATH_REF_{math_counter:04d}]]"
                math_counter += 1
            else:
                placeholder = f"[[STRUCT_REF_{struct_counter:04d}]]"
                struct_counter += 1
            pieces.append(placeholder)
            spans.append(
                ProtectedSpan(
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
        protected_text = "".join(pieces)
        _raise_for_visible_latex(protected_text, owner=f"protected block {block_id}")
        block["clean_text"] = protected_text
        protected_blocks.append(block)

    identity = {
        "policy_id": POLICY_ID,
        "spans": [span.identity_dict() for span in spans],
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
        protected_blocks=protected_blocks,
        spans=tuple(spans),
        plan_sha256=plan_sha256,
    )


def restore_translations(
    translations: Mapping[str, str],
    plan: ProtectionPlan,
) -> tuple[dict[str, str], list[ProtectedSpanIssue]]:
    restored: dict[str, str] = {}
    issues: list[ProtectedSpanIssue] = []
    expected_blocks = {str(block.get("block_id") or "") for block in plan.protected_blocks}

    for block_id, target_value in translations.items():
        block_id = str(block_id)
        target = str(target_value)
        expected_spans = plan.spans_for_block(block_id)
        expected_placeholders = [span.placeholder for span in expected_spans]
        observed_placeholders = _ANY_PLACEHOLDER_RE.findall(target)
        if observed_placeholders != expected_placeholders:
            issues.append(
                ProtectedSpanIssue(
                    block_id=block_id,
                    issue_type="placeholder_sequence_mismatch",
                    expected=expected_placeholders,
                    observed=observed_placeholders,
                )
            )
            continue

        controls = _forbidden_control_codepoints(target)
        if controls:
            issues.append(
                ProtectedSpanIssue(
                    block_id=block_id,
                    issue_type="forbidden_control_character",
                    expected=[],
                    observed=controls,
                )
            )
            continue

        added_math = _model_authored_math(target)
        if added_math:
            issues.append(
                ProtectedSpanIssue(
                    block_id=block_id,
                    issue_type="model_added_math",
                    expected=[],
                    observed=added_math,
                )
            )
            continue

        restored_text = target
        cardinality_bad = False
        for span in expected_spans:
            count = restored_text.count(span.placeholder)
            if count != 1:
                issues.append(
                    ProtectedSpanIssue(
                        block_id=block_id,
                        issue_type="placeholder_cardinality_mismatch",
                        expected=[span.placeholder],
                        observed=[str(count)],
                    )
                )
                cardinality_bad = True
                break
            restored_text = restored_text.replace(span.placeholder, span.source, 1)
        if cardinality_bad:
            continue

        try:
            restored_all = _selected_spans(restored_text, block_id=block_id)
            restored_math = [
                span for span in restored_all if span[2].startswith("math_")
            ]
        except D2LLatexProtectionError as exc:
            issues.append(
                ProtectedSpanIssue(
                    block_id=block_id,
                    issue_type="restored_delimiter_invalid",
                    expected=[],
                    observed=[str(exc)],
                )
            )
            continue

        observed_identity = [
            (kind, restored_text[start:end]) for start, end, kind in restored_all
        ]
        expected_identity = [(span.kind, span.source) for span in expected_spans]
        if observed_identity != expected_identity:
            issues.append(
                ProtectedSpanIssue(
                    block_id=block_id,
                    issue_type="restored_span_sequence_mismatch",
                    expected=[f"{kind}:{value}" for kind, value in expected_identity],
                    observed=[f"{kind}:{value}" for kind, value in observed_identity],
                )
            )
            continue

        source_math = [span.source for span in expected_spans if span.is_math]
        restored_math_values = [
            restored_text[start:end] for start, end, _ in restored_math
        ]
        if restored_math_values != source_math:
            issues.append(
                ProtectedSpanIssue(
                    block_id=block_id,
                    issue_type="restored_math_sequence_mismatch",
                    expected=source_math,
                    observed=restored_math_values,
                )
            )
            continue

        controls = _forbidden_control_codepoints(restored_text)
        if controls:
            issues.append(
                ProtectedSpanIssue(
                    block_id=block_id,
                    issue_type="forbidden_control_character",
                    expected=[],
                    observed=controls,
                )
            )
            continue
        restored[block_id] = restored_text

    missing_blocks = sorted(expected_blocks - {str(key) for key in translations})
    for block_id in missing_blocks:
        issues.append(
            ProtectedSpanIssue(
                block_id=block_id,
                issue_type="missing_translation_block",
                expected=[block_id],
                observed=[],
            )
        )
    return restored, issues


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
        "Your previous JSON changed protected references or introduced math/TeX "
        f"({samples}{extra}). Retranslate the same full window. Copy every "
        "MATH_REF and STRUCT_REF exactly once and in source order within its slot. "
        "Do not expand a reference or author any formula, TeX command, or new "
        "placeholder. Return the same JSON contract with no explanation."
    )


def math_spans_in_text(text: str) -> list[str]:
    """Return exact math spans in source order, raising on malformed delimiters."""

    return [
        text[start:end]
        for start, end, kind in _selected_spans(text, block_id="<text>")
        if kind.startswith("math_")
    ]


def contains_forbidden_control(text: str) -> bool:
    return bool(_forbidden_control_codepoints(text))


def _ordered_block_ids(spans: Sequence[ProtectedSpan]) -> list[str]:
    result: list[str] = []
    for span in spans:
        if span.block_id not in result:
            result.append(span.block_id)
    return result


def _structural_spans(text: str) -> list[tuple[int, int, str]]:
    return [
        (match.start(), match.end(), str(match.lastgroup))
        for match in _STRUCTURAL_RE.finditer(text)
    ]


def _selected_spans(text: str, *, block_id: str) -> list[tuple[int, int, str]]:
    structural = _structural_spans(text)
    opaque_structural = [
        span for span in structural if span[2] != "markdown_marker"
    ]
    math = _math_spans(text, excluded=opaque_structural)
    math_ranges = [(start, end) for start, end, _ in math]
    markdown = [
        span
        for span in structural
        if span[2] == "markdown_marker"
        and not _overlaps_any(span[0], span[1], math_ranges)
    ]
    selected_structural = sorted(
        [*opaque_structural, *markdown],
        key=lambda row: (row[0], row[1]),
    )
    return _merge_nonoverlapping_spans(
        selected_structural,
        math,
        block_id=block_id,
    )


def _math_spans(
    text: str,
    *,
    excluded: Sequence[tuple[int, int, str]],
) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []
    excluded_ranges = [(start, end) for start, end, _ in excluded]
    i = 0
    while i < len(text):
        covering_end = _covering_end(i, excluded_ranges)
        if covering_end is not None:
            i = covering_end
            continue

        if text.startswith(r"\)", i) or text.startswith(r"\]", i):
            if not _is_escaped(text, i):
                raise D2LLatexProtectionError(
                    f"Unexpected closing math delimiter at offset {i}"
                )

        if text.startswith(r"\(", i) and not _is_escaped(text, i):
            end = _find_token_close(text, i + 2, r"\)", excluded_ranges)
            if end is None:
                raise D2LLatexProtectionError(
                    f"Unclosed \\( math delimiter at offset {i}"
                )
            spans.append((i, end + 2, "math_paren"))
            i = end + 2
            continue

        if text.startswith(r"\[", i) and not _is_escaped(text, i):
            end = _find_token_close(text, i + 2, r"\]", excluded_ranges)
            if end is None:
                raise D2LLatexProtectionError(
                    f"Unclosed \\[ math delimiter at offset {i}"
                )
            spans.append((i, end + 2, "math_bracket"))
            i = end + 2
            continue

        if text.startswith("$$", i) and not _is_escaped(text, i):
            end = _find_dollar_close(text, i + 2, display=True, excluded=excluded_ranges)
            if end is None:
                raise D2LLatexProtectionError(
                    f"Unclosed $$ math delimiter at offset {i}"
                )
            spans.append((i, end + 2, "math_display"))
            i = end + 2
            continue

        if text[i] == "$" and not _is_escaped(text, i):
            end = _find_dollar_close(text, i + 1, display=False, excluded=excluded_ranges)
            if end is None:
                if _looks_like_literal_dollar(text, i):
                    i += 1
                    continue
                raise D2LLatexProtectionError(
                    f"Unclosed $ math delimiter at offset {i}"
                )
            content = text[i + 1 : end]
            if not _valid_inline_math_content(content):
                if _looks_like_literal_dollar(text, i):
                    i += 1
                    continue
                raise D2LLatexProtectionError(
                    f"Invalid inline math delimiter at offset {i}"
                )
            spans.append((i, end + 1, "math_inline"))
            i = end + 1
            continue
        i += 1
    return spans


def _find_token_close(
    text: str,
    start: int,
    token: str,
    excluded: Sequence[tuple[int, int]],
) -> int | None:
    cursor = start
    while True:
        index = text.find(token, cursor)
        if index < 0:
            return None
        covering_end = _covering_end(index, excluded)
        if covering_end is not None:
            cursor = covering_end
            continue
        if not _is_escaped(text, index):
            return index
        cursor = index + len(token)


def _find_dollar_close(
    text: str,
    start: int,
    *,
    display: bool,
    excluded: Sequence[tuple[int, int]],
) -> int | None:
    cursor = start
    token = "$$" if display else "$"
    while cursor < len(text):
        index = text.find(token, cursor)
        if index < 0:
            return None
        covering_end = _covering_end(index, excluded)
        if covering_end is not None:
            cursor = covering_end
            continue
        if _is_escaped(text, index):
            cursor = index + len(token)
            continue
        if not display and text.startswith("$$", index):
            raise D2LLatexProtectionError(
                f"Mixed $ and $$ delimiters at offset {index}"
            )
        return index
    return None


def _covering_end(position: int, spans: Sequence[tuple[int, int]]) -> int | None:
    for start, end in spans:
        if start <= position < end:
            return end
        if start > position:
            break
    return None


def _overlaps_any(
    start: int,
    end: int,
    spans: Sequence[tuple[int, int]],
) -> bool:
    return any(start < other_end and other_start < end for other_start, other_end in spans)


def _is_escaped(text: str, position: int) -> bool:
    slashes = 0
    index = position - 1
    while index >= 0 and text[index] == "\\":
        slashes += 1
        index -= 1
    return slashes % 2 == 1


def _looks_like_literal_dollar(text: str, position: int) -> bool:
    tail = text[position + 1 :]
    if not tail:
        return True
    if tail[0].isspace() or tail[0] in ",.;:!?)]}":
        return True
    return re.match(r"[ \t]*[0-9](?:[0-9,._]*)(?:\b|$)", tail) is not None


def _valid_inline_math_content(content: str) -> bool:
    return bool(content) and "\n" not in content and content == content.strip()


def _merge_nonoverlapping_spans(
    structural: Sequence[tuple[int, int, str]],
    math: Sequence[tuple[int, int, str]],
    *,
    block_id: str,
) -> list[tuple[int, int, str]]:
    combined = sorted([*structural, *math], key=lambda row: (row[0], row[1]))
    previous_end = -1
    for start, end, _ in combined:
        if start < previous_end:
            raise D2LLatexProtectionError(
                f"Overlapping protected spans in block {block_id} at offset {start}"
            )
        previous_end = end
    return combined


def _model_authored_math(target: str) -> list[str]:
    observed: list[str] = []
    try:
        observed.extend(math_spans_in_text(target))
    except D2LLatexProtectionError as exc:
        observed.append(str(exc))
    observed.extend(match.group(0) for match in _RAW_TEX_COMMAND_RE.finditer(target))
    return observed


def _raise_for_visible_latex(text: str, *, owner: str) -> None:
    if _RAW_TEX_COMMAND_RE.search(text):
        raise D2LLatexProtectionError(f"{owner} retains a raw TeX command")
    try:
        remaining_math = math_spans_in_text(text)
    except D2LLatexProtectionError as exc:
        raise D2LLatexProtectionError(f"{owner} has malformed math: {exc}") from exc
    if remaining_math:
        raise D2LLatexProtectionError(f"{owner} retains visible math")


def _forbidden_control_codepoints(text: str) -> list[str]:
    return [
        f"U+{ord(char):04X}"
        for char in text
        if (
            0 <= ord(char) <= 8
            or ord(char) in {11, 12}
            or 14 <= ord(char) <= 31
            or ord(char) == 127
        )
    ]


def _raise_for_forbidden_controls(text: str, *, owner: str) -> None:
    controls = _forbidden_control_codepoints(text)
    if controls:
        raise D2LLatexProtectionError(
            f"{owner} contains forbidden controls: {', '.join(controls)}"
        )


__all__ = [
    "D2LLatexProtectionError",
    "MATH_PLACEHOLDER_PREFIX",
    "POLICY_ID",
    "PROMPT_VERSION",
    "ProtectionPlan",
    "ProtectedSpan",
    "ProtectedSpanIssue",
    "contains_forbidden_control",
    "math_spans_in_text",
    "protect_blocks",
    "protected_span_reask_note",
    "restore_translations",
]
