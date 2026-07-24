"""V4 protection with code-owned bracketed emphasis and fixed-only output."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Any, Mapping, Sequence

from pipeline.translate import d2l_latex_markup_line_protected_spans_v4 as v4


POLICY_ID = "d2l_latex_markup_line_protected_spans_v5"
PROMPT_VERSION = "s1_d2l_translation_slots_v5_0_bracketed_fixed_only"

D2LLatexProtectionError = v4.D2LLatexProtectionError
ProtectedSpanIssue = v4.ProtectedSpanIssue


@dataclass(frozen=True)
class ProtectionPlan:
    inner_plan: v4.ProtectionPlan
    plan_sha256: str

    @property
    def protected_blocks(self) -> list[dict[str, Any]]:
        return self.inner_plan.protected_blocks

    @property
    def context_blocks(self) -> list[dict[str, Any]]:
        return self.inner_plan.context_blocks

    @property
    def lexical_blocks(self) -> list[dict[str, Any]]:
        return self.inner_plan.lexical_blocks

    @property
    def protected_span_count(self) -> int:
        return self.inner_plan.protected_span_count

    @property
    def math_span_count(self) -> int:
        return self.inner_plan.math_span_count

    @property
    def policy_id(self) -> str:
        return POLICY_ID

    @property
    def prompt_version(self) -> str:
        return PROMPT_VERSION

    def prompt_legend(
        self,
        block_id_aliases: Mapping[str, str] | None = None,
    ) -> str:
        return self.inner_plan.prompt_legend(block_id_aliases).replace(
            v4.POLICY_ID,
            POLICY_ID,
        )

    def metadata(self) -> dict[str, Any]:
        metadata = dict(self.inner_plan.metadata())
        metadata.update(
            {
                "policy_id": POLICY_ID,
                "prompt_version": PROMPT_VERSION,
                "plan_sha256": self.plan_sha256,
                "bracketed_emphasis_code_owned": True,
                "fixed_only_output_code_owned": True,
            }
        )
        return metadata


def protect_blocks(blocks: Sequence[Mapping[str, Any]]) -> ProtectionPlan:
    inner_plan = v4.protect_blocks(
        blocks,
        protect_bracketed_emphasis=True,
    )
    identity = {
        "policy_id": POLICY_ID,
        "prompt_version": PROMPT_VERSION,
        "inner_plan_sha256": inner_plan.plan_sha256,
    }
    plan_sha256 = sha256(
        json.dumps(
            identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return ProtectionPlan(inner_plan=inner_plan, plan_sha256=plan_sha256)


def restore_translations(
    translations: Mapping[str, str],
    plan: ProtectionPlan,
) -> tuple[dict[str, str], list[ProtectedSpanIssue]]:
    return v4.restore_translations(translations, plan.inner_plan)


def protected_span_reask_note(
    issues: Sequence[ProtectedSpanIssue],
    block_id_aliases: Mapping[str, str] | None = None,
) -> str:
    return v4.protected_span_reask_note(issues, block_id_aliases)


def lexical_source_blocks(plan: ProtectionPlan) -> list[dict[str, Any]]:
    return v4.lexical_source_blocks(plan.inner_plan)


def context_source_blocks(plan: ProtectionPlan) -> list[dict[str, Any]]:
    return v4.context_source_blocks(plan.inner_plan)


def fixed_only_block_ids(plan: ProtectionPlan) -> set[str]:
    return v4.fixed_only_block_ids(plan.inner_plan)


def fixed_only_protected_translations(plan: ProtectionPlan) -> dict[str, str]:
    """Return the exact protected payload that code owns for fixed-only blocks."""

    fixed_ids = fixed_only_block_ids(plan)
    return {
        str(block.get("block_id")): str(
            block.get("clean_text") or block.get("source_text") or ""
        )
        for block in plan.protected_blocks
        if str(block.get("block_id") or "") in fixed_ids
    }


def fixed_source_segments(plan: ProtectionPlan) -> dict[str, list[str]]:
    return v4.fixed_source_segments(plan.inner_plan)


def math_spans_in_text(text: str) -> list[str]:
    return v4.math_spans_in_text(text)


def contains_forbidden_control(text: str) -> bool:
    return v4.contains_forbidden_control(text)


__all__ = [
    "D2LLatexProtectionError",
    "POLICY_ID",
    "PROMPT_VERSION",
    "ProtectedSpanIssue",
    "ProtectionPlan",
    "contains_forbidden_control",
    "context_source_blocks",
    "fixed_only_block_ids",
    "fixed_only_protected_translations",
    "fixed_source_segments",
    "lexical_source_blocks",
    "math_spans_in_text",
    "protect_blocks",
    "protected_span_reask_note",
    "restore_translations",
]
