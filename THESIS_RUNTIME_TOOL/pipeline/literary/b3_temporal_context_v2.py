"""Live-eligible B3 V2 requests with a reusable stable response schema."""

from __future__ import annotations

from copy import deepcopy
import hashlib
from typing import Any, Mapping, Sequence

from pipeline.literary.b3_temporal_context_v1 import (
    B3TemporalBudgetError,
    B3TemporalContextError,
    B3TemporalProfileV1,
    build_b3_temporal_phase_a_bundle_v1,
)
from pipeline.literary.b3_temporal_contract_v1 import (
    validate_b3_temporal_request_v1,
)
from pipeline.literary.b3_temporal_prompts_v2 import (
    B3_TEMPORAL_PROMPT_ID_V2,
    B3_TEMPORAL_SYSTEM_PROMPT_V2,
    b3_temporal_response_schema_v2,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json
from pipeline.literary.structured_prompt_reserve_v1 import (
    structured_prompt_reserve_v1,
)


B3_REQUEST_SCHEMA_VERSION_V2 = "literary_b3_temporal_request_v2"
B3_LIVE_PLAN_SCHEMA_VERSION_V2 = "literary_b3_temporal_live_plan_v2"


def render_b3_temporal_live_request_v2(
    *,
    phase_a_request: Mapping[str, Any],
    profile: B3TemporalProfileV1,
) -> dict[str, Any]:
    source, payload = validate_b3_temporal_request_v1(phase_a_request)
    if source.get("configured_prompt_cap") != profile.prompt_tokens_per_request:
        raise B3TemporalContextError("B3 source request prompt cap differs")
    if source.get("configured_output_cap") != profile.output_tokens_per_request:
        raise B3TemporalContextError("B3 source request output cap differs")

    messages = [
        {"role": "system", "content": B3_TEMPORAL_SYSTEM_PROMPT_V2},
        {"role": "user", "content": canonical_json(payload)},
    ]
    response_schema = b3_temporal_response_schema_v2()
    reserve = structured_prompt_reserve_v1(
        messages=messages,
        response_schema=response_schema,
        output_token_cap=profile.output_tokens_per_request,
    ).to_payload()
    if int(reserve["prompt_token_reserve"]) > profile.prompt_tokens_per_request:
        raise B3TemporalBudgetError("B3 V2 rendered prompt exceeds configured cap")

    body = deepcopy(source)
    body.pop("request_fingerprint", None)
    body.update(
        {
            "schema_version": B3_REQUEST_SCHEMA_VERSION_V2,
            "prompt_id": B3_TEMPORAL_PROMPT_ID_V2,
            "prompt_sha256": hashlib.sha256(
                B3_TEMPORAL_SYSTEM_PROMPT_V2.encode("utf-8")
            ).hexdigest(),
            "messages": messages,
            "response_schema": response_schema,
            "response_schema_hash": canonical_hash(response_schema),
            "token_reserve": reserve,
            "api_eligible": True,
            "api_ineligible_reasons": [],
        }
    )
    return {**body, "request_fingerprint": canonical_hash(body)}


def build_b3_temporal_live_bundle_v2(
    *,
    temporal_input: Mapping[str, Any],
    profile: B3TemporalProfileV1,
    prior_states: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    phase_a = build_b3_temporal_phase_a_bundle_v1(
        temporal_input=temporal_input,
        profile=profile,
        prior_states=prior_states,
    )
    requests = [
        render_b3_temporal_live_request_v2(
            phase_a_request=request,
            profile=profile,
        )
        for request in phase_a["requests"]
    ]
    covered = [
        component_id
        for request in requests
        for component_id in request["component_ids"]
    ]
    expected = [row["component_id"] for row in phase_a["components"]]
    if set(covered) != set(expected) or len(covered) != len(set(covered)):
        raise B3TemporalContextError("B3 V2 requests do not exact-cover components")
    plan_body = {
        "schema_version": B3_LIVE_PLAN_SCHEMA_VERSION_V2,
        "phase": "bounded_live_canary",
        "chapter_id": temporal_input["chapter_id"],
        "source_input_hash": temporal_input["input_hash"],
        "source_b2_artifact_hash": temporal_input["source_b2_artifact_hash"],
        "source_prefix_bundle_hash": temporal_input["source_prefix_bundle_hash"],
        "context_profile_id": profile.profile_id,
        "context_profile_hash": profile.profile_hash,
        "source_phase_a_plan_hash": phase_a["plan"]["plan_hash"],
        "role_id": profile.role_id,
        "component_count": len(expected),
        "request_count": len(requests),
        "requests": [
            {
                "batch_id": row["batch_id"],
                "component_ids": list(row["component_ids"]),
                "request_fingerprint": row["request_fingerprint"],
                "token_reserve": deepcopy(row["token_reserve"]),
            }
            for row in requests
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
        "components": deepcopy(phase_a["components"]),
        "requests": requests,
    }


__all__ = [
    "B3_LIVE_PLAN_SCHEMA_VERSION_V2",
    "B3_REQUEST_SCHEMA_VERSION_V2",
    "build_b3_temporal_live_bundle_v2",
    "render_b3_temporal_live_request_v2",
]
