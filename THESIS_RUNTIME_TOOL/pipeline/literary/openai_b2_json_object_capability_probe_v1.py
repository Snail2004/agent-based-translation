"""Official OpenAI JSON-object capability probes for Literary B2 Slim.

Each B2 role receives an independent one-call capability-only seal. JSON
object mode is only a syntax aid; the canonical B2 schema and local semantic
validator remain authoritative.
"""

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
from pipeline.literary.b2_context_v1 import (
    build_b2_windows_v1,
    load_b2_phase_a_profile,
)
from pipeline.literary.b2_context_v3 import (
    render_b2_frame_request_v2,
    render_b2_interaction_request_v3,
)
from pipeline.literary.b2_contract_v3 import normalize_b2_frame_response_v2
from pipeline.literary.b2_live_canary_v1 import _normalize_interaction_response
from pipeline.literary.b2_prompts_v3 import (
    b2_frame_response_schema_v2,
    b2_interaction_response_schema_v3,
)
from pipeline.literary.chapter_cycle_shared_runtime_v1 import (
    build_literary_code_ref_v1,
)
from pipeline.literary.shared_llm_adapter_v1 import render_literary_request_body
from pipeline.literary.shared_runtime_profile_v2 import (
    LOCAL_VALIDATION_SCHEMA_DIALECT,
    PROMPT_JSON_INSTRUCTION_ID,
    PROMPT_JSON_INSTRUCTION_REVISION,
    LiterarySharedRuntimeProfileV2,
    load_literary_shared_runtime_profile_v2,
)
from pipeline.literary.structured_output_policy_v1 import (
    validate_structured_payload,
)


PROFILE_SCHEMA_VERSION = (
    "literary_openai_b2_json_object_capability_probe_profile_v1"
)
PROBE_PROFILE_ID = "literary_openai_official_b2_json_object_probe_v1"
PROBE_PROFILE_REVISION = "openai_row2_gpt54_b2_json_object_v6"
SHARED_CORE_REVISION = "dece3488f591b726bd4eb0883f42829c7a58410d"
RUNTIME_PROFILE_ID = "literary_shared_llm_openai_official_b2_slim_v3"
RUNTIME_PROFILE_REVISION = "openai_row2_gpt54_b2_prompt_validated_v2"
RUNTIME_PROFILE_SHA256 = (
    "83f1d7913fd1de6a0eeb4f55a425ce3bfa2b613c79853dc6fb05092e0fe145ae"
)
RUNTIME_SOURCE_ALIAS = "openai_official_row2"
ACCEPTED_OBSERVED_MODEL_IDS = ("gpt-5.4", "gpt-5.4-2026-03-05")
PROBE_NAMES = ("frame", "interaction")

_PIPELINE_ROOT = Path(__file__).resolve().parents[1]
_TOOL_ROOT = _PIPELINE_ROOT.parent
_REPO_ROOT = _TOOL_ROOT.parent
DEFAULT_PROFILE_PATH = (
    _PIPELINE_ROOT
    / "configs"
    / "literary_openai_b2_json_object_capability_probe_v1.json"
)
RUNTIME_PROFILE_PATH = (
    _PIPELINE_ROOT
    / "configs"
    / "literary_shared_llm_runtime_openai_b2_slim_v3.json"
)
B2_PHASE_PROFILE_PATH = (
    _PIPELINE_ROOT
    / "configs"
    / "literary_b2_slim_phase_a_profile_v1.json"
)
_IMPLEMENTATION_PATHS = (
    Path(
        "THESIS_RUNTIME_TOOL/pipeline/configs/"
        "literary_openai_b2_json_object_capability_probe_v1.json"
    ),
    Path(
        "THESIS_RUNTIME_TOOL/pipeline/configs/"
        "literary_shared_llm_runtime_openai_b2_slim_v3.json"
    ),
    Path(
        "THESIS_RUNTIME_TOOL/pipeline/configs/"
        "literary_b2_slim_phase_a_profile_v1.json"
    ),
    Path(
        "THESIS_RUNTIME_TOOL/pipeline/literary/"
        "openai_b2_json_object_capability_probe_v1.py"
    ),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/b2_context_v3.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/b2_contract_v3.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/b2_live_canary_v1.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/b2_prompts_v3.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/shared_llm_adapter_v1.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/shared_runtime_profile_v2.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/structured_output_policy_v1.py"),
)


