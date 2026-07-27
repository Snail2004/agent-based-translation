"""One-shot ModelAPI qualification for B4 Editorial Review."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
import subprocess
from typing import Any, Mapping

from pipeline.llm_backend import (
    SharedLlmCapabilityProbe,
    canonical_sha256,
    create_capability_probe_seal,
)
from pipeline.literary.b4_editorial_review_v1 import (
    PACKET_SCHEMA_VERSION,
    RESPONSE_SCHEMA_VERSION,
    ROLE_ID,
    RenderedEditorialReviewV1,
    render_editorial_review_request_v1,
    validate_editorial_review_response_v1,
)
from pipeline.literary.chapter_cycle_shared_runtime_v1 import (
    build_literary_code_ref_v1,
)
from pipeline.literary.checkpoint import canonical_hash, file_sha256
from pipeline.literary.shared_llm_adapter_v1 import (
    render_literary_request_body,
    resolve_transport_response_schema,
)
from pipeline.literary.shared_runtime_profile_v2 import (
    LOCAL_VALIDATION_SCHEMA_DIALECT,
    load_literary_shared_runtime_profile_v2,
)


PROFILE_ID = "literary_modelapi_b4_editorial_capability_probe_v1"
PROFILE_REVISION = "modelapi_gpt54_b4_editorial_json_object_v1"
SOURCE_ALIAS = "modelapi_shared"
STYLE_VERSION = "literary_style_profile_editorial_probe_v1"
STYLE_PROFILE = (
    "Use restrained Vietnamese literary prose for this capability probe.\n"
    f"- Prompt version: {STYLE_VERSION}."
)

_PIPELINE_ROOT = Path(__file__).resolve().parents[1]
_TOOL_ROOT = _PIPELINE_ROOT.parent
_REPO_ROOT = _TOOL_ROOT.parent
RUNTIME_PROFILE_PATH = (
    _PIPELINE_ROOT
    / "configs"
    / "literary_shared_llm_runtime_modelapi_b4_editorial_v1.json"
)
_IMPLEMENTATION_PATHS = (
    Path(
        "THESIS_RUNTIME_TOOL/pipeline/configs/"
        "literary_shared_llm_runtime_modelapi_b4_editorial_v1.json"
    ),
    Path(
        "THESIS_RUNTIME_TOOL/pipeline/literary/"
        "b4_editorial_review_v1.py"
    ),
    Path(
        "THESIS_RUNTIME_TOOL/pipeline/literary/"
        "b4_editorial_live_modelapi_v1.py"
    ),
    Path(
        "THESIS_RUNTIME_TOOL/pipeline/literary/"
        "b4_live_modelapi_v1.py"
    ),
    Path(
        "THESIS_RUNTIME_TOOL/pipeline/literary/"
        "modelapi_b4_editorial_capability_probe_v1.py"
    ),
    Path(
        "THESIS_RUNTIME_TOOL/pipeline/scripts/"
        "run_literary_b4_editorial_live_modelapi_v1.py"
    ),
    Path(
        "THESIS_RUNTIME_TOOL/pipeline/scripts/"
        "run_literary_b4_editorial_review_v1.py"
    ),
)


class LiteraryModelApiB4EditorialProbeError(RuntimeError):
    pass


@dataclass(frozen=True)
class LiteraryModelApiB4EditorialProbePlanV1:
    source: Mapping[str, Any]
    request: Mapping[str, Any]
    rendered: RenderedEditorialReviewV1
    response_schema: Mapping[str, Any]
    validator_ref: Mapping[str, str]
    request_body: Mapping[str, Any]
    implementation_binding: Mapping[str, str]
    seal: Mapping[str, Any]


def build_probe_plan_v1(
    *,
    probe_run_id: str,
    credential_commitment_sha256: str,
    issued_at_utc: str,
    implementation_binding: Mapping[str, str] | None = None,
) -> LiteraryModelApiB4EditorialProbePlanV1:
    runtime = load_literary_shared_runtime_profile_v2(
        RUNTIME_PROFILE_PATH,
        expected_role_ids={ROLE_ID},
    )
    source_binding = dict(runtime.source_binding_for(ROLE_ID))
    if (
        source_binding.get("source_alias") != SOURCE_ALIAS
        or source_binding.get("authority_class") != "third_party"
        or source_binding.get("base_url") != "https://modelapi.vn/v1"
        or source_binding.get("fallback_enabled") is not False
    ):
        raise LiteraryModelApiB4EditorialProbeError(
            "B4 Editorial ModelAPI source binding differs"
        )
    source = _source_record(
        source_binding,
        credential_commitment_sha256,
    )
    rendered = synthetic_probe_rendered_v1()
    request = {
        "messages": [dict(row) for row in rendered.messages],
        "response_schema": deepcopy(rendered.response_schema),
        "request_fingerprint": rendered.request_fingerprint,
    }
    envelope = runtime.output_envelope_for(ROLE_ID)
    transport_schema, _omissions = resolve_transport_response_schema(
        response_schema=rendered.response_schema,
        protocol=str(source["protocol"]),
        output_envelope=envelope,
    )
    validator_ref = validator_ref_v1()
    preset = runtime.role_presets[ROLE_ID]
    generation = deepcopy(dict(preset.generation))
    generation["max_output_tokens"] = 1_500
    request_body = render_literary_request_body(
        preset=replace(preset, generation=generation),
        protocol=str(source["protocol"]),
        capability={"capability_kind": "json_object"},
        messages=request["messages"],
        response_schema=transport_schema,
        instruction_schema=rendered.response_schema,
        schema_name="literary_b4_editorial_review_probe_v1",
        structured_output=runtime.shared_structured_output_for(ROLE_ID),
        output_envelope=envelope,
        base_url=source["base_url"],
    )
    if request_body.get("response_format") != {"type": "json_object"}:
        raise LiteraryModelApiB4EditorialProbeError(
            "B4 Editorial probe must use JSON-object mode"
        )
    schema_sha = canonical_sha256(transport_schema)
    intent = {
        "capability_id": "modelapi_gpt54_literary_b4_editorial_review_v1",
        "capability_revision": (
            f"schema_{schema_sha[:8]}_"
            f"validator_{validator_ref['sha256'][:8]}_v1"
        ),
        "requested_model_id": "gpt-5.4",
        "accepted_observed_model_ids": ["gpt-5.4"],
        "capability_kind": "json_object",
        "schema_name": RESPONSE_SCHEMA_VERSION,
        "schema_dialect": LOCAL_VALIDATION_SCHEMA_DIALECT,
        "schema_sha256": schema_sha,
        "local_validator_id": validator_ref["id"],
        "local_validator_sha256": validator_ref["sha256"],
    }
    binding = dict(
        implementation_binding or build_clean_implementation_binding_v1()
    )
    limits = {
        "max_calls": 1,
        "max_prompt_utf8_bytes": 65_536,
        "max_response_utf8_bytes": 65_536,
        "max_prompt_tokens": 8_000,
        "max_completion_tokens": 1_500,
        "max_total_tokens": 9_500,
        "request_timeout_ms": 120_000,
    }
    seal = create_capability_probe_seal(
        source=source,
        consumer_workstream="literary",
        role_id=ROLE_ID,
        probe_run_id=probe_run_id,
        probe_profile_id=PROFILE_ID,
        probe_profile_revision=PROFILE_REVISION,
        implementation_binding=binding,
        capability_intent=intent,
        response_schema=transport_schema,
        request_body=request_body,
        limits=limits,
        issued_at_utc=issued_at_utc,
    )
    return LiteraryModelApiB4EditorialProbePlanV1(
        source=source,
        request=request,
        rendered=rendered,
        response_schema=transport_schema,
        validator_ref=validator_ref,
        request_body=request_body,
        implementation_binding=binding,
        seal=seal,
    )


def execute_probe_once_v1(
    *,
    probe: SharedLlmCapabilityProbe,
    plan: LiteraryModelApiB4EditorialProbePlanV1,
) -> dict[str, Any]:
    return probe.execute_once(
        seal=plan.seal,
        request_body=plan.request_body,
        local_validator=lambda payload: validate_editorial_review_response_v1(
            rendered=plan.rendered,
            response=payload,
        ),
        local_validator_id=plan.validator_ref["id"],
        local_validator_sha256=plan.validator_ref["sha256"],
    )


def validator_ref_v1() -> dict[str, str]:
    return build_literary_code_ref_v1(
        identifier="literary.b4.editorial_review.validator",
        revision="v1",
        callables=[validate_editorial_review_response_v1],
    )


def synthetic_probe_rendered_v1() -> RenderedEditorialReviewV1:
    packet_body = {
        "schema_version": PACKET_SCHEMA_VERSION,
        "book_id": "probe_book",
        "chapter_id": "probe_ch01",
        "batch_index": 1,
        "batch_count": 1,
        "selection_mode": "all_blocks",
        "source_translation_artifact_hash": "1" * 64,
        "translator_pack_artifact_hash": "2" * 64,
        "lint_report_artifact_hash": "3" * 64,
        "style_profile_version": STYLE_VERSION,
        "style_profile_sha256": canonical_hash(STYLE_PROFILE),
        "candidate_block_ids": ["probe_ch01_b001"],
        "candidates": [
            {
                "block_id": "probe_ch01_b001",
                "block_order": 1,
                "source_text": "Good evening, sir.",
                "current_target_text": "Chào buổi tối, thưa ông.",
                "selection_reasons": ["full_review"],
                "tier1_findings": [],
            }
        ],
        "neighbor_context": [
            {
                "block_id": "probe_ch01_b001",
                "block_order": 1,
                "source_text": "Good evening, sir.",
                "current_target_text": "Chào buổi tối, thưa ông.",
                "candidate": True,
            }
        ],
        "pack_context": {
            "schema_version": "literary_b4_editorial_pack_context_v1",
            "source_translator_pack_artifact_hash": "2" * 64,
            "effective_entity_ids": [],
            "entities": [],
            "relations": [],
            "states": [],
            "idiolect": [],
            "narrative_position": {"frames": [], "capsules": []},
            "open_questions": {},
            "speaker_turns": [],
            "address_pairs": [],
        },
        "provider_calls": 0,
        "semantic_record_mutation_performed": False,
    }
    packet = {
        **packet_body,
        "artifact_hash": canonical_hash(packet_body),
    }
    return render_editorial_review_request_v1(
        review_packet=packet,
        style_profile=STYLE_PROFILE,
    )


def synthetic_probe_response_v1() -> dict[str, Any]:
    return {
        "schema_version": RESPONSE_SCHEMA_VERSION,
        "blocks": [
            {
                "block_id": "probe_ch01_b001",
                "quality_score": 0.95,
                "suggested_action": "accept",
                "proposed_target_text": "Chào buổi tối, thưa ông.",
                "issues": [],
            }
        ],
    }


def build_clean_implementation_binding_v1() -> dict[str, str]:
    if _git_text("status", "--short", "--untracked-files=no"):
        raise LiteraryModelApiB4EditorialProbeError(
            "B4 Editorial probe requires a clean tracked worktree"
        )
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
            raise LiteraryModelApiB4EditorialProbeError(
                "B4 Editorial implementation file is absent: "
                f"{relative.as_posix()}"
            )
        rows.append(
            {"path": relative.as_posix(), "sha256": file_sha256(path)}
        )
    return canonical_sha256(rows)


def _source_record(
    binding: Mapping[str, Any],
    commitment: str,
) -> dict[str, Any]:
    if (
        len(commitment) != 64
        or any(char not in "0123456789abcdef" for char in commitment)
    ):
        raise LiteraryModelApiB4EditorialProbeError(
            "credential commitment is malformed"
        )
    return {
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
        "credential_commitment": commitment,
        "physical_quota_bucket_id": binding["physical_quota_bucket_id"],
        "enabled": True,
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
    "RUNTIME_PROFILE_PATH",
    "build_probe_plan_v1",
    "execute_probe_once_v1",
    "implementation_sha256_v1",
    "synthetic_probe_rendered_v1",
    "synthetic_probe_response_v1",
    "validator_ref_v1",
]
