"""One-shot ModelAPI JSON-object qualification for the B3 claim auditor."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
import json
from pathlib import Path
import subprocess
from typing import Any, Mapping

from pipeline.llm_backend import (
    SharedLlmCapabilityProbe,
    canonical_sha256,
    create_capability_probe_seal,
)
from pipeline.literary.b3_temporal_auditor_v1 import (
    ROLE_ID,
    b3_temporal_audit_response_schema_v1,
    build_b3_temporal_review_overlay_v1,
    render_b3_temporal_audit_request_v1,
    synthetic_b3_temporal_review_packet_v1,
)
from pipeline.literary.chapter_cycle_shared_runtime_v1 import build_literary_code_ref_v1
from pipeline.literary.model_ref_transport_v1 import (
    bind_model_ref_validator_v1,
    project_capability_probe_request_v1,
    resolve_capability_probe_response_v1,
)
from pipeline.literary.checkpoint import file_sha256
from pipeline.literary.shared_llm_adapter_v1 import render_literary_request_body
from pipeline.literary.shared_runtime_profile_v2 import (
    LOCAL_VALIDATION_SCHEMA_DIALECT,
    load_literary_shared_runtime_profile_v2,
)


_PIPELINE_ROOT = Path(__file__).resolve().parents[1]
_TOOL_ROOT = _PIPELINE_ROOT.parent
_REPO_ROOT = _TOOL_ROOT.parent
PROFILE_PATH = (
    _PIPELINE_ROOT / "configs" / "literary_modelapi_b3_temporal_auditor_probe_v1.json"
)
RUNTIME_PROFILE_PATH = (
    _PIPELINE_ROOT
    / "configs"
    / "literary_shared_llm_runtime_modelapi_b3_temporal_auditor_v1.json"
)
_IMPLEMENTATION_PATHS = (
    Path("THESIS_RUNTIME_TOOL/pipeline/configs/literary_modelapi_b3_temporal_auditor_probe_v1.json"),
    Path("THESIS_RUNTIME_TOOL/pipeline/configs/literary_shared_llm_runtime_modelapi_b3_temporal_auditor_v1.json"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/b3_temporal_auditor_v1.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/model_ref_transport_v1.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/model_ref_v1.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/modelapi_b3_temporal_auditor_capability_probe_v1.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/shared_llm_adapter_v1.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/shared_runtime_profile_v2.py"),
)


class B3TemporalAuditProbeError(RuntimeError):
    pass


@dataclass(frozen=True)
class B3TemporalAuditProbePlanV1:
    source: Mapping[str, Any]
    request: Mapping[str, Any]
    response_schema: Mapping[str, Any]
    validator_ref: Mapping[str, str]
    request_body: Mapping[str, Any]
    implementation_binding: Mapping[str, str]
    seal: Mapping[str, Any]


def validator_ref_v1() -> dict[str, str]:
    return build_literary_code_ref_v1(
        identifier="literary.audit.b3_stable_claim.validator",
        revision="v1_3",
        callables=(
            b3_temporal_audit_response_schema_v1,
            build_b3_temporal_review_overlay_v1,
        ),
    )


def load_probe_profile_v1(path: Path = PROFILE_PATH) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as exc:
        raise B3TemporalAuditProbeError("cannot load B3 audit probe profile") from exc
    if payload != _expected_profile():
        raise B3TemporalAuditProbeError("B3 audit probe profile differs")
    return deepcopy(payload)


def build_probe_plan_v1(
    *,
    probe_run_id: str,
    credential_commitment_sha256: str,
    issued_at_utc: str,
    implementation_binding: Mapping[str, str] | None = None,
) -> B3TemporalAuditProbePlanV1:
    profile = load_probe_profile_v1()
    runtime = load_literary_shared_runtime_profile_v2(
        RUNTIME_PROFILE_PATH, expected_role_ids={ROLE_ID}
    )
    binding = dict(runtime.source_binding_for(ROLE_ID))
    if (
        binding.get("authority_class") != "third_party"
        or binding.get("base_url") != "https://modelapi.vn/v1"
        or binding.get("fallback_enabled") is not False
    ):
        raise B3TemporalAuditProbeError("B3 audit source binding differs")
    source = {
        "schema_version": "api_source_v1",
        "source_id": binding["source_id"],
        "source_revision": binding["source_revision"],
        "source_class": binding["source_class"],
        "adapter_id": binding["adapter_id"],
        "protocol": binding["protocol"],
        "route_id": binding["route_id"],
        "endpoint_class": binding["endpoint_class"],
        "base_url": binding["base_url"],
        "credential_ref": binding["credential_ref"],
        "credential_commitment": credential_commitment_sha256,
        "physical_quota_bucket_id": binding["physical_quota_bucket_id"],
        "enabled": True,
    }
    packet = synthetic_b3_temporal_review_packet_v1()
    rendered = render_b3_temporal_audit_request_v1(packet)
    request = project_capability_probe_request_v1(
        {
            "messages": [dict(row) for row in rendered.messages],
            "response_schema": b3_temporal_audit_response_schema_v1(),
            "request_fingerprint": rendered.request_fingerprint,
        }
    )
    schema = deepcopy(dict(request["response_schema"]))
    validator_ref = bind_model_ref_validator_v1(validator_ref_v1())
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
        raise B3TemporalAuditProbeError("B3 audit probe is not JSON-object mode")
    intent = {
        "capability_id": "modelapi_gpt54_literary_b3_temporal_auditor_json_object_v4",
        "capability_revision": (
            f"schema_{canonical_sha256(schema)[:8]}_validator_{validator_ref['sha256'][:8]}_v4"
        ),
        "requested_model_id": profile["requested_model_id"],
        "accepted_observed_model_ids": profile["accepted_observed_model_ids"],
        "capability_kind": profile["capability_kind"],
        "schema_name": profile["schema_name"],
        "schema_dialect": profile["schema_dialect"],
        "schema_sha256": canonical_sha256(schema),
        "local_validator_id": validator_ref["id"],
        "local_validator_sha256": validator_ref["sha256"],
    }
    implementation = dict(
        implementation_binding or build_clean_implementation_binding_v1()
    )
    seal = create_capability_probe_seal(
        source=source,
        consumer_workstream="literary",
        role_id=ROLE_ID,
        probe_run_id=probe_run_id,
        probe_profile_id=profile["profile_id"],
        probe_profile_revision=profile["profile_revision"],
        implementation_binding=implementation,
        capability_intent=intent,
        response_schema=schema,
        request_body=request_body,
        limits=profile["limits"],
        issued_at_utc=issued_at_utc,
    )
    return B3TemporalAuditProbePlanV1(
        source=source,
        request=request,
        response_schema=schema,
        validator_ref=validator_ref,
        request_body=request_body,
        implementation_binding=implementation,
        seal=seal,
    )


def execute_probe_once_v1(
    *, probe: SharedLlmCapabilityProbe, plan: B3TemporalAuditProbePlanV1
) -> dict[str, Any]:
    packet = synthetic_b3_temporal_review_packet_v1()

    def validate(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        resolved = resolve_capability_probe_response_v1(
            projected_request=plan.request,
            response=payload,
        )
        return build_b3_temporal_review_overlay_v1(
            packet=packet,
            decision=resolved,
        )

    return probe.execute_once(
        seal=plan.seal,
        request_body=plan.request_body,
        local_validator=validate,
        local_validator_id=plan.validator_ref["id"],
        local_validator_sha256=plan.validator_ref["sha256"],
    )


def implementation_sha256_v1() -> str:
    rows = []
    for relative in _IMPLEMENTATION_PATHS:
        path = _REPO_ROOT / relative
        if not path.is_file():
            raise B3TemporalAuditProbeError(
                f"implementation file is absent: {relative.as_posix()}"
            )
        rows.append({"path": relative.as_posix(), "sha256": file_sha256(path)})
    return canonical_sha256(rows)


def build_clean_implementation_binding_v1() -> dict[str, str]:
    if _git_text("status", "--short", "--untracked-files=no"):
        raise B3TemporalAuditProbeError("B3 audit probe requires a clean tracked worktree")
    return {
        "shared_core_revision": "de7c74ba348bffe507aa86933f438b3ed4c5af29",
        "consumer_revision": _git_text("rev-parse", "HEAD"),
        "consumer_implementation_sha256": implementation_sha256_v1(),
    }


def _expected_profile() -> dict[str, Any]:
    return {
        "schema_version": "literary_modelapi_b3_temporal_auditor_probe_profile_v1",
        "profile_id": "literary_modelapi_b3_temporal_auditor_probe_v1",
        "profile_revision": "modelapi_gpt54_b3_temporal_auditor_json_object_v4",
        "role_id": ROLE_ID,
        "requested_model_id": "gpt-5.4",
        "accepted_observed_model_ids": ["gpt-5.4"],
        "capability_kind": "json_object",
        "schema_name": "literary_b3_temporal_review_response_v1",
        "schema_dialect": LOCAL_VALIDATION_SCHEMA_DIALECT,
        "generation": {
            "temperature": 1,
            "top_p": 1,
            "seed": 20260721,
            "reasoning_effort": "none",
            "verbosity": "low",
            "max_completion_tokens": 2048,
        },
        "limits": {
            "max_calls": 1,
            "max_prompt_utf8_bytes": 65536,
            "max_response_utf8_bytes": 65536,
            "max_prompt_tokens": 10000,
            "max_completion_tokens": 2048,
            "max_total_tokens": 12048,
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
    return subprocess.run(
        ["git", *args], cwd=_REPO_ROOT, check=True, capture_output=True, text=True
    ).stdout.strip()


__all__ = [
    "PROFILE_PATH",
    "RUNTIME_PROFILE_PATH",
    "B3TemporalAuditProbeError",
    "B3TemporalAuditProbePlanV1",
    "build_clean_implementation_binding_v1",
    "build_probe_plan_v1",
    "execute_probe_once_v1",
    "implementation_sha256_v1",
    "load_probe_profile_v1",
    "validator_ref_v1",
]
