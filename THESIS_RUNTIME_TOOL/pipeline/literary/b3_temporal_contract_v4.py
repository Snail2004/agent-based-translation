"""Request validation for packet-deduplicated Literary B3 V4."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Any, Mapping

from pipeline.literary.b3_temporal_context_v4 import (
    B3_PRIOR_PACKET_SCHEMA_VERSION_V1,
    B3_REQUEST_SCHEMA_VERSION_V4,
)
from pipeline.literary.b3_temporal_contract_v1 import (
    B3TemporalContractError,
    _normalize_b3_temporal_response_common,
)
from pipeline.literary.b3_temporal_prompts_v4 import (
    B3_TEMPORAL_PROMPT_ID_V4,
    B3_TEMPORAL_SYSTEM_PROMPT_V4,
    b3_temporal_response_schema_v4,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json


def normalize_b3_temporal_response_v4(
    *, request: Mapping[str, Any], response: Mapping[str, Any] | str
) -> dict[str, Any]:
    return _normalize_b3_temporal_response_common(
        request=request,
        response=response,
        request_validator=validate_b3_temporal_request_v4,
    )


def validate_b3_temporal_request_v4(
    request: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(request, Mapping):
        raise B3TemporalContractError("B3 V4 request must be an object")
    body = deepcopy(dict(request))
    if body.get("schema_version") != B3_REQUEST_SCHEMA_VERSION_V4:
        raise B3TemporalContractError("foreign B3 V4 request schema")
    fingerprint = body.get("request_fingerprint")
    unsigned = dict(body)
    unsigned.pop("request_fingerprint", None)
    if not isinstance(fingerprint, str) or canonical_hash(unsigned) != fingerprint:
        raise B3TemporalContractError("B3 V4 request fingerprint mismatch")
    if body.get("prompt_id") != B3_TEMPORAL_PROMPT_ID_V4:
        raise B3TemporalContractError("B3 V4 prompt id mismatch")
    if body.get("prompt_sha256") != hashlib.sha256(
        B3_TEMPORAL_SYSTEM_PROMPT_V4.encode("utf-8")
    ).hexdigest():
        raise B3TemporalContractError("B3 V4 prompt bytes differ")
    schema = b3_temporal_response_schema_v4()
    if canonical_json(body.get("response_schema")) != canonical_json(schema):
        raise B3TemporalContractError("B3 V4 response schema differs")
    if body.get("response_schema_hash") != canonical_hash(schema):
        raise B3TemporalContractError("B3 V4 response schema hash mismatch")
    if body.get("api_eligible") is not True or body.get("api_ineligible_reasons") != []:
        raise B3TemporalContractError("B3 V4 request is not live eligible")
    messages = body.get("messages")
    if not isinstance(messages, list) or len(messages) != 2:
        raise B3TemporalContractError("B3 V4 request messages differ")
    if messages[0] != {"role": "system", "content": B3_TEMPORAL_SYSTEM_PROMPT_V4}:
        raise B3TemporalContractError("B3 V4 system message differs")
    if not isinstance(messages[1], Mapping) or messages[1].get("role") != "user":
        raise B3TemporalContractError("B3 V4 user message is absent")
    try:
        payload = json.loads(messages[1].get("content"))
    except (TypeError, json.JSONDecodeError) as exc:
        raise B3TemporalContractError("B3 V4 user payload is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise B3TemporalContractError("B3 V4 user payload must be an object")
    if payload.get("chapter_id") != body.get("chapter_id") or payload.get(
        "batch_id"
    ) != body.get("batch_id"):
        raise B3TemporalContractError("B3 V4 identity differs from payload")
    if payload.get("prior_context_packet_schema_version") != (
        B3_PRIOR_PACKET_SCHEMA_VERSION_V1
    ):
        raise B3TemporalContractError("B3 V4 prior packet contract differs")
    return body, _expanded_payload(payload=payload, body=body)


def _expanded_payload(
    *, payload: Mapping[str, Any], body: Mapping[str, Any]
) -> dict[str, Any]:
    expanded = deepcopy(dict(payload))
    components = expanded.get("components")
    if not isinstance(components, list):
        raise B3TemporalContractError("B3 V4 components must be a list")
    component_by_id: dict[str, dict[str, Any]] = {}
    for raw in components:
        if not isinstance(raw, Mapping):
            raise B3TemporalContractError("B3 V4 component is malformed")
        component = dict(raw)
        component_id = component.get("component_id")
        if not isinstance(component_id, str) or not component_id:
            raise B3TemporalContractError("B3 V4 component id is malformed")
        if component_id in component_by_id:
            raise B3TemporalContractError("B3 V4 repeats a component")
        if "prior_open_states" in component or "prior_pending_cases" in component:
            raise B3TemporalContractError("B3 V4 component repeats packetized context")
        component["prior_open_states"] = []
        component["prior_pending_cases"] = []
        component_by_id[component_id] = component
    component_ids = list(component_by_id)
    if component_ids != body.get("component_ids"):
        raise B3TemporalContractError("B3 V4 component index differs")
    _expand_packets(
        packets=expanded.pop("prior_state_packets", None),
        component_by_id=component_by_id,
        id_field="state_id",
        value_field="state",
        target_field="prior_open_states",
        authority_field="authority_status",
        authority_value="effective",
    )
    _expand_packets(
        packets=expanded.pop("prior_pending_packets", None),
        component_by_id=component_by_id,
        id_field="pending_case_id",
        value_field="pending_case",
        target_field="prior_pending_cases",
        authority_field="authority_status",
        authority_value="pending_review",
    )
    expanded.pop("prior_context_packet_schema_version", None)
    expanded["components"] = [component_by_id[key] for key in component_ids]
    return expanded


def _expand_packets(
    *,
    packets: Any,
    component_by_id: Mapping[str, dict[str, Any]],
    id_field: str,
    value_field: str,
    target_field: str,
    authority_field: str,
    authority_value: str,
) -> None:
    if not isinstance(packets, list):
        raise B3TemporalContractError(f"B3 V4 {value_field} packets are absent")
    seen: set[str] = set()
    for raw in packets:
        if not isinstance(raw, Mapping) or set(raw) != {
            id_field,
            "component_ids",
            value_field,
        }:
            raise B3TemporalContractError(f"B3 V4 {value_field} packet differs")
        packet_id = raw.get(id_field)
        value = raw.get(value_field)
        component_ids = raw.get("component_ids")
        if not isinstance(packet_id, str) or not packet_id or packet_id in seen:
            raise B3TemporalContractError(f"B3 V4 {value_field} packet id differs")
        if not isinstance(value, Mapping) or value.get(id_field) != packet_id:
            raise B3TemporalContractError(f"B3 V4 {value_field} identity differs")
        if value.get(authority_field) != authority_value:
            raise B3TemporalContractError(f"B3 V4 {value_field} claims wrong authority")
        if (
            not isinstance(component_ids, list)
            or not component_ids
            or component_ids != sorted(set(component_ids))
        ):
            raise B3TemporalContractError(f"B3 V4 {value_field} component index differs")
        referents = _packet_referents(value=value, value_field=value_field)
        expected_component_ids = sorted(
            component_id
            for component_id, component in component_by_id.items()
            if referents
            and referents <= set(component.get("referent_refs") or [])
        )
        if component_ids != expected_component_ids:
            raise B3TemporalContractError(
                f"B3 V4 {value_field} component relevance differs"
            )
        for component_id in component_ids:
            component = component_by_id.get(component_id)
            if component is None:
                raise B3TemporalContractError(
                    f"B3 V4 {value_field} references a foreign component"
                )
            component[target_field].append(deepcopy(dict(value)))
        seen.add(packet_id)


def _packet_referents(*, value: Mapping[str, Any], value_field: str) -> set[str]:
    source: Mapping[str, Any] = value
    if value_field == "pending_case":
        proposed = value.get("proposed_action")
        if not isinstance(proposed, Mapping):
            raise B3TemporalContractError("B3 V4 pending packet lacks an action")
        source = proposed
    subject = source.get("subject_referent_refs")
    counterpart = source.get("counterpart_referent_refs")
    if not isinstance(subject, list) or not isinstance(counterpart, list):
        raise B3TemporalContractError(f"B3 V4 {value_field} referents differ")
    referents = {
        value
        for value in [*subject, *counterpart]
        if isinstance(value, str) and value
    }
    if not referents:
        raise B3TemporalContractError(f"B3 V4 {value_field} referents are absent")
    return referents


__all__ = [
    "normalize_b3_temporal_response_v4",
    "validate_b3_temporal_request_v4",
]
