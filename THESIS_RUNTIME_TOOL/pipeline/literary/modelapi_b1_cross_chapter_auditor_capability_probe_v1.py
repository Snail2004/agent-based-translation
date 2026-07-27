"""One-shot ModelAPI JSON-object probes for B1 cross-chapter hearings."""

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
from pipeline.literary.b1_cross_chapter_auditor_live_v1 import (
    CROSS_CHAPTER_MODEL_REF_FIELDS_V1,
    IDENTITY_ROLE_ID,
    IDENTITY_ROUTE,
    ROLE_ID_BY_ROUTE,
    SCHEMA_NAME_BY_ROUTE,
    STABLE_CLAIM_ROLE_ID,
    STABLE_CLAIM_ROUTE,
    make_hearing_semantic_validator_v1,
    response_schema_for_route_v1,
    validator_ref_for_route_v1,
)
from pipeline.literary.b1_cross_chapter_audit_bridge_v1 import (
    render_identity_hearing_request_v1,
    render_stable_claim_hearing_request_v1,
)
from pipeline.literary.checkpoint import file_sha256
from pipeline.literary.model_ref_transport_v1 import (
    bind_model_ref_validator_v1,
    project_capability_probe_request_v1,
    resolve_capability_probe_response_v1,
)
from pipeline.literary.shared_llm_adapter_v1 import render_literary_request_body
from pipeline.literary.shared_runtime_profile_v2 import (
    LOCAL_VALIDATION_SCHEMA_DIALECT,
    load_literary_shared_runtime_profile_v2,
)


_PIPELINE_ROOT = Path(__file__).resolve().parents[1]
_TOOL_ROOT = _PIPELINE_ROOT.parent
_REPO_ROOT = _TOOL_ROOT.parent

PROFILE_PATH = (
    _PIPELINE_ROOT
    / "configs"
    / "literary_modelapi_b1_cross_chapter_auditor_probe_v1.json"
)
RUNTIME_PROFILE_PATH = (
    _PIPELINE_ROOT
    / "configs"
    / "literary_shared_llm_runtime_modelapi_b1_cross_chapter_auditor_v1.json"
)
DESIGN_DOC = _REPO_ROOT / "design" / "LITERARY_PROMPT_DESIGN.md"

_IMPLEMENTATION_PATHS = (
    Path("design/LITERARY_PROMPT_DESIGN.md"),
    Path(
        "THESIS_RUNTIME_TOOL/pipeline/configs/"
        "literary_modelapi_b1_cross_chapter_auditor_probe_v1.json"
    ),
    Path(
        "THESIS_RUNTIME_TOOL/pipeline/configs/"
        "literary_shared_llm_runtime_modelapi_b1_cross_chapter_auditor_v1.json"
    ),
    Path(
        "THESIS_RUNTIME_TOOL/pipeline/literary/"
        "b1_cross_chapter_audit_bridge_v1.py"
    ),
    Path(
        "THESIS_RUNTIME_TOOL/pipeline/literary/"
        "b1_cross_chapter_auditor_live_v1.py"
    ),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/model_ref_transport_v1.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/model_ref_v1.py"),
    Path(
        "THESIS_RUNTIME_TOOL/pipeline/literary/"
        "modelapi_b1_cross_chapter_auditor_capability_probe_v1.py"
    ),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/shared_llm_adapter_v1.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/shared_runtime_profile_v2.py"),
)


class B1CrossChapterAuditorProbeError(RuntimeError):
    pass


@dataclass(frozen=True)
class B1CrossChapterAuditorProbePlanV1:
    route: str
    role_id: str
    profile: Mapping[str, Any]
    source: Mapping[str, Any]
    component: Mapping[str, Any]
    request: Mapping[str, Any]
    response_schema: Mapping[str, Any]
    validator_ref: Mapping[str, str]
    request_body: Mapping[str, Any]
    implementation_binding: Mapping[str, str]
    seal: Mapping[str, Any]


