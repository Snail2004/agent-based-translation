"""Provider-neutral capability contract for Literary B3 temporal V7."""

from __future__ import annotations

import hashlib
from typing import Any, Mapping

from pipeline.literary.b3_temporal_context_v4 import (
    B3_PRIOR_PACKET_SCHEMA_VERSION_V1,
)
from pipeline.literary.b3_temporal_context_v7 import (
    B3_REQUEST_SCHEMA_VERSION_V7,
    B3_REVIEW_PACKET_SCHEMA_VERSION_V1,
)
from pipeline.literary.b3_temporal_contract_v7 import (
    normalize_b3_temporal_response_v7,
    validate_b3_temporal_request_v7,
)
from pipeline.literary.b3_temporal_prompts_v7 import (
    B3_TEMPORAL_PROMPT_ID_V7,
    B3_TEMPORAL_SYSTEM_PROMPT_V7,
    b3_temporal_response_schema_v7,
)
from pipeline.literary.chapter_cycle_shared_runtime_v1 import (
    build_literary_code_ref_v1,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json
from pipeline.literary.structured_prompt_reserve_v1 import (
    structured_prompt_reserve_v1,
)


def synthetic_b3_probe_request_v7() -> dict[str, Any]:
    chapter_id = "literary_b3_probe_chapter"
    batch_id = "literary_b3_probe_batch"
    component_id = "literary_b3_probe_component"
    payload = {
        "request_kind": "chapter_temporal_state_batch",
        "chapter_id": chapter_id,
        "batch_id": batch_id,
        "source_b2_artifact_hash": "1" * 64,
        "source_prefix_bundle_hash": "2" * 64,
        "components": [
            {
                "component_id": component_id,
                "component_hash": "3" * 64,
                "component_kind": "unresolved",
                "domain_hints": [],
                "referent_refs": [],
                "frame_segment_ids": [],
                "speaker_turns": [],
                "salient_events": [],
                "review_ids": [],
            }
        ],
        "referent_packets": [],
        "source_packets": [],
        "frame_packets": [],
        "prior_context_packet_schema_version": B3_PRIOR_PACKET_SCHEMA_VERSION_V1,
        "prior_state_packets": [],
        "prior_pending_packets": [],
        "review_packet_schema_version": B3_REVIEW_PACKET_SCHEMA_VERSION_V1,
        "b2_review_packets": [],
    }
    messages = [
        {"role": "system", "content": B3_TEMPORAL_SYSTEM_PROMPT_V7},
        {"role": "user", "content": canonical_json(payload)},
    ]
    schema = b3_temporal_response_schema_v7()
    reserve = structured_prompt_reserve_v1(
        messages=messages,
        response_schema=schema,
        output_token_cap=1024,
    ).to_payload()
    body = {
        "schema_version": B3_REQUEST_SCHEMA_VERSION_V7,
        "request_kind": "chapter_temporal_state_batch",
        "prompt_id": B3_TEMPORAL_PROMPT_ID_V7,
        "prompt_sha256": hashlib.sha256(
            B3_TEMPORAL_SYSTEM_PROMPT_V7.encode("utf-8")
        ).hexdigest(),
        "chapter_id": chapter_id,
        "batch_id": batch_id,
        "component_ids": [component_id],
        "messages": messages,
        "response_schema": schema,
        "response_schema_hash": canonical_hash(schema),
        "token_reserve": reserve,
        "configured_prompt_cap": 10000,
        "configured_output_cap": 1024,
        "api_eligible": True,
        "api_ineligible_reasons": [],
        "context_hashes": {
            "source_input_hash": "4" * 64,
            "source_b2_artifact_hash": "1" * 64,
            "source_prefix_bundle_hash": "2" * 64,
            "component_hashes": ["3" * 64],
        },
        "gold_or_oracle_loaded": False,
        "production_publish_performed": False,
    }
    return {**body, "request_fingerprint": canonical_hash(body)}


def empty_b3_probe_response_v3(
    request: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": "literary_b3_temporal_response_v3",
        "chapter_id": request["chapter_id"],
        "batch_id": request["batch_id"],
        "component_results": [
            {
                "component_id": request["component_ids"][0],
                "disposition": "no_durable_change",
                "state_actions": [],
                "pending_route": "none",
                "pending_reason": None,
                "inherited_parked_identities": [],
            }
        ],
    }


def b3_validator_ref_v4() -> dict[str, str]:
    return build_literary_code_ref_v1(
        identifier="literary.b3.temporal_state.validator",
        revision="v5",
        callables=(
            validate_b3_temporal_request_v7,
            normalize_b3_temporal_response_v7,
        ),
    )


def validate_literary_b3_probe_payload_v3(
    *, request: Mapping[str, Any], payload: Mapping[str, Any]
) -> dict[str, Any]:
    return normalize_b3_temporal_response_v7(request=request, response=payload)


__all__ = [
    "b3_validator_ref_v4",
    "empty_b3_probe_response_v3",
    "synthetic_b3_probe_request_v7",
    "validate_literary_b3_probe_payload_v3",
]
