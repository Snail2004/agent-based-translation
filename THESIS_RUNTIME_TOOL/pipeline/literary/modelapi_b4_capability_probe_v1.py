"""One-shot ModelAPI qualification for the two live B4 roles."""

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
from pipeline.literary.b4_address_anchor_v1 import (
    ROLE_ID as ADDRESS_ROLE_ID,
    build_address_anchor_artifact_v1,
    render_address_anchor_request_v1,
    validate_address_anchor_response_v1,
)
from pipeline.literary.b4_story_bible_assembler_v1 import (
    ANCHOR_INPUT_SCHEMA_VERSION,
    ANCHOR_OUTPUT_SCHEMA_VERSION,
    SCHEMA_VERSION as STORY_BIBLE_SCHEMA_VERSION,
    WINDOW_SCHEMA_VERSION,
)
from pipeline.literary.b4_translator_v1 import (
    RESPONSE_SCHEMA_VERSION,
    ROLE_ID as TRANSLATOR_ROLE_ID,
    render_translation_window_request_v1,
    validate_translation_window_response_v1,
)
from pipeline.literary.b4_translator_pack_v1 import (
    project_translator_pack_tiered_v2,
    seal_translator_pack_v1,
)
from pipeline.literary.chapter_cycle_shared_runtime_v1 import (
    build_literary_code_ref_v1,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json, file_sha256
from pipeline.literary.shared_llm_adapter_v1 import (
    render_literary_request_body,
    resolve_transport_response_schema,
)
from pipeline.literary.shared_runtime_profile_v2 import (
    LOCAL_VALIDATION_SCHEMA_DIALECT,
    load_literary_shared_runtime_profile_v2,
)


RUNTIME_ROLE_IDS = frozenset({ADDRESS_ROLE_ID, TRANSLATOR_ROLE_ID})
PROFILE_ID = "literary_modelapi_b4_capability_probe_v1"
PROFILE_REVISION = "modelapi_gpt54_b4_json_object_v1"
SOURCE_ALIAS = "modelapi_shared"
STYLE_VERSION = "literary_style_profile_probe_v1"
STYLE_PROFILE = (
    "Use restrained Vietnamese for this capability probe.\n"
    f"- Prompt version: {STYLE_VERSION}."
)

_PIPELINE_ROOT = Path(__file__).resolve().parents[1]
_TOOL_ROOT = _PIPELINE_ROOT.parent
_REPO_ROOT = _TOOL_ROOT.parent
RUNTIME_PROFILE_PATH = (
    _PIPELINE_ROOT
    / "configs"
    / "literary_shared_llm_runtime_modelapi_b4_live_pilot_v1.json"
)
_IMPLEMENTATION_PATHS = (
    Path(
        "THESIS_RUNTIME_TOOL/pipeline/configs/"
        "literary_shared_llm_runtime_modelapi_b4_live_pilot_v1.json"
    ),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/b4_address_anchor_v1.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/b4_live_modelapi_v1.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/b4_story_bible_assembler_v1.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/b4_translator_v1.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/b4_translator_pack_v1.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/literary/modelapi_b4_capability_probe_v1.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/scripts/run_literary_b4_live_modelapi_v1.py"),
    Path("THESIS_RUNTIME_TOOL/pipeline/scripts/run_literary_b4_translator_pack_v1.py"),
)


class LiteraryModelApiB4ProbeError(RuntimeError):
    pass


@dataclass(frozen=True)
class LiteraryModelApiB4ProbePlanV1:
    role_id: str
    source: Mapping[str, Any]
    request: Mapping[str, Any]
    rendered: Any
    response_schema: Mapping[str, Any]
    validator_ref: Mapping[str, str]
    request_body: Mapping[str, Any]
    implementation_binding: Mapping[str, str]
    seal: Mapping[str, Any]


def build_probe_plan_v1(
    *,
    role_id: str,
    probe_run_id: str,
    credential_commitment_sha256: str,
    issued_at_utc: str,
    implementation_binding: Mapping[str, str] | None = None,
) -> LiteraryModelApiB4ProbePlanV1:
    if role_id not in RUNTIME_ROLE_IDS:
        raise LiteraryModelApiB4ProbeError(f"unsupported B4 role: {role_id}")
    runtime = load_literary_shared_runtime_profile_v2(
        RUNTIME_PROFILE_PATH,
        expected_role_ids=RUNTIME_ROLE_IDS,
    )
    source_binding = dict(runtime.source_binding_for(role_id))
    if (
        source_binding.get("source_alias") != SOURCE_ALIAS
        or source_binding.get("authority_class") != "third_party"
        or source_binding.get("base_url") != "https://modelapi.vn/v1"
        or source_binding.get("fallback_enabled") is not False
    ):
        raise LiteraryModelApiB4ProbeError("B4 ModelAPI source binding differs")
    source = _source_record(source_binding, credential_commitment_sha256)
    rendered = synthetic_probe_rendered_v1(role_id)
    request = {
        "messages": [dict(row) for row in rendered.messages],
        "response_schema": deepcopy(rendered.response_schema),
        "request_fingerprint": rendered.request_fingerprint,
    }
    envelope = runtime.output_envelope_for(role_id)
    transport_schema, _omissions = resolve_transport_response_schema(
        response_schema=rendered.response_schema,
        protocol=str(source["protocol"]),
        output_envelope=envelope,
    )
    validator_ref = validator_ref_v1(role_id)
    preset = runtime.role_presets[role_id]
    generation = deepcopy(dict(preset.generation))
    generation["max_output_tokens"] = 1_500
    request_body = render_literary_request_body(
        preset=replace(preset, generation=generation),
        protocol=str(source["protocol"]),
        capability={"capability_kind": "json_object"},
        messages=request["messages"],
        response_schema=transport_schema,
        instruction_schema=rendered.response_schema,
        schema_name=(
            "literary_b4_address_anchor_probe_v2"
            if role_id == ADDRESS_ROLE_ID
            else "literary_b4_translator_probe_v8"
        ),
        structured_output=runtime.shared_structured_output_for(role_id),
        output_envelope=envelope,
        base_url=source["base_url"],
    )
    if request_body.get("response_format") != {"type": "json_object"}:
        raise LiteraryModelApiB4ProbeError("B4 probe must use JSON-object mode")
    schema_sha = canonical_sha256(transport_schema)
    capability_suffix = "address_anchor" if role_id == ADDRESS_ROLE_ID else "translator"
    intent = {
        "capability_id": (
            "modelapi_gpt54_literary_b4_address_anchor_v2"
            if role_id == ADDRESS_ROLE_ID
            else "modelapi_gpt54_literary_b4_translator_v8"
        ),
        "capability_revision": (
            f"schema_{schema_sha[:8]}_validator_{validator_ref['sha256'][:8]}_v1"
        ),
        "requested_model_id": "gpt-5.4",
        "accepted_observed_model_ids": ["gpt-5.4"],
        "capability_kind": "json_object",
        "schema_name": (
            "literary_b4_address_anchor_response_v2"
            if role_id == ADDRESS_ROLE_ID
            else RESPONSE_SCHEMA_VERSION
        ),
        "schema_dialect": LOCAL_VALIDATION_SCHEMA_DIALECT,
        "schema_sha256": schema_sha,
        "local_validator_id": validator_ref["id"],
        "local_validator_sha256": validator_ref["sha256"],
    }
    binding = dict(implementation_binding or build_clean_implementation_binding_v1())
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
        role_id=role_id,
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
    return LiteraryModelApiB4ProbePlanV1(
        role_id=role_id,
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
    plan: LiteraryModelApiB4ProbePlanV1,
) -> dict[str, Any]:
    if plan.role_id == ADDRESS_ROLE_ID:
        validate = lambda payload: validate_address_anchor_response_v1(
            rendered=plan.rendered,
            response=payload,
        )
    else:
        validate = lambda payload: validate_translation_window_response_v1(
            rendered=plan.rendered,
            response=payload,
        )
    return probe.execute_once(
        seal=plan.seal,
        request_body=plan.request_body,
        local_validator=validate,
        local_validator_id=plan.validator_ref["id"],
        local_validator_sha256=plan.validator_ref["sha256"],
    )


def validator_ref_v1(role_id: str) -> dict[str, str]:
    if role_id == ADDRESS_ROLE_ID:
        return build_literary_code_ref_v1(
            identifier="literary.b4.address_anchor.validator",
            revision="v3",
            callables=[validate_address_anchor_response_v1],
        )
    if role_id == TRANSLATOR_ROLE_ID:
        return build_literary_code_ref_v1(
            identifier="literary.b4.translator.validator",
            revision="v2",
            callables=[validate_translation_window_response_v1],
        )
    raise LiteraryModelApiB4ProbeError(f"unsupported B4 role: {role_id}")


def synthetic_probe_rendered_v1(role_id: str) -> Any:
    if role_id == ADDRESS_ROLE_ID:
        return render_address_anchor_request_v1(
            anchor_input=_synthetic_anchor_input("1" * 64),
            style_profile=STYLE_PROFILE,
            style_profile_version=STYLE_VERSION,
            measured_arm=False,
        )
    if role_id != TRANSLATOR_ROLE_ID:
        raise LiteraryModelApiB4ProbeError(f"unsupported B4 role: {role_id}")
    story = _seal(
        {
            "schema_version": STORY_BIBLE_SCHEMA_VERSION,
            "book_id": "probe_book",
            "chapter_id": "probe_ch01",
            "chapter_order": 1,
            "entities": [
                _synthetic_entity("probe_speaker", "I"),
                _synthetic_entity("probe_addressee", "sir"),
            ],
            "relations": [],
            "states": [],
            "idiolect": [],
            "narrative_position": {
                "capsules": [],
                "frames": [
                    {
                        "frame_segment_id": "probe_frame_1",
                        "start_block_id": "probe_ch01_b001",
                        "end_block_id": "probe_ch01_b001",
                        "narrative_mode": "direct_current",
                    }
                ],
                "handoff": None,
            },
            "open_questions": {},
            "lineage": {},
            "memory_budget": {},
            "provider_calls": 0,
        }
    )
    anchor_rendered = render_address_anchor_request_v1(
        anchor_input=_synthetic_anchor_input(story["artifact_hash"]),
        style_profile=STYLE_PROFILE,
        style_profile_version=STYLE_VERSION,
        measured_arm=False,
    )
    anchor_validated = validate_address_anchor_response_v1(
        rendered=anchor_rendered,
        response=synthetic_probe_response_v1(ADDRESS_ROLE_ID, anchor_rendered),
    )
    anchor = build_address_anchor_artifact_v1(
        rendered=anchor_rendered,
        validated_response=anchor_validated,
        provider_receipt=None,
        provider_called=False,
    )
    window = _seal(
        {
            "schema_version": WINDOW_SCHEMA_VERSION,
            "book_id": "probe_book",
            "chapter_id": "probe_ch01",
            "window_id": "probe_window_1",
            "window_order": 1,
            "window_plan_hash": "2" * 64,
            "active_block_ids": ["probe_ch01_b001"],
            "preceding_tail_block_ids": [],
            "estimated_active_source_tokens": 8,
            "speaker_turns": [
                {
                    "speaker_turn_id": "probe_turn_1",
                    "block_id": "probe_ch01_b001",
                    "chapter_id": "probe_ch01",
                    "chapter_order": 1,
                    "frame_segment_id": "probe_frame_1",
                    "speaker": _synthetic_endpoint("probe_speaker", "I"),
                    "addressee": _synthetic_endpoint("probe_addressee", "sir"),
                    "address_terms": ["sir"],
                    "register_cue": "formal",
                    "register_cue_raw": None,
                    "delivery_tone": None,
                    "utterance_anchor": "Good evening, sir.",
                    "window_membership": "active",
                    "established_in_chapter": "probe_ch01",
                }
            ],
            "address_pairs": [
                {
                    "pair_id": "probe_pair_1",
                    "unanchored": False,
                    "speaker_effective_entity_id": "probe_speaker",
                    "addressee_effective_entity_id": "probe_addressee",
                    "turn_ids": ["probe_turn_1"],
                    "source_block_ids": ["probe_ch01_b001"],
                }
            ],
            "lineage": {},
            "provider_calls": 0,
        }
    )
    projected_pack = project_translator_pack_tiered_v2(
        story_bible=story,
        address_anchor=anchor,
        window_slices=[window],
    )
    translator_pack = seal_translator_pack_v1(
        projected=projected_pack,
        budget_report={
            "translator_cap_tokens": 64_000,
            "headroom_tokens": 4_000,
            "fixed_prompt_upper_bound_tokens": 2_000,
            "pack_budget_tokens": 58_000,
            "pack_estimated_tokens": 1_000,
            "max_full_prompt_upper_bound_tokens": 3_000,
            "safety_multiplier": 1.25,
            "calibration_artifact_hash": "3" * 64,
        },
    )
    return render_translation_window_request_v1(
        style_profile=STYLE_PROFILE,
        style_profile_version=STYLE_VERSION,
        measured_arm=False,
        translator_pack_bytes=_bytes(translator_pack),
        address_anchor_bytes=_bytes(anchor),
        window_slice_bytes=_bytes(window),
        chapter={
            "chapter_id": "probe_ch01",
            "blocks": [
                {
                    "block_id": "probe_ch01_b001",
                    "clean_text": "Good evening, sir.",
                }
            ],
        },
        accepted_tail_translations={},
    )


def synthetic_probe_response_v1(role_id: str, rendered: Any) -> dict[str, Any]:
    if role_id == ADDRESS_ROLE_ID:
        return {
            "schema_version": ANCHOR_OUTPUT_SCHEMA_VERSION,
            "chapter_id": rendered.anchor_input["chapter_id"],
            "anchor_input_artifact_hash": rendered.anchor_input["artifact_hash"],
            "pair_decisions": [
                {
                    "pair_id": "P1",
                    "pronoun_pair": {
                        "speaker": "tôi",
                        "addressee": "ông",
                    },
                    "vocative_options": [{"form": "thưa ông"}],
                    "register_shifts": [],
                    "evidence_refs": ["probe_ch01_b001"],
                    "model_confidence": "medium",
                    "not_anchored": None,
                }
            ],
        }
    if role_id == TRANSLATOR_ROLE_ID:
        return {
            "schema_version": RESPONSE_SCHEMA_VERSION,
            "blocks": [
                {
                    "block_id": "probe_ch01_b001",
                    "target_text": "Ban dich thu nghiem.",
                }
            ],
        }
    raise LiteraryModelApiB4ProbeError(f"unsupported B4 role: {role_id}")


def build_clean_implementation_binding_v1() -> dict[str, str]:
    if _git_text("status", "--short", "--untracked-files=no"):
        raise LiteraryModelApiB4ProbeError(
            "B4 probe requires a clean tracked worktree"
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
            raise LiteraryModelApiB4ProbeError(
                f"B4 implementation file is absent: {relative.as_posix()}"
            )
        rows.append({"path": relative.as_posix(), "sha256": file_sha256(path)})
    return canonical_sha256(rows)


def _synthetic_entity(entity_id: str, surface: str) -> dict[str, Any]:
    return {
        "effective_entity_id": entity_id,
        "canonical_surface": surface,
        "aliases": [],
        "stable_surfaces": [surface],
        "claims": {},
        "referent_kind": "person",
        "first_seen": "probe_ch01_b001",
        "member_card_ids": [entity_id],
        "member_chapters": ["probe_ch01"],
        "record_class": "person",
        "established_in_chapter": "probe_ch01",
    }


def _synthetic_endpoint(entity_id: str, surface: str) -> dict[str, Any]:
    return {
        "surface": surface,
        "candidate_card_ids": [entity_id],
        "effective_entity_ids": [entity_id],
        "resolution_status": "resolved_candidate",
        "resolved_to_effective_entity": True,
        "unresolved": False,
    }


def _synthetic_anchor_input(story_hash: str) -> dict[str, Any]:
    return _seal(
        {
            "schema_version": ANCHOR_INPUT_SCHEMA_VERSION,
            "book_id": "probe_book",
            "chapter_id": "probe_ch01",
            "story_bible_artifact_hash": story_hash,
            "pairs": [
                {
                    "pair_id": "probe_pair_1",
                    "speaker_effective_entity_id": "probe_speaker",
                    "addressee_effective_entity_id": "probe_addressee",
                    "speaker_surface": "I",
                    "addressee_surface": "sir",
                    "observed_terms": [],
                    "registers": [{"register_cue": "formal", "count": 1}],
                    "tones": [],
                    "turn_count": 1,
                    "example_anchor": "Good evening, sir.",
                    "source_block_ids": ["probe_ch01_b001"],
                    "relations": [],
                    "speaker_claims": {
                        "gender": {"value": "masculine", "evidence_ref": "e1"}
                    },
                    "addressee_claims": {
                        "gender": {"value": "masculine", "evidence_ref": "e2"}
                    },
                    "evidence_completeness": {
                        "speaker_resolved": True,
                        "addressee_resolved": True,
                        "turn_count": 1,
                        "vocative_count": 1,
                        "relation_present": False,
                        "relation_contested": False,
                        "missing_claims": {"speaker": [], "addressee": []},
                        "pending_identity": False,
                        "anchorable": True,
                    },
                }
            ],
            "provider_calls": 0,
        }
    )


def _source_record(binding: Mapping[str, Any], commitment: str) -> dict[str, Any]:
    if len(commitment) != 64 or any(c not in "0123456789abcdef" for c in commitment):
        raise LiteraryModelApiB4ProbeError("credential commitment is malformed")
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


def _seal(body: Mapping[str, Any]) -> dict[str, Any]:
    payload = deepcopy(dict(body))
    return {**payload, "artifact_hash": canonical_hash(payload)}


def _bytes(value: Mapping[str, Any]) -> bytes:
    return (canonical_json(dict(value)) + "\n").encode("utf-8")


def _git_text(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=_REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


__all__ = [
    "ADDRESS_ROLE_ID",
    "LiteraryModelApiB4ProbeError",
    "LiteraryModelApiB4ProbePlanV1",
    "RUNTIME_PROFILE_PATH",
    "RUNTIME_ROLE_IDS",
    "TRANSLATOR_ROLE_ID",
    "build_clean_implementation_binding_v1",
    "build_probe_plan_v1",
    "execute_probe_once_v1",
    "implementation_sha256_v1",
    "synthetic_probe_rendered_v1",
    "synthetic_probe_response_v1",
    "validator_ref_v1",
]
