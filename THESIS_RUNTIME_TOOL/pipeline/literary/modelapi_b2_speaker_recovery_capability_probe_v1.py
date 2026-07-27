"""One-shot ModelAPI JSON-object probe for B2 Slim speaker recovery."""

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
from pipeline.literary.b2_recovery_batch_v1 import (
    validate_registry_recovery_batch_response_v1,
)
from pipeline.literary.b2_recovery_v1 import (
    RECOVERY_INDEX_SCHEMA_VERSION,
    RECOVERY_VALIDATOR_VERSION,
)
from pipeline.literary.b2_slim_speaker_recovery_v1 import (
    apply_b2_slim_speaker_recovery_decision_v1,
    make_b2_slim_speaker_recovery_validator_v1,
    render_b2_slim_speaker_recovery_request_v1,
)
from pipeline.literary.chapter_cycle_shared_runtime_v1 import build_literary_code_ref_v1
from pipeline.literary.model_ref_transport_v1 import (
    bind_model_ref_validator_v1,
    project_capability_probe_request_v1,
    resolve_capability_probe_response_v1,
)
from pipeline.literary.checkpoint import canonical_hash, file_sha256
from pipeline.literary.shared_llm_adapter_v1 import render_literary_request_body
from pipeline.literary.shared_runtime_profile_v2 import (
    LOCAL_VALIDATION_SCHEMA_DIALECT,
    load_literary_shared_runtime_profile_v2,
)
from pipeline.literary.structured_output_policy_v1 import validate_structured_payload


ROLE_ID = "literary.b2.registry_recovery"
SCHEMA_NAME = "literary_b2_registry_recovery_batch_v1"
PROFILE_SCHEMA_VERSION = "literary_modelapi_b2_speaker_recovery_probe_profile_v1"
PROFILE_ID = "literary_modelapi_b2_speaker_recovery_probe_v1"
PROFILE_REVISION = "modelapi_gpt54_b2_speaker_recovery_json_object_v5"
RUNTIME_PROFILE_ID = "literary_shared_llm_modelapi_b2_speaker_recovery_v1"
RUNTIME_PROFILE_REVISION = (
    "modelapi_gpt54_b2_speaker_recovery_prompt_validated_v4"
)