class LiteraryOpenAiB2CapabilityProbeError(RuntimeError):
    pass


@dataclass(frozen=True)
class _ProbeSpec:
    probe_name: str
    role_id: str
    capability_id: str
    capability_revision: str
    schema_name: str
    schema_sha256: str
    local_validator_id: str
    local_validator_revision: str
    local_validator_sha256: str


@dataclass(frozen=True)
class LiteraryOpenAiB2ProbePlanV1:
    probe_name: str
    profile: Mapping[str, Any]
    runtime_profile_sha256: str
    source: Mapping[str, Any]
    request: Mapping[str, Any]
    response_schema: Mapping[str, Any]
    request_body: Mapping[str, Any]
    implementation_binding: Mapping[str, str]
    seal: Mapping[str, Any]


_SPECS = {
    "frame": _ProbeSpec(
        probe_name="frame",
        role_id="literary.b2.frame",
        capability_id="openai_row2_gpt54_literary_b2_frame_json_object_v1",
        capability_revision=(
            "frame_schema_6c753a5e_validator_e623327e_openai_row2_v5"
        ),
        schema_name="literary_b2_frame_v2",
        schema_sha256=(
            "6c753a5ec6928394053f2b762dd74324f7ebea9878762f2918607abfa841233b"
        ),
        local_validator_id="literary.b2.frame.validator",
        local_validator_revision="v2",
        local_validator_sha256=(
            "e623327ebb7b0d42b47600e60c15c67cf26fe9107065a2aef1196b3eb866ca85"
        ),
    ),
    "interaction": _ProbeSpec(
        probe_name="interaction",
        role_id="literary.b2.interaction",
        capability_id=(
            "openai_row2_gpt54_literary_b2_interaction_json_object_v1"
        ),
        # Rebaselined when addressee absence states were added, and again when
        # social register was split from delivery tone. The pin refuses schema
        # drift; moving it deliberately makes the old evidence stop qualifying.
        capability_revision=(
            "interaction_schema_5c43ad64_validator_b65749f3_openai_row2_v7"
        ),
        schema_name="literary_b2_interaction_v3",
        schema_sha256=(
            "5c43ad64740596df27d08a3e2e1e693a76c675a33f659fc376ce03758c3a6434"
        ),
        local_validator_id="literary.b2.interaction.validator",
        local_validator_revision="v4",
        local_validator_sha256=(
            "b65749f3272a092aa2d209ba6065f8d5df7cbdcf94161b3d2657034de46c09d5"
        ),
    ),
}


def load_literary_openai_b2_probe_profile_v1(
    path: Path = DEFAULT_PROFILE_PATH,
) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as exc:
        raise LiteraryOpenAiB2CapabilityProbeError(
            "cannot load official OpenAI B2 probe profile"
        ) from exc
    if payload != _expected_profile():
        raise LiteraryOpenAiB2CapabilityProbeError(
            "official OpenAI B2 probe profile differs from the closed contract"
        )
    return deepcopy(payload)


def synthetic_b2_probe_request_v1(probe_name: str) -> dict[str, Any]:
    _spec(probe_name)
    chapter = _synthetic_chapter()
    prefix = _synthetic_prefix()
    profile = load_b2_phase_a_profile(B2_PHASE_PROFILE_PATH)
    if probe_name == "frame":
        return render_b2_frame_request_v2(
            chapter=chapter,
            prefix_bundle=prefix,
            profile=profile,
        )
    window = build_b2_windows_v1(chapter, profile=profile)[0]
    return render_b2_interaction_request_v3(
        window=window,
        prefix_bundle=prefix,
        profile=profile,
        frame_context={"frame_segments": []},
    )


