"""Stable-schema request validation for Literary B3 temporal V2."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Any, Mapping

from pipeline.literary.b3_temporal_context_v2 import (
    B3_REQUEST_SCHEMA_VERSION_V2,
)
from pipeline.literary.b3_temporal_contract_v1 import (
    B3TemporalContractError,
    _normalize_b3_temporal_response_common,
)
from pipeline.literary.b3_temporal_prompts_v2 import (
    B3_TEMPORAL_PROMPT_ID_V2,
    B3_TEMPORAL_SYSTEM_PROMPT_V2,
    b3_temporal_response_schema_v2,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json


def normalize_b3_temporal_response_v2(
    *, request: Mapping[str, Any], response: Mapping[str, Any] | str
) -> dict[str, Any]:
    return _normalize_b3_temporal_response_common(
        request=request,
        response=response,
        request_validator=validate_b3_temporal_request_v2,
    )


def validate_b3_temporal_request_v2(
    request: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(request, Mapping):
        raise B3TemporalContractError("B3 V2 request must be an object")
    body = deepcopy(dict(request))
    if body.get("schema_version") != B3_REQUEST_SCHEMA_VERSION_V2:
        raise B3TemporalContractError("foreign B3 V2 request schema")
    fingerprint = body.get("request_fingerprint")
    if not isinstance(fingerprint, str) or len(fingerprint) != 64:
        raise B3TemporalContractError("B3 V2 request fingerprint is malformed")
    unsigned = dict(body)
    unsigned.pop("request_fingerprint", None)
    if canonical_hash(unsigned) != fingerprint:
        raise B3TemporalContractError("B3 V2 request fingerprint mismatch")
    if body.get("prompt_id") != B3_TEMPORAL_PROMPT_ID_V2:
        raise B3TemporalContractError("B3 V2 prompt id mismatch")
    expected_prompt_hash = hashlib.sha256(
        B3_TEMPORAL_SYSTEM_PROMPT_V2.encode("utf-8")
    ).hexdigest()
    if body.get("prompt_sha256") != expected_prompt_hash:
        raise B3TemporalContractError("B3 V2 prompt bytes differ")
    schema = body.get("response_schema")
    expected_schema = b3_temporal_response_schema_v2()
    if not isinstance(schema, Mapping) or canonical_json(schema) != canonical_json(
        expected_schema
    ):
        raise B3TemporalContractError("B3 V2 response schema is not stable canonical")
    if body.get("response_schema_hash") != canonical_hash(expected_schema):
        raise B3TemporalContractError("B3 V2 response schema hash mismatch")
    if body.get("api_eligible") is not True or body.get(
        "api_ineligible_reasons"
    ) != []:
        raise B3TemporalContractError("B3 V2 request is not live eligible")
    messages = body.get("messages")
    if not isinstance(messages, list) or len(messages) != 2:
        raise B3TemporalContractError("B3 V2 request messages differ")
    if messages[0] != {"role": "system", "content": B3_TEMPORAL_SYSTEM_PROMPT_V2}:
        raise B3TemporalContractError("B3 V2 system message differs")
    if not isinstance(messages[1], Mapping) or messages[1].get("role") != "user":
        raise B3TemporalContractError("B3 V2 user message is absent")
    try:
        payload = json.loads(messages[1].get("content"))
    except (TypeError, json.JSONDecodeError) as exc:
        raise B3TemporalContractError("B3 V2 user payload is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise B3TemporalContractError("B3 V2 user payload must be an object")
    if payload.get("chapter_id") != body.get("chapter_id") or payload.get(
        "batch_id"
    ) != body.get("batch_id"):
        raise B3TemporalContractError("B3 V2 request identity differs from payload")
    components = payload.get("components")
    if not isinstance(components, list):
        raise B3TemporalContractError("B3 V2 components must be a list")
    component_ids = [
        row.get("component_id") for row in components if isinstance(row, Mapping)
    ]
    if component_ids != body.get("component_ids"):
        raise B3TemporalContractError("B3 V2 component index differs from payload")
    return body, payload


__all__ = [
    "normalize_b3_temporal_response_v2",
    "validate_b3_temporal_request_v2",
]
