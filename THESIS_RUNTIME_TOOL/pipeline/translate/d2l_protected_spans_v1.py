"""Deterministic protection for non-translatable spans in D2L prose blocks."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re
from typing import Any, Mapping


POLICY_ID = "d2l_protected_spans_v1"
PROMPT_VERSION = "s1_d2l_soft_glossary_v2_4_protected"

_PLACEHOLDER_PREFIX = "[[D2LPS_"
_PLACEHOLDER_RE = re.compile(r"\[\[D2LPS_[0-9]{4}\]\]")
_PROTECTED_RE = re.compile(
    r"(?P<d2l_begin>:begin_tab:`[^`\r\n]+`)"
    r"|(?P<d2l_end>:end_tab:)"
    r"|(?P<math_display>\$\$[\s\S]*?\$\$)"
    r"|(?P<math_paren>\\\([\s\S]*?\\\))"
    r"|(?P<math_bracket>\\\[[\s\S]*?\\\])"
    r"|(?P<math_inline>(?<!\\)\$(?:\\.|[^$\r\n])+(?<!\\)\$)"
    r"|(?P<sphinx_role>:[A-Za-z_][A-Za-z0-9_-]*:`[^`\r\n]+`)"
    r"|(?P<inline_code>`[^`\r\n]+`)"
    r"|(?P<html_tag><[^>\r\n]+>)"
    r"|(?P<url>https?://[^\s)<]+)"
    r"|(?P<markdown_marker>\[\*{1,3}|\*{1,3}\]|\(\*{1,3}|\*{1,3}\)|~~|\*{1,3}|(?m:^[#]{1,6}[ \t]+))"
)


class D2LProtectedSpanError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProtectedSpan:
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
    def protected_span_count(self) -> int:
        return len(self.spans)

    def spans_for_block(self, block_id: str) -> list[ProtectedSpan]:
        return [span for span in self.spans if span.block_id == block_id]

    def prompt_legend(
        self,
        block_id_aliases: Mapping[str, str] | None = None,
    ) -> str:
        lines = [
            f"PROTECTED SOURCE SPANS ({POLICY_ID})",
            "Translate the prose naturally while copying every placeholder exactly once",
            "and in the same order within its block. Output the placeholder, never the",
            "protected source bytes shown in this read-only map. Code restores those bytes.",
            "Map values are JSON-encoded source strings for understanding only.",
            "READ-ONLY MAP",
        ]
        lines.extend(
            f"- {(block_id_aliases or {}).get(span.block_id, span.block_id)} "
            f"{span.placeholder} = "
            f"{json.dumps(span.source, ensure_ascii=False)}"
            for span in self.spans
        )
        return "\n".join(lines)

    def metadata(self) -> dict[str, Any]:
        return {
            "policy_id": POLICY_ID,
            "plan_sha256": self.plan_sha256,
            "protected_span_count": self.protected_span_count,
            "block_counts": {
                block_id: len(self.spans_for_block(block_id))
                for block_id in sorted({span.block_id for span in self.spans})
            },
        }


def protect_blocks(blocks: list[Mapping[str, Any]]) -> ProtectionPlan:
    protected_blocks: list[dict[str, Any]] = []
    spans: list[ProtectedSpan] = []
    counter = 1
    for raw_block in blocks:
        block = dict(raw_block)
        block_id = str(block.get("block_id") or "")
        if not block_id:
            raise D2LProtectedSpanError("Protected block lacks block_id")
        source = str(block.get("clean_text") or block.get("source_text") or "")
        if _PLACEHOLDER_PREFIX in source:
            raise D2LProtectedSpanError(
                f"Source block contains reserved placeholder prefix: {block_id}"
            )
        pieces: list[str] = []
        cursor = 0
        for match in _PROTECTED_RE.finditer(source):
            pieces.append(source[cursor : match.start()])
            placeholder = f"[[D2LPS_{counter:04d}]]"
            pieces.append(placeholder)
            spans.append(
                ProtectedSpan(
                    block_id=block_id,
                    placeholder=placeholder,
                    kind=str(match.lastgroup),
                    source=match.group(0),
                    start=match.start(),
                    end=match.end(),
                )
            )
            counter += 1
            cursor = match.end()
        pieces.append(source[cursor:])
        block["clean_text"] = "".join(pieces)
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
    for block_id, target_value in translations.items():
        target = str(target_value)
        expected_spans = plan.spans_for_block(str(block_id))
        expected_placeholders = [span.placeholder for span in expected_spans]
        observed_placeholders = _PLACEHOLDER_RE.findall(target)
        if observed_placeholders != expected_placeholders:
            issues.append(
                ProtectedSpanIssue(
                    block_id=str(block_id),
                    issue_type="placeholder_sequence_mismatch",
                    expected=expected_placeholders,
                    observed=observed_placeholders,
                )
            )
            continue
        restored_text = target
        for span in expected_spans:
            if restored_text.count(span.placeholder) != 1:
                issues.append(
                    ProtectedSpanIssue(
                        block_id=str(block_id),
                        issue_type="placeholder_cardinality_mismatch",
                        expected=[span.placeholder],
                        observed=[str(restored_text.count(span.placeholder))],
                    )
                )
                break
            restored_text = restored_text.replace(span.placeholder, span.source, 1)
        else:
            observed_spans = [
                (str(match.lastgroup), match.group(0))
                for match in _PROTECTED_RE.finditer(restored_text)
            ]
            expected_identity = [(span.kind, span.source) for span in expected_spans]
            if observed_spans != expected_identity:
                issues.append(
                    ProtectedSpanIssue(
                        block_id=str(block_id),
                        issue_type="restored_span_sequence_mismatch",
                        expected=[f"{kind}:{value}" for kind, value in expected_identity],
                        observed=[f"{kind}:{value}" for kind, value in observed_spans],
                    )
                )
                continue
            controls = [
                f"U+{ord(ch):04X}"
                for ch in restored_text
                if ord(ch) < 32 and ch not in "\n\r\t"
            ]
            if controls:
                issues.append(
                    ProtectedSpanIssue(
                        block_id=str(block_id),
                        issue_type="forbidden_control_character",
                        expected=[],
                        observed=controls,
                    )
                )
                continue
            restored[str(block_id)] = restored_text
    return restored, issues


def protected_span_reask_note(
    issues: list[ProtectedSpanIssue],
    block_id_aliases: Mapping[str, str] | None = None,
) -> str:
    samples = "; ".join(
        f"{(block_id_aliases or {}).get(issue.block_id, issue.block_id)}:"
        f"{issue.issue_type}"
        for issue in issues[:8]
    )
    extra = "" if len(issues) <= 8 else f"; +{len(issues) - 8} more"
    return (
        "Your previous JSON changed, omitted, duplicated, or reordered protected "
        f"placeholders ({samples}{extra}). Retranslate the same full window. Copy "
        "every [[D2LPS_####]] placeholder exactly once and in source order within "
        "its block. Do not replace a placeholder with its mapped source bytes. "
        "Return the same JSON contract with no explanation."
    )