def empty_b2_probe_response_v1(
    probe_name: str, *, request: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    request = dict(request or synthetic_b2_probe_request_v1(probe_name))
    if probe_name == "frame":
        return {
            "schema_version": "literary_b2_frame_response_v2",
            "chapter_id": request["chapter_id"],
            "frame_starts": [
                {
                    "start_block_id": "literary_b2_probe_b001",
                    "narrator_surface": None,
                    "narrator_status": "external_or_authorial",
                    "candidate_card_ids": [],
                    "narrative_mode": "external_narration",
                    "boundary_cue_anchor": None,
                }
            ],
            "review_requests": [],
        }
    if probe_name == "interaction":
        return {
            "schema_version": "literary_b2_interaction_response_v3",
            "chapter_id": request["chapter_id"],
            "window_id": request["window_id"],
            "speaker_turns": [],
            "salient_events": [],
            "review_requests": [],
        }
    raise LiteraryOpenAiB2CapabilityProbeError("unknown B2 probe name")


def validate_literary_openai_b2_probe_payload_v1(
    *,
    probe_name: str,
    request: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    spec = _spec(probe_name)
    schema = _verified_schema(spec)
    validate_structured_payload(payload, canonical_schema=schema)
    if probe_name == "frame":
        return normalize_b2_frame_response_v2(request=request, response=payload)
    return _normalize_interaction_response(
        contract_version="v3",
        request=request,
        response=payload,
    )


def build_clean_implementation_binding_v1(
    repo_root: Path = _REPO_ROOT,
) -> dict[str, str]:
    root = Path(repo_root).resolve()
    dirty = _git_text(root, "status", "--porcelain", "--untracked-files=no")
    if dirty:
        raise LiteraryOpenAiB2CapabilityProbeError(
            "live B2 probe seal requires a clean tracked Literary worktree"
        )
    revision = _git_text(root, "rev-parse", "HEAD")
    if not _is_git_oid(revision):
        raise LiteraryOpenAiB2CapabilityProbeError(
            "consumer Git revision is invalid"
        )
    return {
        "shared_core_revision": SHARED_CORE_REVISION,
        "consumer_revision": revision,
        "consumer_implementation_sha256": implementation_sha256_v1(root),
    }


def implementation_sha256_v1(repo_root: Path = _REPO_ROOT) -> str:
    root = Path(repo_root).resolve()
    rows: list[dict[str, str]] = []
    for relative in _IMPLEMENTATION_PATHS:
        path = root / relative
        if not path.is_file():
            raise LiteraryOpenAiB2CapabilityProbeError(
                f"B2 probe implementation file is absent: {relative.as_posix()}"
            )
        rows.append(
            {
                "path": relative.as_posix(),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    body = {
        "schema_version": "literary_openai_b2_probe_implementation_v1",
        "shared_core_revision": SHARED_CORE_REVISION,
        "runtime_profile_sha256": RUNTIME_PROFILE_SHA256,
        "role_bindings": [
            {
                "role_id": spec.role_id,
                "schema_sha256": spec.schema_sha256,
                "local_validator_sha256": spec.local_validator_sha256,
            }
            for spec in _SPECS.values()
        ],
        "files": rows,
    }
    return canonical_sha256(body)


def build_literary_openai_b2_probe_plan_v1(
    *,
    probe_name: str,
    probe_run_id: str,
    credential_commitment_sha256: str,
    issued_at_utc: str,
    repo_root: Path = _REPO_ROOT,
) -> LiteraryOpenAiB2ProbePlanV1:
    spec = _spec(probe_name)
    profile = load_literary_openai_b2_probe_profile_v1()
    runtime_profile = load_literary_shared_runtime_profile_v2(
        RUNTIME_PROFILE_PATH
    )
    implementation_binding = build_clean_implementation_binding_v1(repo_root)
    source = _materialize_source(
        credential_commitment_sha256=credential_commitment_sha256,
        runtime_profile=runtime_profile,
        spec=spec,
    )
    request = synthetic_b2_probe_request_v1(probe_name)
    response_schema = _verified_schema(spec)
    _verified_validator_ref(spec)
    preset = runtime_profile.role_presets[spec.role_id]
    probe_generation = deepcopy(dict(preset.generation))
    probe_generation.update(
        {
            "temperature": profile["generation"]["temperature"],
            "top_p": profile["generation"]["top_p"],
            "seed": profile["generation"]["seed"],
            "reasoning_effort": profile["generation"]["reasoning_effort"],
            "verbosity": profile["generation"]["verbosity"],
            "max_output_tokens": profile["generation"][
                "max_completion_tokens"
            ],
        }
    )
    probe_preset = replace(preset, generation=probe_generation)
    request_body = render_literary_request_body(
        preset=probe_preset,
        protocol=source["protocol"],
        capability={"capability_kind": "json_object"},
        messages=request["messages"],
        response_schema=response_schema,
        instruction_schema=response_schema,
        schema_name=spec.schema_name,
        structured_output=runtime_profile.shared_structured_output_for(
            spec.role_id
        ),
        output_envelope=runtime_profile.output_envelope_for(spec.role_id),
        base_url=source["base_url"],
    )
    if request_body.get("response_format") != {"type": "json_object"}:
        raise LiteraryOpenAiB2CapabilityProbeError(
            "B2 probe must use OpenAI JSON-object mode without native schema"
        )
    intent = _intent(spec)
    limits = _shared_probe_limits(profile)
    seal = create_capability_probe_seal(
        source=source,
        consumer_workstream="literary",
        role_id=spec.role_id,
        probe_run_id=probe_run_id,
        probe_profile_id=profile["profile_id"],
        probe_profile_revision=profile["profile_revision"],
        implementation_binding=implementation_binding,
        capability_intent=intent,
        response_schema=response_schema,
        request_body=request_body,
        limits=limits,
        issued_at_utc=issued_at_utc,
    )
    return LiteraryOpenAiB2ProbePlanV1(
        probe_name=probe_name,
        profile=deepcopy(profile),
        runtime_profile_sha256=runtime_profile.profile_sha256,
        source=deepcopy(source),
        request=deepcopy(request),
        response_schema=deepcopy(response_schema),
        request_body=deepcopy(request_body),
        implementation_binding=dict(implementation_binding),
        seal=deepcopy(seal),
    )


def execute_literary_openai_b2_probe_once_v1(
    *,
    probe: SharedLlmCapabilityProbe,
    plan: LiteraryOpenAiB2ProbePlanV1,
    cost_fact: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    spec = _spec(plan.probe_name)

    def validate(payload: Mapping[str, Any]) -> dict[str, Any]:
        return validate_literary_openai_b2_probe_payload_v1(
            probe_name=plan.probe_name,
            request=plan.request,
            payload=payload,
        )

    result = probe.execute_once(
        seal=plan.seal,
        request_body=plan.request_body,
        local_validator=validate,
        local_validator_id=spec.local_validator_id,
        local_validator_sha256=spec.local_validator_sha256,
        cost_fact=cost_fact,
    )
    if set(result) != {
        "status",
        "provider_called",
        "probe_seal_sha256",
        "receipt",
        "capability_evidence",
    }:
        raise LiteraryOpenAiB2CapabilityProbeError(
            "B2 probe exposed an unexpected payload"
        )
    return result


def _expected_profile() -> dict[str, Any]:
    return {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "profile_id": PROBE_PROFILE_ID,
        "profile_revision": PROBE_PROFILE_REVISION,
        "shared_core_revision": SHARED_CORE_REVISION,
        "runtime_profile": {
            "profile_id": RUNTIME_PROFILE_ID,
            "profile_revision": RUNTIME_PROFILE_REVISION,
            "profile_sha256": RUNTIME_PROFILE_SHA256,
            "source_alias": RUNTIME_SOURCE_ALIAS,
        },
        "capability_intents": [
            {
                "probe_name": spec.probe_name,
                "capability_id": spec.capability_id,
                "capability_revision": spec.capability_revision,
                "role_id": spec.role_id,
                "requested_model_id": "gpt-5.4",
                "accepted_observed_model_ids": list(
                    ACCEPTED_OBSERVED_MODEL_IDS
                ),
                "capability_kind": "json_object",
                "schema_name": spec.schema_name,
                "schema_dialect": LOCAL_VALIDATION_SCHEMA_DIALECT,
                "schema_sha256": spec.schema_sha256,
                "local_validator_id": spec.local_validator_id,
                "local_validator_revision": spec.local_validator_revision,
                "local_validator_sha256": spec.local_validator_sha256,
            }
            for spec in _SPECS.values()
        ],
        "generation": {
            "temperature": 1.0,
            "top_p": 1.0,
            "seed": 20260720,
            "reasoning_effort": "none",
            "verbosity": "low",
            "max_completion_tokens": 1024,
        },
        "limits": {
            "max_calls_per_probe": 1,
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


def _materialize_source(
    *,
    credential_commitment_sha256: str,
    runtime_profile: LiterarySharedRuntimeProfileV2,
    spec: _ProbeSpec,
) -> dict[str, Any]:
    if (
        runtime_profile.profile_id != RUNTIME_PROFILE_ID
        or runtime_profile.profile_revision != RUNTIME_PROFILE_REVISION
        or runtime_profile.profile_sha256 != RUNTIME_PROFILE_SHA256
    ):
        raise LiteraryOpenAiB2CapabilityProbeError(
            "B2 runtime profile bytes differ from the probe binding"
        )
    role_binding = runtime_profile.role_bindings[spec.role_id]
    if role_binding.source_alias != RUNTIME_SOURCE_ALIAS:
        raise LiteraryOpenAiB2CapabilityProbeError(
            "B2 role source alias differs from the probe binding"
        )
    source = dict(runtime_profile.source_binding_for(spec.role_id))
    if source["authority_class"] != "direct_official_openai":
        raise LiteraryOpenAiB2CapabilityProbeError(
            "B2 JSON-object probe source is not direct official OpenAI"
        )
    if source["fallback_enabled"] is not False:
        raise LiteraryOpenAiB2CapabilityProbeError("B2 probe source enables fallback")
    if runtime_profile.shared_structured_output_for(spec.role_id) != {
        "mode": "prompt_validated",
        "schema_dialect": LOCAL_VALIDATION_SCHEMA_DIALECT,
    }:
        raise LiteraryOpenAiB2CapabilityProbeError(
            "B2 role does not select Shared prompt_validated mode"
        )
    if runtime_profile.output_envelope_for(spec.role_id) != {
        "mode": "json_object",
        "schema_dialect": LOCAL_VALIDATION_SCHEMA_DIALECT,
        "instruction_id": PROMPT_JSON_INSTRUCTION_ID,
        "instruction_revision": PROMPT_JSON_INSTRUCTION_REVISION,
    }:
        raise LiteraryOpenAiB2CapabilityProbeError(
            "B2 role JSON-object instruction binding differs"
        )
    if runtime_profile.role_presets[spec.role_id].requested_model_id != "gpt-5.4":
        raise LiteraryOpenAiB2CapabilityProbeError("B2 probe model differs")
    if not _is_sha256(credential_commitment_sha256):
        raise LiteraryOpenAiB2CapabilityProbeError(
            "credential commitment must be SHA-256"
        )
    return {
        "schema_version": "api_source_v1",
        "source_id": source["source_id"],
        "source_revision": source["source_revision"],
        "source_class": source["source_class"],
        "adapter_id": source["adapter_id"],
        "protocol": source["protocol"],
        "route_id": source["route_id"],
        "endpoint_class": source["endpoint_class"],
        "base_url": source["base_url"],
        "credential_ref": source["credential_ref"],
        "credential_commitment": credential_commitment_sha256,
        "physical_quota_bucket_id": source["physical_quota_bucket_id"],
        "enabled": True,
    }


def _verified_schema(spec: _ProbeSpec) -> dict[str, Any]:
    schema = (
        b2_frame_response_schema_v2()
        if spec.probe_name == "frame"
        else b2_interaction_response_schema_v3()
    )
    if canonical_sha256(schema) != spec.schema_sha256:
        raise LiteraryOpenAiB2CapabilityProbeError(
            f"B2 {spec.probe_name} canonical schema hash differs"
        )
    return schema


def _verified_validator_ref(spec: _ProbeSpec) -> dict[str, str]:
    callables = (
        (validate_structured_payload, normalize_b2_frame_response_v2)
        if spec.probe_name == "frame"
        else (validate_structured_payload, _normalize_interaction_response)
    )
    ref = build_literary_code_ref_v1(
        identifier=spec.local_validator_id,
        revision=spec.local_validator_revision,
        callables=callables,
    )
    if ref["sha256"] != spec.local_validator_sha256:
        raise LiteraryOpenAiB2CapabilityProbeError(
            f"B2 {spec.probe_name} local validator hash differs"
        )
    return ref


def _intent(spec: _ProbeSpec) -> dict[str, Any]:
    return {
        "capability_id": spec.capability_id,
        "capability_revision": spec.capability_revision,
        "requested_model_id": "gpt-5.4",
        "accepted_observed_model_ids": list(ACCEPTED_OBSERVED_MODEL_IDS),
        "capability_kind": "json_object",
        "schema_name": spec.schema_name,
        "schema_dialect": LOCAL_VALIDATION_SCHEMA_DIALECT,
        "schema_sha256": spec.schema_sha256,
        "local_validator_id": spec.local_validator_id,
        "local_validator_sha256": spec.local_validator_sha256,
    }


def _shared_probe_limits(profile: Mapping[str, Any]) -> dict[str, Any]:
    limits = dict(profile["limits"])
    if limits.pop("max_calls_per_probe") != 1:
        raise LiteraryOpenAiB2CapabilityProbeError(
            "each B2 capability probe must allow exactly one call"
        )
    limits["max_calls"] = 1
    return limits


def _synthetic_chapter() -> dict[str, Any]:
    return {
        "chapter_id": "literary_b2_probe_ch01",
        "blocks": [
            {
                "block_id": "literary_b2_probe_b001",
                "order_index": 1,
                "block_type": "paragraph",
                "clean_text": "A narrator records a brief uneventful moment.",
            }
        ],
    }


def _synthetic_prefix() -> dict[str, Any]:
    body = {
        "b0_context_cards": [],
        "candidate_only_context_cards": [],
        "prefix_identity_uncertainties": [],
    }
    return {**body, "prefix_bundle_hash": canonical_sha256(body)}


def _spec(probe_name: str) -> _ProbeSpec:
    try:
        return _SPECS[probe_name]
    except KeyError as exc:
        raise LiteraryOpenAiB2CapabilityProbeError(
            "unknown B2 probe name"
        ) from exc


def _git_text(repo_root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_git_oid(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) in {40, 64}
        and all(character in "0123456789abcdef" for character in value)
    )


__all__ = [
    "ACCEPTED_OBSERVED_MODEL_IDS",
    "DEFAULT_PROFILE_PATH",
    "LiteraryOpenAiB2CapabilityProbeError",
    "LiteraryOpenAiB2ProbePlanV1",
    "PROBE_NAMES",
    "PROBE_PROFILE_ID",
    "PROBE_PROFILE_REVISION",
    "RUNTIME_PROFILE_SHA256",
    "SHARED_CORE_REVISION",
    "build_clean_implementation_binding_v1",
    "build_literary_openai_b2_probe_plan_v1",
    "empty_b2_probe_response_v1",
    "execute_literary_openai_b2_probe_once_v1",
    "implementation_sha256_v1",
    "load_literary_openai_b2_probe_profile_v1",
    "synthetic_b2_probe_request_v1",
    "validate_literary_openai_b2_probe_payload_v1",
]