def synthetic_hearing_v1(route: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    block_id = "literary_b1_cross_chapter_probe_b001"
    source_blocks = {block_id: "Mara Vale is not the Mara Vale named on the old gate."}
    prior = {
        "prior_card_id": "b0ent_probe_prior",
        "canonical_surface": "Mara Vale",
        "stable_surfaces": ["Mara Vale"],
        "record_class": "unresolved_named_reference",
        "referent_kind": "unknown",
        "claim_state": "provisional",
        "first_supported_block_id": block_id,
        "provenance_refs": [{"chapter_id": "probe_ch01", "block_id": block_id}],
        "identity_summary": "An older written occurrence of the name.",
        "presence_basis": "inscription_or_document",
    }
    alternate_prior = {
        **deepcopy(prior),
        "prior_card_id": "b0ent_probe_prior_alternate",
        "identity_summary": "A participant previously associated with the east farm.",
        "presence_basis": "direct_participant",
    }
    common = {
        "component_id": "b1xhear_probe_component",
        "chapter_id": "probe_ch02",
        "source_block_ids": [block_id],
        "lifecycle_state": "ready_for_hearing",
    }
    if route == IDENTITY_ROUTE:
        component = {
            **common,
            "question_type": "identity_linkage",
            "review_route": IDENTITY_ROUTE,
            "prior_card_ids": [
                prior["prior_card_id"],
                alternate_prior["prior_card_id"],
            ],
            "prior_candidate_snapshots": [prior, alternate_prior],
            "candidate_contexts": [],
            "current_entity_id": "b0ent_probe_current",
            "current_entity_ids": ["b0ent_probe_current"],
            "current_card_snapshots": [
                {
                    "entity_id": "b0ent_probe_current",
                    "canonical_surface": "Mara Vale",
                    "record_class": "named_entity_candidate",
                    "referent_kind": {"value": "person"},
                    "support_block_ids": [block_id],
                }
            ],
            "current_dossier_snapshots": [
                {
                    "scan_observation_id": "b1obs_probe_mara",
                    "surface": "Mara Vale",
                    "referent_kind_claim": "person",
                    "identity_summary": "A living participant in the current scene.",
                }
            ],
            "trigger": {
                "scan_verdict": "uncertain",
                "reason_code": "other",
                "reason": "The same surface has incompatible presence evidence.",
            },
        }
    elif route == STABLE_CLAIM_ROUTE:
        component = {
            **common,
            "question_type": "stable_claim",
            "review_route": STABLE_CLAIM_ROUTE,
            "prior_card_id": prior["prior_card_id"],
            "prior_card_snapshot": prior,
            "field": "role_or_occupation",
            "existing_value": "owner",
            "observed_value": "tenant",
            "reason": "The current chapter directly conflicts with the prior claim.",
            "current_card_snapshot": {
                "entity_id": "b0ent_probe_current",
                "canonical_surface": "Mara Vale",
            },
            "current_dossier_snapshot": {
                "scan_observation_id": "b1obs_probe_mara",
                "surface": "Mara Vale",
            },
        }
    else:
        raise B1CrossChapterAuditorProbeError("unknown hearing probe route")
    queue = {
        "chapter_id": "probe_ch02",
        "queue_hash": "a" * 64,
        "registry_hash": "b" * 64,
    }
    return component, queue, source_blocks


def synthetic_persistent_response_v1(route: str) -> dict[str, Any]:
    block_id = "literary_b1_cross_chapter_probe_b001"
    if route == IDENTITY_ROUTE:
        excluded = "b0ent_probe_prior"
        return {
            "component_id": "b1xhear_probe_component",
            "verdict": "insufficient_evidence",
            "merge_target_prior_card_id": None,
            "excluded_prior_card_ids": [excluded],
            "field_adjudications": [],
            "evidence": [
                {
                    "block_id": block_id,
                    "quote": "Mara Vale is not the Mara Vale named on the old gate.",
                    "supports_excluded_prior_card_ids": [excluded],
                }
            ],
            "reason": "The supplied sentence excludes the gate inscription but does not identify the remaining candidate.",
            "resolution_condition": "A supplied block must link the current participant to the remaining prior candidate.",
        }
    if route == STABLE_CLAIM_ROUTE:
        return {
            "component_id": "b1xhear_probe_component",
            "verdict": "uphold_existing",
            "effective_from_block_id": None,
            "revealed_at_block_id": None,
            "corrected_value": None,
            "evidence": [
                {
                    "block_id": block_id,
                    "quote": "Mara Vale is not the Mara Vale named on the old gate.",
                }
            ],
            "reason": "The supplied evidence does not establish a correction.",
            "resolution_condition": None,
        }
    raise B1CrossChapterAuditorProbeError("unknown hearing probe route")


def load_probe_profile_v1(path: Path = PROFILE_PATH) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as exc:
        raise B1CrossChapterAuditorProbeError(
            "cannot load cross-chapter Auditor probe profile"
        ) from exc
    if payload != _expected_profile():
        raise B1CrossChapterAuditorProbeError(
            "cross-chapter Auditor probe profile differs"
        )
    return deepcopy(payload)


def implementation_sha256_v1() -> str:
    rows = []
    for relative in _IMPLEMENTATION_PATHS:
        path = _REPO_ROOT / relative
        if not path.is_file():
            raise B1CrossChapterAuditorProbeError(
                f"implementation file is absent: {relative.as_posix()}"
            )
        rows.append({"path": relative.as_posix(), "sha256": file_sha256(path)})
    return canonical_sha256(rows)


def build_clean_implementation_binding_v1() -> dict[str, str]:
    if _git_text("status", "--short", "--untracked-files=no"):
        raise B1CrossChapterAuditorProbeError(
            "capability probe requires a clean tracked worktree"
        )
    return {
        "shared_core_revision": "de7c74ba348bffe507aa86933f438b3ed4c5af29",
        "consumer_revision": _git_text("rev-parse", "HEAD"),
        "consumer_implementation_sha256": implementation_sha256_v1(),
    }


def build_probe_plan_v1(
    *,
    route: str,
    probe_run_id: str,
    credential_commitment_sha256: str,
    issued_at_utc: str,
    implementation_binding: Mapping[str, str] | None = None,
) -> B1CrossChapterAuditorProbePlanV1:
    role_id = ROLE_ID_BY_ROUTE.get(route)
    if role_id is None:
        raise B1CrossChapterAuditorProbeError("unknown hearing probe route")
    profile = load_probe_profile_v1()
    runtime = load_literary_shared_runtime_profile_v2(
        RUNTIME_PROFILE_PATH,
        expected_role_ids={IDENTITY_ROLE_ID, STABLE_CLAIM_ROLE_ID},
    )
    source_binding = dict(runtime.source_binding_for(role_id))
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
    component, queue, source_blocks = synthetic_hearing_v1(route)
    preset = runtime.role_presets[role_id]
    model_contract = {
        "model_id": preset.requested_model_id,
        "reasoning_effort": profile["generation"]["reasoning_effort"],
        "temperature": profile["generation"]["temperature"],
        "seed": profile["generation"]["seed"],
        "max_output_tokens": profile["generation"]["max_completion_tokens"],
    }
    if route == IDENTITY_ROUTE:
        rendered = render_identity_hearing_request_v1(
            component,
            queue=queue,
            source_blocks=source_blocks,
            design_doc=DESIGN_DOC,
            model_contract=model_contract,
        )
    else:
        rendered = render_stable_claim_hearing_request_v1(
            component,
            queue=queue,
            source_blocks=source_blocks,
            design_doc=DESIGN_DOC,
            model_contract=model_contract,
        )
    request = project_capability_probe_request_v1(
        {
            "messages": [dict(row) for row in rendered["messages"]],
            "response_schema": response_schema_for_route_v1(route),
            "request_fingerprint": rendered["request_fingerprint"],
        },
        field_names_by_namespace=CROSS_CHAPTER_MODEL_REF_FIELDS_V1,
    )
    schema = deepcopy(dict(request["response_schema"]))
    validator_ref = bind_model_ref_validator_v1(validator_ref_for_route_v1(route))
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
        schema_name=SCHEMA_NAME_BY_ROUTE[route],
        structured_output=runtime.shared_structured_output_for(role_id),
        output_envelope=runtime.output_envelope_for(role_id),
        base_url=source["base_url"],
    )
    if request_body.get("response_format") != {"type": "json_object"}:
        raise B1CrossChapterAuditorProbeError("probe is not JSON-object mode")
    route_slug = "identity" if route == IDENTITY_ROUTE else "stable_claim"
    intent = {
        "capability_id": (
            f"modelapi_gpt54_literary_b1_cross_chapter_{route_slug}_json_object_v3"
        ),
        "capability_revision": (
            f"schema_{canonical_sha256(schema)[:8]}_"
            f"validator_{validator_ref['sha256'][:8]}_v3"
        ),
        "requested_model_id": profile["requested_model_id"],
        "accepted_observed_model_ids": profile["accepted_observed_model_ids"],
        "capability_kind": profile["capability_kind"],
        "schema_name": SCHEMA_NAME_BY_ROUTE[route],
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
        probe_profile_revision=f"{profile['profile_revision']}.{route_slug}",
        implementation_binding=binding,
        capability_intent=intent,
        response_schema=schema,
        request_body=request_body,
        limits=profile["limits"],
        issued_at_utc=issued_at_utc,
    )
    return B1CrossChapterAuditorProbePlanV1(
        route=route,
        role_id=role_id,
        profile=profile,
        source=source,
        component=component,
        request=request,
        response_schema=schema,
        validator_ref=validator_ref,
        request_body=request_body,
        implementation_binding=binding,
        seal=seal,
    )


