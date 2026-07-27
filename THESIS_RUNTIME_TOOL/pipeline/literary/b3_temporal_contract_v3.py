"""Cross-chapter request validation for Literary B3 temporal V3."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Any, Mapping

from pipeline.literary.b3_temporal_context_v3 import B3_REQUEST_SCHEMA_VERSION_V3
from pipeline.literary.b3_temporal_contract_v1 import (
    B3TemporalContractError,
    _normalize_b3_temporal_response_common,
)
from pipeline.literary.b3_temporal_prompts_v3 import (
    B3_TEMPORAL_PROMPT_ID_V3,
    B3_TEMPORAL_SYSTEM_PROMPT_V3,
    b3_temporal_response_schema_v3,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json


def normalize_b3_temporal_response_v3(
    *, request: Mapping[str, Any], response: Mapping[str, Any] | str
) -> dict[str, Any]:
    return _normalize_b3_temporal_response_common(
        request=request,
        response=response,
        request_validator=validate_b3_temporal_request_v3,
    )


def validate_b3_temporal_request_v3(
    request: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(request, Mapping):
        raise B3TemporalContractError("B3 V3 request must be an object")
    body = deepcopy(dict(request))
    if body.get("schema_version") != B3_REQUEST_SCHEMA_VERSION_V3:
        raise B3TemporalContractError("foreign B3 V3 request schema")
    fingerprint = body.get("request_fingerprint")
    if not isinstance(fingerprint, str) or len(fingerprint) != 64:
        raise B3TemporalContractError("B3 V3 request fingerprint is malformed")
    unsigned = dict(body)
    unsigned.pop("request_fingerprint", None)
    if canonical_hash(unsigned) != fingerprint:
        raise B3TemporalContractError("B3 V3 request fingerprint mismatch")
    if body.get("prompt_id") != B3_TEMPORAL_PROMPT_ID_V3:
        raise B3TemporalContractError("B3 V3 prompt id mismatch")
    if body.get("prompt_sha256") != hashlib.sha256(
        B3_TEMPORAL_SYSTEM_PROMPT_V3.encode("utf-8")
    ).hexdigest():
        raise B3TemporalContractError("B3 V3 prompt bytes differ")
    expected_schema = b3_temporal_response_schema_v3()
    if canonical_json(body.get("response_schema")) != canonical_json(expected_schema):
        raise B3TemporalContractError("B3 V3 response schema differs")
    if body.get("response_schema_hash") != canonical_hash(expected_schema):
        raise B3TemporalContractError("B3 V3 response schema hash mismatch")
    if body.get("api_eligible") is not True or body.get("api_ineligible_reasons") != []:
        raise B3TemporalContractError("B3 V3 request is not live eligible")
    messages = body.get("messages")
    if not isinstance(messages, list) or len(messages) != 2:
        raise B3TemporalContractError("B3 V3 request messages differ")
    if messages[0] != {"role": "system", "content": B3_TEMPORAL_SYSTEM_PROMPT_V3}:
        raise B3TemporalContractError("B3 V3 system message differs")
    if not isinstance(messages[1], Mapping) or messages[1].get("role") != "user":
        raise B3TemporalContractError("B3 V3 user message is absent")
    try:
        payload = json.loads(messages[1].get("content"))
    except (TypeError, json.JSONDecodeError) as exc:
        raise B3TemporalContractError("B3 V3 user payload is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise B3TemporalContractError("B3 V3 user payload must be an object")
    if payload.get("chapter_id") != body.get("chapter_id") or payload.get(
        "batch_id"
    ) != body.get("batch_id"):
        raise B3TemporalContractError("B3 V3 identity differs from payload")
    components = payload.get("components")
    if not isinstance(components, list):
        raise B3TemporalContractError("B3 V3 components must be a list")
    component_ids = [
        row.get("component_id") for row in components if isinstance(row, Mapping)
    ]
    if component_ids != body.get("component_ids"):
        raise B3TemporalContractError("B3 V3 component index differs")
    for component in components:
        if not isinstance(component, Mapping):
            raise B3TemporalContractError("B3 V3 component is malformed")
        pending = component.get("prior_pending_cases")
        if not isinstance(pending, list):
            raise B3TemporalContractError("B3 V3 prior pending context is absent")
        for row in pending:
            if not isinstance(row, Mapping) or row.get("authority_status") != "pending_review":
                raise B3TemporalContractError("B3 V3 pending context claims authority")
    return body, payload


__all__ = [
    "normalize_b3_temporal_response_v3",
    "validate_b3_temporal_request_v3",
]
