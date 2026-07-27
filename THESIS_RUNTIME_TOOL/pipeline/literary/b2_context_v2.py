"""Versioned Literary B2 V2 interaction request rendering."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from pipeline.literary.b2_context_v1 import (
    B2ContextBudgetError,
    B2PhaseAProfile,
    _rendered_request,
    _frame_context_candidate_sources_v1,
    _required_string,
    _source_block_view,
    build_candidate_packet_v1,
)
from pipeline.literary.b2_prompts_v2 import (
    B2_INTERACTION_PROMPT_ID_V2_1,
    B2_INTERACTION_SYSTEM_PROMPT_V2_1,
    bind_b2_interaction_response_schema_v2,
)
from pipeline.literary.checkpoint import canonical_json
from pipeline.literary.structured_prompt_reserve_v1 import (
    structured_prompt_reserve_v1,
)


def render_b2_interaction_request_v2(
    *,
    window: Mapping[str, Any],
    prefix_bundle: Mapping[str, Any],
    profile: B2PhaseAProfile,
    frame_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Render the V2 interaction contract without changing source windowing."""

    chapter_id = _required_string(window.get("chapter_id"), "window chapter_id")
    window_id = _required_string(window.get("window_id"), "window_id")
    active_blocks = list(window.get("active_blocks") or [])
    tail_blocks = list(window.get("preceding_tail") or [])
    supplemental_sources = _frame_context_candidate_sources_v1(
        frame_context=frame_context,
        chapter_id=chapter_id,
    )
    packet = build_candidate_packet_v1(
        chapter_id=chapter_id,
        active_blocks=active_blocks,
        tail_blocks=tail_blocks,
        prefix_bundle=prefix_bundle,
        candidate_card_cap=profile.interaction_candidate_card_cap,
        profile=profile,
        supplemental_candidate_sources=supplemental_sources,
    )
    if packet["overflow"]:
        raise B2ContextBudgetError(
            f"B2 interaction candidate context overflow: {packet['overflow_reasons']}"
        )
    dependency_status = "ready" if frame_context is not None else "pending_b2a"
    user_payload = {
        "request_kind": "window_interaction",
        "chapter_id": chapter_id,
        "window_id": window_id,
        "active_blocks": [_source_block_view(row) for row in active_blocks],
        "preceding_tail": [_source_block_view(row) for row in tail_blocks],
        "frame_context_status": dependency_status,
        "frame_context": deepcopy(frame_context) if frame_context is not None else None,
        "candidate_packets": packet,
        "prior_relation_states": [],
    }
    messages = [
        {"role": "system", "content": B2_INTERACTION_SYSTEM_PROMPT_V2_1},
        {"role": "user", "content": canonical_json(user_payload)},
    ]
    response_schema = bind_b2_interaction_response_schema_v2(
        chapter_id=chapter_id,
        window_id=window_id,
        active_block_ids=[str(row["block_id"]) for row in active_blocks],
        support_block_ids=[
            str(row["block_id"]) for row in [*active_blocks, *tail_blocks]
        ],
        candidate_card_ids=[
            str(row["candidate_card_id"])
            for row in packet["candidate_cards"]
        ],
    )
    reserve = structured_prompt_reserve_v1(
        messages=messages,
        response_schema=response_schema,
        output_token_cap=profile.interaction_output_tokens,
    ).to_payload()
    conservative_prompt_reserve = reserve["prompt_token_reserve"]
    if frame_context is None:
        conservative_prompt_reserve += profile.pending_frame_context_reserve_tokens
    reserve["dependency_context_reserve_tokens"] = (
        profile.pending_frame_context_reserve_tokens if frame_context is None else 0
    )
    reserve["conservative_prompt_token_reserve"] = conservative_prompt_reserve
    reserve["conservative_total_token_reserve"] = (
        conservative_prompt_reserve + profile.interaction_output_tokens
    )
    if conservative_prompt_reserve > profile.interaction_prompt_tokens:
        raise B2ContextBudgetError("B2 V2 interaction prompt exceeds configured cap")
    reasons = ["phase_a_zero_api"]
    if frame_context is None:
        reasons.append("b2a_dependency_not_available")
    return _rendered_request(
        request_kind="window_interaction",
        prompt_id=B2_INTERACTION_PROMPT_ID_V2_1,
        prompt_text=B2_INTERACTION_SYSTEM_PROMPT_V2_1,
        chapter_id=chapter_id,
        window_id=window_id,
        messages=messages,
        response_schema=response_schema,
        reserve=reserve,
        configured_prompt_cap=profile.interaction_prompt_tokens,
        dependency_status=dependency_status,
        api_eligible=False,
        api_ineligible_reasons=reasons,
        context_hashes={
            "candidate_packet_hash": packet["packet_hash"],
            "window_hash": window["window_hash"],
        },
    )


__all__ = ["render_b2_interaction_request_v2"]