def execute_probe_once_v1(
    *, probe: SharedLlmCapabilityProbe, plan: B1CrossChapterAuditorProbePlanV1
) -> dict[str, Any]:
    component, queue, source_blocks = synthetic_hearing_v1(plan.route)
    if plan.route == IDENTITY_ROUTE:
        rendered = render_identity_hearing_request_v1(
            component,
            queue=queue,
            source_blocks=source_blocks,
            design_doc=DESIGN_DOC,
            model_contract={
                "model_id": plan.profile["requested_model_id"],
                "reasoning_effort": plan.profile["generation"]["reasoning_effort"],
                "temperature": plan.profile["generation"]["temperature"],
                "seed": plan.profile["generation"]["seed"],
                "max_output_tokens": plan.profile["generation"][
                    "max_completion_tokens"
                ],
            },
        )
    else:
        rendered = render_stable_claim_hearing_request_v1(
            component,
            queue=queue,
            source_blocks=source_blocks,
            design_doc=DESIGN_DOC,
            model_contract={
                "model_id": plan.profile["requested_model_id"],
                "reasoning_effort": plan.profile["generation"]["reasoning_effort"],
                "temperature": plan.profile["generation"]["temperature"],
                "seed": plan.profile["generation"]["seed"],
                "max_output_tokens": plan.profile["generation"][
                    "max_completion_tokens"
                ],
            },
        )
    semantic_validator = make_hearing_semantic_validator_v1(
        component=component,
        rendered_request=rendered,
    )

    def validate(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        resolved = resolve_capability_probe_response_v1(
            projected_request=plan.request,
            response=payload,
            field_names_by_namespace=CROSS_CHAPTER_MODEL_REF_FIELDS_V1,
        )
        return semantic_validator(resolved)

    return probe.execute_once(
        seal=plan.seal,
        request_body=plan.request_body,
        local_validator=validate,
        local_validator_id=plan.validator_ref["id"],
        local_validator_sha256=plan.validator_ref["sha256"],
    )


def _expected_profile() -> dict[str, Any]:
    return {
        "schema_version": "literary_modelapi_b1_cross_chapter_auditor_probe_profile_v1",
        "profile_id": "literary_modelapi_b1_cross_chapter_auditor_probe_v1",
        "profile_revision": "modelapi_gpt54_b1_cross_chapter_schema_v3",
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
            "max_completion_tokens": 1200,
        },
        "limits": {
            "max_calls": 1,
            "max_prompt_utf8_bytes": 65536,
            "max_response_utf8_bytes": 32768,
            "max_prompt_tokens": 12000,
            "max_completion_tokens": 1200,
            "max_total_tokens": 13200,
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
        ["git", *args],
        cwd=_REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


__all__ = [
    "B1CrossChapterAuditorProbeError",
    "B1CrossChapterAuditorProbePlanV1",
    "DESIGN_DOC",
    "PROFILE_PATH",
    "RUNTIME_PROFILE_PATH",
    "build_clean_implementation_binding_v1",
    "build_probe_plan_v1",
    "execute_probe_once_v1",
    "implementation_sha256_v1",
    "load_probe_profile_v1",
    "synthetic_hearing_v1",
    "synthetic_persistent_response_v1",
]
