"""Official OpenAI B1 native Structured Output capability probe.

The module is a Literary-owned semantic binding around the neutral one-call
Shared LLM capability executor. It owns no credential loading, transport,
scheduler, ledger, cache, or publication authority.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any, Mapping

from pipeline.llm_backend import (
    SharedLlmCapabilityProbe,
    canonical_json,
    canonical_sha256,
    create_capability_probe_seal,
)
from pipeline.literary.b0_entity_inventory_experiment import (
    entity_inventory_response_schema,
    validate_entity_inventory_response,
)
from pipeline.literary.chapter_cycle_shared_runtime_v1 import (
    build_literary_code_ref_v1,
)
from pipeline.literary.shared_runtime_profile_v2 import (
    DEFAULT_PROFILE_V2_PATH,
    OPENAI_NATIVE_SCHEMA_DIALECT,
    LiterarySharedRuntimeProfileV2,
    load_literary_shared_runtime_profile_v2,
)
from pipeline.literary.structured_output_policy_v1 import (
    TRANSPORT_ONLY_OMISSIONS,
    project_transport_schema_v1,
    validate_structured_payload,
)


PROFILE_SCHEMA_VERSION = "literary_openai_b1_capability_probe_profile_v1"
PROBE_PROFILE_ID = "literary_openai_official_b1_native_so_probe_v1"
PROBE_PROFILE_REVISION = "openai_row2_gpt54_v2"
CAPABILITY_ID = "openai_row2_gpt54_literary_b1_native_so_v1"
CAPABILITY_REVISION = (
    "b1_transport_eed09132_validator_1f85e47c_openai_row2_v2"
)
ACCEPTED_OBSERVED_MODEL_IDS = ("gpt-5.4", "gpt-5.4-2026-03-05")
SHARED_CORE_REVISION = "dece3488f591b726bd4eb0883f42829c7a58410d"
RUNTIME_PROFILE_ID = "literary_shared_llm_openai_official_v2"
RUNTIME_PROFILE_REVISION = "openai_row2_gpt54_native_v1"
RUNTIME_PROFILE_SHA256 = (
    "63ca4ef6dfcde42effb893de3430c591e6e1d150ad549e47856e4e9b8d1c3aa9"
)
RUNTIME_SOURCE_ALIAS = "openai_official_row2"
B1_ROLE_ID = "literary.b1.entity_inventory"
B1_CANONICAL_SCHEMA_SHA256 = (
    "b113338e2e36aed1d6c381574fd0f58cfc0b3db4ea5141569af30bd9f84469ce"
)
B1_TRANSPORT_SCHEMA_SHA256 = (
    "eed0913225d2805eb2c533c85d3e69c5c323dd79c53c73756049d12d0a28fa11"
)
B1_OMISSION_SET_SHA256 = (
    "8fb164b651288f2618b7afaa8a1d1e5e5470d55e2120485fa7d0d23fe67f32e2"
)
B1_SCHEMA_SHA256 = B1_TRANSPORT_SCHEMA_SHA256
B1_VALIDATOR_ID = "literary.b1.entity_inventory.validator"
B1_VALIDATOR_REVISION = "v1"
B1_VALIDATOR_SHA256 = (
    "1f85e47ccdcb9f3760f355b7363811b885d915172d46d68255f498658c75161f"
)
PROBE_REQUEST_FINGERPRINT = canonical_sha256(
    {"contract": "literary_openai_b1_capability_probe_v1"}
)

_PIPELINE_ROOT = Path(__file__).resolve().parents[1]
_TOOL_ROOT = _PIPELINE_ROOT.parent
_REPO_ROOT = _TOOL_ROOT.parent
DEFAULT_PROFILE_PATH = (
    _PIPELINE_ROOT / "configs" / "literary_openai_b1_capability_probe_v1.json"
)
_IMPLEMENTATION_PATHS = (
    Path(
        "THESIS_RUNTIME_TOOL/pipeline/configs/"
        "literary_openai_b1_capability_probe_v1.json"
    ),
    Path(
        "THESIS_RUNTIME_TOOL/pipeline/configs/"
        "literary_shared_llm_runtime_openai_official_v2.json"
    ),
    Path(
        "THESIS_RUNTIME_TOOL/pipeline/literary/"
        "openai_b1_capability_probe_v1.py"
    ),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/shared_runtime_profile_v2.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/b0_entity_inventory_experiment.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/structured_output_policy_v1.py"),
    Path(
        "THESIS_RUNTIME_TOOL/pipeline/literary/"
        "chapter_cycle_shared_runtime_v1.py"
    ),
)


class LiteraryOpenAiCapabilityProbeError(RuntimeError):
    pass


@dataclass(frozen=True)
class LiteraryOpenAiB1ProbePlanV1:
    profile: Mapping[str, Any]
    runtime_profile_sha256: str
    source: Mapping[str, Any]
    canonical_schema: Mapping[str, Any]
    response_schema: Mapping[str, Any]
    omitted_transport_constraints: tuple[Mapping[str, Any], ...]
    request_body: Mapping[str, Any]
    implementation_binding: Mapping[str, str]
    seal: Mapping[str, Any]


def load_literary_openai_b1_probe_profile_v1(
    path: Path = DEFAULT_PROFILE_PATH,
) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as exc:
        raise LiteraryOpenAiCapabilityProbeError(
            "cannot load official OpenAI B1 probe profile"
        ) from exc
    _exact_keys(
        payload,
        {
            "schema_version",
            "profile_id",
            "profile_revision",
            "shared_core_revision",
            "runtime_profile",
            "capability_intent",
            "transport_projection",
            "generation",
            "limits",
            "safety",
        },
        "probe profile",
    )
    if payload["schema_version"] != PROFILE_SCHEMA_VERSION:
        raise LiteraryOpenAiCapabilityProbeError("probe profile schema differs")
    if payload["shared_core_revision"] != SHARED_CORE_REVISION:
        raise LiteraryOpenAiCapabilityProbeError(
            "probe shared-core revision differs"
        )
    if (
        payload["profile_id"] != PROBE_PROFILE_ID
        or payload["profile_revision"] != PROBE_PROFILE_REVISION
    ):
        raise LiteraryOpenAiCapabilityProbeError(
            "probe profile identity differs"
        )

    runtime_ref = _mapping(payload["runtime_profile"], "runtime_profile")
    expected_runtime_ref = {
        "profile_id": RUNTIME_PROFILE_ID,
        "profile_revision": RUNTIME_PROFILE_REVISION,
        "profile_sha256": RUNTIME_PROFILE_SHA256,
        "source_alias": RUNTIME_SOURCE_ALIAS,
    }
    if runtime_ref != expected_runtime_ref:
        raise LiteraryOpenAiCapabilityProbeError(
            "probe runtime profile reference differs"
        )

    intent = _mapping(payload["capability_intent"], "capability_intent")
    _exact_keys(
        intent,
        {
            "capability_id",
            "capability_revision",
            "role_id",
            "requested_model_id",
            "accepted_observed_model_ids",
            "capability_kind",
            "schema_name",
            "schema_dialect",
            "schema_sha256",
            "local_validator_id",
            "local_validator_revision",
            "local_validator_sha256",
        },
        "capability_intent",
    )
    expected_intent = {
        "capability_id": CAPABILITY_ID,
        "capability_revision": CAPABILITY_REVISION,
        "role_id": B1_ROLE_ID,
        "requested_model_id": "gpt-5.4",
        "accepted_observed_model_ids": list(ACCEPTED_OBSERVED_MODEL_IDS),
        "capability_kind": "native_structured_output",
        "schema_name": "literary_b1_entity_inventory_v1",
        "schema_dialect": OPENAI_NATIVE_SCHEMA_DIALECT,
        "schema_sha256": B1_SCHEMA_SHA256,
        "local_validator_id": B1_VALIDATOR_ID,
        "local_validator_revision": B1_VALIDATOR_REVISION,
        "local_validator_sha256": B1_VALIDATOR_SHA256,
    }
    for field, expected in expected_intent.items():
        if intent[field] != expected:
            raise LiteraryOpenAiCapabilityProbeError(
                f"probe capability intent differs at {field}"
            )

    projection = _mapping(
        payload["transport_projection"], "transport_projection"
    )
    expected_projection = {
        "projection_id": "literary_openai_transport_schema_projection_v1",
        "canonical_schema_sha256": B1_CANONICAL_SCHEMA_SHA256,
        "transport_schema_sha256": B1_TRANSPORT_SCHEMA_SHA256,
        "omitted_keywords": sorted(TRANSPORT_ONLY_OMISSIONS),
        "omitted_constraint_count": 27,
        "omission_set_sha256": B1_OMISSION_SET_SHA256,
    }
    if projection != expected_projection:
        raise LiteraryOpenAiCapabilityProbeError(
            "probe transport projection differs"
        )

    generation = _mapping(payload["generation"], "generation")
    _exact_keys(
        generation,
        {
            "temperature",
            "top_p",
            "seed",
            "reasoning_effort",
            "verbosity",
            "max_completion_tokens",
        },
        "generation",
    )
    if generation != {
        "temperature": 1.0,
        "top_p": 1.0,
        "seed": 20260720,
        "reasoning_effort": "none",
        "verbosity": "low",
        "max_completion_tokens": 1024,
    }:
        raise LiteraryOpenAiCapabilityProbeError(
            "probe generation policy differs"
        )

    limits = _mapping(payload["limits"], "limits")
    _exact_keys(
        limits,
        {
            "max_calls",
            "max_prompt_utf8_bytes",
            "max_response_utf8_bytes",
            "max_prompt_tokens",
            "max_completion_tokens",
            "max_total_tokens",
            "request_timeout_ms",
        },
        "limits",
    )
    if limits["max_calls"] != 1:
        raise LiteraryOpenAiCapabilityProbeError(
            "probe must allow exactly one call"
        )
    if limits["max_completion_tokens"] != generation["max_completion_tokens"]:
        raise LiteraryOpenAiCapabilityProbeError(
            "probe completion caps differ"
        )

    expected_safety = {
        "authority": "capability_only",
        "fallback_enabled": False,
        "transport_retry_max": 0,
        "semantic_retry_max": 0,
        "response_cache_enabled": False,
        "application_publish_enabled": False,
    }
    if _mapping(payload["safety"], "safety") != expected_safety:
        raise LiteraryOpenAiCapabilityProbeError(
            "probe safety policy differs"
        )
    return deepcopy(payload)


def materialize_official_openai_source_v1(
    *,
    credential_commitment_sha256: str,
    profile: Mapping[str, Any],
    runtime_profile: LiterarySharedRuntimeProfileV2,
) -> dict[str, Any]:
    runtime_ref = _mapping(profile["runtime_profile"], "runtime_profile")
    if (
        runtime_profile.profile_id != runtime_ref["profile_id"]
        or runtime_profile.profile_revision != runtime_ref["profile_revision"]
        or runtime_profile.profile_sha256 != runtime_ref["profile_sha256"]
    ):
        raise LiteraryOpenAiCapabilityProbeError(
            "runtime profile bytes differ from the probe binding"
        )
    source_alias = runtime_ref["source_alias"]
    source_binding = dict(runtime_profile.source_binding_for(B1_ROLE_ID))
    role_binding = runtime_profile.role_bindings[B1_ROLE_ID]
    if role_binding.source_alias != source_alias:
        raise LiteraryOpenAiCapabilityProbeError(
            "B1 source alias differs from the probe binding"
        )
    if source_binding["authority_class"] != "direct_official_openai":
        raise LiteraryOpenAiCapabilityProbeError(
            "probe source is not direct official OpenAI"
        )
    if source_binding["fallback_enabled"] is not False:
        raise LiteraryOpenAiCapabilityProbeError("probe source enables fallback")
    envelope = dict(runtime_profile.output_envelope_for(B1_ROLE_ID))
    if envelope != {
        "mode": "native_schema",
        "schema_dialect": OPENAI_NATIVE_SCHEMA_DIALECT,
        "instruction_id": None,
        "instruction_revision": None,
    }:
        raise LiteraryOpenAiCapabilityProbeError(
            "B1 output envelope is not official native schema"
        )
    requested_model = runtime_profile.role_presets[B1_ROLE_ID].requested_model_id
    if requested_model != profile["capability_intent"]["requested_model_id"]:
        raise LiteraryOpenAiCapabilityProbeError(
            "runtime B1 model differs from capability intent"
        )
    if not _is_sha256(credential_commitment_sha256):
        raise LiteraryOpenAiCapabilityProbeError(
            "credential commitment must be SHA-256"
        )
    return {
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
        "physical_quota_bucket_id": source_binding[
            "physical_quota_bucket_id"
        ],
        "enabled": True,
    }


def synthetic_probe_chapter_v1() -> dict[str, Any]:
    return {
        "chapter_id": "literary_openai_probe_chapter_v1",
        "blocks": [
            {
                "block_id": "literary_openai_probe_b001",
                "order_index": 1,
                "block_type": "paragraph",
                "clean_text": (
                    "A deliberately entity-free synthetic capability probe block."
                ),
            }
        ],
    }


def empty_probe_response_v1() -> dict[str, Any]:
    return {
        "entity_candidates": [],
        "glossary_candidates": [],
        "unresolved_referents": [],
        "chapter_priority_order": [],
    }


def validate_literary_openai_b1_probe_payload_v1(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    canonical_schema, _, _ = _verified_b1_schemas()
    validate_structured_payload(payload, canonical_schema=canonical_schema)
    return validate_entity_inventory_response(
        payload,
        synthetic_probe_chapter_v1(),
        request_fingerprint=PROBE_REQUEST_FINGERPRINT,
    )


def build_clean_implementation_binding_v1(
    repo_root: Path = _REPO_ROOT,
) -> dict[str, str]:
    root = Path(repo_root).resolve()
    dirty = _git_text(root, "status", "--porcelain", "--untracked-files=no")
    if dirty:
        raise LiteraryOpenAiCapabilityProbeError(
            "live probe seal requires a clean tracked Literary worktree"
        )
    revision = _git_text(root, "rev-parse", "HEAD")
    if not _is_git_oid(revision):
        raise LiteraryOpenAiCapabilityProbeError(
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
            raise LiteraryOpenAiCapabilityProbeError(
                f"probe implementation file is absent: {relative.as_posix()}"
            )
        rows.append(
            {
                "path": relative.as_posix(),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    body = {
        "schema_version": "literary_openai_b1_probe_implementation_v1",
        "shared_core_revision": SHARED_CORE_REVISION,
        "runtime_profile_sha256": RUNTIME_PROFILE_SHA256,
        "canonical_schema_sha256": B1_CANONICAL_SCHEMA_SHA256,
        "transport_schema_sha256": B1_TRANSPORT_SCHEMA_SHA256,
        "omission_set_sha256": B1_OMISSION_SET_SHA256,
        "local_validator_sha256": B1_VALIDATOR_SHA256,
        "files": rows,
    }
    return canonical_sha256(body)


def build_literary_openai_b1_probe_plan_v1(
    *,
    probe_run_id: str,
    credential_commitment_sha256: str,
    issued_at_utc: str,
    repo_root: Path = _REPO_ROOT,
) -> LiteraryOpenAiB1ProbePlanV1:
    profile = load_literary_openai_b1_probe_profile_v1()
    runtime_profile = load_literary_shared_runtime_profile_v2(
        DEFAULT_PROFILE_V2_PATH
    )
    implementation_binding = build_clean_implementation_binding_v1(repo_root)
    source = materialize_official_openai_source_v1(
        credential_commitment_sha256=credential_commitment_sha256,
        profile=profile,
        runtime_profile=runtime_profile,
    )
    canonical_schema, transport_schema, omissions = _verified_b1_schemas()
    validator_ref = _verified_b1_validator_ref()
    intent_profile = profile["capability_intent"]
    intent = {
        key: deepcopy(intent_profile[key])
        for key in (
            "capability_id",
            "capability_revision",
            "requested_model_id",
            "accepted_observed_model_ids",
            "capability_kind",
            "schema_name",
            "schema_dialect",
            "schema_sha256",
            "local_validator_id",
            "local_validator_sha256",
        )
    }
    if validator_ref != {
        "id": intent["local_validator_id"],
        "revision": B1_VALIDATOR_REVISION,
        "sha256": intent["local_validator_sha256"],
    }:
        raise LiteraryOpenAiCapabilityProbeError(
            "B1 validator implementation differs"
        )
    request_body = _request_body(
        profile=profile, response_schema=transport_schema
    )
    seal = create_capability_probe_seal(
        source=source,
        consumer_workstream="literary",
        role_id=intent_profile["role_id"],
        probe_run_id=probe_run_id,
        probe_profile_id=profile["profile_id"],
        probe_profile_revision=profile["profile_revision"],
        implementation_binding=implementation_binding,
        capability_intent=intent,
        response_schema=transport_schema,
        request_body=request_body,
        limits=profile["limits"],
        issued_at_utc=issued_at_utc,
    )
    return LiteraryOpenAiB1ProbePlanV1(
        profile=deepcopy(profile),
        runtime_profile_sha256=runtime_profile.profile_sha256,
        source=deepcopy(source),
        canonical_schema=deepcopy(canonical_schema),
        response_schema=deepcopy(transport_schema),
        omitted_transport_constraints=tuple(deepcopy(omissions)),
        request_body=deepcopy(request_body),
        implementation_binding=dict(implementation_binding),
        seal=deepcopy(seal),
    )


def execute_literary_openai_b1_probe_once_v1(
    *,
    probe: SharedLlmCapabilityProbe,
    plan: LiteraryOpenAiB1ProbePlanV1,
    cost_fact: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    result = probe.execute_once(
        seal=plan.seal,
        request_body=plan.request_body,
        local_validator=validate_literary_openai_b1_probe_payload_v1,
        local_validator_id=B1_VALIDATOR_ID,
        local_validator_sha256=B1_VALIDATOR_SHA256,
        cost_fact=cost_fact,
    )
    if set(result) != {
        "status",
        "provider_called",
        "probe_seal_sha256",
        "receipt",
        "capability_evidence",
    }:
        raise LiteraryOpenAiCapabilityProbeError(
            "probe exposed an unexpected payload"
        )
    if result["status"] not in {"qualified", "failed"}:
        raise LiteraryOpenAiCapabilityProbeError(
            "probe returned an unknown status"
        )
    return result


def _request_body(
    *, profile: Mapping[str, Any], response_schema: Mapping[str, Any]
) -> dict[str, Any]:
    generation = profile["generation"]
    intent = profile["capability_intent"]
    return {
        "model": intent["requested_model_id"],
        "messages": [
            {
                "role": "system",
                "content": (
                    "Capability probe only. Return the exact empty inventory "
                    "object required by the response schema. Do not infer or "
                    "add literary content."
                ),
            },
            {
                "role": "user",
                "content": canonical_json(synthetic_probe_chapter_v1()),
            },
        ],
        "temperature": generation["temperature"],
        "top_p": generation["top_p"],
        "seed": generation["seed"],
        "reasoning_effort": generation["reasoning_effort"],
        "verbosity": generation["verbosity"],
        "max_completion_tokens": generation["max_completion_tokens"],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": intent["schema_name"],
                "strict": True,
                "schema": deepcopy(dict(response_schema)),
            },
        },
    }


def _verified_b1_schemas(
) -> tuple[dict[str, Any], dict[str, Any], tuple[Mapping[str, Any], ...]]:
    canonical_schema = entity_inventory_response_schema()
    if canonical_sha256(canonical_schema) != B1_CANONICAL_SCHEMA_SHA256:
        raise LiteraryOpenAiCapabilityProbeError(
            "B1 canonical schema hash differs"
        )
    transport_schema, omissions = project_transport_schema_v1(canonical_schema)
    if canonical_sha256(transport_schema) != B1_TRANSPORT_SCHEMA_SHA256:
        raise LiteraryOpenAiCapabilityProbeError(
            "B1 transport schema hash differs"
        )
    if canonical_sha256(list(omissions)) != B1_OMISSION_SET_SHA256:
        raise LiteraryOpenAiCapabilityProbeError(
            "B1 transport omission set differs"
        )
    if len(omissions) != 27 or {
        str(row["keyword"]) for row in omissions
    } != set(TRANSPORT_ONLY_OMISSIONS):
        raise LiteraryOpenAiCapabilityProbeError(
            "B1 transport omissions are incomplete"
        )
    return canonical_schema, transport_schema, omissions


def _verified_b1_validator_ref() -> dict[str, str]:
    ref = build_literary_code_ref_v1(
        identifier=B1_VALIDATOR_ID,
        revision=B1_VALIDATOR_REVISION,
        callables=(validate_structured_payload, validate_entity_inventory_response),
    )
    if ref["sha256"] != B1_VALIDATOR_SHA256:
        raise LiteraryOpenAiCapabilityProbeError(
            "B1 local validator hash differs"
        )
    return ref


def _git_text(repo_root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise LiteraryOpenAiCapabilityProbeError(f"{label} must be an object")
    return dict(value)


def _exact_keys(
    value: Mapping[str, Any], expected: set[str], label: str
) -> None:
    if set(value) != expected:
        raise LiteraryOpenAiCapabilityProbeError(
            f"{label} field set differs"
        )


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
    "B1_CANONICAL_SCHEMA_SHA256",
    "B1_OMISSION_SET_SHA256",
    "B1_ROLE_ID",
    "B1_SCHEMA_SHA256",
    "B1_TRANSPORT_SCHEMA_SHA256",
    "B1_VALIDATOR_ID",
    "B1_VALIDATOR_SHA256",
    "CAPABILITY_ID",
    "CAPABILITY_REVISION",
    "LiteraryOpenAiB1ProbePlanV1",
    "LiteraryOpenAiCapabilityProbeError",
    "RUNTIME_PROFILE_SHA256",
    "PROBE_PROFILE_ID",
    "PROBE_PROFILE_REVISION",
    "SHARED_CORE_REVISION",
    "build_clean_implementation_binding_v1",
    "build_literary_openai_b1_probe_plan_v1",
    "empty_probe_response_v1",
    "execute_literary_openai_b1_probe_once_v1",
    "implementation_sha256_v1",
    "load_literary_openai_b1_probe_profile_v1",
    "materialize_official_openai_source_v1",
    "synthetic_probe_chapter_v1",
    "validate_literary_openai_b1_probe_payload_v1",
]
