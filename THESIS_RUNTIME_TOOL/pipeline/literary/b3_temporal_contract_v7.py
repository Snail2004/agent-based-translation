"""Request validation for many-parked-identity Literary B3 V7."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Any, Mapping

from pipeline.literary.b3_temporal_context_v4 import B3_PRIOR_PACKET_SCHEMA_VERSION_V1
from pipeline.literary.b3_temporal_context_v6 import B3_REVIEW_PACKET_SCHEMA_VERSION_V1
from pipeline.literary.b3_temporal_context_v7 import B3_REQUEST_SCHEMA_VERSION_V7
from pipeline.literary.b3_temporal_contract_v1 import (
    B3TemporalContractError,
    _normalize_b3_temporal_response_common,
)
from pipeline.literary.b3_temporal_contract_v4 import _expanded_payload
from pipeline.literary.b3_temporal_contract_v6 import _expanded_review_payload_v6
from pipeline.literary.b3_temporal_prompts_v7 import (
    B3_TEMPORAL_PROMPT_ID_V7,
    B3_TEMPORAL_SYSTEM_PROMPT_V7,
    b3_temporal_response_schema_v7,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json


def normalize_b3_temporal_response_v7(
    *, request: Mapping[str, Any], response: Mapping[str, Any] | str
) -> dict[str, Any]:
    return _normalize_b3_temporal_response_common(
        request=request,
        response=response,
        request_validator=validate_b3_temporal_request_v7,
    )


def validate_b3_temporal_request_v7(
    request: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(request, Mapping):
        raise B3TemporalContractError("B3 V7 request must be an object")
    body = deepcopy(dict(request))
    if body.get("schema_version") != B3_REQUEST_SCHEMA_VERSION_V7:
        raise B3TemporalContractError("foreign B3 V7 request schema")
    fingerprint = body.get("request_fingerprint")
    unsigned = dict(body)
    unsigned.pop("request_fingerprint", None)
    if not isinstance(fingerprint, str) or canonical_hash(unsigned) != fingerprint:
        raise B3TemporalContractError("B3 V7 request fingerprint mismatch")
    if body.get("prompt_id") != B3_TEMPORAL_PROMPT_ID_V7:
        raise B3TemporalContractError("B3 V7 prompt id mismatch")
    if body.get("prompt_sha256") != hashlib.sha256(
        B3_TEMPORAL_SYSTEM_PROMPT_V7.encode("utf-8")
    ).hexdigest():
        raise B3TemporalContractError("B3 V7 prompt bytes differ")
    schema = b3_temporal_response_schema_v7()
    if canonical_json(body.get("response_schema")) != canonical_json(schema):
        raise B3TemporalContractError("B3 V7 response schema differs")
    if body.get("response_schema_hash") != canonical_hash(schema):
        raise B3TemporalContractError("B3 V7 response schema hash mismatch")
    if body.get("api_eligible") is not True or body.get(
        "api_ineligible_reasons"
    ) != []:
        raise B3TemporalContractError("B3 V7 request is not live eligible")
    messages = body.get("messages")
    if not isinstance(messages, list) or len(messages) != 2:
        raise B3TemporalContractError("B3 V7 request messages differ")
    if messages[0] != {"role": "system", "content": B3_TEMPORAL_SYSTEM_PROMPT_V7}:
        raise B3TemporalContractError("B3 V7 system message differs")
    if not isinstance(messages[1], Mapping) or messages[1].get("role") != "user":
        raise B3TemporalContractError("B3 V7 user message is absent")
    try:
        payload = json.loads(messages[1].get("content"))
    except (TypeError, json.JSONDecodeError) as exc:
        raise B3TemporalContractError("B3 V7 user payload is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise B3TemporalContractError("B3 V7 user payload must be an object")
    if payload.get("chapter_id") != body.get("chapter_id") or payload.get(
        "batch_id"
    ) != body.get("batch_id"):
        raise B3TemporalContractError("B3 V7 identity differs from payload")
    if payload.get("prior_context_packet_schema_version") != (
        B3_PRIOR_PACKET_SCHEMA_VERSION_V1
    ):
        raise B3TemporalContractError("B3 V7 prior packet contract differs")
    if payload.get("review_packet_schema_version") != (
        B3_REVIEW_PACKET_SCHEMA_VERSION_V1
    ):
        raise B3TemporalContractError("B3 V7 review packet contract differs")
    review_expanded = _expanded_review_payload_v6(payload=payload, body=body)
    return body, _expanded_payload(payload=review_expanded, body=body)


__all__ = [
    "normalize_b3_temporal_response_v7",
    "validate_b3_temporal_request_v7",
]
