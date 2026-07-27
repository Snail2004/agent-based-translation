"""Sequential cross-chapter B3 requests with a stable response schema."""

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
from pipeline.literary.b3_temporal_contract_v1 import validate_b3_temporal_request_v1
from pipeline.literary.b3_temporal_prompts_v3 import (
    B3_TEMPORAL_PROMPT_ID_V3,
    B3_TEMPORAL_SYSTEM_PROMPT_V3,
    b3_temporal_response_schema_v3,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json
from pipeline.literary.structured_prompt_reserve_v1 import structured_prompt_reserve_v1


B3_REQUEST_SCHEMA_VERSION_V3 = "literary_b3_temporal_request_v3"
B3_LIVE_PLAN_SCHEMA_VERSION_V3 = "literary_b3_temporal_live_plan_v3"


def render_b3_temporal_live_request_v3(
    *, phase_a_request: Mapping[str, Any], profile: B3TemporalProfileV1
) -> dict[str, Any]:
    source, payload = validate_b3_temporal_request_v1(phase_a_request)
    if source.get("configured_prompt_cap") != profile.prompt_tokens_per_request:
        raise B3TemporalContextError("B3 V3 source request prompt cap differs")
    if source.get("configured_output_cap") != profile.output_tokens_per_request:
        raise B3TemporalContextError("B3 V3 source request output cap differs")
    messages = [
        {"role": "system", "content": B3_TEMPORAL_SYSTEM_PROMPT_V3},
        {"role": "user", "content": canonical_json(payload)},
    ]
    response_schema = b3_temporal_response_schema_v3()
    reserve = structured_prompt_reserve_v1(
        messages=messages,
        response_schema=response_schema,
        output_token_cap=profile.output_tokens_per_request,
    ).to_payload()
    if int(reserve["prompt_token_reserve"]) > profile.prompt_tokens_per_request:
        raise B3TemporalBudgetError("B3 V3 rendered prompt exceeds configured cap")
    body = deepcopy(source)
    body.pop("request_fingerprint", None)
    body.update(
        {
            "schema_version": B3_REQUEST_SCHEMA_VERSION_V3,
            "prompt_id": B3_TEMPORAL_PROMPT_ID_V3,
            "prompt_sha256": hashlib.sha256(
                B3_TEMPORAL_SYSTEM_PROMPT_V3.encode("utf-8")
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


def render_b3_temporal_sequential_batch_v3(
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
    messages = [
        {"role": "system", "content": B3_TEMPORAL_SYSTEM_PROMPT_V3},
        {"role": "user", "content": canonical_json(material["user_payload"])},
    ]
    response_schema = b3_temporal_response_schema_v3()
    reserve = structured_prompt_reserve_v1(
        messages=messages,
        response_schema=response_schema,
        output_token_cap=profile.output_tokens_per_request,
    ).to_payload()
    if int(reserve["prompt_token_reserve"]) > profile.prompt_tokens_per_request:
        raise B3TemporalBudgetError("B3 V3 rendered prompt exceeds configured cap")
    body = {
        "schema_version": B3_REQUEST_SCHEMA_VERSION_V3,
        "request_kind": "chapter_temporal_state_batch",
        "prompt_id": B3_TEMPORAL_PROMPT_ID_V3,
        "prompt_sha256": hashlib.sha256(
            B3_TEMPORAL_SYSTEM_PROMPT_V3.encode("utf-8")
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


def build_b3_temporal_cross_chapter_bundle_v3(
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
    batches = _balanced_batches_v3(
        temporal_input=temporal_input,
        components=components,
        profile=profile,
    )
    requests = [
        render_b3_temporal_sequential_batch_v3(
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
        raise B3TemporalContextError("B3 V3 requests do not exact-cover components")
    plan_body = {
        "schema_version": B3_LIVE_PLAN_SCHEMA_VERSION_V3,
        "phase": "bounded_cross_chapter_live",
        "chapter_id": temporal_input["chapter_id"],
        "source_input_hash": temporal_input["input_hash"],
        "source_b2_artifact_hash": temporal_input["source_b2_artifact_hash"],
        "source_prefix_bundle_hash": temporal_input["source_prefix_bundle_hash"],
        "context_profile_id": profile.profile_id,
        "context_profile_hash": profile.profile_hash,
        "component_plan_hash": canonical_hash(
            [row["component_hash"] for row in components]
        ),
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
                int(row["token_reserve"]["prompt_token_reserve"]) for row in requests
            ),
            "output_token_reserve": sum(
                int(row["token_reserve"]["output_token_cap"]) for row in requests
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


def _balanced_batches_v3(
    *,
    temporal_input: Mapping[str, Any],
    components: Sequence[Mapping[str, Any]],
    profile: B3TemporalProfileV1,
) -> list[list[Mapping[str, Any]]]:
    """Find the smallest deterministic balanced packing under the real cap."""

    if not components:
        return []
    weighted = sorted(
        components,
        key=lambda row: (-len(canonical_json(row)), int(row["component_ordinal"])),
    )
    maximum_bins = min(profile.max_requests_per_chapter, len(weighted))
    for bin_count in range(1, maximum_bins + 1):
        bins: list[list[Mapping[str, Any]]] = [[] for _ in range(bin_count)]
        weights = [0 for _ in range(bin_count)]
        possible = True
        for component in weighted:
            candidates = [
                index
                for index, batch in enumerate(bins)
                if len(batch) < profile.max_components_per_request
            ]
            if not candidates:
                possible = False
                break
            index = min(candidates, key=lambda value: (weights[value], value))
            bins[index].append(component)
            weights[index] += len(canonical_json(component))
        if not possible:
            continue
        ordered = [
            sorted(batch, key=lambda row: int(row["component_ordinal"]))
            for batch in bins
            if batch
        ]
        try:
            for index, batch in enumerate(ordered, 1):
                render_b3_temporal_sequential_batch_v3(
                    temporal_input=temporal_input,
                    components=batch,
                    profile=profile,
                    batch_ordinal=index,
                )
        except B3TemporalBudgetError:
            continue
        ordered.sort(
            key=lambda batch: min(int(row["component_ordinal"]) for row in batch)
        )
        return ordered
    raise B3TemporalBudgetError(
        "B3 V3 could not pack components within the sealed request cap"
    )


__all__ = [
    "B3_LIVE_PLAN_SCHEMA_VERSION_V3",
    "B3_REQUEST_SCHEMA_VERSION_V3",
    "build_b3_temporal_cross_chapter_bundle_v3",
    "render_b3_temporal_live_request_v3",
    "render_b3_temporal_sequential_batch_v3",
]
