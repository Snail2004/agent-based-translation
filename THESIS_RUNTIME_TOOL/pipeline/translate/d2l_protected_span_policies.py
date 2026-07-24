"""Versioned dispatch for D2L protected-span contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from pipeline.translate import d2l_latex_markup_line_protected_spans_v4
from pipeline.translate import d2l_latex_markup_line_protected_spans_v5
from pipeline.translate import d2l_latex_markup_protected_spans_v3
from pipeline.translate import d2l_latex_protected_spans_v2
from pipeline.translate import d2l_protected_spans_v1
from pipeline.translate.d2l_translation_slots_v1 import (
    PROMPT_VERSION as TRANSLATION_SLOTS_V1_PROMPT_VERSION,
)


@dataclass(frozen=True)
class ProtectedSpanPolicy:
    policy_id: str
    prompt_version: str
    translation_slots_prompt_version: str
    protect_blocks: Callable[..., Any]
    restore_translations: Callable[..., Any]
    reask_note: Callable[..., str]
    hides_source_bytes: bool
    context_source_blocks: Callable[..., list[dict[str, Any]]] | None = None
    lexical_source_blocks: Callable[..., list[dict[str, Any]]] | None = None
    fixed_only_block_ids: Callable[..., set[str]] | None = None
    fixed_only_protected_translations: (
        Callable[..., dict[str, str]] | None
    ) = None


_POLICIES = {
    d2l_protected_spans_v1.POLICY_ID: ProtectedSpanPolicy(
        policy_id=d2l_protected_spans_v1.POLICY_ID,
        prompt_version=d2l_protected_spans_v1.PROMPT_VERSION,
        translation_slots_prompt_version=TRANSLATION_SLOTS_V1_PROMPT_VERSION,
        protect_blocks=d2l_protected_spans_v1.protect_blocks,
        restore_translations=d2l_protected_spans_v1.restore_translations,
        reask_note=d2l_protected_spans_v1.protected_span_reask_note,
        hides_source_bytes=False,
    ),
    d2l_latex_protected_spans_v2.POLICY_ID: ProtectedSpanPolicy(
        policy_id=d2l_latex_protected_spans_v2.POLICY_ID,
        prompt_version=d2l_latex_protected_spans_v2.PROMPT_VERSION,
        translation_slots_prompt_version=d2l_latex_protected_spans_v2.PROMPT_VERSION,
        protect_blocks=d2l_latex_protected_spans_v2.protect_blocks,
        restore_translations=d2l_latex_protected_spans_v2.restore_translations,
        reask_note=d2l_latex_protected_spans_v2.protected_span_reask_note,
        hides_source_bytes=True,
    ),
    d2l_latex_markup_protected_spans_v3.POLICY_ID: ProtectedSpanPolicy(
        policy_id=d2l_latex_markup_protected_spans_v3.POLICY_ID,
        prompt_version=d2l_latex_markup_protected_spans_v3.PROMPT_VERSION,
        translation_slots_prompt_version=(
            d2l_latex_markup_protected_spans_v3.PROMPT_VERSION
        ),
        protect_blocks=d2l_latex_markup_protected_spans_v3.protect_blocks,
        restore_translations=(
            d2l_latex_markup_protected_spans_v3.restore_translations
        ),
        reask_note=d2l_latex_markup_protected_spans_v3.protected_span_reask_note,
        hides_source_bytes=True,
    ),
    d2l_latex_markup_line_protected_spans_v4.POLICY_ID: ProtectedSpanPolicy(
        policy_id=d2l_latex_markup_line_protected_spans_v4.POLICY_ID,
        prompt_version=d2l_latex_markup_line_protected_spans_v4.PROMPT_VERSION,
        translation_slots_prompt_version=(
            d2l_latex_markup_line_protected_spans_v4.PROMPT_VERSION
        ),
        protect_blocks=d2l_latex_markup_line_protected_spans_v4.protect_blocks,
        restore_translations=(
            d2l_latex_markup_line_protected_spans_v4.restore_translations
        ),
        reask_note=(
            d2l_latex_markup_line_protected_spans_v4.protected_span_reask_note
        ),
        hides_source_bytes=True,
        context_source_blocks=(
            d2l_latex_markup_line_protected_spans_v4.context_source_blocks
        ),
        lexical_source_blocks=(
            d2l_latex_markup_line_protected_spans_v4.lexical_source_blocks
        ),
        fixed_only_block_ids=(
            d2l_latex_markup_line_protected_spans_v4.fixed_only_block_ids
        ),
    ),
    d2l_latex_markup_line_protected_spans_v5.POLICY_ID: ProtectedSpanPolicy(
        policy_id=d2l_latex_markup_line_protected_spans_v5.POLICY_ID,
        prompt_version=d2l_latex_markup_line_protected_spans_v5.PROMPT_VERSION,
        translation_slots_prompt_version=(
            d2l_latex_markup_line_protected_spans_v5.PROMPT_VERSION
        ),
        protect_blocks=d2l_latex_markup_line_protected_spans_v5.protect_blocks,
        restore_translations=(
            d2l_latex_markup_line_protected_spans_v5.restore_translations
        ),
        reask_note=(
            d2l_latex_markup_line_protected_spans_v5.protected_span_reask_note
        ),
        hides_source_bytes=True,
        context_source_blocks=(
            d2l_latex_markup_line_protected_spans_v5.context_source_blocks
        ),
        lexical_source_blocks=(
            d2l_latex_markup_line_protected_spans_v5.lexical_source_blocks
        ),
        fixed_only_block_ids=(
            d2l_latex_markup_line_protected_spans_v5.fixed_only_block_ids
        ),
        fixed_only_protected_translations=(
            d2l_latex_markup_line_protected_spans_v5.fixed_only_protected_translations
        ),
    ),
}

POLICY_IDS = tuple(_POLICIES)


def get_protected_span_policy(policy_id: str | None) -> ProtectedSpanPolicy | None:
    if policy_id is None:
        return None
    try:
        return _POLICIES[policy_id]
    except KeyError as exc:
        raise ValueError(f"Unknown protected-span policy: {policy_id}") from exc


__all__ = ["POLICY_IDS", "ProtectedSpanPolicy", "get_protected_span_policy"]
