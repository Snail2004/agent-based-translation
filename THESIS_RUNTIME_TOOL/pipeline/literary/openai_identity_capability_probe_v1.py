"""Official OpenAI Identity-Auditor Structured Output capability probe.

The synthetic fixture contains no production entity and carries no registry
authority. The module only binds the production Identity response schema and
local validator to one neutral Shared LLM capability attempt.
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
from pipeline.literary.book_source_lineage import (
    build_book_source_manifest,
    state_lineage_id_for_manifest,
)
from pipeline.literary.chapter_cycle_review_v1 import (
    REVIEW_LEDGER_SCHEMA_VERSION,
    REVIEW_LEDGER_VALIDATOR_VERSION,
)
from pipeline.literary.chapter_cycle_shared_runtime_v1 import (
    build_literary_code_ref_v1,
)
from pipeline.literary.chapter_prefix_prior_v1 import (
    CANDIDATE_ONLY_SCOPE,
    PREFIX_PRIOR_SCHEMA_VERSION,
    PREFIX_PRIOR_VALIDATOR_VERSION,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json, file_sha256
from pipeline.literary.incremental_identity_auditor_v1 import (
    build_incremental_identity_index_v1,
    incremental_identity_response_schema_v1,
    validate_incremental_identity_response_v1,
)
from pipeline.literary.shared_llm_adapter_v1 import (
    render_literary_request_body,
    resolve_transport_response_schema,
)
from pipeline.literary.shared_runtime_profile_v2 import (
    DEFAULT_PROFILE_V2_PATH,
    OPENAI_NATIVE_SCHEMA_DIALECT,
    load_literary_shared_runtime_profile_v2,
)
from pipeline.literary.structured_output_policy_v1 import (
    TRANSPORT_ONLY_OMISSIONS,
    validate_structured_payload,
)


ROLE_ID = "literary.audit.identity_surface"
PROFILE_SCHEMA_VERSION = "literary_openai_identity_capability_probe_profile_v1"
PROBE_PROFILE_ID = "literary_openai_official_identity_native_so_probe_v1"
PROBE_PROFILE_REVISION = "openai_row2_gpt54_identity_v1"
CAPABILITY_ID = "openai_row2_gpt54_literary_identity_native_so_v1"
CAPABILITY_REVISION = "identity_transport_4bf9dc6a_validator_34a8bcbf_v1"
ACCEPTED_OBSERVED_MODEL_IDS = ("gpt-5.4", "gpt-5.4-2026-03-05")
SHARED_CORE_REVISION = "de7c74ba348bffe507aa86933f438b3ed4c5af29"
RUNTIME_PROFILE_ID = "literary_shared_llm_openai_official_v2"
RUNTIME_PROFILE_REVISION = "openai_row2_gpt54_native_v1"
RUNTIME_PROFILE_SHA256 = (
    "63ca4ef6dfcde42effb893de3430c591e6e1d150ad549e47856e4e9b8d1c3aa9"
)
RUNTIME_SOURCE_ALIAS = "openai_official_row2"
CANONICAL_SCHEMA_SHA256 = (
    "a6a6e356f83405f17f3aa3bede30d5af9f5ca58f9c63913c5aec1686af7abb0c"
)
TRANSPORT_SCHEMA_SHA256 = (
    "4bf9dc6abdf0f873b36a1ebc507c466cbca73cdca8f639ca9ef5e004ab91709b"
)
OMISSION_SET_SHA256 = (
    "a6a41d56fed7324cf9bae1b9cd0f0c214fa2ee4516b689a0d8368b2aeb011988"
)
VALIDATOR_ID = "literary.audit.identity_surface.validator"
VALIDATOR_REVISION = "v1"
VALIDATOR_SHA256 = (
    "34a8bcbf9a7931b273d08640a4ddb525773a216931f3b9003f39c1686685b754"
)
PROBE_REQUEST_FINGERPRINT = canonical_sha256(
    {"contract": "literary_openai_identity_capability_probe_v1"}
)

_PIPELINE_ROOT = Path(__file__).resolve().parents[1]
_TOOL_ROOT = _PIPELINE_ROOT.parent
_REPO_ROOT = _TOOL_ROOT.parent
DEFAULT_PROFILE_PATH = (
    _PIPELINE_ROOT
    / "configs"
    / "literary_openai_identity_capability_probe_v1.json"
)
_IMPLEMENTATION_PATHS = (
    Path(
        "THESIS_RUNTIME_TOOL/pipeline/configs/"
        "literary_openai_identity_capability_probe_v1.json"
    ),
    Path(
        "THESIS_RUNTIME_TOOL/pipeline/configs/"
        "literary_shared_llm_runtime_openai_official_v2.json"
    ),
    Path(
        "THESIS_RUNTIME_TOOL/pipeline/literary/"
        "openai_identity_capability_probe_v1.py"
    ),
    Path(
        "THESIS_RUNTIME_TOOL/pipeline/literary/"
        "incremental_identity_auditor_v1.py"
    ),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/chapter_cycle_review_v1.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/chapter_prefix_prior_v1.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/shared_llm_adapter_v1.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/shared_runtime_profile_v2.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/structured_output_policy_v1.py"),
)


class LiteraryOpenAiIdentityCapabilityProbeError(RuntimeError):
    pass


@dataclass(frozen=True)
class LiteraryOpenAiIdentityProbePlanV1:
    profile: Mapping[str, Any]
    runtime_profile_sha256: str
    source: Mapping[str, Any]
    index: Mapping[str, Any]
    canonical_schema: Mapping[str, Any]
    response_schema: Mapping[str, Any]
    omitted_transport_constraints: tuple[Mapping[str, Any], ...]
    validator_ref: Mapping[str, str]
    request_body: Mapping[str, Any]
    implementation_binding: Mapping[str, str]
    seal: Mapping[str, Any]


def load_literary_openai_identity_probe_profile_v1(
    path: Path = DEFAULT_PROFILE_PATH,
) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as exc:
        raise LiteraryOpenAiIdentityCapabilityProbeError(
            "cannot load official OpenAI Identity probe profile"
        ) from exc
    if payload != _expected_profile():
        raise LiteraryOpenAiIdentityCapabilityProbeError(
            "official OpenAI Identity probe profile differs from the closed contract"
        )
    return deepcopy(payload)


def synthetic_identity_probe_document_v1() -> dict[str, Any]:
    return {
        "document_id": "literary_identity_capability_probe",
        "chapters": [
            {
                "chapter_id": "probe_ch01",
                "blocks": [
                    {
                        "block_id": "probe_ch01_b001",
                        "order_index": 1,
                        "source_text": "An old lintel bears the name Rowan Vale.",
                    }
                ],
            },
            {
                "chapter_id": "probe_ch02",
                "blocks": [
                    {
                        "block_id": "probe_ch02_b001",
                        "order_index": 1,
                        "source_text": "A visitor says, 'My name is Rowan Vale.'",
                    }
                ],
            },
        ],
    }


def _synthetic_card(
    card_id: str, *, chapter_id: str, block_id: str, uncertainty_id: str
) -> dict[str, Any]:
    body = {
        "prior_card_id": card_id,
        "canonical_surface": "Rowan Vale",
        "stable_surfaces": ["Rowan Vale"],
        "effective_claims": {
            "referent_kind": None,
            "referential_gender": None,
            "identity_summary": None,
        },
        "disputed_claims": [
            {
                "disputed_field": "identity_membership",
                "historical_value": None,
                "status": "pending",
                "pending_reason_codes": ["conflicting_evidence"],
                "evidence_manifest_hashes": [],
                "hearing_count": 0,
                "automatic_hearing_limit": 2,
                "same_evidence_reopen_forbidden": True,
                "next_review_trigger": "identity_resolution",
                "revision_ids": [],
                "uncertainty_id": uncertainty_id,
            }
        ],
        "authority_scope": CANDIDATE_ONLY_SCOPE,
        "first_supported_block_id": block_id,
        "provenance_refs": [{"chapter_id": chapter_id, "block_id": block_id}],
        "source_candidate_id": f"source_{card_id}",
    }
    return {**body, "context_card_hash": canonical_hash(body)}


def _synthetic_prefix(document: Mapping[str, Any]) -> dict[str, Any]:
    manifest = build_book_source_manifest(document)
    uncertainty_body = {
        "surface_key": "rowan vale",
        "prior_card_ids": ["probe_card_old", "probe_card_current"],
        "chapter_ids": ["probe_ch01", "probe_ch02"],
        "status": "pending_identity_review",
        "authority_effect": "candidate_only",
        "reason_code": "cross_chapter_surface_collision",
    }
    uncertainty = {
        "uncertainty_id": "prefixunc1_" + canonical_hash(uncertainty_body)[:20],
        **uncertainty_body,
    }
    cards = [
        _synthetic_card(
            "probe_card_old",
            chapter_id="probe_ch01",
            block_id="probe_ch01_b001",
            uncertainty_id=uncertainty["uncertainty_id"],
        ),
        _synthetic_card(
            "probe_card_current",
            chapter_id="probe_ch02",
            block_id="probe_ch02_b001",
            uncertainty_id=uncertainty["uncertainty_id"],
        ),
    ]
    body = {
        "schema_version": PREFIX_PRIOR_SCHEMA_VERSION,
        "validator_version": PREFIX_PRIOR_VALIDATOR_VERSION,
        "state_lineage_id": state_lineage_id_for_manifest(manifest),
        "book_source_manifest_hash": manifest["manifest_hash"],
        "coverage_through_chapter_id": "probe_ch02",
        "covered_chapter_ids": ["probe_ch01", "probe_ch02"],
        "audited_inventory_provenance": [],
        "claim_cards": [
            {"prior_card_id": "probe_card_old"},
            {"prior_card_id": "probe_card_current"},
        ],
        "b0_context_cards": [],
        "candidate_only_context_cards": cards,
        "source_entity_manifest": [],
        "glossary_context_cards": [],
        "source_glossary_manifest": [],
        "claim_projection_hashes": [],
        "glossary_projection_hashes": [],
        "prefix_identity_uncertainties": [uncertainty],
        "production_publish_performed": False,
    }
    return {**body, "prefix_bundle_hash": canonical_hash(body)}


def _synthetic_review(prefix: Mapping[str, Any]) -> dict[str, Any]:
    identity_body = {
        "state_lineage_id": prefix["state_lineage_id"],
        "chapter_id": "probe_ch02",
        "source_kind": "synthetic_identity_capability_evidence",
        "route": "book_identity_auditor",
        "subject_prior_card_ids": ["probe_card_current", "probe_card_old"],
        "disputed_field": "identity_membership",
        "source_block_ids": ["probe_ch01_b001", "probe_ch02_b001"],
        "evidence_manifest_hash": canonical_hash({"evidence": "neutral_probe"}),
        "source_artifact_hash": canonical_hash({"artifact": "neutral_probe"}),
    }
    row = {
        "review_item_id": "cycrev1_" + canonical_hash(identity_body)[:20],
        **identity_body,
        "lifecycle_state": "queued",
        "authority_effect": "none",
        "reason_code": "identity_collision",
        "reopen_classification": None,
    }
    body = {
        "schema_version": REVIEW_LEDGER_SCHEMA_VERSION,
        "validator_version": REVIEW_LEDGER_VALIDATOR_VERSION,
        "state_lineage_id": prefix["state_lineage_id"],
        "coverage_through_chapter_id": "probe_ch02",
        "observed_queue_hashes": [canonical_hash(row)],
        "review_items": [row],
        "production_publish_performed": False,
    }
    return {**body, "review_ledger_hash": canonical_hash(body)}


def synthetic_identity_probe_index_v1() -> dict[str, Any]:
    document = synthetic_identity_probe_document_v1()
    prefix = _synthetic_prefix(document)
    return build_incremental_identity_index_v1(
        document=document,
        prefix_bundle=prefix,
        review_ledger=_synthetic_review(prefix),
    )


def synthetic_identity_probe_response_v1(
    index: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    index = dict(index or synthetic_identity_probe_index_v1())
    component = index["components"][0]
    source_ids = list(component["source_block_ids"])
    return {
        "component_id": component["component_id"],
        "candidate_actions": [
            {
                "prior_card_id": card_id,
                "action": "keep",
                "target_prior_card_id": None,
                "source_block_ids": source_ids,
                "resolution_note": (
                    "Capability fixture only; keep both candidate records independent."
                ),
            }
            for card_id in component["candidate_prior_card_ids"]
        ],
        "surface_scope_actions": [],
    }


def identity_validator_ref_v1() -> dict[str, str]:
    ref = build_literary_code_ref_v1(
        identifier=VALIDATOR_ID,
        revision=VALIDATOR_REVISION,
        callables=(validate_structured_payload, validate_incremental_identity_response_v1),
    )
    if ref["sha256"] != VALIDATOR_SHA256:
        raise LiteraryOpenAiIdentityCapabilityProbeError(
            "Identity local validator hash differs"
        )
    return ref


def validate_literary_openai_identity_probe_payload_v1(
    *, index: Mapping[str, Any], payload: Mapping[str, Any]
) -> dict[str, Any]:
    schema = incremental_identity_response_schema_v1()
    validate_structured_payload(payload, canonical_schema=schema)
    return validate_incremental_identity_response_v1(
        payload,
        index=index,
        request_fingerprint=PROBE_REQUEST_FINGERPRINT,
    )


def implementation_sha256_v1(repo_root: Path = _REPO_ROOT) -> str:
    root = Path(repo_root).resolve()
    rows = []
    for relative in _IMPLEMENTATION_PATHS:
        path = root / relative
        if not path.is_file():
            raise LiteraryOpenAiIdentityCapabilityProbeError(
                f"Identity probe implementation file is absent: {relative.as_posix()}"
            )
        rows.append({"path": relative.as_posix(), "sha256": file_sha256(path)})
    return canonical_sha256(rows)


def build_clean_implementation_binding_v1(
    repo_root: Path = _REPO_ROOT,
) -> dict[str, str]:
    root = Path(repo_root).resolve()
    status = _git_text(root, "status", "--short", "--untracked-files=no")
    if status:
        raise LiteraryOpenAiIdentityCapabilityProbeError(
            "Identity capability probe requires a clean tracked worktree"
        )
    revision = _git_text(root, "rev-parse", "HEAD")
    return {
        "shared_core_revision": SHARED_CORE_REVISION,
        "consumer_revision": revision,
        "consumer_implementation_sha256": implementation_sha256_v1(root),
    }


def build_literary_openai_identity_probe_plan_v1(
    *,
    probe_run_id: str,
    credential_commitment_sha256: str,
    issued_at_utc: str,
    repo_root: Path = _REPO_ROOT,
    implementation_binding: Mapping[str, str] | None = None,
) -> LiteraryOpenAiIdentityProbePlanV1:
    profile = load_literary_openai_identity_probe_profile_v1()
    runtime = load_literary_shared_runtime_profile_v2(DEFAULT_PROFILE_V2_PATH)
    if (
        runtime.profile_id != RUNTIME_PROFILE_ID
        or runtime.profile_revision != RUNTIME_PROFILE_REVISION
        or runtime.profile_sha256 != RUNTIME_PROFILE_SHA256
    ):
        raise LiteraryOpenAiIdentityCapabilityProbeError(
            "Identity runtime profile differs from the probe binding"
        )
    binding = dict(
        implementation_binding or build_clean_implementation_binding_v1(repo_root)
    )
    source_binding = dict(runtime.source_binding_for(ROLE_ID))
    role_binding = runtime.role_bindings[ROLE_ID]
    if role_binding.source_alias != RUNTIME_SOURCE_ALIAS:
        raise LiteraryOpenAiIdentityCapabilityProbeError(
            "Identity source alias differs from the probe binding"
        )
    if source_binding["authority_class"] != "direct_official_openai":
        raise LiteraryOpenAiIdentityCapabilityProbeError(
            "Identity probe source is not direct official OpenAI"
        )
    if source_binding["fallback_enabled"] is not False:
        raise LiteraryOpenAiIdentityCapabilityProbeError(
            "Identity probe source enables fallback"
        )
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
    canonical_schema = incremental_identity_response_schema_v1()
    if canonical_sha256(canonical_schema) != CANONICAL_SCHEMA_SHA256:
        raise LiteraryOpenAiIdentityCapabilityProbeError(
            "Identity canonical schema hash differs"
        )
    envelope = runtime.output_envelope_for(ROLE_ID)
    if envelope != {
        "mode": "native_schema",
        "schema_dialect": OPENAI_NATIVE_SCHEMA_DIALECT,
        "instruction_id": None,
        "instruction_revision": None,
    }:
        raise LiteraryOpenAiIdentityCapabilityProbeError(
            "Identity output envelope is not official native schema"
        )
    transport_schema, omissions = resolve_transport_response_schema(
        response_schema=canonical_schema,
        protocol=source["protocol"],
        output_envelope=envelope,
    )
    if canonical_sha256(transport_schema) != TRANSPORT_SCHEMA_SHA256:
        raise LiteraryOpenAiIdentityCapabilityProbeError(
            "Identity transport schema hash differs"
        )
    if (
        len(omissions) != 8
        or canonical_sha256(list(omissions)) != OMISSION_SET_SHA256
        or {row["keyword"] for row in omissions} != set(TRANSPORT_ONLY_OMISSIONS)
    ):
        raise LiteraryOpenAiIdentityCapabilityProbeError(
            "Identity transport omission set differs"
        )
    validator_ref = identity_validator_ref_v1()
    index = synthetic_identity_probe_index_v1()
    expected_response = synthetic_identity_probe_response_v1(index)
    messages = [
        {
            "role": "system",
            "content": (
                "Capability probe only. Return exactly the JSON object supplied "
                "as required_response. Do not infer, merge, or publish identity."
            ),
        },
        {
            "role": "user",
            "content": canonical_json(
                {
                    "fixture": "neutral synthetic identity response shape",
                    "required_response": expected_response,
                }
            ),
        },
    ]
    preset = runtime.role_presets[ROLE_ID]
    generation = dict(preset.generation)
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
        capability={"capability_kind": "native_structured_output"},
        messages=messages,
        response_schema=transport_schema,
        schema_name=profile["capability_intent"]["schema_name"],
        structured_output=runtime.shared_structured_output_for(ROLE_ID),
        output_envelope=envelope,
        base_url=source["base_url"],
    )
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
    seal = create_capability_probe_seal(
        source=source,
        consumer_workstream="literary",
        role_id=ROLE_ID,
        probe_run_id=probe_run_id,
        probe_profile_id=profile["profile_id"],
        probe_profile_revision=profile["profile_revision"],
        implementation_binding=binding,
        capability_intent=intent,
        response_schema=transport_schema,
        request_body=request_body,
        limits=profile["limits"],
        issued_at_utc=issued_at_utc,
    )
    return LiteraryOpenAiIdentityProbePlanV1(
        profile=profile,
        runtime_profile_sha256=runtime.profile_sha256,
        source=source,
        index=index,
        canonical_schema=canonical_schema,
        response_schema=transport_schema,
        omitted_transport_constraints=tuple(deepcopy(omissions)),
        validator_ref=validator_ref,
        request_body=request_body,
        implementation_binding=binding,
        seal=seal,
    )


def execute_literary_openai_identity_probe_once_v1(
    *,
    probe: SharedLlmCapabilityProbe,
    plan: LiteraryOpenAiIdentityProbePlanV1,
    cost_fact: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    def validate(payload: Mapping[str, Any]) -> dict[str, Any]:
        return validate_literary_openai_identity_probe_payload_v1(
            index=plan.index,
            payload=payload,
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
        "capability_intent": {
            "capability_id": CAPABILITY_ID,
            "capability_revision": CAPABILITY_REVISION,
            "role_id": ROLE_ID,
            "requested_model_id": "gpt-5.4",
            "accepted_observed_model_ids": list(ACCEPTED_OBSERVED_MODEL_IDS),
            "capability_kind": "native_structured_output",
            "schema_name": "literary_identity_surface_auditor_v1",
            "schema_dialect": OPENAI_NATIVE_SCHEMA_DIALECT,
            "schema_sha256": TRANSPORT_SCHEMA_SHA256,
            "local_validator_id": VALIDATOR_ID,
            "local_validator_revision": VALIDATOR_REVISION,
            "local_validator_sha256": VALIDATOR_SHA256,
        },
        "transport_projection": {
            "projection_id": "literary_openai_transport_schema_projection_v1",
            "canonical_schema_sha256": CANONICAL_SCHEMA_SHA256,
            "transport_schema_sha256": TRANSPORT_SCHEMA_SHA256,
            "omitted_keywords": sorted(TRANSPORT_ONLY_OMISSIONS),
            "omitted_constraint_count": 8,
            "omission_set_sha256": OMISSION_SET_SHA256,
        },
        "generation": {
            "temperature": 1.0,
            "top_p": 1.0,
            "seed": 20260720,
            "reasoning_effort": "none",
            "verbosity": "low",
            "max_completion_tokens": 1024,
        },
        "limits": {
            "max_calls": 1,
            "max_prompt_utf8_bytes": 16000,
            "max_response_utf8_bytes": 12000,
            "max_prompt_tokens": 6000,
            "max_completion_tokens": 1024,
            "max_total_tokens": 7024,
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


def _git_text(repo_root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


__all__ = [
    "CANONICAL_SCHEMA_SHA256",
    "DEFAULT_PROFILE_PATH",
    "OMISSION_SET_SHA256",
    "ROLE_ID",
    "SHARED_CORE_REVISION",
    "TRANSPORT_SCHEMA_SHA256",
    "VALIDATOR_ID",
    "VALIDATOR_SHA256",
    "LiteraryOpenAiIdentityCapabilityProbeError",
    "LiteraryOpenAiIdentityProbePlanV1",
    "build_clean_implementation_binding_v1",
    "build_literary_openai_identity_probe_plan_v1",
    "execute_literary_openai_identity_probe_once_v1",
    "identity_validator_ref_v1",
    "implementation_sha256_v1",
    "load_literary_openai_identity_probe_profile_v1",
    "synthetic_identity_probe_document_v1",
    "synthetic_identity_probe_index_v1",
    "synthetic_identity_probe_response_v1",
    "validate_literary_openai_identity_probe_payload_v1",
]
