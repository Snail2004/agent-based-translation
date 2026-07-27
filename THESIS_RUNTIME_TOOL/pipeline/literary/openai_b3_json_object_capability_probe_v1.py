"""One-shot official OpenAI JSON-object qualification for Literary B3 V2."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any, Mapping

from pipeline.llm_backend import (
    SharedLlmCapabilityProbe,
    canonical_sha256,
    create_capability_probe_seal,
)
from pipeline.literary.b3_temporal_context_v2 import B3_REQUEST_SCHEMA_VERSION_V2
from pipeline.literary.b3_temporal_contract_v2 import (
    normalize_b3_temporal_response_v2,
    validate_b3_temporal_request_v2,
)
from pipeline.literary.b3_temporal_prompts_v2 import (
    B3_TEMPORAL_PROMPT_ID_V2,
    B3_TEMPORAL_SYSTEM_PROMPT_V2,
    b3_temporal_response_schema_v2,
)
from pipeline.literary.b3_temporal_context_v4 import B3_PRIOR_PACKET_SCHEMA_VERSION_V1
from pipeline.literary.b3_temporal_context_v5 import B3_REQUEST_SCHEMA_VERSION_V5
from pipeline.literary.b3_temporal_contract_v5 import (
    normalize_b3_temporal_response_v5,
    validate_b3_temporal_request_v5,
)
from pipeline.literary.b3_temporal_prompts_v5 import (
    B3_TEMPORAL_PROMPT_ID_V5,
    B3_TEMPORAL_SYSTEM_PROMPT_V5,
    b3_temporal_response_schema_v5,
)
from pipeline.literary.chapter_cycle_shared_runtime_v1 import (
    build_literary_code_ref_v1,
)
from pipeline.literary.model_ref_transport_v1 import (
    bind_model_ref_validator_v1,
    project_capability_probe_request_v1,
    resolve_capability_probe_response_v1,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json, file_sha256
from pipeline.literary.shared_llm_adapter_v1 import render_literary_request_body
from pipeline.literary.shared_runtime_profile_v2 import (
    LOCAL_VALIDATION_SCHEMA_DIALECT,
    load_literary_shared_runtime_profile_v2,
)
from pipeline.literary.structured_prompt_reserve_v1 import (
    structured_prompt_reserve_v1,
)


ROLE_ID = "literary.b3.temporal_state"
RUNTIME_SOURCE_ALIAS = "openai_official_row2"
ACCEPTED_OBSERVED_MODEL_IDS = ("gpt-5.4", "gpt-5.4-2026-03-05")

_PIPELINE_ROOT = Path(__file__).resolve().parents[1]
_TOOL_ROOT = _PIPELINE_ROOT.parent
_REPO_ROOT = _TOOL_ROOT.parent
DEFAULT_PROFILE_PATH = (
    _PIPELINE_ROOT
    / "configs"
    / "literary_openai_b3_json_object_probe_v1.json"
)
RUNTIME_PROFILE_PATH = (
    _PIPELINE_ROOT
    / "configs"
    / "literary_shared_llm_runtime_openai_b3_temporal_v1.json"
)
_IMPLEMENTATION_PATHS = (
    Path("THESIS_RUNTIME_TOOL/pipeline/configs/literary_openai_b3_json_object_probe_v1.json"),
    Path("THESIS_RUNTIME_TOOL/pipeline/configs/literary_shared_llm_runtime_openai_b3_temporal_v1.json"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/b3_temporal_context_v2.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/b3_temporal_contract_v1.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/b3_temporal_contract_v2.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/b3_temporal_prompts_v2.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/model_ref_transport_v1.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/model_ref_v1.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/openai_b3_json_object_capability_probe_v1.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/shared_llm_adapter_v1.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/shared_runtime_profile_v2.py"),
)


class LiteraryOpenAiB3CapabilityProbeError(RuntimeError):
    pass


@dataclass(frozen=True)
class LiteraryOpenAiB3ProbePlanV1:
    profile: Mapping[str, Any]
    runtime_profile_sha256: str
    source: Mapping[str, Any]
    request: Mapping[str, Any]
    response_schema: Mapping[str, Any]
    validator_ref: Mapping[str, str]
    request_body: Mapping[str, Any]
    implementation_binding: Mapping[str, str]
    seal: Mapping[str, Any]


def load_literary_openai_b3_probe_profile_v1(
    path: Path = DEFAULT_PROFILE_PATH,
) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as exc:
        raise LiteraryOpenAiB3CapabilityProbeError(
            "cannot load official OpenAI B3 probe profile"
        ) from exc
    if payload != _expected_profile():
        raise LiteraryOpenAiB3CapabilityProbeError(
            "official OpenAI B3 probe profile differs from the closed contract"
        )
    return deepcopy(payload)


def synthetic_b3_probe_request_v2() -> dict[str, Any]:
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
                "speaker_turns": [],
                "salient_events": [],
                "prior_open_states": [],
                "b2_review_requests": [],
            }
        ],
        "referent_packets": [],
        "source_packets": [],
        "frame_packets": [],
    }
    messages = [
        {"role": "system", "content": B3_TEMPORAL_SYSTEM_PROMPT_V2},
        {"role": "user", "content": canonical_json(payload)},
    ]
    schema = b3_temporal_response_schema_v2()
    reserve = structured_prompt_reserve_v1(
        messages=messages,
        response_schema=schema,
        output_token_cap=1024,
    ).to_payload()
    body = {
        "schema_version": B3_REQUEST_SCHEMA_VERSION_V2,
        "request_kind": "chapter_temporal_state_batch",
        "prompt_id": B3_TEMPORAL_PROMPT_ID_V2,
        "prompt_sha256": hashlib.sha256(
            B3_TEMPORAL_SYSTEM_PROMPT_V2.encode("utf-8")
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


def empty_b3_probe_response_v1(
    request: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    request = dict(request or synthetic_b3_probe_request_v2())
    return {
        "schema_version": "literary_b3_temporal_response_v1",
        "chapter_id": request["chapter_id"],
        "batch_id": request["batch_id"],
        "component_results": [
            {
                "component_id": request["component_ids"][0],
                "disposition": "no_durable_change",
                "state_actions": [],
                "pending_route": "none",
                "pending_reason": None,
            }
        ],
    }


def synthetic_b3_probe_request_v5() -> dict[str, Any]:
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
                "b2_review_requests": [],
            }
        ],
        "referent_packets": [],
        "source_packets": [],
        "frame_packets": [],
        "prior_context_packet_schema_version": B3_PRIOR_PACKET_SCHEMA_VERSION_V1,
        "prior_state_packets": [],
        "prior_pending_packets": [],
    }
    messages = [
        {"role": "system", "content": B3_TEMPORAL_SYSTEM_PROMPT_V5},
        {"role": "user", "content": canonical_json(payload)},
    ]
    schema = b3_temporal_response_schema_v5()
    reserve = structured_prompt_reserve_v1(
        messages=messages,
        response_schema=schema,
        output_token_cap=1024,
    ).to_payload()
    body = {
        "schema_version": B3_REQUEST_SCHEMA_VERSION_V5,
        "request_kind": "chapter_temporal_state_batch",
        "prompt_id": B3_TEMPORAL_PROMPT_ID_V5,
        "prompt_sha256": hashlib.sha256(
            B3_TEMPORAL_SYSTEM_PROMPT_V5.encode("utf-8")
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


def empty_b3_probe_response_v2(
    request: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    request = dict(request or synthetic_b3_probe_request_v5())
    return {
        "schema_version": "literary_b3_temporal_response_v2",
        "chapter_id": request["chapter_id"],
        "batch_id": request["batch_id"],
        "component_results": [
            {
                "component_id": request["component_ids"][0],
                "disposition": "no_durable_change",
                "state_actions": [],
                "pending_route": "none",
                "pending_reason": None,
                "inherited_parked_identity": None,
            }
        ],
    }


def b3_validator_ref_v2() -> dict[str, str]:
    return build_literary_code_ref_v1(
        identifier="literary.b3.temporal_state.validator",
        revision="v2",
        callables=(
            validate_b3_temporal_request_v2,
            normalize_b3_temporal_response_v2,
        ),
    )


def b3_validator_ref_v3() -> dict[str, str]:
    return build_literary_code_ref_v1(
        identifier="literary.b3.temporal_state.validator",
        revision="v3",
        callables=(
            validate_b3_temporal_request_v5,
            normalize_b3_temporal_response_v5,
        ),
    )


def validate_literary_openai_b3_probe_payload_v1(
    *, request: Mapping[str, Any], payload: Mapping[str, Any]
) -> dict[str, Any]:
    return normalize_b3_temporal_response_v2(request=request, response=payload)


def validate_literary_openai_b3_probe_payload_v2(
    *, request: Mapping[str, Any], payload: Mapping[str, Any]
) -> dict[str, Any]:
    return normalize_b3_temporal_response_v5(request=request, response=payload)


def implementation_sha256_v1() -> str:
    rows = []
    for relative in _IMPLEMENTATION_PATHS:
        path = _REPO_ROOT / relative
        if not path.is_file():
            raise LiteraryOpenAiB3CapabilityProbeError(
                f"B3 probe implementation file is absent: {relative.as_posix()}"
            )
        rows.append({"path": relative.as_posix(), "sha256": file_sha256(path)})
    return canonical_sha256(rows)


def build_clean_implementation_binding_v1() -> dict[str, str]:
    status = _git_text("status", "--short", "--untracked-files=no")
    if status:
        raise LiteraryOpenAiB3CapabilityProbeError(
            "B3 capability probe requires a clean tracked worktree"
        )
    return {
        "shared_core_revision": load_literary_openai_b3_probe_profile_v1()[
            "shared_core_revision"
        ],
        "consumer_revision": _git_text("rev-parse", "HEAD"),
        "consumer_implementation_sha256": implementation_sha256_v1(),
    }


def build_literary_openai_b3_probe_plan_v1(
    *,
    probe_run_id: str,
    credential_commitment_sha256: str,
    issued_at_utc: str,
    implementation_binding: Mapping[str, str] | None = None,
) -> LiteraryOpenAiB3ProbePlanV1:
    profile = load_literary_openai_b3_probe_profile_v1()
    runtime = load_literary_shared_runtime_profile_v2(
        RUNTIME_PROFILE_PATH,
        expected_role_ids={ROLE_ID},
    )
    binding = dict(implementation_binding or build_clean_implementation_binding_v1())
    source_binding = dict(runtime.source_binding_for(ROLE_ID))
    source = {
        "schema_version": "api_source_v1",
        "source_id": source_binding["source_id"],
        "source_revision": source_binding["source_revision"],
        "source_class": source_binding["source_class"],
        "adapter_id": source_binding["adapter_id"],
        "protocol": source_binding["protocol"],
        "route_id": source_binding["route_id"],
        "endpoint_class": source_binding["endpoint_class"],
        "base_url": source_binding["base_url"],
        "credential_ref": source_binding["credential_ref"],
        "credential_commitment": credential_commitment_sha256,
        "physical_quota_bucket_id": source_binding["physical_quota_bucket_id"],
        "enabled": True,
    }
    canonical_request = synthetic_b3_probe_request_v2()
    request = project_capability_probe_request_v1(canonical_request)
    schema = deepcopy(dict(request["response_schema"]))
    validator_ref = bind_model_ref_validator_v1(b3_validator_ref_v2())
    preset = runtime.role_presets[ROLE_ID]
    generation = deepcopy(dict(preset.generation))
    generation.update(
        {
            "temperature": profile["generation"]["temperature"],
            "top_p": profile["generation"]["top_p"],
            "seed": profile["generation"]["seed"],
            "reasoning_effort": profile["generation"]["reasoning_effort"],
            "verbosity": profile["generation"]["verbosity"],
            "max_output_tokens": profile["generation"]["max_completion_tokens"],
        }
    )
    request_body = render_literary_request_body(
        preset=replace(preset, generation=generation),
        protocol=source["protocol"],
        capability={"capability_kind": "json_object"},
        messages=request["messages"],
        response_schema=schema,
        instruction_schema=schema,
        schema_name=profile["schema_name"],
        structured_output=runtime.shared_structured_output_for(ROLE_ID),
        output_envelope=runtime.output_envelope_for(ROLE_ID),
        base_url=source["base_url"],
    )
    if request_body.get("response_format") != {"type": "json_object"}:
        raise LiteraryOpenAiB3CapabilityProbeError(
            "B3 probe must use JSON-object mode without native schema"
        )
    schema_sha = canonical_sha256(schema)
    capability_revision = (
        f"stable_schema_{schema_sha[:8]}_validator_{validator_ref['sha256'][:8]}_v1"
    )
    intent = {
        "capability_id": "openai_row2_gpt54_literary_b3_json_object_v1",
        "capability_revision": capability_revision,
        "requested_model_id": profile["requested_model_id"],
        "accepted_observed_model_ids": profile["accepted_observed_model_ids"],
        "capability_kind": profile["capability_kind"],
        "schema_name": profile["schema_name"],
        "schema_dialect": profile["schema_dialect"],
        "schema_sha256": schema_sha,
        "local_validator_id": validator_ref["id"],
        "local_validator_sha256": validator_ref["sha256"],
    }
    seal = create_capability_probe_seal(
        source=source,
        consumer_workstream="literary",
        role_id=ROLE_ID,
        probe_run_id=probe_run_id,
        probe_profile_id=profile["profile_id"],
        probe_profile_revision=profile["profile_revision"],
        implementation_binding=binding,
        capability_intent=intent,
        response_schema=schema,
        request_body=request_body,
        limits=profile["limits"],
        issued_at_utc=issued_at_utc,
    )
    return LiteraryOpenAiB3ProbePlanV1(
        profile=profile,
        runtime_profile_sha256=runtime.profile_sha256,
        source=source,
        request=request,
        response_schema=schema,
        validator_ref=validator_ref,
        request_body=request_body,
        implementation_binding=binding,
        seal=seal,
    )


def execute_literary_openai_b3_probe_once_v1(
    *,
    probe: SharedLlmCapabilityProbe,
    plan: LiteraryOpenAiB3ProbePlanV1,
    cost_fact: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    canonical_request = synthetic_b3_probe_request_v2()

    def validate(payload: Mapping[str, Any]) -> dict[str, Any]:
        resolved = resolve_capability_probe_response_v1(
            projected_request=plan.request,
            response=payload,
        )
        return validate_literary_openai_b3_probe_payload_v1(
            request=canonical_request,
            payload=resolved,
        )

    return probe.execute_once(
        seal=plan.seal,
        request_body=plan.request_body,
        local_validator=validate,
        local_validator_id=plan.validator_ref["id"],
        local_validator_sha256=plan.validator_ref["sha256"],
        cost_fact=cost_fact,
    )


def _expected_profile() -> dict[str, Any]:
    return {
        "schema_version": "literary_openai_b3_json_object_probe_profile_v1",
        "profile_id": "literary_openai_official_b3_json_object_probe_v1",
        "profile_revision": "openai_row2_gpt54_b3_stable_schema_v1",
        "shared_core_revision": "de7c74ba348bffe507aa86933f438b3ed4c5af29",
        "role_id": ROLE_ID,
        "requested_model_id": "gpt-5.4",
        "accepted_observed_model_ids": list(ACCEPTED_OBSERVED_MODEL_IDS),
        "capability_kind": "json_object",
        "schema_name": "literary_b3_temporal_response_v2",
        "schema_dialect": LOCAL_VALIDATION_SCHEMA_DIALECT,
        "generation": {
            "temperature": 1,
            "top_p": 1,
            "seed": 20260720,
            "reasoning_effort": "none",
            "verbosity": "low",
            "max_completion_tokens": 1024,
        },
        "limits": {
            "max_calls": 1,
            "max_prompt_utf8_bytes": 65536,
            "max_response_utf8_bytes": 32768,
            "max_prompt_tokens": 10000,
            "max_completion_tokens": 1024,
            "max_total_tokens": 11024,
            "request_timeout_ms": 120000,
        },
        "safety": {
            "authority": "capability_only",
            "fallback_enabled": False,
            "transport_retry_max": 0,
            "semantic_retry_max": 0,
            "response_cache_enabled": False,
            "application_publish_enabled": False,
        },
    }


def _git_text(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=_REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


__all__ = [
    "DEFAULT_PROFILE_PATH",
    "ROLE_ID",
    "RUNTIME_PROFILE_PATH",
    "LiteraryOpenAiB3CapabilityProbeError",
    "LiteraryOpenAiB3ProbePlanV1",
    "b3_validator_ref_v2",
    "b3_validator_ref_v3",
    "build_clean_implementation_binding_v1",
    "build_literary_openai_b3_probe_plan_v1",
    "empty_b3_probe_response_v1",
    "empty_b3_probe_response_v2",
    "execute_literary_openai_b3_probe_once_v1",
    "implementation_sha256_v1",
    "load_literary_openai_b3_probe_profile_v1",
    "synthetic_b3_probe_request_v2",
    "synthetic_b3_probe_request_v5",
    "validate_literary_openai_b3_probe_payload_v1",
    "validate_literary_openai_b3_probe_payload_v2",
]
