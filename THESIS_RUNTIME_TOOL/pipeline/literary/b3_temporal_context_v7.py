"""B3 V7 request construction with many parked identity markers."""

from __future__ import annotations

from copy import deepcopy
import hashlib
from typing import Any, Mapping, Sequence

from pipeline.literary.b3_temporal_context_v1 import (
    B3TemporalBudgetError,
    B3TemporalContextError,
    B3TemporalProfileV1,
    build_b3_temporal_batch_payload_v1,
    build_b3_temporal_components_v1,
)
from pipeline.literary.b3_temporal_context_v4 import (
    B3_PRIOR_PACKET_SCHEMA_VERSION_V1,
    _packetize_prior_context_v4,
)
from pipeline.literary.b3_temporal_context_v6 import (
    B3_REVIEW_PACKET_SCHEMA_VERSION_V1,
    _overlap_aware_batches_v6,
    _packetize_review_context_v6,
)
from pipeline.literary.b3_temporal_prompts_v7 import (
    B3_TEMPORAL_PROMPT_ID_V7,
    B3_TEMPORAL_SYSTEM_PROMPT_V7,
    b3_temporal_response_schema_v7,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json
from pipeline.literary.structured_prompt_reserve_v1 import structured_prompt_reserve_v1


B3_REQUEST_SCHEMA_VERSION_V7 = "literary_b3_temporal_request_v7"
B3_LIVE_PLAN_SCHEMA_VERSION_V7 = "literary_b3_temporal_live_plan_v7"


def render_b3_temporal_sequential_batch_v7(
    *,
    temporal_input: Mapping[str, Any],
    components: Sequence[Mapping[str, Any]],
    profile: B3TemporalProfileV1,
    batch_ordinal: int,
) -> dict[str, Any]:
    material = build_b3_temporal_batch_payload_v1(
        temporal_input=temporal_input,
        components=components,
        profile=profile,
        batch_ordinal=batch_ordinal,
    )
    payload = _packetize_review_context_v6(
        _packetize_prior_context_v4(material["user_payload"])
    )
    messages = [
        {"role": "system", "content": B3_TEMPORAL_SYSTEM_PROMPT_V7},
        {"role": "user", "content": canonical_json(payload)},
    ]
    response_schema = b3_temporal_response_schema_v7()
    reserve = structured_prompt_reserve_v1(
        messages=messages,
        response_schema=response_schema,
        output_token_cap=profile.output_tokens_per_request,
    ).to_payload()
    if int(reserve["prompt_token_reserve"]) > profile.prompt_tokens_per_request:
        raise B3TemporalBudgetError("B3 V7 rendered prompt exceeds configured cap")
    body = {
        "schema_version": B3_REQUEST_SCHEMA_VERSION_V7,
        "request_kind": "chapter_temporal_state_batch",
        "prompt_id": B3_TEMPORAL_PROMPT_ID_V7,
        "prompt_sha256": hashlib.sha256(
            B3_TEMPORAL_SYSTEM_PROMPT_V7.encode("utf-8")
        ).hexdigest(),
        "chapter_id": material["chapter_id"],
        "batch_id": material["batch_id"],
        "component_ids": material["component_ids"],
        "messages": messages,
        "response_schema": response_schema,
        "response_schema_hash": canonical_hash(response_schema),
        "token_reserve": reserve,
        "configured_prompt_cap": profile.prompt_tokens_per_request,
        "configured_output_cap": profile.output_tokens_per_request,
        "api_eligible": True,
        "api_ineligible_reasons": [],
        "context_hashes": material["context_hashes"],
        "gold_or_oracle_loaded": False,
        "production_publish_performed": False,
    }
    return {**body, "request_fingerprint": canonical_hash(body)}


def build_b3_temporal_cross_chapter_bundle_v7(
    *,
    temporal_input: Mapping[str, Any],
    profile: B3TemporalProfileV1,
    prior_states: Sequence[Mapping[str, Any]],
    prior_pending_cases: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    components = build_b3_temporal_components_v1(
        temporal_input=temporal_input,
        profile=profile,
        prior_states=prior_states,
        prior_pending_cases=prior_pending_cases,
    )
    batches = _overlap_aware_batches_v6(
        temporal_input=temporal_input,
        components=components,
        profile=profile,
    )
    requests = [
        render_b3_temporal_sequential_batch_v7(
            temporal_input=temporal_input,
            components=batch,
            profile=profile,
            batch_ordinal=index,
        )
        for index, batch in enumerate(batches, 1)
    ]
    expected = [row["component_id"] for row in components]
    covered = [
        component_id
        for request in requests
        for component_id in request["component_ids"]
    ]
    if set(covered) != set(expected) or len(covered) != len(set(covered)):
        raise B3TemporalContextError("B3 V7 requests do not exact-cover components")
    plan_body = {
        "schema_version": B3_LIVE_PLAN_SCHEMA_VERSION_V7,
        "phase": "bounded_cross_chapter_live",
        "chapter_id": temporal_input["chapter_id"],
        "source_input_hash": temporal_input["input_hash"],
        "source_b2_artifact_hash": temporal_input["source_b2_artifact_hash"],
        "source_prefix_bundle_hash": temporal_input["source_prefix_bundle_hash"],
        "parked_identity_index_hash": temporal_input.get(
            "parked_identity_index_hash"
        ),
        "context_profile_id": profile.profile_id,
        "context_profile_hash": profile.profile_hash,
        "component_plan_hash": canonical_hash(
            [row["component_hash"] for row in components]
        ),
        "prior_packet_contract": B3_PRIOR_PACKET_SCHEMA_VERSION_V1,
        "review_packet_contract": B3_REVIEW_PACKET_SCHEMA_VERSION_V1,
        "role_id": profile.role_id,
        "component_count": len(expected),
        "request_count": len(requests),
        "batch_membership": [
            {
                "batch_ordinal": index,
                "component_ids": list(request["component_ids"]),
                "initial_request_fingerprint": request["request_fingerprint"],
                "initial_token_reserve": deepcopy(request["token_reserve"]),
            }
            for index, request in enumerate(requests, 1)
        ],
        "token_reserve": {
            "prompt_token_reserve": sum(
                int(row["token_reserve"]["prompt_token_reserve"])
                for row in requests
            ),
            "output_token_reserve": sum(
                int(row["token_reserve"]["output_token_cap"])
                for row in requests
            ),
        },
        "api_calls_performed": 0,
        "gold_or_oracle_loaded": False,
        "production_publish_performed": False,
    }
    plan_body["token_reserve"]["total_token_reserve"] = sum(
        plan_body["token_reserve"].values()
    )
    return {
        "plan": {**plan_body, "plan_hash": canonical_hash(plan_body)},
        "components": deepcopy(components),
        "initial_requests": requests,
    }


__all__ = [
    "B3_LIVE_PLAN_SCHEMA_VERSION_V7",
    "B3_REQUEST_SCHEMA_VERSION_V7",
    "B3_REVIEW_PACKET_SCHEMA_VERSION_V1",
    "build_b3_temporal_cross_chapter_bundle_v7",
    "render_b3_temporal_sequential_batch_v7",
]
