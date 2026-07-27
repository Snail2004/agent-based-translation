"""One-shot official OpenAI JSON-object qualification for Literary B1-Scan."""

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
from pipeline.literary.b1_scan_v1 import (
    b1_scan_response_schema_v1,
    make_b1_scan_semantic_validator_v1,
    render_b1_scan_request_v1,
    validate_b1_scan_response_v1,
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
from pipeline.literary.shared_llm_adapter_v1 import render_literary_request_body
from pipeline.literary.shared_runtime_profile_v2 import (
    LOCAL_VALIDATION_SCHEMA_DIALECT,
    load_literary_shared_runtime_profile_v2,
)


ROLE_ID = "literary.b1.scan"
RUNTIME_SOURCE_ALIAS = "openai_official_row2"
ACCEPTED_OBSERVED_MODEL_IDS = ("gpt-5.4", "gpt-5.4-2026-03-05")

_PIPELINE_ROOT = Path(__file__).resolve().parents[1]
_TOOL_ROOT = _PIPELINE_ROOT.parent
_REPO_ROOT = _TOOL_ROOT.parent
DEFAULT_PROFILE_PATH = (
    _PIPELINE_ROOT
    / "configs"
    / "literary_openai_b1_scan_json_object_probe_v1.json"
)
MODELAPI_PROFILE_PATH = (
    _PIPELINE_ROOT
    / "configs"
    / "literary_modelapi_b1_scan_json_object_probe_v1.json"
)
RUNTIME_PROFILE_PATH = (
    _PIPELINE_ROOT
    / "configs"
    / "literary_shared_llm_runtime_openai_b1_scan_v1.json"
)
MODELAPI_RUNTIME_PROFILE_PATH = (
    _PIPELINE_ROOT
    / "configs"
    / "literary_shared_llm_runtime_modelapi_b1_scan_v10_24k_16k_12k_memory.json"
)
DESIGN_DOC = _REPO_ROOT / "design" / "LITERARY_PROMPT_DESIGN.md"
_IMPLEMENTATION_PATHS = (
    Path("design/LITERARY_PROMPT_DESIGN.md"),
    Path("THESIS_RUNTIME_TOOL/pipeline/configs/literary_openai_b1_scan_json_object_probe_v1.json"),
    Path("THESIS_RUNTIME_TOOL/pipeline/configs/literary_modelapi_b1_scan_json_object_probe_v1.json"),
    Path("THESIS_RUNTIME_TOOL/pipeline/configs/literary_shared_llm_runtime_openai_b1_scan_v1.json"),
    Path("THESIS_RUNTIME_TOOL/pipeline/configs/literary_shared_llm_runtime_modelapi_b1_scan_v10_24k_16k_12k_memory.json"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/b1_scan_v1.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/model_ref_transport_v1.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/model_ref_v1.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/openai_b1_scan_capability_probe_v1.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/shared_llm_adapter_v1.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/shared_runtime_profile_v1.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/shared_runtime_profile_v2.py"),
)


class LiteraryOpenAiB1ScanProbeError(RuntimeError):
    pass


@dataclass(frozen=True)
class LiteraryOpenAiB1ScanProbePlanV1:
    profile: Mapping[str, Any]
    source: Mapping[str, Any]
    request: Mapping[str, Any]
    response_schema: Mapping[str, Any]
    validator_ref: Mapping[str, str]
    request_body: Mapping[str, Any]
    implementation_binding: Mapping[str, str]
    seal: Mapping[str, Any]


def synthetic_b1_scan_chapter_v1() -> dict[str, Any]:
    return {
        "chapter_id": "literary_b1_scan_probe_chapter",
        "blocks": [
            {
                "block_id": "literary_b1_scan_probe_b001",
                "order_index": 1,
                "clean_text": "Mara Vale entered North House.",
            }
        ],
    }


def synthetic_b1_scan_response_v1() -> dict[str, Any]:
    return {
        "schema_id": "LiteraryB1ScanOutputV1",
        "chapter_id": "literary_b1_scan_probe_chapter",
        "entity_observations": [
            {
                "surface": "Mara Vale",
                "source_block_ids": ["literary_b1_scan_probe_b001"],
                "referent_kind_claim": "person",
                "record_class": "named_entity_candidate",
                "presence_basis": "direct_presence",
                "scan_note": "A named participant entering the house.",
            }
        ],
        "glossary_observations": [],
        "prior_continuity_proposals": [],
    }


def b1_scan_validator_ref_v1() -> dict[str, str]:
    return build_literary_code_ref_v1(
        identifier="literary.b1.scan.validator",
        revision="v1",
        callables=(
            b1_scan_response_schema_v1,
            validate_b1_scan_response_v1,
            make_b1_scan_semantic_validator_v1,
        ),
    )


def load_probe_profile_v1(path: Path = DEFAULT_PROFILE_PATH) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as exc:
        raise LiteraryOpenAiB1ScanProbeError("cannot load B1-Scan probe profile") from exc
    if payload not in _expected_profiles():
        raise LiteraryOpenAiB1ScanProbeError("B1-Scan probe profile differs")
    return deepcopy(payload)


def implementation_sha256_v1() -> str:
    rows = []
    for relative in _IMPLEMENTATION_PATHS:
        path = _REPO_ROOT / relative
        if not path.is_file():
            raise LiteraryOpenAiB1ScanProbeError(
                f"B1-Scan implementation file is absent: {relative.as_posix()}"
            )
        rows.append({"path": relative.as_posix(), "sha256": file_sha256(path)})
    return canonical_sha256(rows)


def build_clean_implementation_binding_v1() -> dict[str, str]:
    status = _git_text("status", "--short", "--untracked-files=no")
    if status:
        raise LiteraryOpenAiB1ScanProbeError(
            "B1-Scan capability probe requires a clean tracked worktree"
        )
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
    profile_path: Path = DEFAULT_PROFILE_PATH,
    runtime_profile_path: Path = RUNTIME_PROFILE_PATH,
) -> LiteraryOpenAiB1ScanProbePlanV1:
    profile = load_probe_profile_v1(profile_path)
    runtime = load_literary_shared_runtime_profile_v2(
        runtime_profile_path, expected_role_ids={ROLE_ID}
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
    chapter = synthetic_b1_scan_chapter_v1()
    preset = runtime.role_presets[ROLE_ID]
    rendered = render_b1_scan_request_v1(
        chapter=chapter,
        design_doc=DESIGN_DOC,
        model_id=profile["requested_model_id"],
        max_output_tokens=profile["generation"]["max_completion_tokens"],
        memory_token_budget=preset.generation.get("memory_token_budget"),
        memory_dormancy_chapters=preset.generation.get(
            "memory_dormancy_chapters", 3
        ),
        chapter_order_by_id={chapter["chapter_id"]: 1},
    )
    request = project_capability_probe_request_v1({
        "messages": [dict(row) for row in rendered.messages],
        "response_schema": b1_scan_response_schema_v1(),
        "request_fingerprint": rendered.request_fingerprint,
    })
    schema = deepcopy(dict(request["response_schema"]))
    validator_ref = bind_model_ref_validator_v1(b1_scan_validator_ref_v1())
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
        raise LiteraryOpenAiB1ScanProbeError("B1-Scan probe is not JSON-object mode")
    intent = {
        "capability_id": (
            "openai_row2_gpt54_literary_b1_scan_json_object_v1"
            if source["source_id"] == "openai_official_row2_v1"
            else "modelapi_gpt54_literary_b1_scan_json_object_v1"
        ),
        "capability_revision": (
            f"schema_{canonical_sha256(schema)[:8]}_"
            f"validator_{validator_ref['sha256'][:8]}_v1"
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
    return LiteraryOpenAiB1ScanProbePlanV1(
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
    *, probe: SharedLlmCapabilityProbe, plan: LiteraryOpenAiB1ScanProbePlanV1
) -> dict[str, Any]:
    chapter = synthetic_b1_scan_chapter_v1()
    rendered = render_b1_scan_request_v1(chapter=chapter, design_doc=DESIGN_DOC)
    semantic_validator = make_b1_scan_semantic_validator_v1(
        chapter=chapter, rendered=rendered
    )

    def validator(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        resolved = resolve_capability_probe_response_v1(
            projected_request=plan.request,
            response=payload,
        )
        return semantic_validator(resolved)

    return probe.execute_once(
        seal=plan.seal,
        request_body=plan.request_body,
        local_validator=validator,
        local_validator_id=plan.validator_ref["id"],
        local_validator_sha256=plan.validator_ref["sha256"],
    )


def _expected_profiles() -> list[dict[str, Any]]:
    common = {
        "role_id": ROLE_ID,
        "requested_model_id": "gpt-5.4",
        "capability_kind": "json_object",
        "schema_name": "literary_b1_scan_response_v1",
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
    return [
        {
        "schema_version": "literary_openai_b1_scan_json_object_probe_profile_v1",
        "profile_id": "literary_openai_official_b1_scan_json_object_probe_v1",
        "profile_revision": "openai_row2_gpt54_b1_scan_schema_v1",
        "accepted_observed_model_ids": list(ACCEPTED_OBSERVED_MODEL_IDS),
            **deepcopy(common),
        },
        {
            "schema_version": "literary_modelapi_b1_scan_json_object_probe_profile_v1",
            "profile_id": "literary_modelapi_b1_scan_json_object_probe_v1",
            "profile_revision": "modelapi_gpt54_b1_scan_schema_v1",
            "accepted_observed_model_ids": ["gpt-5.4"],
            **deepcopy(common),
        },
    ]


def _git_text(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=_REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


__all__ = [
    "DEFAULT_PROFILE_PATH",
    "DESIGN_DOC",
    "MODELAPI_PROFILE_PATH",
    "MODELAPI_RUNTIME_PROFILE_PATH",
    "ROLE_ID",
    "RUNTIME_PROFILE_PATH",
    "LiteraryOpenAiB1ScanProbeError",
    "LiteraryOpenAiB1ScanProbePlanV1",
    "b1_scan_validator_ref_v1",
    "build_clean_implementation_binding_v1",
    "build_probe_plan_v1",
    "execute_probe_once_v1",
    "implementation_sha256_v1",
    "load_probe_profile_v1",
    "synthetic_b1_scan_chapter_v1",
    "synthetic_b1_scan_response_v1",
]
