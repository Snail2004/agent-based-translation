"""Checkpointed cross-chapter runner for sequential Literary B3 batches."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from pipeline.llm_backend import canonical_sha256
from pipeline.literary.b3_temporal_context_v1 import (
    B3TemporalProfileV1,
    load_b2_temporal_input_v1,
    refresh_b3_temporal_component_prior_context_v1,
)
from pipeline.literary.b3_temporal_context_v4 import (
    B3_PRIOR_PACKET_SCHEMA_VERSION_V1,
)
from pipeline.literary.b3_temporal_context_v7 import (
    B3_REQUEST_SCHEMA_VERSION_V7,
    B3_REVIEW_PACKET_SCHEMA_VERSION_V1,
    build_b3_temporal_cross_chapter_bundle_v7,
    render_b3_temporal_sequential_batch_v7,
)
from pipeline.literary.b3_temporal_contract_v7 import (
    normalize_b3_temporal_response_v7,
    validate_b3_temporal_request_v7,
)
from pipeline.literary.b3_parked_identity_v2 import (
    build_parked_identity_index_v2,
    empty_parked_identity_index_v2,
    verify_parked_identity_index_v2,
)
from pipeline.literary.b3_temporal_prefix_v1 import (
    B3_TEMPORAL_CHAPTER_ARTIFACT_SCHEMA_VERSION_V1,
    build_b3_temporal_prefix_v1,
    fold_b3_temporal_batch_artifact_v1,
)
from pipeline.literary.b3_temporal_prompts_v7 import (
    B3_TEMPORAL_PROMPT_ID_V7,
    B3_TEMPORAL_SYSTEM_PROMPT_V7,
    b3_temporal_response_schema_v7,
)
from pipeline.literary.chapter_cycle_shared_runtime_v1 import (
    BACKEND_MODE_SHARED_V1,
    LiterarySharedRunnerBindingsV1,
)
from pipeline.literary.checkpoint import (
    canonical_hash,
    canonical_json,
    file_sha256,
    resolve_existing_canonical_path,
)
from pipeline.literary.model_ref_v1 import (
    MODEL_REF_FIELDS_V1,
    MODEL_REF_MODE_CLASSIFIED_V1,
    project_model_response_schema_v1,
)
from pipeline.literary.b3_temporal_capability_contract_v4 import (
    b3_validator_ref_v4,
)
from pipeline.literary.openai_b3_json_object_capability_probe_v1 import ROLE_ID
from pipeline.literary.semantic_run_identity_v1 import (
    LiterarySemanticIdentityError,
    build_literary_semantic_stage_identity_v1,
    verify_literary_semantic_stage_identity_v1,
)
from pipeline.literary.shared_runtime_profile_v2 import LiterarySharedRuntimeProfileV2


B3_CHAPTER_RUN_SEAL_SCHEMA_VERSION_V1 = "literary_b3_chapter_run_seal_v1_1"
B3_CHAPTER_REPORT_SCHEMA_VERSION_V1 = "literary_b3_chapter_run_report_v1_1"

# State consolidation adds a typed list of corroborating state IDs to prior
# context. Keep this extension role-local so unrelated Literary transports do
# not change their field-map hash.
B3_MODEL_REF_FIELDS_V1: Mapping[str, tuple[str, ...]] = {
    namespace: tuple(fields)
    + (("corroborating_state_ids",) if namespace == "state" else ())
    for namespace, fields in MODEL_REF_FIELDS_V1.items()
}


class B3TemporalChapterRunnerError(RuntimeError):
    pass


def bind_b3_runtime_call_budget_v1(
    profile: LiterarySharedRuntimeProfileV2,
    *,
    max_calls: int,
) -> LiterarySharedRuntimeProfileV2:
    """Materialize B3 aggregate limits from the sealed per-run call cap."""
    if not isinstance(max_calls, int) or isinstance(max_calls, bool) or max_calls < 1:
        raise B3TemporalChapterRunnerError(
            "B3 runtime max_calls must be a positive integer"
        )
    try:
        binding = profile.role_bindings[ROLE_ID]
    except KeyError as exc:
        raise B3TemporalChapterRunnerError(
            "B3 runtime profile lacks the temporal-state role"
        ) from exc

    generation = dict(binding.preset.generation)
    limits = dict(binding.preset.limits)
    max_input_tokens = generation["max_input_tokens"]
    max_output_tokens = generation["max_output_tokens"]
    limits.update(
        {
            "max_calls": max_calls,
            "max_prompt_tokens": max_input_tokens * max_calls,
            "max_completion_tokens": max_output_tokens * max_calls,
            "max_total_tokens": (max_input_tokens + max_output_tokens) * max_calls,
        }
    )
    effective_preset = replace(
        binding.preset,
        limits=MappingProxyType(limits),
    )
    effective_bindings = dict(profile.role_bindings)
    effective_bindings[ROLE_ID] = replace(binding, preset=effective_preset)

    public_body = profile.public_payload()
    public_body.pop("profile_sha256")
    target = next(
        row for row in public_body["roles"] if row["role_id"] == ROLE_ID
    )
    target["limits"] = deepcopy(limits)
    return replace(
        profile,
        role_bindings=MappingProxyType(effective_bindings),
        profile_sha256=canonical_sha256(public_body),
    )


def prepare_b3_temporal_chapter_run_v1(
    *,
    b2_run_root: Path,
    speaker_recovery_root: Path | None = None,
    identity_hearing_root: Path | None = None,
    prior_b3_roots: Sequence[Path],
    output_root: Path,
    profile: B3TemporalProfileV1,
    shared_runtime: LiterarySharedRunnerBindingsV1,
    current_git_head: str,
    max_calls: int,
) -> dict[str, Any]:
    output = Path(output_root).resolve()
    source_root = Path(b2_run_root).resolve()
    recovery_root = (
        Path(speaker_recovery_root).resolve()
        if speaker_recovery_root is not None
        else None
    )
    hearing_root = (
        Path(identity_hearing_root).resolve()
        if identity_hearing_root is not None
        else None
    )
    if output.exists():
        raise B3TemporalChapterRunnerError("B3 chapter output root must not exist")
    if not source_root.is_dir():
        raise B3TemporalChapterRunnerError("B3 chapter B2 source root is absent")
    if output == source_root or source_root in output.parents:
        raise B3TemporalChapterRunnerError(
            "B3 chapter output may not live inside the B2 source root"
        )
    if recovery_root is not None and not recovery_root.is_dir():
        raise B3TemporalChapterRunnerError(
            "B3 chapter speaker recovery root is absent"
        )
    if recovery_root is not None and (
        recovery_root == source_root
        or output == recovery_root
        or recovery_root in output.parents
    ):
        raise B3TemporalChapterRunnerError(
            "B3 speaker recovery root must be a separate immutable source"
        )
    if hearing_root is not None and not hearing_root.is_dir():
        raise B3TemporalChapterRunnerError("B3 identity hearing root is absent")
    if hearing_root is not None and (
        hearing_root == source_root
        or output == hearing_root
        or hearing_root in output.parents
        or (recovery_root is not None and hearing_root == recovery_root)
    ):
        raise B3TemporalChapterRunnerError(
            "B3 identity hearing root must be a separate immutable source"
        )
    if not isinstance(max_calls, int) or isinstance(max_calls, bool) or max_calls < 1:
        raise B3TemporalChapterRunnerError("B3 max_calls must be a positive integer")
    if max_calls > profile.max_requests_per_chapter:
        raise B3TemporalChapterRunnerError(
            "B3 sealed call cap exceeds context profile ceiling: "
            f"sealed={max_calls}, ceiling={profile.max_requests_per_chapter}"
        )

    parked_index = (
        build_parked_identity_index_v2(hearing_root)
        if hearing_root is not None
        else empty_parked_identity_index_v2()
    )
    temporal_input_kwargs: dict[str, Any] = {}
    if recovery_root is not None:
        temporal_input_kwargs["speaker_recovery_root"] = recovery_root
    if hearing_root is not None:
        temporal_input_kwargs["parked_identity_index"] = parked_index
    temporal_input = load_b2_temporal_input_v1(
        source_root,
        **temporal_input_kwargs,
    )
    prefix = build_b3_temporal_prefix_v1(prior_b3_roots)
    if any(
        row["chapter_id"] == temporal_input["chapter_id"]
        for row in prefix["source_chapters"]
    ):
        raise B3TemporalChapterRunnerError("current B3 chapter already exists in prefix")
    bundle = build_b3_temporal_cross_chapter_bundle_v7(
        temporal_input=temporal_input,
        profile=profile,
        prior_states=prefix["effective_open_states"],
        prior_pending_cases=prefix["pending_cases"],
    )
    request_count = int(bundle["plan"]["request_count"])
    if request_count > max_calls:
        raise B3TemporalChapterRunnerError(
            "B3 chapter plan exceeds sealed call cap: "
            f"required={request_count}, sealed={max_calls}"
        )
    if set(shared_runtime.runtime_profile.role_bindings) != {ROLE_ID}:
        raise B3TemporalChapterRunnerError("B3 runtime must exact-cover only B3")
    _verify_runtime_budget_alignment(
        shared_runtime=shared_runtime,
        profile=profile,
        max_calls=max_calls,
    )
    shared_runtime.capability_for(
        role_id=ROLE_ID,
        response_schema=project_model_response_schema_v1(
            b3_temporal_response_schema_v7()
        ),
        binding_schema=b3_temporal_response_schema_v7(),
    )
    semantic_identity = _semantic_identity(
        shared_runtime=shared_runtime,
        profile=profile,
    )
    component_catalog_body = {
        "schema_version": "literary_b3_temporal_component_catalog_v2",
        "chapter_id": temporal_input["chapter_id"],
        "components": bundle["components"],
    }
    component_catalog = {
        **component_catalog_body,
        "catalog_hash": canonical_hash(component_catalog_body),
    }
    source_tree_hash = _tree_hash(source_root)
    recovery_tree_hash = _tree_hash(recovery_root) if recovery_root is not None else None
    hearing_tree_hash = _tree_hash(hearing_root) if hearing_root is not None else None
    recovery_binding = temporal_input.get("speaker_recovery_binding")
    seal_body = {
        "schema_version": B3_CHAPTER_RUN_SEAL_SCHEMA_VERSION_V1,
        "backend_mode": BACKEND_MODE_SHARED_V1,
        "phase": "bounded_cross_chapter_live",
        "current_git_head": _required_text(current_git_head, "current_git_head"),
        "semantic_run_id": _required_text(shared_runtime.run_id, "run_id"),
        "chapter_id": temporal_input["chapter_id"],
        "source_b2_run_root": str(source_root),
        "source_b2_tree_hash": source_tree_hash,
        "source_b2_artifact_hash": temporal_input["source_b2_artifact_hash"],
        "source_b2_speaker_recovery_root": (
            str(recovery_root) if recovery_root is not None else None
        ),
        "source_b2_speaker_recovery_tree_hash": recovery_tree_hash,
        "source_b2_speaker_recovery_artifact_hash": (
            recovery_binding["speaker_recovery_artifact_hash"]
            if recovery_binding is not None
            else None
        ),
        "source_identity_hearing_root": (
            str(hearing_root) if hearing_root is not None else None
        ),
        "source_identity_hearing_tree_hash": hearing_tree_hash,
        "parked_identity_index_hash": parked_index["index_hash"],
        "source_b1_prefix_hash": temporal_input["source_prefix_bundle_hash"],
        "prior_temporal_prefix_hash": prefix["prefix_hash"],
        "component_catalog_hash": component_catalog["catalog_hash"],
        "live_plan_hash": bundle["plan"]["plan_hash"],
        "semantic_stage_identity": semantic_identity,
        "initial_transport_identity": shared_runtime.identity_payload(),
        "limits": {
            "max_calls": max_calls,
            "max_prompt_tokens_per_call": profile.prompt_tokens_per_request,
            "max_completion_tokens_per_call": profile.output_tokens_per_request,
            "max_total_tokens": max_calls
            * (profile.prompt_tokens_per_request + profile.output_tokens_per_request),
            "transport_retries_per_attempt": 0,
            "semantic_retries": 0,
        },
        "resume_policy": {
            "semantic_stage_identity_immutable": True,
            "completed_batch_reuse": "required",
            "transport_source_may_change": True,
            "replacement_source_requires_sealed_model_and_capability": True,
            "silent_fallback_or_rotation": False,
        },
        "safety": {
            "application_response_cache_enabled": False,
            "gold_or_oracle_allowed": False,
            "production_publish_enabled": False,
            "mandatory_stop_after_chapter": temporal_input["chapter_id"],
        },
        "issued_at_utc": _now(),
    }
    seal = {**seal_body, "seal_hash": canonical_hash(seal_body)}
    output.mkdir(parents=True, exist_ok=False)
    _write_new_json(output / "run_seal.json", seal)
    _write_new_json(output / "prior_temporal_prefix.json", prefix)
    _write_new_json(output / "parked_identity_index.json", parked_index)
    _write_new_json(output / "component_catalog.json", component_catalog)
    _write_new_json(output / "live_plan.json", bundle["plan"])
    _write_new_json(
        output / "initial_request_previews.json",
        {
            "schema_version": "literary_b3_initial_request_previews_v1",
            "non_executable_after_prior_state_changes": True,
            "requests": bundle["initial_requests"],
        },
    )
    if _tree_hash(source_root) != source_tree_hash:
        raise B3TemporalChapterRunnerError("B2 source changed during B3 preparation")
    if recovery_root is not None and _tree_hash(recovery_root) != recovery_tree_hash:
        raise B3TemporalChapterRunnerError(
            "B2 speaker recovery changed during B3 preparation"
        )
    if hearing_root is not None and _tree_hash(hearing_root) != hearing_tree_hash:
        raise B3TemporalChapterRunnerError(
            "identity hearing changed during B3 preparation"
        )
    return seal


def execute_b3_temporal_chapter_run_v1(
    *,
    output_root: Path,
    profile: B3TemporalProfileV1,
    shared_runtime: LiterarySharedRunnerBindingsV1,
    current_git_head: str,
) -> dict[str, Any]:
    output = Path(output_root).resolve()
    seal = _verified_hashed_object(output / "run_seal.json", "seal_hash")
    if seal.get("schema_version") != B3_CHAPTER_RUN_SEAL_SCHEMA_VERSION_V1:
        raise B3TemporalChapterRunnerError("foreign B3 chapter seal")
    if seal.get("semantic_run_id") != shared_runtime.run_id:
        raise B3TemporalChapterRunnerError("B3 semantic run ID changed across resume")
    observed_semantic = _semantic_identity(
        shared_runtime=shared_runtime,
        profile=profile,
    )
    _verify_b3_resume_semantic_identity_v1(
        expected=seal["semantic_stage_identity"],
        observed=observed_semantic,
    )
    source_root = resolve_existing_canonical_path(seal["source_b2_run_root"])
    if _tree_hash(source_root) != seal["source_b2_tree_hash"]:
        raise B3TemporalChapterRunnerError("B3 source B2 tree changed after seal")
    recovery_root_value = seal.get("source_b2_speaker_recovery_root")
    recovery_root = (
        resolve_existing_canonical_path(recovery_root_value)
        if recovery_root_value is not None
        else None
    )
    if recovery_root is not None and _tree_hash(recovery_root) != seal.get(
        "source_b2_speaker_recovery_tree_hash"
    ):
        raise B3TemporalChapterRunnerError(
            "B3 source speaker recovery tree changed after seal"
        )
    hearing_root_value = seal.get("source_identity_hearing_root")
    hearing_root = (
        resolve_existing_canonical_path(hearing_root_value)
        if hearing_root_value is not None
        else None
    )
    if hearing_root is not None and _tree_hash(hearing_root) != seal.get(
        "source_identity_hearing_tree_hash"
    ):
        raise B3TemporalChapterRunnerError(
            "B3 source identity hearing tree changed after seal"
        )
    final_path = output / "chapter_temporal_artifact.json"
    report_path = output / "chapter_report.json"
    if final_path.exists() or report_path.exists():
        if not (final_path.exists() and report_path.exists()):
            raise B3TemporalChapterRunnerError("B3 final artifact/report is incomplete")
        _verified_hashed_object(final_path, "artifact_hash")
        return _verified_hashed_object(report_path, "report_hash")

    prefix = _verified_hashed_object(output / "prior_temporal_prefix.json", "prefix_hash")
    if prefix["prefix_hash"] != seal["prior_temporal_prefix_hash"]:
        raise B3TemporalChapterRunnerError("B3 prior prefix differs from seal")
    catalog = _verified_hashed_object(output / "component_catalog.json", "catalog_hash")
    if catalog["catalog_hash"] != seal["component_catalog_hash"]:
        raise B3TemporalChapterRunnerError("B3 component catalog differs from seal")
    plan = _verified_hashed_object(output / "live_plan.json", "plan_hash")
    if plan["plan_hash"] != seal["live_plan_hash"]:
        raise B3TemporalChapterRunnerError("B3 chapter plan differs from seal")
    parked_index = verify_parked_identity_index_v2(
        _read_object(output / "parked_identity_index.json")
    )
    if parked_index["index_hash"] != seal.get("parked_identity_index_hash"):
        raise B3TemporalChapterRunnerError("B3 parked identity index differs from seal")
    temporal_input_kwargs: dict[str, Any] = {}
    if recovery_root is not None:
        temporal_input_kwargs["speaker_recovery_root"] = recovery_root
    if hearing_root is not None:
        temporal_input_kwargs["parked_identity_index"] = parked_index
    temporal_input = load_b2_temporal_input_v1(
        source_root,
        **temporal_input_kwargs,
    )
    if temporal_input["source_b2_artifact_hash"] != seal["source_b2_artifact_hash"]:
        raise B3TemporalChapterRunnerError("B3 source artifact differs from seal")
    recovery_binding = temporal_input.get("speaker_recovery_binding")
    observed_recovery_hash = (
        recovery_binding["speaker_recovery_artifact_hash"]
        if recovery_binding is not None
        else None
    )
    if observed_recovery_hash != seal.get(
        "source_b2_speaker_recovery_artifact_hash"
    ):
        raise B3TemporalChapterRunnerError(
            "B3 source speaker recovery artifact differs from seal"
        )
    shared_runtime.capability_for(
        role_id=ROLE_ID,
        response_schema=project_model_response_schema_v1(
            b3_temporal_response_schema_v7()
        ),
        binding_schema=b3_temporal_response_schema_v7(),
    )

    component_by_id = {
        row["component_id"]: row for row in catalog.get("components") or []
    }
    if len(component_by_id) != len(catalog.get("components") or []):
        raise B3TemporalChapterRunnerError("B3 component catalog repeats IDs")
    effective = deepcopy(list(prefix.get("effective_open_states") or []))
    pending = deepcopy(list(prefix.get("pending_cases") or []))
    aggregate = _empty_aggregate(prefix)
    accepted_rows: list[dict[str, Any]] = []

    memberships = list(plan.get("batch_membership") or [])
    if len(memberships) > int(seal["limits"]["max_calls"]):
        raise B3TemporalChapterRunnerError(
            "B3 batch plan exceeds sealed call cap: "
            f"required={len(memberships)}, sealed={seal['limits']['max_calls']}"
        )
    for membership in memberships:
        ordinal = int(membership["batch_ordinal"])
        component_ids = list(membership["component_ids"])
        batch_root = output / "batches" / f"{ordinal:02d}"
        accepted_path = batch_root / "accepted.json"
        if accepted_path.exists():
            accepted, artifact, usage = _load_accepted_batch(
                output=output,
                accepted_path=accepted_path,
                seal_hash=seal["seal_hash"],
                expected_ordinal=ordinal,
                expected_component_ids=component_ids,
            )
        else:
            refreshed = [
                refresh_b3_temporal_component_prior_context_v1(
                    component=component_by_id[component_id],
                    profile=profile,
                    prior_states=effective,
                    prior_pending_cases=pending,
                )
                for component_id in component_ids
            ]
            request = render_b3_temporal_sequential_batch_v7(
                temporal_input=temporal_input,
                components=refreshed,
                profile=profile,
                batch_ordinal=ordinal,
            )
            validate_b3_temporal_request_v7(request)
            if request["component_ids"] != component_ids:
                raise B3TemporalChapterRunnerError("B3 batch membership changed")
            accepted, artifact, usage = _execute_batch(
                output=output,
                batch_root=batch_root,
                seal=seal,
                request=request,
                shared_runtime=shared_runtime,
                source_root=source_root,
                speaker_recovery_root=recovery_root,
                identity_hearing_root=hearing_root,
                component_ids=component_ids,
                batch_ordinal=ordinal,
            )
        effective, pending = fold_b3_temporal_batch_artifact_v1(
            effective_states=effective,
            pending_cases=pending,
            batch_artifact=artifact,
        )
        _accumulate(aggregate, artifact)
        accepted_rows.append({**accepted, "usage": usage})
        checkpoint_body = {
            "schema_version": "literary_b3_temporal_checkpoint_v1",
            "seal_hash": seal["seal_hash"],
            "completed_batch_ordinals": [row["batch_ordinal"] for row in accepted_rows],
            "accepted_batch_hashes": [row["accepted_hash"] for row in accepted_rows],
            "effective_projection_hash": canonical_hash(effective),
            "pending_projection_hash": canonical_hash(pending),
            "created_at_utc": _now(),
        }
        checkpoint = {**checkpoint_body, "checkpoint_hash": canonical_hash(checkpoint_body)}
        checkpoint_path = output / "checkpoints" / f"after_batch_{ordinal:02d}.json"
        if not checkpoint_path.exists():
            _write_new_json(checkpoint_path, checkpoint)

    expected_components = list(component_by_id)
    observed_components = [
        row["component_id"] for row in aggregate["component_results"]
    ]
    if set(observed_components) != set(expected_components) or len(
        observed_components
    ) != len(set(observed_components)):
        raise B3TemporalChapterRunnerError("B3 chapter results do not exact-cover components")
    closed_ids = sorted(set(aggregate["closed_prior_state_ids"]))
    artifact_body = {
        "schema_version": B3_TEMPORAL_CHAPTER_ARTIFACT_SCHEMA_VERSION_V1,
        "chapter_id": seal["chapter_id"],
        "run_seal_hash": seal["seal_hash"],
        "source_b2_artifact_hash": seal["source_b2_artifact_hash"],
        "source_b2_speaker_recovery_artifact_hash": seal.get(
            "source_b2_speaker_recovery_artifact_hash"
        ),
        "source_prefix_bundle_hash": seal["source_b1_prefix_hash"],
        "prior_temporal_prefix_hash": seal["prior_temporal_prefix_hash"],
        "parked_identity_index": parked_index,
        "batch_artifacts": [
            {
                "batch_ordinal": row["batch_ordinal"],
                "request_fingerprint": row["request_fingerprint"],
                "artifact_hash": row["artifact_hash"],
                "transport_identity_hash": row["transport_identity_hash"],
            }
            for row in accepted_rows
        ],
        "component_results": sorted(
            aggregate["component_results"], key=lambda row: row["component_id"]
        ),
        "new_state_rows": sorted(
            aggregate["new_state_rows"], key=lambda row: row["state_id"]
        ),
        "transition_rows": sorted(
            aggregate["transition_rows"], key=lambda row: row["transition_id"]
        ),
        "reinforcement_rows": sorted(
            aggregate["reinforcement_rows"],
            key=lambda row: row["reinforcement_id"],
        ),
        "historical_observations": sorted(
            aggregate["historical_observations"],
            key=lambda row: row["observation_id"],
        ),
        "non_effective_observations": sorted(
            aggregate["non_effective_observations"],
            key=lambda row: row["observation_id"],
        ),
        "pending_cases": pending,
        "resolved_cases": deepcopy(list(prefix.get("resolved_cases") or [])),
        "carried_prior_pending_case_ids": sorted(
            row["pending_case_id"] for row in prefix.get("pending_cases") or []
        ),
        "effective_state_projection": effective,
        "closed_prior_state_ids": closed_ids,
        "identity_mutation_performed": False,
        "production_publish_performed": False,
    }
    if aggregate["quarantined_actions"]:
        artifact_body["quarantined_actions"] = sorted(
            aggregate["quarantined_actions"],
            key=lambda row: (row["component_id"], row["action_ordinal"]),
        )
    if aggregate["quarantined_component_results"]:
        artifact_body["quarantined_component_results"] = sorted(
            aggregate["quarantined_component_results"],
            key=lambda row: row["component_id"],
        )
    chapter_artifact = {
        **artifact_body,
        "artifact_hash": canonical_hash(artifact_body),
    }
    _write_new_json(final_path, chapter_artifact)
    usage = _aggregate_usage([row["usage"] for row in accepted_rows])
    report_body = {
        "schema_version": B3_CHAPTER_REPORT_SCHEMA_VERSION_V1,
        "status": "complete_mandatory_stop",
        "seal_hash": seal["seal_hash"],
        "chapter_id": seal["chapter_id"],
        "artifact_hash": chapter_artifact["artifact_hash"],
        "source_b2_speaker_recovery_artifact_hash": seal.get(
            "source_b2_speaker_recovery_artifact_hash"
        ),
        "parked_identity_index_hash": parked_index["index_hash"],
        "api_calls_performed": len(accepted_rows),
        "usage": usage,
        "counts": {
            "components": len(observed_components),
            "new_states": len(aggregate["new_state_rows"]),
            "transitions": len(aggregate["transition_rows"]),
            "reinforcements": len(aggregate["reinforcement_rows"]),
            "historical_observations": len(aggregate["historical_observations"]),
            "non_effective_observations": len(
                aggregate["non_effective_observations"]
            ),
            "quarantined_actions": len(aggregate["quarantined_actions"]),
            "quarantined_component_results": len(
                aggregate["quarantined_component_results"]
            ),
            "pending_cases_total": len(pending),
            "effective_states": len(effective),
        },
        "transport_attempts": [
            {
                "batch_ordinal": row["batch_ordinal"],
                "transport_identity_hash": row["transport_identity_hash"],
                "physical_quota_bucket_id": row["physical_quota_bucket_id"],
            }
            for row in accepted_rows
        ],
        "source_artifact_mutated": False,
        "gold_or_oracle_loaded": False,
        "production_publish_performed": False,
        "mandatory_stop_observed": True,
        "completed_at_utc": _now(),
    }
    report = {**report_body, "report_hash": canonical_hash(report_body)}
    _write_new_json(report_path, report)
    return report


def _execute_batch(
    *,
    output: Path,
    batch_root: Path,
    seal: Mapping[str, Any],
    request: Mapping[str, Any],
    shared_runtime: LiterarySharedRunnerBindingsV1,
    source_root: Path,
    speaker_recovery_root: Path | None,
    identity_hearing_root: Path | None,
    component_ids: Sequence[str],
    batch_ordinal: int,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    attempt_id = _safe_id(shared_runtime.attempt_run_id)
    attempt_root = batch_root / "attempts" / attempt_id
    if attempt_root.exists():
        raise B3TemporalChapterRunnerError(
            "B3 attempt ID already exists; resume requires a new transport attempt ID"
        )
    attempt_root.mkdir(parents=True, exist_ok=False)
    _write_new_json(attempt_root / "request.json", request)
    transport_identity = shared_runtime.identity_payload()
    _write_new_json(
        attempt_root / "stage_started.json",
        {
            "schema_version": "literary_b3_temporal_batch_started_v1",
            "seal_hash": seal["seal_hash"],
            "batch_ordinal": batch_ordinal,
            "request_fingerprint": request["request_fingerprint"],
            "transport_identity_hash": transport_identity["identity_sha256"],
            "physical_quota_bucket_id": shared_runtime.api_source_for(ROLE_ID)[
                "physical_quota_bucket_id"
            ],
            "started_at_utc": _now(),
        },
    )

    def validate(response: Mapping[str, Any]) -> Mapping[str, Any]:
        return normalize_b3_temporal_response_v7(request=request, response=response)

    try:
        input_bindings = [
            {"name": "b3_chapter_run_seal", "sha256": seal["seal_hash"]},
            {
                "name": "b3_source_b2_artifact",
                "sha256": seal["source_b2_artifact_hash"],
            },
            {
                "name": "b3_prior_temporal_prefix",
                "sha256": seal["prior_temporal_prefix_hash"],
            },
        ]
        if seal.get("source_b2_speaker_recovery_artifact_hash") is not None:
            input_bindings.append(
                {
                    "name": "b3_source_b2_speaker_recovery_artifact",
                    "sha256": seal["source_b2_speaker_recovery_artifact_hash"],
                }
            )
        if seal.get("parked_identity_index_hash") is not None:
            input_bindings.append(
                {
                    "name": "b3_parked_identity_index",
                    "sha256": seal["parked_identity_index_hash"],
                }
            )
        result = shared_runtime.execute_accepted_request(
            role_id=ROLE_ID,
            stage_id=f"b3_temporal_{seal['chapter_id']}_batch_{batch_ordinal:02d}",
            logical_request_id=(
                f"b3_temporal_{seal['chapter_id']}_{request['request_fingerprint'][:24]}"
            ),
            request=request,
            schema_name="literary_b3_temporal_response_v7",
            semantic_validator=validate,
            validator_ref=b3_validator_ref_v4(),
            application_contract_id="literary.b3.temporal_state.apply_v1",
            application_contract_revision="v1",
            output_dir=attempt_root,
            model_reference_mode=MODEL_REF_MODE_CLASSIFIED_V1,
            model_reference_fields=B3_MODEL_REF_FIELDS_V1,
            additional_input_bindings=tuple(input_bindings),
        )
        usage = _validated_usage(result.usage, seal["limits"])
        artifact = dict(result.semantic_payload)
        _write_new_json(attempt_root / "batch_artifact.json", artifact)
        if _tree_hash(source_root) != seal["source_b2_tree_hash"]:
            raise B3TemporalChapterRunnerError("B2 source changed during B3 call")
        if speaker_recovery_root is not None and _tree_hash(
            speaker_recovery_root
        ) != seal.get("source_b2_speaker_recovery_tree_hash"):
            raise B3TemporalChapterRunnerError(
                "B2 speaker recovery changed during B3 call"
            )
        if identity_hearing_root is not None and _tree_hash(
            identity_hearing_root
        ) != seal.get("source_identity_hearing_tree_hash"):
            raise B3TemporalChapterRunnerError(
                "identity hearing changed during B3 call"
            )
        accepted_body = {
            "schema_version": "literary_b3_temporal_batch_accepted_v1",
            "seal_hash": seal["seal_hash"],
            "batch_ordinal": batch_ordinal,
            "component_ids": list(component_ids),
            "attempt_run_id": shared_runtime.attempt_run_id,
            "attempt_artifact_path": (
                attempt_root.relative_to(output) / "batch_artifact.json"
            ).as_posix(),
            "request_fingerprint": request["request_fingerprint"],
            "artifact_hash": artifact["artifact_hash"],
            "transport_identity_hash": transport_identity["identity_sha256"],
            "physical_quota_bucket_id": shared_runtime.api_source_for(ROLE_ID)[
                "physical_quota_bucket_id"
            ],
            "usage": usage,
            "accepted_at_utc": _now(),
        }
        accepted = {**accepted_body, "accepted_hash": canonical_hash(accepted_body)}
        _write_new_json(batch_root / "accepted.json", accepted)
        return accepted, artifact, usage
    except Exception as exc:
        _write_new_json(
            attempt_root / "failure.json",
            {
                "schema_version": "literary_b3_temporal_batch_failure_v1",
                "seal_hash": seal["seal_hash"],
                "batch_ordinal": batch_ordinal,
                "request_fingerprint": request["request_fingerprint"],
                "attempt_run_id": shared_runtime.attempt_run_id,
                "error_type": type(exc).__name__,
                "message": _safe_error(exc),
                "production_publish_performed": False,
                "failed_at_utc": _now(),
            },
        )
        raise


def _load_accepted_batch(
    *,
    output: Path,
    accepted_path: Path,
    seal_hash: str,
    expected_ordinal: int,
    expected_component_ids: Sequence[str],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    accepted = _verified_hashed_object(accepted_path, "accepted_hash")
    if accepted.get("seal_hash") != seal_hash:
        raise B3TemporalChapterRunnerError("accepted B3 batch belongs to another seal")
    if accepted.get("batch_ordinal") != expected_ordinal or accepted.get(
        "component_ids"
    ) != list(expected_component_ids):
        raise B3TemporalChapterRunnerError("accepted B3 batch membership differs")
    artifact_path = (output / accepted["attempt_artifact_path"]).resolve()
    if output not in artifact_path.parents:
        raise B3TemporalChapterRunnerError("accepted B3 artifact escapes output root")
    artifact = _verified_hashed_object(artifact_path, "artifact_hash")
    if artifact.get("artifact_hash") != accepted.get("artifact_hash"):
        raise B3TemporalChapterRunnerError("accepted B3 artifact hash differs")
    if artifact.get("request_fingerprint") != accepted.get("request_fingerprint"):
        raise B3TemporalChapterRunnerError("accepted B3 request fingerprint differs")
    return accepted, artifact, _validated_usage(accepted.get("usage"), None)


def _semantic_identity(
    *,
    shared_runtime: LiterarySharedRunnerBindingsV1,
    profile: B3TemporalProfileV1,
) -> dict[str, Any]:
    return build_literary_semantic_stage_identity_v1(
        shared_runtime=shared_runtime,
        role_id=ROLE_ID,
        prompt_id=B3_TEMPORAL_PROMPT_ID_V7,
        prompt_sha256=hashlib.sha256(
            B3_TEMPORAL_SYSTEM_PROMPT_V7.encode("utf-8")
        ).hexdigest(),
        response_schema_sha256=canonical_hash(b3_temporal_response_schema_v7()),
        validator_ref=b3_validator_ref_v4(),
        application_contract_id="literary.b3.temporal_state.apply_v1",
        application_contract_revision="v1",
        context_contract={
            "profile_id": profile.profile_id,
            "profile_hash": profile.profile_hash,
            "profile_sha256": profile.profile_sha256,
            "request_schema_version": B3_REQUEST_SCHEMA_VERSION_V7,
            "prior_packet_contract": B3_PRIOR_PACKET_SCHEMA_VERSION_V1,
            "review_packet_contract": B3_REVIEW_PACKET_SCHEMA_VERSION_V1,
        },
    )


def _verify_b3_resume_semantic_identity_v1(
    *,
    expected: Mapping[str, Any],
    observed: Mapping[str, Any],
) -> None:
    try:
        verify_literary_semantic_stage_identity_v1(
            expected=expected,
            observed=observed,
        )
        return
    except LiterarySemanticIdentityError as original_error:
        before = deepcopy(dict(expected))
        after = deepcopy(dict(observed))

        for row in (before, after):
            row.pop("semantic_identity_hash", None)
            context = row.get("context_contract")
            if isinstance(context, dict):
                context.pop("profile_hash", None)
                context.pop("profile_sha256", None)

        capacity_paths = (
            ("generation", "max_input_tokens"),
            ("generation", "max_output_tokens"),
            ("limits", "max_calls"),
            ("limits", "max_prompt_tokens"),
            ("limits", "max_completion_tokens"),
            ("limits", "max_total_tokens"),
        )
        for section, key in capacity_paths:
            before_section = before.get(section)
            after_section = after.get(section)
            old_value = (
                before_section.get(key)
                if isinstance(before_section, Mapping)
                else None
            )
            new_value = (
                after_section.get(key)
                if isinstance(after_section, Mapping)
                else None
            )
            if old_value is None and new_value is None:
                continue
            if (
                not isinstance(old_value, int)
                or isinstance(old_value, bool)
                or not isinstance(new_value, int)
                or isinstance(new_value, bool)
                or new_value < old_value
            ):
                raise B3TemporalChapterRunnerError(
                    f"B3 resume capacity is not upward: {section}.{key}"
                ) from original_error
            before_section.pop(key, None)
            after_section.pop(key, None)

        if canonical_hash(before) != canonical_hash(after):
            raise B3TemporalChapterRunnerError(
                "B3 semantic identity changed across resume"
            ) from original_error


def _verify_runtime_budget_alignment(
    *,
    shared_runtime: LiterarySharedRunnerBindingsV1,
    profile: B3TemporalProfileV1,
    max_calls: int,
) -> None:
    preset = shared_runtime.role_preset_for(ROLE_ID)
    generation = dict(preset.generation)
    limits = dict(preset.limits)
    expected = {
        "max_calls": max_calls,
        "max_prompt_tokens": profile.prompt_tokens_per_request * max_calls,
        "max_completion_tokens": profile.output_tokens_per_request * max_calls,
        "max_total_tokens": (
            profile.prompt_tokens_per_request + profile.output_tokens_per_request
        )
        * max_calls,
    }
    if generation.get("max_input_tokens") != profile.prompt_tokens_per_request:
        raise B3TemporalChapterRunnerError("B3 runtime input cap differs from context cap")
    if generation.get("max_output_tokens") != profile.output_tokens_per_request:
        raise B3TemporalChapterRunnerError("B3 runtime output cap differs from context cap")
    if any(limits.get(field) != value for field, value in expected.items()):
        raise B3TemporalChapterRunnerError("B3 runtime aggregate limits differ from run cap")


def _empty_aggregate(prefix: Mapping[str, Any]) -> dict[str, list[Any]]:
    return {
        "component_results": [],
        "new_state_rows": [],
        "transition_rows": [],
        "reinforcement_rows": [],
        "historical_observations": [],
        "non_effective_observations": [],
        "quarantined_actions": [],
        "quarantined_component_results": [],
        "closed_prior_state_ids": [],
        "prior_pending_case_ids": [
            row["pending_case_id"] for row in prefix.get("pending_cases") or []
        ],
    }


def _accumulate(target: dict[str, list[Any]], artifact: Mapping[str, Any]) -> None:
    for field in (
        "component_results",
        "new_state_rows",
        "transition_rows",
        "reinforcement_rows",
        "historical_observations",
        "non_effective_observations",
        "quarantined_actions",
        "quarantined_component_results",
        "closed_prior_state_ids",
    ):
        target[field].extend(deepcopy(list(artifact.get(field) or [])))


def _validated_usage(
    usage: Mapping[str, Any] | None, limits: Mapping[str, Any] | None
) -> dict[str, Any]:
    if not isinstance(usage, Mapping):
        raise B3TemporalChapterRunnerError("B3 provider usage is unknown")
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    total = usage.get("total_tokens")
    if not all(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0
        for value in (prompt, completion, total)
    ):
        raise B3TemporalChapterRunnerError("B3 provider usage is incomplete")
    if prompt + completion != total:
        raise B3TemporalChapterRunnerError("B3 provider usage is inconsistent")
    if limits is not None:
        if prompt > limits["max_prompt_tokens_per_call"]:
            raise B3TemporalChapterRunnerError("B3 prompt usage exceeded cap")
        if completion > limits["max_completion_tokens_per_call"]:
            raise B3TemporalChapterRunnerError("B3 completion usage exceeded cap")
    return deepcopy(dict(usage))


def _aggregate_usage(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    fields = (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "cached_input_tokens",
        "reasoning_tokens",
    )
    return {
        field: sum(int(row.get(field) or 0) for row in rows)
        for field in fields
    }


def _verified_hashed_object(path: Path, field: str) -> dict[str, Any]:
    row = _read_object(path)
    expected = row.get(field)
    unsigned = dict(row)
    unsigned.pop(field, None)
    if expected != canonical_hash(unsigned):
        raise B3TemporalChapterRunnerError(f"{path.name} hash mismatch")
    return row


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise B3TemporalChapterRunnerError(f"cannot read JSON object: {path}") from exc
    if not isinstance(value, dict):
        raise B3TemporalChapterRunnerError(f"JSON artifact must be an object: {path}")
    return value


def _write_new_json(path: Path, payload: Mapping[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise B3TemporalChapterRunnerError(f"refusing to overwrite artifact: {target}")
    target.write_text(canonical_json(payload) + "\n", encoding="utf-8")


def _tree_hash(root: Path) -> str:
    rows = [
        {"path": path.relative_to(root).as_posix(), "sha256": file_sha256(path)}
        for path in sorted(Path(root).rglob("*"))
        if path.is_file()
    ]
    return canonical_hash(rows)


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "attempt"


def _safe_error(exc: Exception) -> str:
    message = str(exc)
    if "sk-" in message or "Bearer " in message:
        return "credential material was redacted from the B3 failure"
    return message[:1200]


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise B3TemporalChapterRunnerError(f"{label} must be non-empty text")
    return value


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [
    "B3_CHAPTER_REPORT_SCHEMA_VERSION_V1",
    "B3_CHAPTER_RUN_SEAL_SCHEMA_VERSION_V1",
    "B3_MODEL_REF_FIELDS_V1",
    "B3TemporalChapterRunnerError",
    "execute_b3_temporal_chapter_run_v1",
    "prepare_b3_temporal_chapter_run_v1",
]
