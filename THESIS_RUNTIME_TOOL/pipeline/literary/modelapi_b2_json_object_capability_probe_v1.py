"""One-shot ModelAPI JSON-object qualification for Literary B2 Slim."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
import json
import subprocess
from typing import Any, Mapping

from pipeline.llm_backend import (
    SharedLlmCapabilityProbe,
    canonical_sha256,
    create_capability_probe_seal,
)
from pipeline.literary.b2_prompts_v3 import (
    b2_frame_response_schema_v2,
    b2_interaction_response_schema_v3,
)
from pipeline.literary.chapter_cycle_shared_runtime_v1 import (
    build_literary_code_ref_v1,
)
from pipeline.literary.model_ref_transport_v1 import (
    bind_model_ref_validator_v1,
    project_capability_probe_request_v1,
    resolve_capability_probe_response_v1,
)
from pipeline.literary.checkpoint import file_sha256
from pipeline.literary.openai_b2_json_object_capability_probe_v1 import (
    synthetic_b2_probe_request_v1,
    validate_literary_openai_b2_probe_payload_v1,
)
from pipeline.literary.shared_llm_adapter_v1 import render_literary_request_body
from pipeline.literary.shared_runtime_profile_v2 import (
    LOCAL_VALIDATION_SCHEMA_DIALECT,
    load_literary_shared_runtime_profile_v2,
)


PROFILE_SCHEMA_VERSION = "literary_modelapi_b2_json_object_probe_profile_v1"
PROFILE_ID = "literary_modelapi_b2_json_object_probe_v1"
PROFILE_REVISION = "modelapi_gpt54_b2_json_object_v3"
RUNTIME_PROFILE_ID = "literary_shared_llm_modelapi_b2_slim_v1"
RUNTIME_PROFILE_REVISION = "modelapi_gpt54_b2_prompt_validated_v1"
SOURCE_ALIAS = "modelapi_shared"
PROBE_NAMES = ("frame", "interaction")

_PIPELINE_ROOT = Path(__file__).resolve().parents[1]
_TOOL_ROOT = _PIPELINE_ROOT.parent
_REPO_ROOT = _TOOL_ROOT.parent
PROFILE_PATH = (
    _PIPELINE_ROOT / "configs" / "literary_modelapi_b2_json_object_probe_v1.json"
)
RUNTIME_PROFILE_PATH = (
    _PIPELINE_ROOT / "configs" / "literary_shared_llm_runtime_modelapi_b2_slim_v1.json"
)
_IMPLEMENTATION_PATHS = (
    Path("THESIS_RUNTIME_TOOL/pipeline/configs/literary_modelapi_b2_json_object_probe_v1.json"),
    Path("THESIS_RUNTIME_TOOL/pipeline/configs/literary_shared_llm_runtime_modelapi_b2_slim_v1.json"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/modelapi_b2_json_object_capability_probe_v1.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/model_ref_transport_v1.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/model_ref_v1.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/b2_context_v3.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/b2_contract_v3.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/b2_live_canary_v1.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/b2_prompts_v3.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/shared_llm_adapter_v1.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/shared_runtime_profile_v2.py"),
)


class LiteraryModelApiB2ProbeError(RuntimeError):
    pass


@dataclass(frozen=True)
class LiteraryModelApiB2ProbePlanV1:
    probe_name: str
    source: Mapping[str, Any]
    request: Mapping[str, Any]
    response_schema: Mapping[str, Any]
    validator_ref: Mapping[str, str]
    request_body: Mapping[str, Any]
    implementation_binding: Mapping[str, str]
    seal: Mapping[str, Any]


def load_modelapi_b2_probe_profile_v1(path: Path = PROFILE_PATH) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as exc:
        raise LiteraryModelApiB2ProbeError("cannot load ModelAPI B2 probe profile") from exc
    if payload != _expected_profile():
        raise LiteraryModelApiB2ProbeError("ModelAPI B2 probe profile differs")
    return deepcopy(payload)


def build_modelapi_b2_probe_plan_v1(
    *,
    probe_name: str,
    probe_run_id: str,
    credential_commitment_sha256: str,
    issued_at_utc: str,
    implementation_binding: Mapping[str, str] | None = None,
) -> LiteraryModelApiB2ProbePlanV1:
    role_id, schema_name, _canonical_schema, validator_ref = _probe_contract(probe_name)
    validator_ref = bind_model_ref_validator_v1(validator_ref)
    profile = load_modelapi_b2_probe_profile_v1()
    runtime = load_literary_shared_runtime_profile_v2(
        RUNTIME_PROFILE_PATH, expected_role_ids=set(_role_ids())
    )
    if (
        runtime.profile_id != RUNTIME_PROFILE_ID
        or runtime.profile_revision != RUNTIME_PROFILE_REVISION
    ):
        raise LiteraryModelApiB2ProbeError("ModelAPI B2 runtime profile differs")
    source_binding = dict(runtime.source_binding_for(role_id))
    if (
        source_binding.get("source_alias") != SOURCE_ALIAS
        or source_binding.get("authority_class") != "third_party"
        or source_binding.get("fallback_enabled") is not False
    ):
        raise LiteraryModelApiB2ProbeError("ModelAPI B2 source binding differs")
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
        "credential_commitment": _sha256(credential_commitment_sha256),
        "physical_quota_bucket_id": source_binding["physical_quota_bucket_id"],
        "enabled": True,
    }
    request = project_capability_probe_request_v1(
        synthetic_b2_probe_request_v1(probe_name)
    )
    schema = deepcopy(dict(request["response_schema"]))
    preset = runtime.role_presets[role_id]
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
        schema_name=schema_name,
        structured_output=runtime.shared_structured_output_for(role_id),
        output_envelope=runtime.output_envelope_for(role_id),
        base_url=source["base_url"],
    )
    if request_body.get("response_format") != {"type": "json_object"}:
        raise LiteraryModelApiB2ProbeError(
            "third-party B2 probe must use JSON-object mode without native schema"
        )
    intent = {
        "capability_id": f"modelapi_gpt54_{role_id.replace('.', '_')}_json_object_v1",
        "capability_revision": (
            f"schema_{canonical_sha256(schema)[:8]}_"
            f"validator_{validator_ref['sha256'][:8]}_v3"
        ),
        "requested_model_id": profile["requested_model_id"],
        "accepted_observed_model_ids": profile["accepted_observed_model_ids"],
        "capability_kind": profile["capability_kind"],
        "schema_name": schema_name,
        "schema_dialect": profile["schema_dialect"],
        "schema_sha256": canonical_sha256(schema),
        "local_validator_id": validator_ref["id"],
        "local_validator_sha256": validator_ref["sha256"],
    }
    binding = dict(implementation_binding or build_clean_implementation_binding_v1())
    seal = create_capability_probe_seal(
        source=source,
        consumer_workstream="literary",
        role_id=role_id,
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
    return LiteraryModelApiB2ProbePlanV1(
        probe_name=probe_name,
        source=source,
        request=deepcopy(request),
        response_schema=deepcopy(schema),
        validator_ref=validator_ref,
        request_body=request_body,
        implementation_binding=binding,
        seal=seal,
    )


def execute_modelapi_b2_probe_once_v1(
    *, probe: SharedLlmCapabilityProbe, plan: LiteraryModelApiB2ProbePlanV1
) -> dict[str, Any]:
    canonical_request = synthetic_b2_probe_request_v1(plan.probe_name)

    def validate(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        resolved = resolve_capability_probe_response_v1(
            projected_request=plan.request,
            response=payload,
        )
        return validate_modelapi_b2_probe_payload_v1(
            probe_name=plan.probe_name,
            request=canonical_request,
            payload=resolved,
        )

    return probe.execute_once(
        seal=plan.seal,
        request_body=plan.request_body,
        local_validator=validate,
        local_validator_id=plan.validator_ref["id"],
        local_validator_sha256=plan.validator_ref["sha256"],
    )


def validate_modelapi_b2_probe_payload_v1(
    *,
    probe_name: str,
    request: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    if probe_name == "frame":
        return validate_literary_openai_b2_probe_payload_v1(
            probe_name=probe_name,
            request=request,
            payload=payload,
        )
    if probe_name == "interaction":
        from pipeline.literary.b2_live_canary_v1 import (
            _normalize_interaction_response,
        )

        return _normalize_interaction_response(
            contract_version="v3",
            request=request,
            response=payload,
        )
    raise LiteraryModelApiB2ProbeError("unknown B2 probe name")


def build_clean_implementation_binding_v1() -> dict[str, str]:
    if _git_text("status", "--short", "--untracked-files=no"):
        raise LiteraryModelApiB2ProbeError("B2 probe requires a clean tracked worktree")
    return {
        "shared_core_revision": "de7c74ba348bffe507aa86933f438b3ed4c5af29",
        "consumer_revision": _git_text("rev-parse", "HEAD"),
        "consumer_implementation_sha256": implementation_sha256_v1(),
    }


def implementation_sha256_v1() -> str:
    rows = []
    for relative in _IMPLEMENTATION_PATHS:
        path = _REPO_ROOT / relative
        if not path.is_file():
            raise LiteraryModelApiB2ProbeError(
                f"B2 implementation file is absent: {relative.as_posix()}"
            )
        rows.append({"path": relative.as_posix(), "sha256": file_sha256(path)})
    return canonical_sha256(rows)


def _probe_contract(
    probe_name: str,
) -> tuple[str, str, dict[str, Any], dict[str, str]]:
    if probe_name == "frame":
        role_id = "literary.b2.frame"
        schema_name = "literary_b2_frame_v2"
        schema = b2_frame_response_schema_v2()
        from pipeline.literary.b2_contract_v3 import normalize_b2_frame_response_v2
        from pipeline.literary.structured_output_policy_v1 import validate_structured_payload

        callables = (validate_structured_payload, normalize_b2_frame_response_v2)
        revision = "v2"
    elif probe_name == "interaction":
        role_id = "literary.b2.interaction"
        schema_name = "literary_b2_interaction_v3"
        schema = b2_interaction_response_schema_v3()
        from pipeline.literary.b2_live_canary_v1 import _normalize_interaction_response

        callables = (_normalize_interaction_response,)
        revision = "v3"
    else:
        raise LiteraryModelApiB2ProbeError("unknown B2 probe name")
    validator_ref = build_literary_code_ref_v1(
        identifier=f"literary.b2.{probe_name}.validator",
        revision=revision,
        callables=callables,
    )
    return role_id, schema_name, schema, validator_ref


def _role_ids() -> tuple[str, str]:
    return ("literary.b2.frame", "literary.b2.interaction")


def _expected_profile() -> dict[str, Any]:
    return {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "profile_id": PROFILE_ID,
        "profile_revision": PROFILE_REVISION,
        "requested_model_id": "gpt-5.4",
        "accepted_observed_model_ids": ["gpt-5.4"],
        "capability_kind": "json_object",
        "schema_dialect": LOCAL_VALIDATION_SCHEMA_DIALECT,
        "generation": {
            "temperature": 1,
            "top_p": 1,
            "seed": 20260721,
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


def _sha256(value: Any) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        char not in "0123456789abcdef" for char in value
    ):
        raise LiteraryModelApiB2ProbeError("credential commitment is malformed")
    return value


def _git_text(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=_REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


__all__ = [
    "PROFILE_PATH",
    "PROBE_NAMES",
    "RUNTIME_PROFILE_PATH",
    "LiteraryModelApiB2ProbeError",
    "LiteraryModelApiB2ProbePlanV1",
    "build_clean_implementation_binding_v1",
    "build_modelapi_b2_probe_plan_v1",
    "execute_modelapi_b2_probe_once_v1",
    "validate_modelapi_b2_probe_payload_v1",
    "load_modelapi_b2_probe_profile_v1",
]
