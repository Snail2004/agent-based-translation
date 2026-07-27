"""Bounded request rendering for Literary B2 slim V3."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping, Sequence

from pipeline.literary.b2_context_v1 import (
    B2ContextBudgetError,
    B2PhaseAProfile,
    _chapter_blocks,
    _frame_context_candidate_sources_v1,
    _is_heading,
    _rendered_request,
    _required_string,
    _source_block_view,
    build_candidate_packet_v1,
    project_b2_candidate_packet_for_model_v1,
    project_b2_interaction_candidate_packet_for_model_v1,
)
from pipeline.literary.b2_prompts_v3 import (
    B2_FRAME_PROMPT_ID_V5,
    B2_FRAME_SYSTEM_PROMPT_V5,
    B2_SLIM_INTERACTION_PROMPT_ID_V11,
    B2_SLIM_INTERACTION_SYSTEM_PROMPT_V11,
    b2_frame_response_schema_v2,
    b2_interaction_response_schema_v3,
)
from pipeline.literary.checkpoint import canonical_json
from pipeline.literary.structured_prompt_reserve_v1 import (
    structured_prompt_reserve_v1,
)


def render_b2_frame_request_v2(
    *,
    chapter: Mapping[str, Any],
    prefix_bundle: Mapping[str, Any],
    profile: B2PhaseAProfile,
    supplemental_candidate_sources: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    chapter_id = _required_string(chapter.get("chapter_id"), "chapter_id")
    blocks = [row for row in _chapter_blocks(chapter) if not _is_heading(row)]
    source_packet = build_candidate_packet_v1(
        chapter_id=chapter_id,
        active_blocks=blocks,
        tail_blocks=[],
        prefix_bundle=prefix_bundle,
        candidate_card_cap=profile.frame_candidate_card_cap,
        profile=profile,
        supplemental_candidate_sources=supplemental_candidate_sources,
    )
    if source_packet["overflow"]:
        raise B2ContextBudgetError(
            "B2 V2 frame candidate context overflow: "
            f"{source_packet['overflow_reasons']}"
        )
    packet = project_b2_candidate_packet_for_model_v1(source_packet)
    user_payload = {
        "request_kind": "chapter_frame",
        "chapter_id": chapter_id,
        "chapter_blocks": [_source_block_view(row) for row in blocks],
        "candidate_packets": packet,
    }
    messages = [
        {"role": "system", "content": B2_FRAME_SYSTEM_PROMPT_V5},
        {"role": "user", "content": canonical_json(user_payload)},
    ]
    response_schema = b2_frame_response_schema_v2()
    reserve = structured_prompt_reserve_v1(
        messages=messages,
        response_schema=response_schema,
        output_token_cap=profile.frame_output_tokens,
    ).to_payload()
    if reserve["prompt_token_reserve"] > profile.frame_prompt_tokens:
        raise B2ContextBudgetError("B2 V2 frame prompt exceeds configured cap")
    return _rendered_request(
        request_kind="chapter_frame",
        prompt_id=B2_FRAME_PROMPT_ID_V5,
        prompt_text=B2_FRAME_SYSTEM_PROMPT_V5,
        chapter_id=chapter_id,
        window_id=None,
        messages=messages,
        response_schema=response_schema,
        reserve=reserve,
        configured_prompt_cap=profile.frame_prompt_tokens,
        dependency_status="ready",
        api_eligible=False,
        api_ineligible_reasons=["phase_a_zero_api"],
        context_hashes={
            "candidate_packet_hash": packet["packet_hash"],
            "source_candidate_packet_hash": source_packet["packet_hash"],
        },
    )


def render_b2_interaction_request_v3(
    *,
    window: Mapping[str, Any],
    prefix_bundle: Mapping[str, Any],
    profile: B2PhaseAProfile,
    frame_context: Mapping[str, Any] | None = None,
    prior_effective_states: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    chapter_id = _required_string(window.get("chapter_id"), "window chapter_id")
    window_id = _required_string(window.get("window_id"), "window_id")
    active_blocks = list(window.get("active_blocks") or [])
    tail_blocks = list(window.get("preceding_tail") or [])
    supplemental_sources = _frame_context_candidate_sources_v1(
        frame_context=frame_context,
        chapter_id=chapter_id,
    )
    source_packet = build_candidate_packet_v1(
        chapter_id=chapter_id,
        active_blocks=active_blocks,
        # Tail text preserves local continuity, but it must not widen the
        # candidate roster for this active interaction window.
        tail_blocks=[],
        prefix_bundle=prefix_bundle,
        candidate_card_cap=profile.interaction_candidate_card_cap,
        profile=profile,
        supplemental_candidate_sources=supplemental_sources,
    )
    if source_packet["overflow"]:
        raise B2ContextBudgetError(
            "B2 V3 interaction candidate context overflow: "
            f"{source_packet['overflow_reasons']}"
        )
    packet = project_b2_interaction_candidate_packet_for_model_v1(source_packet)
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
        "prior_effective_states": [deepcopy(dict(row)) for row in prior_effective_states],
    }
    messages = [
        {"role": "system", "content": B2_SLIM_INTERACTION_SYSTEM_PROMPT_V11},
        {"role": "user", "content": canonical_json(user_payload)},
    ]
    response_schema = b2_interaction_response_schema_v3()
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
        raise B2ContextBudgetError("B2 V3 interaction prompt exceeds configured cap")
    reasons = ["phase_a_zero_api"]
    if frame_context is None:
        reasons.append("b2a_dependency_not_available")
    return _rendered_request(
        request_kind="window_interaction",
        prompt_id=B2_SLIM_INTERACTION_PROMPT_ID_V11,
        prompt_text=B2_SLIM_INTERACTION_SYSTEM_PROMPT_V11,
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
            "source_candidate_packet_hash": source_packet["packet_hash"],
            "window_hash": window["window_hash"],
        },
    )


__all__ = [
    "render_b2_frame_request_v2",
    "render_b2_interaction_request_v3",
]
