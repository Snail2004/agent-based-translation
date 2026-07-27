"""Deduplicated sequential cross-chapter B3 request construction."""

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
from pipeline.literary.b3_temporal_prompts_v4 import (
    B3_TEMPORAL_PROMPT_ID_V4,
    B3_TEMPORAL_SYSTEM_PROMPT_V4,
    b3_temporal_response_schema_v4,
)
from pipeline.literary.b3_temporal_prefix_v1 import (
    B3_MODEL_HIDDEN_STATE_FIELDS_V1,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json
from pipeline.literary.structured_prompt_reserve_v1 import structured_prompt_reserve_v1


B3_REQUEST_SCHEMA_VERSION_V4 = "literary_b3_temporal_request_v4"
B3_LIVE_PLAN_SCHEMA_VERSION_V4 = "literary_b3_temporal_live_plan_v4"
B3_PRIOR_PACKET_SCHEMA_VERSION_V1 = "literary_b3_prior_context_packets_v1"


def render_b3_temporal_sequential_batch_v4(
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
    payload = _packetize_prior_context_v4(material["user_payload"])
    messages = [
        {"role": "system", "content": B3_TEMPORAL_SYSTEM_PROMPT_V4},
        {"role": "user", "content": canonical_json(payload)},
    ]
    response_schema = b3_temporal_response_schema_v4()
    reserve = structured_prompt_reserve_v1(
        messages=messages,
        response_schema=response_schema,
        output_token_cap=profile.output_tokens_per_request,
    ).to_payload()
    if int(reserve["prompt_token_reserve"]) > profile.prompt_tokens_per_request:
        raise B3TemporalBudgetError("B3 V4 rendered prompt exceeds configured cap")
    body = {
        "schema_version": B3_REQUEST_SCHEMA_VERSION_V4,
        "request_kind": "chapter_temporal_state_batch",
        "prompt_id": B3_TEMPORAL_PROMPT_ID_V4,
        "prompt_sha256": hashlib.sha256(
            B3_TEMPORAL_SYSTEM_PROMPT_V4.encode("utf-8")
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


def build_b3_temporal_cross_chapter_bundle_v4(
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
    batches = _balanced_batches_v4(
        temporal_input=temporal_input,
        components=components,
        profile=profile,
    )
    requests = [
        render_b3_temporal_sequential_batch_v4(
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
        raise B3TemporalContextError("B3 V4 requests do not exact-cover components")
    plan_body = {
        "schema_version": B3_LIVE_PLAN_SCHEMA_VERSION_V4,
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
        "prior_packet_contract": B3_PRIOR_PACKET_SCHEMA_VERSION_V1,
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


def _packetize_prior_context_v4(payload: Mapping[str, Any]) -> dict[str, Any]:
    body = deepcopy(dict(payload))
    components = body.get("components")
    if not isinstance(components, list):
        raise B3TemporalContextError("B3 V4 components are absent")
    state_packets: dict[str, dict[str, Any]] = {}
    pending_packets: dict[str, dict[str, Any]] = {}
    for raw_component in components:
        component = dict(raw_component)
        component_id = str(component.get("component_id") or "")
        for raw_state in component.pop("prior_open_states", []):
            state = deepcopy(dict(raw_state))
            # Absorbed state ids stay in the authoritative artifact for
            # lineage lookup; they are not semantic context for the model.
            for field_name in B3_MODEL_HIDDEN_STATE_FIELDS_V1:
                state.pop(field_name, None)
            state_id = str(state.get("state_id") or "")
            packet = state_packets.setdefault(
                state_id,
                {"state_id": state_id, "component_ids": [], "state": state},
            )
            if canonical_json(packet["state"]) != canonical_json(state):
                raise B3TemporalContextError("B3 prior state drifted across components")
            packet["component_ids"].append(component_id)
        for raw_pending in component.pop("prior_pending_cases", []):
            pending = deepcopy(dict(raw_pending))
            pending_id = str(pending.get("pending_case_id") or "")
            packet = pending_packets.setdefault(
                pending_id,
                {
                    "pending_case_id": pending_id,
                    "component_ids": [],
                    "pending_case": pending,
                },
            )
            if canonical_json(packet["pending_case"]) != canonical_json(pending):
                raise B3TemporalContextError("B3 pending case drifted across components")
            packet["component_ids"].append(component_id)
        raw_component.clear()
        raw_component.update(component)
    for packet in [*state_packets.values(), *pending_packets.values()]:
        packet["component_ids"] = sorted(set(packet["component_ids"]))
    body["prior_context_packet_schema_version"] = B3_PRIOR_PACKET_SCHEMA_VERSION_V1
    body["prior_state_packets"] = [
        state_packets[key] for key in sorted(state_packets)
    ]
    body["prior_pending_packets"] = [
        pending_packets[key] for key in sorted(pending_packets)
    ]
    return body


def _balanced_batches_v4(
    *,
    temporal_input: Mapping[str, Any],
    components: Sequence[Mapping[str, Any]],
    profile: B3TemporalProfileV1,
) -> list[list[Mapping[str, Any]]]:
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
        for component in weighted:
            candidates = [
                index
                for index, batch in enumerate(bins)
                if len(batch) < profile.max_components_per_request
            ]
            if not candidates:
                break
            index = min(candidates, key=lambda value: (weights[value], value))
            bins[index].append(component)
            weights[index] += len(canonical_json(component))
        else:
            ordered = [
                sorted(batch, key=lambda row: int(row["component_ordinal"]))
                for batch in bins
                if batch
            ]
            ordered.sort(
                key=lambda batch: min(int(row["component_ordinal"]) for row in batch)
            )
            try:
                for index, batch in enumerate(ordered, 1):
                    render_b3_temporal_sequential_batch_v4(
                        temporal_input=temporal_input,
                        components=batch,
                        profile=profile,
                        batch_ordinal=index,
                    )
            except B3TemporalBudgetError:
                continue
            return ordered
    raise B3TemporalBudgetError(
        "B3 V4 could not pack components within the sealed request cap"
    )


__all__ = [
    "B3_LIVE_PLAN_SCHEMA_VERSION_V4",
    "B3_PRIOR_PACKET_SCHEMA_VERSION_V1",
    "B3_REQUEST_SCHEMA_VERSION_V4",
    "build_b3_temporal_cross_chapter_bundle_v4",
    "render_b3_temporal_sequential_batch_v4",
]