_PIPELINE_ROOT = Path(__file__).resolve().parents[1]
_TOOL_ROOT = _PIPELINE_ROOT.parent
_REPO_ROOT = _TOOL_ROOT.parent
PROFILE_PATH = (
    _PIPELINE_ROOT
    / "configs"
    / "literary_modelapi_b2_speaker_recovery_json_object_probe_v1.json"
)
RUNTIME_PROFILE_PATH = (
    _PIPELINE_ROOT
    / "configs"
    / "literary_shared_llm_runtime_modelapi_b2_speaker_recovery_v1.json"
)
_IMPLEMENTATION_PATHS = (
    Path("THESIS_RUNTIME_TOOL/pipeline/configs/literary_modelapi_b2_speaker_recovery_json_object_probe_v1.json"),
    Path("THESIS_RUNTIME_TOOL/pipeline/configs/literary_shared_llm_runtime_modelapi_b2_speaker_recovery_v1.json"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/modelapi_b2_speaker_recovery_capability_probe_v1.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/model_ref_transport_v1.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/model_ref_v1.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/b2_slim_speaker_recovery_v1.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/b2_recovery_batch_v1.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/b2_recovery_prompts_v1.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/b2_recovery_v1.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/shared_llm_adapter_v1.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/shared_runtime_profile_v2.py"),
)


class LiteraryModelApiB2SpeakerProbeError(RuntimeError):
    pass


@dataclass(frozen=True)
class LiteraryModelApiB2SpeakerProbePlanV1:
    source: Mapping[str, Any]
    index: Mapping[str, Any]
    request: Mapping[str, Any]
    response_schema: Mapping[str, Any]
    validator_ref: Mapping[str, str]
    request_body: Mapping[str, Any]
    implementation_binding: Mapping[str, str]
    seal: Mapping[str, Any]


def load_probe_profile_v1(path: Path = PROFILE_PATH) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as exc:
        raise LiteraryModelApiB2SpeakerProbeError("cannot load probe profile") from exc
    if payload != _expected_profile():
        raise LiteraryModelApiB2SpeakerProbeError("speaker probe profile differs")
    return deepcopy(payload)


def validator_ref_v1() -> dict[str, str]:
    return build_literary_code_ref_v1(
        identifier="literary.b2.registry_recovery.validator",
        revision="speaker_slim_v2",
        callables=(
            validate_structured_payload,
            validate_registry_recovery_batch_response_v1,
            apply_b2_slim_speaker_recovery_decision_v1,
        ),
    )


def build_probe_plan_v1(
    *,
    probe_run_id: str,
    credential_commitment_sha256: str,
    issued_at_utc: str,
    implementation_binding: Mapping[str, str] | None = None,
) -> LiteraryModelApiB2SpeakerProbePlanV1:
    profile = load_probe_profile_v1()
    runtime = load_literary_shared_runtime_profile_v2(
        RUNTIME_PROFILE_PATH, expected_role_ids={ROLE_ID}
    )
    if (
        runtime.profile_id != RUNTIME_PROFILE_ID
        or runtime.profile_revision != RUNTIME_PROFILE_REVISION
    ):
        raise LiteraryModelApiB2SpeakerProbeError("runtime profile differs")
    source_binding = dict(runtime.source_binding_for(ROLE_ID))
    if (
        source_binding.get("authority_class") != "third_party"
        or source_binding.get("fallback_enabled") is not False
        or source_binding.get("base_url") != "https://modelapi.vn/v1"
    ):
        raise LiteraryModelApiB2SpeakerProbeError("source binding differs")
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
    index = synthetic_probe_index_v1()
    rendered = render_b2_slim_speaker_recovery_request_v1(index)
    if rendered is None:
        raise LiteraryModelApiB2SpeakerProbeError("synthetic speaker request is empty")
    request = project_capability_probe_request_v1(
        {
            "request_fingerprint": rendered.request_fingerprint,
            "messages": [dict(row) for row in rendered.messages],
            "response_schema": deepcopy(rendered.response_schema),
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
        schema_name=SCHEMA_NAME,
        structured_output=runtime.shared_structured_output_for(ROLE_ID),
        output_envelope=runtime.output_envelope_for(ROLE_ID),
        base_url=source["base_url"],
    )
    if request_body.get("response_format") != {"type": "json_object"}:
        raise LiteraryModelApiB2SpeakerProbeError(
            "third-party speaker probe must use JSON-object mode"
        )
    binding = dict(implementation_binding or build_clean_implementation_binding_v1())
    intent = {
        "capability_id": "modelapi_gpt54_literary_b2_registry_recovery_json_object_v3",
        "capability_revision": (
            f"schema_{canonical_hash(schema)[:8]}_"
            f"validator_{validator_ref['sha256'][:8]}_v5"
        ),
        "requested_model_id": profile["requested_model_id"],
        "accepted_observed_model_ids": profile["accepted_observed_model_ids"],
        "capability_kind": profile["capability_kind"],
        "schema_name": SCHEMA_NAME,
        "schema_dialect": profile["schema_dialect"],
        "schema_sha256": canonical_sha256(schema),
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
    return LiteraryModelApiB2SpeakerProbePlanV1(
        source=source,
        index=index,
        request=request,
        response_schema=schema,
        validator_ref=validator_ref,
        request_body=request_body,
        implementation_binding=binding,
        seal=seal,
    )


def execute_probe_once_v1(
    *, probe: SharedLlmCapabilityProbe, plan: LiteraryModelApiB2SpeakerProbePlanV1
) -> dict[str, Any]:
    rendered = render_b2_slim_speaker_recovery_request_v1(plan.index)
    if rendered is None:
        raise LiteraryModelApiB2SpeakerProbeError("probe request disappeared")
    semantic_validator = make_b2_slim_speaker_recovery_validator_v1(
        index=plan.index, request=rendered
    )

    def validate(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        validate_structured_payload(payload, canonical_schema=plan.response_schema)
        resolved = resolve_capability_probe_response_v1(
            projected_request=plan.request,
            response=payload,
        )
        return semantic_validator(resolved)

    return probe.execute_once(
        seal=plan.seal,
        request_body=plan.request_body,
        local_validator=validate,
        local_validator_id=plan.validator_ref["id"],
        local_validator_sha256=plan.validator_ref["sha256"],
    )


def synthetic_probe_index_v1() -> dict[str, Any]:
    card = {
        "candidate_card_id": "probe_ent_rowan",
        "canonical_surface": "Mr. Rowan",
        "authority_scope": "chapter_confirmed_prefix",
    }
    ticket_body = {
        "chapter_id": "probe_chapter",
        "source_row_kind": "speaker_turn",
        "source_row_id": "probe_turn",
        "endpoint_role": "speaker",
        "observed_surface": None,
        "reference_form": "unknown",
        "resolution_status": "unresolved",
        "candidate_card_ids": [card["candidate_card_id"]],
        "issue_kind": "contextual_speaker_attribution",
        "source_anchor": "Ready?",
        "source_block_ids": ["probe_block"],
        "source_window_id": "b2frm2_probe_frame",
        "source_frame_segment_id": "b2frm2_probe_frame",
        "source_review_signals": [
            {
                "review_id": "probe_review",
                "origin": "model",
                "candidate_card_ids": [card["candidate_card_id"]],
                "reason": "The local speaker requires review.",
            }
        ],
        "evidence_hash": canonical_hash({"probe": "speaker"}),
        "lifecycle_state": "open",
        "hearing_count": 0,
        "authority_effect": "none",
    }
    ticket = {
        "ticket_id": f"b2slimgap1_{canonical_hash(ticket_body)[:20]}",
        **ticket_body,
    }
    component_body = {
        "component_kind": "registry_gap",
        "chapter_id": "probe_chapter",
        "ordinal": 1,
        "ticket_ids": [ticket["ticket_id"]],
        "source_block_ids": ["probe_block"],
        "candidate_card_ids": [card["candidate_card_id"]],
        "overflow": False,
        "overflow_reasons": [],
        "authority_effect": "none",
    }
    component = {
        "component_id": f"b2slimgapcomp1_{canonical_hash(component_body)[:20]}",
        **component_body,
    }
    body = {
        "schema_version": RECOVERY_INDEX_SCHEMA_VERSION,
        "validator_version": RECOVERY_VALIDATOR_VERSION,
        "chapter_id": "probe_chapter",
        "source_b2_artifact_hash": "a" * 64,
        "source_request_fingerprints": ["b" * 64],
        "source_blocks": [
            {
                "block_id": "probe_block",
                "block_type": "dialogue",
                "text": "Mr. Rowan asked, \"Ready?\"",
            }
        ],
        "candidate_cards": [card],
        "registry_gap_tickets": [ticket],
        "event_review_cases": [],
        "registry_components": [component],
        "event_components": [],
        "counts": {
            "registry_gap_tickets": 1,
            "event_review_cases": 0,
            "registry_components": 1,
            "event_components": 0,
            "overflow_components": 0,
        },
        "semantic_halt_required": False,
        "book_global_identity_mutation_performed": False,
        "translation_performed": False,
        "production_publish_performed": False,
        "slim_speaker_policy": {
            "trigger": "explicit_pending_speaker_attribution_only",
            "accepted_turn_reinspection": False,
            "unticketed_turn_mutation": False,
        },
    }
    return {**body, "recovery_index_hash": canonical_hash(body)}


def build_clean_implementation_binding_v1() -> dict[str, str]:
    if _git_text("status", "--short", "--untracked-files=no"):
        raise LiteraryModelApiB2SpeakerProbeError("probe requires a clean worktree")
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
            raise LiteraryModelApiB2SpeakerProbeError(
                f"implementation file is absent: {relative.as_posix()}"
            )
        rows.append({"path": relative.as_posix(), "sha256": file_sha256(path)})
    return canonical_sha256(rows)


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
        raise LiteraryModelApiB2SpeakerProbeError("credential commitment is malformed")
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
    "ROLE_ID",
    "RUNTIME_PROFILE_PATH",
    "LiteraryModelApiB2SpeakerProbeError",
    "LiteraryModelApiB2SpeakerProbePlanV1",
    "build_clean_implementation_binding_v1",
    "build_probe_plan_v1",
    "execute_probe_once_v1",
    "implementation_sha256_v1",
    "load_probe_profile_v1",
    "synthetic_probe_index_v1",
    "validator_ref_v1",
]
