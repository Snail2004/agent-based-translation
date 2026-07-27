"""Packet-deduplicated, overlap-aware sequential B3 request construction."""

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
from pipeline.literary.b3_temporal_prompts_v6 import (
    B3_TEMPORAL_PROMPT_ID_V6,
    B3_TEMPORAL_SYSTEM_PROMPT_V6,
    b3_temporal_response_schema_v6,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json
from pipeline.literary.structured_prompt_reserve_v1 import structured_prompt_reserve_v1


B3_REQUEST_SCHEMA_VERSION_V6 = "literary_b3_temporal_request_v6"
B3_LIVE_PLAN_SCHEMA_VERSION_V6 = "literary_b3_temporal_live_plan_v6"
B3_REVIEW_PACKET_SCHEMA_VERSION_V1 = "literary_b3_review_packets_v1"


def render_b3_temporal_sequential_batch_v6(
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
        {"role": "system", "content": B3_TEMPORAL_SYSTEM_PROMPT_V6},
        {"role": "user", "content": canonical_json(payload)},
    ]
    response_schema = b3_temporal_response_schema_v6()
    reserve = structured_prompt_reserve_v1(
        messages=messages,
        response_schema=response_schema,
        output_token_cap=profile.output_tokens_per_request,
    ).to_payload()
    if int(reserve["prompt_token_reserve"]) > profile.prompt_tokens_per_request:
        raise B3TemporalBudgetError("B3 V6 rendered prompt exceeds configured cap")
    body = {
        "schema_version": B3_REQUEST_SCHEMA_VERSION_V6,
        "request_kind": "chapter_temporal_state_batch",
        "prompt_id": B3_TEMPORAL_PROMPT_ID_V6,
        "prompt_sha256": hashlib.sha256(
            B3_TEMPORAL_SYSTEM_PROMPT_V6.encode("utf-8")
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


def build_b3_temporal_cross_chapter_bundle_v6(
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
        render_b3_temporal_sequential_batch_v6(
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
        raise B3TemporalContextError("B3 V6 requests do not exact-cover components")
    plan_body = {
        "schema_version": B3_LIVE_PLAN_SCHEMA_VERSION_V6,
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


def _packetize_review_context_v6(payload: Mapping[str, Any]) -> dict[str, Any]:
    body = deepcopy(dict(payload))
    components = body.get("components")
    if not isinstance(components, list):
        raise B3TemporalContextError("B3 V6 components are absent")
    packets: dict[str, dict[str, Any]] = {}
    for raw_component in components:
        if not isinstance(raw_component, dict):
            raise B3TemporalContextError("B3 V6 component is malformed")
        component_id = _nonempty_string(
            raw_component.get("component_id"), "component_id"
        )
        rows = raw_component.pop("b2_review_requests", None)
        if not isinstance(rows, list):
            raise B3TemporalContextError("B3 V6 component reviews are absent")
        review_ids: list[str] = []
        for raw_row in rows:
            if not isinstance(raw_row, Mapping):
                raise B3TemporalContextError("B3 V6 review row is malformed")
            row = deepcopy(dict(raw_row))
            review_id = _nonempty_string(row.pop("review_id", None), "review_id")
            source_block_ids = _sorted_string_set(
                row.pop("source_block_ids", None), "review source_block_ids"
            )
            referent_refs = _sorted_string_set(
                row.pop("referent_refs", None), "review referent_refs"
            )
            if review_id in review_ids:
                raise B3TemporalContextError(
                    "B3 V6 component repeats a review binding"
                )
            packet = packets.setdefault(
                review_id,
                {
                    "review_id": review_id,
                    "review": row,
                    "source_block_ids": [],
                    "referent_refs": [],
                    "component_bindings": [],
                },
            )
            if canonical_json(packet["review"]) != canonical_json(row):
                raise B3TemporalContextError(
                    "B3 V6 review metadata drifted across components"
                )
            packet["component_bindings"].append(
                {
                    "component_id": component_id,
                    "source_block_ids": source_block_ids,
                    "referent_refs": referent_refs,
                }
            )
            packet["source_block_ids"].extend(source_block_ids)
            packet["referent_refs"].extend(referent_refs)
            review_ids.append(review_id)
        raw_component["review_ids"] = review_ids
    for packet in packets.values():
        bindings = packet["component_bindings"]
        bindings.sort(key=lambda row: row["component_id"])
        if len(bindings) != len({row["component_id"] for row in bindings}):
            raise B3TemporalContextError("B3 V6 review repeats a component binding")
        packet["source_block_ids"] = sorted(set(packet["source_block_ids"]))
        packet["referent_refs"] = sorted(set(packet["referent_refs"]))
    body["review_packet_schema_version"] = B3_REVIEW_PACKET_SCHEMA_VERSION_V1
    body["b2_review_packets"] = [packets[key] for key in sorted(packets)]
    return body


def _overlap_aware_batches_v6(
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
        if bin_count * profile.max_components_per_request < len(weighted):
            continue
        bins = _candidate_bins_v6(
            temporal_input=temporal_input,
            weighted=weighted,
            profile=profile,
            bin_count=bin_count,
        )
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
                render_b3_temporal_sequential_batch_v6(
                    temporal_input=temporal_input,
                    components=batch,
                    profile=profile,
                    batch_ordinal=index,
                )
        except B3TemporalBudgetError:
            continue
        return ordered
    raise B3TemporalBudgetError(
        "B3 V6 could not pack components within the sealed request cap"
    )


def _candidate_bins_v6(
    *,
    temporal_input: Mapping[str, Any],
    weighted: Sequence[Mapping[str, Any]],
    profile: B3TemporalProfileV1,
    bin_count: int,
) -> list[list[Mapping[str, Any]]]:
    remaining = list(weighted)
    bins: list[list[Mapping[str, Any]]] = [[remaining.pop(0)]]
    while len(bins) < bin_count:
        seed_index = min(
            range(len(remaining)),
            key=lambda index: (
                max(
                    _component_affinity_v6(remaining[index], seed[0])
                    for seed in bins
                ),
                -len(canonical_json(remaining[index])),
                int(remaining[index]["component_ordinal"]),
            ),
        )
        bins.append([remaining.pop(seed_index)])

    while remaining:
        component = remaining.pop(0)
        candidates = [
            index
            for index, batch in enumerate(bins)
            if len(batch) < profile.max_components_per_request
        ]
        if not candidates:
            return bins
        current_sizes = [
            _packetized_payload_size_v6(
                temporal_input=temporal_input,
                components=batch,
                profile=profile,
            )
            for batch in bins
        ]
        ranked: list[tuple[tuple[int, int, int, int], int]] = []
        for index in candidates:
            projected_size = _packetized_payload_size_v6(
                temporal_input=temporal_input,
                components=[*bins[index], component],
                profile=profile,
            )
            projected_sizes = list(current_sizes)
            projected_sizes[index] = projected_size
            affinity = sum(
                _component_affinity_v6(component, existing)
                for existing in bins[index]
            )
            ranked.append(
                (
                    (
                        max(projected_sizes),
                        sum(projected_sizes),
                        -affinity,
                        index,
                    ),
                    index,
                )
            )
        target = min(ranked)[1]
        bins[target].append(component)
    return bins


def _packetized_payload_size_v6(
    *,
    temporal_input: Mapping[str, Any],
    components: Sequence[Mapping[str, Any]],
    profile: B3TemporalProfileV1,
) -> int:
    material = build_b3_temporal_batch_payload_v1(
        temporal_input=temporal_input,
        components=components,
        profile=profile,
        batch_ordinal=1,
    )
    payload = _packetize_review_context_v6(
        _packetize_prior_context_v4(material["user_payload"])
    )
    return len(canonical_json(payload))


def _component_affinity_v6(
    left: Mapping[str, Any], right: Mapping[str, Any]
) -> int:
    left_reviews = {
        str(row.get("review_id"))
        for row in left.get("b2_review_requests") or []
        if isinstance(row, Mapping) and row.get("review_id")
    }
    right_reviews = {
        str(row.get("review_id"))
        for row in right.get("b2_review_requests") or []
        if isinstance(row, Mapping) and row.get("review_id")
    }
    left_refs = {
        str(value) for value in left.get("referent_refs") or [] if value
    }
    right_refs = {
        str(value) for value in right.get("referent_refs") or [] if value
    }
    return 4 * len(left_reviews.intersection(right_reviews)) + len(
        left_refs.intersection(right_refs)
    )


def _nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise B3TemporalContextError(f"B3 V6 {label} is malformed")
    return value


def _sorted_string_set(value: Any, label: str) -> list[str]:
    if (
        not isinstance(value, list)
        or not all(isinstance(item, str) and item for item in value)
        or value != sorted(set(value))
    ):
        raise B3TemporalContextError(f"B3 V6 {label} differs")
    return list(value)


__all__ = [
    "B3_LIVE_PLAN_SCHEMA_VERSION_V6",
    "B3_REQUEST_SCHEMA_VERSION_V6",
    "B3_REVIEW_PACKET_SCHEMA_VERSION_V1",
    "build_b3_temporal_cross_chapter_bundle_v6",
    "render_b3_temporal_sequential_batch_v6",
]
