"""One-shot ModelAPI JSON-object qualification for Literary B1-Enrich."""

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
from pipeline.literary.b1_enrich_v1 import (
    b1_enrich_response_schema_v1,
    make_b1_enrich_semantic_validator_v1,
    render_b1_enrich_request_v1,
    validate_b1_enrich_capability_payload_v1,
    validate_b1_enrich_response_v1,
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


ROLE_ID = "literary.b1.enrich"
_PIPELINE_ROOT = Path(__file__).resolve().parents[1]
_TOOL_ROOT = _PIPELINE_ROOT.parent
_REPO_ROOT = _TOOL_ROOT.parent
PROFILE_PATH = _PIPELINE_ROOT / "configs" / "literary_modelapi_b1_enrich_json_object_probe_v3.json"
RUNTIME_PROFILE_PATH = (
    _PIPELINE_ROOT
    / "configs"
    / "literary_shared_llm_runtime_modelapi_b1_enrich_v4.json"
)
DESIGN_DOC = _REPO_ROOT / "design" / "LITERARY_PROMPT_DESIGN.md"
_IMPLEMENTATION_PATHS = (
    Path("design/LITERARY_PROMPT_DESIGN.md"),
    Path("THESIS_RUNTIME_TOOL/pipeline/configs/literary_modelapi_b1_enrich_json_object_probe_v3.json"),
    Path("THESIS_RUNTIME_TOOL/pipeline/configs/literary_shared_llm_runtime_modelapi_b1_enrich_v4.json"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/b1_enrich_v1.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/model_ref_transport_v1.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/model_ref_v1.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/modelapi_b1_enrich_capability_probe_v1.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/shared_llm_adapter_v1.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/shared_runtime_profile_v2.py"),
)


class B1EnrichProbeError(RuntimeError):
    pass


@dataclass(frozen=True)
class B1EnrichProbePlanV1:
    profile: Mapping[str, Any]
    source: Mapping[str, Any]
    request: Mapping[str, Any]
    response_schema: Mapping[str, Any]
    validator_ref: Mapping[str, str]
    request_body: Mapping[str, Any]
    implementation_binding: Mapping[str, str]
    seal: Mapping[str, Any]


def synthetic_chapter_v1() -> dict[str, Any]:
    """A miniature chapter shaped like the real thing, in a neutral book.

    A single sentence used to be enough to prove the transport worked, but it
    starved the model of context: with almost nothing to read it copied the
    ``bk_ch01`` placeholder out of the prompt's example line instead of the
    chapter it was given, and the probe failed on a mistake no real chapter has
    ever produced.  Several ordered blocks with two distinguishable referents
    match production conditions.

    The chapter id follows the real ``<book>_ch<NN>`` shape but is deliberately
    NOT the id used in the prompt example, so copying the example still fails.
    """

    lines = (
        "The traveler was Mara Vale, who came up the lane before the rain reached the valley.",
        "North House stood with its shutters closed against the weather.",
        "An older man waited under the porch and did not offer his name.",
        "She asked after the tenant; he answered that the tenant was away.",
        "Mara Vale said she would wait, and sat down on the cold step.",
    )
    return {
        "chapter_id": "pv_ch03",
        "blocks": [
            {
                "block_id": f"pv_ch03_b{index:03d}",
                "order_index": index,
                "clean_text": text,
            }
            for index, text in enumerate(lines, start=1)
        ],
    }


def synthetic_scan_artifact_v1() -> dict[str, Any]:
    """Two tasks of different kinds, so the probe covers more than one branch.

    A person task exercises the required gender/life_stage checks; a place task
    exercises the branch where those checks do not apply.  One task could pass
    while the other silently could not.
    """

    return {
        "artifact_hash": "a" * 64,
        "chapter_id": "pv_ch03",
        "entity_observations": [
            {
                "observation_id": "b1obs_mara",
                "surface": "Mara Vale",
                "source_block_ids": ["pv_ch03_b001", "pv_ch03_b005"],
                "referent_kind_claim": "person",
                "record_class": "named_entity_candidate",
                "presence_basis": "direct_presence",
                "scan_note": "Named participant arriving and waiting.",
            },
            {
                "observation_id": "b1obs_house",
                "surface": "North House",
                "source_block_ids": ["pv_ch03_b002"],
                "referent_kind_claim": "place",
                "record_class": "named_entity_candidate",
                "presence_basis": "direct_presence",
                "scan_note": "Named dwelling in the current scene.",
            },
            {
                "observation_id": "b1obs_traveler",
                "surface": "The traveler",
                "source_block_ids": ["pv_ch03_b001"],
                "referent_kind_claim": "person",
                "record_class": "important_unnamed_referent",
                "presence_basis": "direct_presence",
                "scan_note": "An individualized description linked in the chapter.",
            },
        ],
        "glossary_observations": [],
    }


def synthetic_response_v1() -> dict[str, Any]:
    """The shape a compliant answer takes, covering both fixture tasks."""

    def _unclear(field: str, block_id: str) -> dict[str, Any]:
        return {
            "field": field,
            "status": "unclear",
            "value": None,
            "basis": None,
            "anchor_block_ids": [block_id],
            "story_time_note": None,
        }

    return {
        "schema_id": "LiteraryB1EnrichOutputV1",
        "chapter_id": "pv_ch03",
        "entities": [
            {
                "scan_observation_id": "b1obs_mara",
                "claims": [
                    _unclear("gender", "pv_ch03_b001"),
                    _unclear("life_stage", "pv_ch03_b001"),
                ],
                "kinship_links": [],
                "links": [],
                "address_forms_used": [],
                "aliases_observed": [],
                "identity_summary": "A named person arriving at the house; further identity is unresolved.",
                "distinguishing_note": None,
            },
            {
                "scan_observation_id": "b1obs_house",
                "claims": [_unclear("place_type", "pv_ch03_b002")],
                "kinship_links": [],
                "links": [],
                "address_forms_used": [],
                "aliases_observed": [],
                "identity_summary": "A named dwelling present in the current scene.",
                "distinguishing_note": None,
            },
            {
                "scan_observation_id": "b1obs_traveler",
                "claims": [
                    _unclear("gender", "pv_ch03_b001"),
                    _unclear("life_stage", "pv_ch03_b001"),
                ],
                "kinship_links": [],
                "links": [],
                "address_forms_used": [],
                "aliases_observed": [],
                "identity_summary": "A traveler explicitly identified in this chapter as Mara Vale.",
                "distinguishing_note": None,
            },
        ],
        "additional_entities": [],
        "spurious_challenges": [],
        "same_referent_proposals": [
            {
                "subject_ref": "scan:b1obs_traveler",
                "target_ref": "scan:b1obs_mara",
                "proposal_basis": "chapter_context_description",
                "source_block_ids": ["pv_ch03_b001"],
                "reason": "The chapter explicitly identifies the traveler as Mara Vale.",
            }
        ],
        "conflict_findings": [],
        "presence_correction_findings": [],
        "glossary_items": [],
    }


def validator_ref_v1() -> dict[str, str]:
    return build_literary_code_ref_v1(
        identifier="literary.b1.enrich.validator",
        revision="v3",
        callables=(
            b1_enrich_response_schema_v1,
            validate_b1_enrich_capability_payload_v1,
            validate_b1_enrich_response_v1,
            make_b1_enrich_semantic_validator_v1,
        ),
    )


def load_probe_profile_v1(path: Path = PROFILE_PATH) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as exc:
        raise B1EnrichProbeError("cannot load B1-Enrich probe profile") from exc
    if payload != _expected_profile():
        raise B1EnrichProbeError("B1-Enrich probe profile differs")
    return deepcopy(payload)


def implementation_sha256_v1() -> str:
    rows = []
    for relative in _IMPLEMENTATION_PATHS:
        path = _REPO_ROOT / relative
        if not path.is_file():
            raise B1EnrichProbeError(f"implementation file is absent: {relative.as_posix()}")
        rows.append({"path": relative.as_posix(), "sha256": file_sha256(path)})
    return canonical_sha256(rows)


def build_clean_implementation_binding_v1() -> dict[str, str]:
    if _git_text("status", "--short", "--untracked-files=no"):
        raise B1EnrichProbeError("capability probe requires a clean tracked worktree")
    return {
        "shared_core_revision": "de7c74ba348bffe507aa86933f438b3ed4c5af29",
        "consumer_revision": _git_text("rev-parse", "HEAD"),
        "consumer_implementation_sha256": implementation_sha256_v1(),
    }


def build_probe_plan_v1(
    *,
    probe_run_id: str,
    credential_commitment_sha256: str,
    issued_at_utc: str,
    implementation_binding: Mapping[str, str] | None = None,
) -> B1EnrichProbePlanV1:
    profile = load_probe_profile_v1()
    runtime = load_literary_shared_runtime_profile_v2(
        RUNTIME_PROFILE_PATH, expected_role_ids={ROLE_ID}
    )
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
    chapter = synthetic_chapter_v1()
    scan = synthetic_scan_artifact_v1()
    rendered = render_b1_enrich_request_v1(
        chapter=chapter,
        scan_artifact=scan,
        design_doc=DESIGN_DOC,
        model_id=profile["requested_model_id"],
        max_output_tokens=profile["generation"]["max_completion_tokens"],
    )
    request = project_capability_probe_request_v1({
        "messages": [dict(row) for row in rendered.messages],
        "response_schema": b1_enrich_response_schema_v1(),
        "request_fingerprint": rendered.request_fingerprint,
    })
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
        raise B1EnrichProbeError("probe is not JSON-object mode")
    intent = {
        "capability_id": "modelapi_gpt54_literary_b1_enrich_json_object_v3",
        "capability_revision": (
            f"schema_{canonical_sha256(schema)[:8]}_validator_{validator_ref['sha256'][:8]}_v3"
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
    binding = dict(implementation_binding or build_clean_implementation_binding_v1())
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
    return B1EnrichProbePlanV1(
        profile=profile,
        source=source,
        request=request,
        response_schema=schema,
        validator_ref=validator_ref,
        request_body=request_body,
        implementation_binding=binding,
        seal=seal,
    )


def execute_probe_once_v1(
    *, probe: SharedLlmCapabilityProbe, plan: B1EnrichProbePlanV1
) -> dict[str, Any]:
    chapter = synthetic_chapter_v1()
    scan = synthetic_scan_artifact_v1()
    rendered = render_b1_enrich_request_v1(
        chapter=chapter, scan_artifact=scan, design_doc=DESIGN_DOC
    )

    def validate(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        resolved = resolve_capability_probe_response_v1(
            projected_request=plan.request,
            response=payload,
        )
        return validate_b1_enrich_capability_payload_v1(
            resolved,
            chapter=chapter,
            scan_artifact=scan,
            request_fingerprint=rendered.request_fingerprint,
        )

    return probe.execute_once(
        seal=plan.seal,
        request_body=plan.request_body,
        local_validator=validate,
        local_validator_id=plan.validator_ref["id"],
        local_validator_sha256=plan.validator_ref["sha256"],
    )


def _expected_profile() -> dict[str, Any]:
    return {
        "schema_version": "literary_modelapi_b1_enrich_json_object_probe_profile_v1",
        "profile_id": "literary_modelapi_b1_enrich_json_object_probe_v3",
        "profile_revision": "modelapi_gpt54_b1_enrich_schema_v3",
        "role_id": ROLE_ID,
        "requested_model_id": "gpt-5.4",
        "accepted_observed_model_ids": ["gpt-5.4"],
        "capability_kind": "json_object",
        "schema_name": "literary_b1_enrich_response_v1",
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
            "max_prompt_tokens": 16000,
            "max_completion_tokens": 2048,
            "max_total_tokens": 18048,
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
    "DESIGN_DOC",
    "PROFILE_PATH",
    "ROLE_ID",
    "RUNTIME_PROFILE_PATH",
    "B1EnrichProbeError",
    "B1EnrichProbePlanV1",
    "build_clean_implementation_binding_v1",
    "build_probe_plan_v1",
    "execute_probe_once_v1",
    "synthetic_chapter_v1",
    "synthetic_response_v1",
    "synthetic_scan_artifact_v1",
    "validator_ref_v1",
]
