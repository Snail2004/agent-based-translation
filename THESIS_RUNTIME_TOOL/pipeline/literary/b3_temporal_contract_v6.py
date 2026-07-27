"""Request validation for review-packet-aware Literary B3 V6."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Any, Mapping

from pipeline.literary.b3_temporal_context_v4 import B3_PRIOR_PACKET_SCHEMA_VERSION_V1
from pipeline.literary.b3_temporal_context_v6 import (
    B3_REQUEST_SCHEMA_VERSION_V6,
    B3_REVIEW_PACKET_SCHEMA_VERSION_V1,
)
from pipeline.literary.b3_temporal_contract_v1 import (
    B3TemporalContractError,
    _normalize_b3_temporal_response_common,
)
from pipeline.literary.b3_temporal_contract_v4 import _expanded_payload
from pipeline.literary.b3_temporal_prompts_v6 import (
    B3_TEMPORAL_PROMPT_ID_V6,
    B3_TEMPORAL_SYSTEM_PROMPT_V6,
    b3_temporal_response_schema_v6,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json


def normalize_b3_temporal_response_v6(
    *, request: Mapping[str, Any], response: Mapping[str, Any] | str
) -> dict[str, Any]:
    return _normalize_b3_temporal_response_common(
        request=request,
        response=response,
        request_validator=validate_b3_temporal_request_v6,
    )


def validate_b3_temporal_request_v6(
    request: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(request, Mapping):
        raise B3TemporalContractError("B3 V6 request must be an object")
    body = deepcopy(dict(request))
    if body.get("schema_version") != B3_REQUEST_SCHEMA_VERSION_V6:
        raise B3TemporalContractError("foreign B3 V6 request schema")
    fingerprint = body.get("request_fingerprint")
    unsigned = dict(body)
    unsigned.pop("request_fingerprint", None)
    if not isinstance(fingerprint, str) or canonical_hash(unsigned) != fingerprint:
        raise B3TemporalContractError("B3 V6 request fingerprint mismatch")
    if body.get("prompt_id") != B3_TEMPORAL_PROMPT_ID_V6:
        raise B3TemporalContractError("B3 V6 prompt id mismatch")
    if body.get("prompt_sha256") != hashlib.sha256(
        B3_TEMPORAL_SYSTEM_PROMPT_V6.encode("utf-8")
    ).hexdigest():
        raise B3TemporalContractError("B3 V6 prompt bytes differ")
    schema = b3_temporal_response_schema_v6()
    if canonical_json(body.get("response_schema")) != canonical_json(schema):
        raise B3TemporalContractError("B3 V6 response schema differs")
    if body.get("response_schema_hash") != canonical_hash(schema):
        raise B3TemporalContractError("B3 V6 response schema hash mismatch")
    if body.get("api_eligible") is not True or body.get(
        "api_ineligible_reasons"
    ) != []:
        raise B3TemporalContractError("B3 V6 request is not live eligible")
    messages = body.get("messages")
    if not isinstance(messages, list) or len(messages) != 2:
        raise B3TemporalContractError("B3 V6 request messages differ")
    if messages[0] != {"role": "system", "content": B3_TEMPORAL_SYSTEM_PROMPT_V6}:
        raise B3TemporalContractError("B3 V6 system message differs")
    if not isinstance(messages[1], Mapping) or messages[1].get("role") != "user":
        raise B3TemporalContractError("B3 V6 user message is absent")
    try:
        payload = json.loads(messages[1].get("content"))
    except (TypeError, json.JSONDecodeError) as exc:
        raise B3TemporalContractError("B3 V6 user payload is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise B3TemporalContractError("B3 V6 user payload must be an object")
    if payload.get("chapter_id") != body.get("chapter_id") or payload.get(
        "batch_id"
    ) != body.get("batch_id"):
        raise B3TemporalContractError("B3 V6 identity differs from payload")
    if payload.get("prior_context_packet_schema_version") != (
        B3_PRIOR_PACKET_SCHEMA_VERSION_V1
    ):
        raise B3TemporalContractError("B3 V6 prior packet contract differs")
    if payload.get("review_packet_schema_version") != (
        B3_REVIEW_PACKET_SCHEMA_VERSION_V1
    ):
        raise B3TemporalContractError("B3 V6 review packet contract differs")
    review_expanded = _expanded_review_payload_v6(payload=payload, body=body)
    return body, _expanded_payload(payload=review_expanded, body=body)


def _expanded_review_payload_v6(
    *, payload: Mapping[str, Any], body: Mapping[str, Any]
) -> dict[str, Any]:
    expanded = deepcopy(dict(payload))
    raw_components = expanded.get("components")
    if not isinstance(raw_components, list):
        raise B3TemporalContractError("B3 V6 components must be a list")
    components: dict[str, dict[str, Any]] = {}
    review_order: dict[str, list[str]] = {}
    for raw in raw_components:
        if not isinstance(raw, Mapping):
            raise B3TemporalContractError("B3 V6 component is malformed")
        component = dict(raw)
        component_id = _nonempty_string(
            component.get("component_id"), "component id"
        )
        if component_id in components:
            raise B3TemporalContractError("B3 V6 repeats a component")
        if "b2_review_requests" in component:
            raise B3TemporalContractError(
                "B3 V6 component repeats packetized reviews"
            )
        review_ids = _ordered_string_set(
            component.pop("review_ids", None), "component review index"
        )
        component["b2_review_requests"] = []
        components[component_id] = component
        review_order[component_id] = review_ids
    if list(components) != list(body.get("component_ids") or []):
        raise B3TemporalContractError("B3 V6 component index differs")

    component_sources = _component_packet_ownership(
        expanded.get("source_packets"),
        packet_id_field="block_id",
        component_ids=set(components),
        label="source",
    )
    candidate_to_ref, supplied_refs = _candidate_ref_index(
        expanded.get("referent_packets"), component_ids=set(components)
    )
    component_refs = {
        component_id: set(
            _ordered_string_set(
                component.get("referent_refs"), "component referent index"
            )
        )
        for component_id, component in components.items()
    }
    for component_id, refs in component_refs.items():
        if not refs <= supplied_refs:
            raise B3TemporalContractError(
                "B3 V6 component refers to an unsupplied referent"
            )

    packets = expanded.pop("b2_review_packets", None)
    if not isinstance(packets, list):
        raise B3TemporalContractError("B3 V6 review packets are absent")
    packet_by_id: dict[str, dict[str, Any]] = {}
    binding_by_pair: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in packets:
        if not isinstance(raw, Mapping) or set(raw) != {
            "review_id",
            "review",
            "source_block_ids",
            "referent_refs",
            "component_bindings",
        }:
            raise B3TemporalContractError("B3 V6 review packet differs")
        review_id = _nonempty_string(raw.get("review_id"), "review id")
        if review_id in packet_by_id:
            raise B3TemporalContractError("B3 V6 repeats a review packet")
        review = deepcopy(_mapping(raw.get("review"), "review metadata"))
        if {"review_id", "source_block_ids", "referent_refs"}.intersection(review):
            raise B3TemporalContractError(
                "B3 V6 review metadata repeats packet-owned fields"
            )
        candidate_ids = set(
            _sorted_string_set(
                review.get("candidate_card_ids"), "review candidate ids"
            )
        )
        global_sources = set(
            _sorted_string_set(
                raw.get("source_block_ids"), "review source index"
            )
        )
        global_refs = set(
            _sorted_string_set(
                raw.get("referent_refs"), "review referent index"
            )
        )
        mapped_refs = {
            candidate_to_ref[value]
            for value in candidate_ids
            if value in candidate_to_ref
        }
        if not global_refs <= mapped_refs:
            raise B3TemporalContractError(
                "B3 V6 review refers to a foreign candidate"
            )
        raw_bindings = raw.get("component_bindings")
        if not isinstance(raw_bindings, list) or not raw_bindings:
            raise B3TemporalContractError("B3 V6 review bindings are absent")
        actual_component_ids: list[str] = []
        bound_sources: set[str] = set()
        bound_refs: set[str] = set()
        for raw_binding in raw_bindings:
            if not isinstance(raw_binding, Mapping) or set(raw_binding) != {
                "component_id",
                "source_block_ids",
                "referent_refs",
            }:
                raise B3TemporalContractError("B3 V6 review binding differs")
            component_id = _nonempty_string(
                raw_binding.get("component_id"), "review component id"
            )
            if component_id not in components:
                raise B3TemporalContractError(
                    "B3 V6 review references a foreign component"
                )
            if (review_id, component_id) in binding_by_pair:
                raise B3TemporalContractError(
                    "B3 V6 review repeats a component binding"
                )
            sources = set(
                _sorted_string_set(
                    raw_binding.get("source_block_ids"),
                    "review binding source index",
                )
            )
            refs = set(
                _sorted_string_set(
                    raw_binding.get("referent_refs"),
                    "review binding referent index",
                )
            )
            expected_sources = global_sources.intersection(
                component_sources[component_id]
            )
            expected_refs = global_refs.intersection(component_refs[component_id])
            if sources != expected_sources or refs != expected_refs:
                raise B3TemporalContractError(
                    "B3 V6 review binding relevance differs"
                )
            if not sources and not refs:
                raise B3TemporalContractError("B3 V6 review binding is irrelevant")
            binding = {
                "component_id": component_id,
                "source_block_ids": sorted(sources),
                "referent_refs": sorted(refs),
            }
            binding_by_pair[(review_id, component_id)] = binding
            actual_component_ids.append(component_id)
            bound_sources.update(sources)
            bound_refs.update(refs)
        expected_component_ids = sorted(
            component_id
            for component_id in components
            if global_sources.intersection(component_sources[component_id])
            or global_refs.intersection(component_refs[component_id])
        )
        if actual_component_ids != expected_component_ids:
            raise B3TemporalContractError(
                "B3 V6 review component coverage differs"
            )
        if bound_sources != global_sources or bound_refs != global_refs:
            raise B3TemporalContractError("B3 V6 review evidence is not exact-covered")
        packet_by_id[review_id] = review

    for component_id, declared_review_ids in review_order.items():
        bound_review_ids = sorted(
            review_id
            for review_id in packet_by_id
            if (review_id, component_id) in binding_by_pair
        )
        if set(declared_review_ids) != set(bound_review_ids):
            raise B3TemporalContractError(
                "B3 V6 component reviews are not exact-covered"
            )
        for review_id in declared_review_ids:
            row = deepcopy(packet_by_id[review_id])
            binding = binding_by_pair[(review_id, component_id)]
            row.update(
                {
                    "review_id": review_id,
                    "source_block_ids": list(binding["source_block_ids"]),
                    "referent_refs": list(binding["referent_refs"]),
                }
            )
            components[component_id]["b2_review_requests"].append(row)

    expanded.pop("review_packet_schema_version", None)
    expanded["components"] = [components[key] for key in components]
    return expanded


def _component_packet_ownership(
    raw_packets: Any,
    *,
    packet_id_field: str,
    component_ids: set[str],
    label: str,
) -> dict[str, set[str]]:
    if not isinstance(raw_packets, list):
        raise B3TemporalContractError(f"B3 V6 {label} packets are absent")
    result = {component_id: set() for component_id in component_ids}
    seen: set[str] = set()
    for raw in raw_packets:
        packet = _mapping(raw, f"{label} packet")
        packet_id = _nonempty_string(packet.get(packet_id_field), packet_id_field)
        if packet_id in seen:
            raise B3TemporalContractError(f"B3 V6 repeats a {label} packet")
        owners = set(
            _sorted_string_set(packet.get("component_ids"), f"{label} owners")
        )
        if not owners or not owners <= component_ids:
            raise B3TemporalContractError(
                f"B3 V6 {label} packet has foreign owner"
            )
        for component_id in owners:
            result[component_id].add(packet_id)
        seen.add(packet_id)
    return result


def _candidate_ref_index(
    raw_packets: Any, *, component_ids: set[str]
) -> tuple[dict[str, str], set[str]]:
    if not isinstance(raw_packets, list):
        raise B3TemporalContractError("B3 V6 referent packets are absent")
    candidate_to_ref: dict[str, str] = {}
    supplied_refs: set[str] = set()
    for raw in raw_packets:
        packet = _mapping(raw, "referent packet")
        ref = _nonempty_string(packet.get("referent_ref"), "referent ref")
        if ref in supplied_refs:
            raise B3TemporalContractError("B3 V6 repeats a referent packet")
        owners = set(
            _sorted_string_set(packet.get("component_ids"), "referent owners")
        )
        if not owners or not owners <= component_ids:
            raise B3TemporalContractError(
                "B3 V6 referent packet has foreign owner"
            )
        card = _mapping(packet.get("candidate_card"), "candidate card")
        candidate_id = _nonempty_string(
            card.get("candidate_card_id"), "candidate card id"
        )
        previous = candidate_to_ref.setdefault(candidate_id, ref)
        if previous != ref:
            raise B3TemporalContractError(
                "B3 V6 candidate card maps to multiple referents"
            )
        supplied_refs.add(ref)
    return candidate_to_ref, supplied_refs


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise B3TemporalContractError(f"B3 V6 {label} is malformed")
    return value


def _nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise B3TemporalContractError(f"B3 V6 {label} is malformed")
    return value


def _ordered_string_set(value: Any, label: str) -> list[str]:
    if (
        not isinstance(value, list)
        or not all(isinstance(item, str) and item for item in value)
        or len(value) != len(set(value))
    ):
        raise B3TemporalContractError(f"B3 V6 {label} differs")
    return list(value)


def _sorted_string_set(value: Any, label: str) -> list[str]:
    rows = _ordered_string_set(value, label)
    if rows != sorted(rows):
        raise B3TemporalContractError(f"B3 V6 {label} is not sorted")
    return rows


__all__ = [
    "normalize_b3_temporal_response_v6",
    "validate_b3_temporal_request_v6",
]
