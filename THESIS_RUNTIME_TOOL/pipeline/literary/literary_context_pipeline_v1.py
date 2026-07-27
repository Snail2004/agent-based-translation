from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
from typing import Any, Mapping, Sequence
import uuid

from pipeline.literary.b2_context_v1 import (
    build_b2_windows_v1,
    load_b2_phase_a_profile,
    load_real_b1_run_input_v1,
)
from pipeline.literary.b2_live_canary_v1 import (
    execute_b2_frame_live_v1,
    execute_b2_interactions_live_v1,
    prepare_b2_ch1_canary_v1,
)
from pipeline.literary.chapter_cycle_live_executor_v1 import (
    ChapterCycleLiveExecutorV1,
)
from pipeline.literary.chapter_cycle_shared_runtime_v1 import (
    BACKEND_MODE_LEGACY,
    BACKEND_MODE_SHARED_V1,
    LiterarySharedRunnerBindingsV1,
)
from pipeline.literary.chapter_cycle_orchestrator_v1 import (
    initialize_chapter_cycle_run_v1,
    load_chapter_cycle_plan_v1,
    load_chapter_cycle_state_v1,
    resume_chapter_cycle_run_v1,
    run_chapter_cycle_until_boundary_v1,
)
from pipeline.literary.checkpoint import (
    canonical_hash,
    canonical_json,
    file_sha256,
    write_checkpoint_atomic,
)
from pipeline.literary.context_pipeline_profile_v1 import (
    LiteraryContextPipelineProfile,
    load_context_pipeline_profile_v1,
)
from pipeline.literary.literary_pipeline_profile_v1 import (
    load_literary_pipeline_profile,
)
from pipeline.scripts.run_chapter_registry_v4_real import FROZEN_DB_SHA256
from pipeline.scripts.run_literary_b2_recovery_live_v1 import (
    run as run_b2_recovery_live_v1,
)
from pipeline.scripts.run_literary_chapter_cycle_v1 import (
    write_literary_run_summary_v1,
)


PLAN_SCHEMA_VERSION = "literary_context_pipeline_plan_v1"
PLAN_SCHEMA_VERSION_SHARED = "literary_context_pipeline_plan_v2_shared_backend"
STATE_SCHEMA_VERSION = "literary_context_pipeline_state_v1"
STATE_SCHEMA_VERSION_SHARED = "literary_context_pipeline_state_v2_shared_backend"
SNAPSHOT_SCHEMA_VERSION = "literary_b1_completed_prefix_snapshot_v1"
GENERATED_PROFILE_SCHEMA_VERSION = "literary_context_generated_profiles_v1"
CHECKPOINT_SCHEMA_VERSION = "literary_context_chapter_checkpoint_v1"
CHECKPOINT_SCHEMA_VERSION_SHARED = (
    "literary_context_chapter_checkpoint_v2_shared_backend"
)
SUMMARY_SCHEMA_VERSION = "literary_context_pipeline_summary_v1"
REPLAY_PLAN_SCHEMA_VERSION = "literary_context_pipeline_replay_plan_v1"


class LiteraryContextPipelineError(RuntimeError):
    """Raised when the unified B1-through-B2 cycle cannot continue safely."""


def _backend_binding_v1(
    *,
    backend_mode: str,
    shared_runtime: LiterarySharedRunnerBindingsV1 | None,
) -> dict[str, Any]:
    if backend_mode == BACKEND_MODE_LEGACY:
        if shared_runtime is not None:
            raise LiteraryContextPipelineError(
                "legacy context mode cannot receive a shared runtime"
            )
        return {
            "backend_mode": BACKEND_MODE_LEGACY,
            "shared_runtime_identity": None,
            "shared_runtime_identity_hash": None,
        }
    if backend_mode != BACKEND_MODE_SHARED_V1:
        raise LiteraryContextPipelineError(
            "backend_mode must be exactly legacy or shared_v1"
        )
    if shared_runtime is None:
        raise LiteraryContextPipelineError(
            "shared_v1 context mode requires an injected shared runtime"
        )
    identity = shared_runtime.identity_payload()
    return {
        "backend_mode": BACKEND_MODE_SHARED_V1,
        "shared_runtime_identity": identity,
        "shared_runtime_identity_hash": canonical_hash(identity),
    }


def _plan_backend_binding_v1(plan: Mapping[str, Any]) -> dict[str, Any]:
    schema = plan.get("schema_version")
    if schema == PLAN_SCHEMA_VERSION:
        return {
            "backend_mode": BACKEND_MODE_LEGACY,
            "shared_runtime_identity": None,
            "shared_runtime_identity_hash": None,
        }
    if schema != PLAN_SCHEMA_VERSION_SHARED:
        raise LiteraryContextPipelineError("foreign context pipeline plan")
    identity = _object(
        plan.get("shared_runtime_identity"), "shared_runtime_identity"
    )
    identity_hash = _required_string(
        plan.get("shared_runtime_identity_hash"),
        "shared_runtime_identity_hash",
    )
    if (
        plan.get("backend_mode") != BACKEND_MODE_SHARED_V1
        or canonical_hash(identity) != identity_hash
    ):
        raise LiteraryContextPipelineError(
            "shared context plan backend identity is malformed"
        )
    return {
        "backend_mode": BACKEND_MODE_SHARED_V1,
        "shared_runtime_identity": deepcopy(dict(identity)),
        "shared_runtime_identity_hash": identity_hash,
    }


def tree_hash_v1(root: Path) -> str:
    source = Path(root).resolve()
    if not source.is_dir():
        raise LiteraryContextPipelineError(f"artifact root is absent: {source}")
    rows = [
        {
            "path": path.relative_to(source).as_posix(),
            "sha256": file_sha256(path),
        }
        for path in sorted(row for row in source.rglob("*") if row.is_file())
    ]
    if not rows:
        raise LiteraryContextPipelineError(f"artifact root is empty: {source}")
    return canonical_hash(rows)


def b2_source_tree_hash_v1(root: Path) -> str:
    """Reproduce the source-tree hash dialect sealed by B2 canary artifacts."""
    source = Path(root).resolve()
    if not source.is_dir():
        raise LiteraryContextPipelineError(f"artifact root is absent: {source}")
    rows = [
        {
            "relative_path": path.relative_to(source).as_posix(),
            "sha256": file_sha256(path),
        }
        for path in sorted(row for row in source.rglob("*") if row.is_file())
    ]
    if not rows:
        raise LiteraryContextPipelineError(f"artifact root is empty: {source}")
    return canonical_hash(rows)


def snapshot_completed_b1_prefix_v1(
    *,
    source_run_root: Path,
    output_root: Path,
    chapter_count: int,
    current_git_head: str,
) -> dict[str, Any]:
    source = Path(source_run_root).resolve()
    output = Path(output_root).resolve()
    if output.exists():
        return verify_b1_prefix_snapshot_v1(
            output, current_git_head=current_git_head
        )
    source_tree_before = tree_hash_v1(source)
    plan = _read_object(source / "run_plan.json", "B1 plan")
    summary = _read_object(source / "run_summary.json", "B1 summary")
    _verify_embedded_hash(plan, "plan_hash", "B1 plan")
    _verify_embedded_hash(summary, "summary_hash", "B1 summary")
    completed = _string_list(
        summary.get("completed_chapter_ids"), "completed_chapter_ids"
    )
    if (
        not isinstance(chapter_count, int)
        or isinstance(chapter_count, bool)
        or not 1 <= chapter_count <= len(completed)
    ):
        raise LiteraryContextPipelineError(
            "snapshot chapter_count exceeds the completed B1 prefix"
        )
    selected = completed[:chapter_count]
    report_rows = summary.get("chapter_reports")
    if not isinstance(report_rows, list) or len(report_rows) != len(completed):
        raise LiteraryContextPipelineError("B1 summary report index is stale")

    source_envelopes = sorted(
        source.glob("stages/*/live/run_envelope_*.json")
    )
    if not source_envelopes:
        raise LiteraryContextPipelineError("B1 run has no source run envelope")
    heads = {
        _required_string(
            _read_object(path, "B1 run envelope").get("git_head"),
            "run envelope git_head",
        )
        for path in source_envelopes
    }
    if heads != {current_git_head}:
        raise LiteraryContextPipelineError(
            "B1 run envelopes do not bind the current Git HEAD"
        )

    temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp")
    if temporary.exists():
        raise LiteraryContextPipelineError("snapshot temporary root exists")
    temporary.mkdir(parents=True)
    try:
        shutil.copy2(source / "run_plan.json", temporary / "run_plan.json")
        snapshot_summary_body = deepcopy(summary)
        snapshot_summary_body.pop("summary_hash", None)
        snapshot_summary_body["status"] = (
            "complete"
            if selected == _string_list(
                plan.get("ordered_chapter_ids"), "ordered_chapter_ids"
            )
            else "stopped"
        )
        snapshot_summary_body["completed_chapter_ids"] = selected
        snapshot_summary_body["chapter_reports"] = deepcopy(
            report_rows[:chapter_count]
        )
        snapshot_summary = {
            **snapshot_summary_body,
            "summary_hash": canonical_hash(snapshot_summary_body),
        }
        _write_new_json(temporary / "run_summary.json", snapshot_summary)

        for row in snapshot_summary["chapter_reports"]:
            if not isinstance(row, Mapping):
                raise LiteraryContextPipelineError(
                    "B1 chapter report index row is malformed"
                )
            report_source = _contained_path(
                source, row.get("path"), "B1 chapter report"
            )
            report_target = temporary / Path(str(row["path"]))
            report_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(report_source, report_target)
            prefix_source = report_source.parent / "final_prefix.json"
            if not prefix_source.is_file():
                raise LiteraryContextPipelineError(
                    "B1 chapter report lacks final_prefix.json"
                )
            shutil.copy2(prefix_source, report_target.parent / "final_prefix.json")

        envelope_target = (
            temporary
            / "stages"
            / "source"
            / "live"
            / "run_envelope_001.json"
        )
        envelope_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_envelopes[0], envelope_target)

        copied_files = [
            {
                "path": path.relative_to(temporary).as_posix(),
                "sha256": file_sha256(path),
            }
            for path in sorted(
                row for row in temporary.rglob("*") if row.is_file()
            )
        ]
        manifest_body = {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "source_run_root": str(source),
            "source_tree_hash": source_tree_before,
            "source_plan_hash": plan["plan_hash"],
            "source_summary_hash": summary["summary_hash"],
            "completed_chapter_ids": selected,
            "source_git_head": current_git_head,
            "files": copied_files,
            "production_publish_performed": False,
        }
        manifest = {
            **manifest_body,
            "snapshot_hash": canonical_hash(manifest_body),
        }
        _write_new_json(temporary / "snapshot_manifest.json", manifest)
        output.parent.mkdir(parents=True, exist_ok=True)
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)

    if tree_hash_v1(source) != source_tree_before:
        raise LiteraryContextPipelineError(
            "B1 source run changed while creating its snapshot"
        )
    return verify_b1_prefix_snapshot_v1(
        output, current_git_head=current_git_head
    )


def verify_b1_prefix_snapshot_v1(
    root: Path, *, current_git_head: str
) -> dict[str, Any]:
    source = Path(root).resolve()
    manifest = _read_object(
        source / "snapshot_manifest.json", "B1 snapshot manifest"
    )
    if manifest.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        raise LiteraryContextPipelineError("foreign B1 snapshot schema")
    _verify_embedded_hash(manifest, "snapshot_hash", "B1 snapshot manifest")
    if manifest.get("source_git_head") != current_git_head:
        raise LiteraryContextPipelineError(
            "B1 snapshot belongs to another Git HEAD"
        )
    expected_files = manifest.get("files")
    if not isinstance(expected_files, list) or not expected_files:
        raise LiteraryContextPipelineError("B1 snapshot file manifest is empty")
    for row in expected_files:
        if not isinstance(row, Mapping):
            raise LiteraryContextPipelineError(
                "B1 snapshot file manifest is malformed"
            )
        path = _contained_path(source, row.get("path"), "snapshot file")
        if file_sha256(path) != row.get("sha256"):
            raise LiteraryContextPipelineError("B1 snapshot file hash changed")
    loaded = load_real_b1_run_input_v1(
        source, current_git_head=current_git_head
    )
    if loaded["ordered_chapter_ids"] != manifest.get("completed_chapter_ids"):
        raise LiteraryContextPipelineError(
            "B1 snapshot coverage differs from its manifest"
        )
    return {
        "snapshot_root": str(source),
        "snapshot_hash": manifest["snapshot_hash"],
        "snapshot_tree_hash": tree_hash_v1(source),
        "completed_chapter_ids": list(loaded["ordered_chapter_ids"]),
        "input_hash": loaded["input_hash"],
    }


def generate_chapter_runtime_profiles_v1(
    *,
    output_root: Path,
    profile: LiteraryContextPipelineProfile,
    b1_snapshot_root: Path,
    chapter_id: str,
    current_git_head: str,
) -> dict[str, Any]:
    output = Path(output_root).resolve()
    real_input = load_real_b1_run_input_v1(
        b1_snapshot_root, current_git_head=current_git_head
    )
    chapter_rows = [
        row for row in real_input["chapters"] if row["chapter_id"] == chapter_id
    ]
    if len(chapter_rows) != 1:
        raise LiteraryContextPipelineError(
            "generated profile chapter is absent from B1 snapshot"
        )
    b2_phase = load_b2_phase_a_profile(profile.b2_phase_profile_path)
    windows = build_b2_windows_v1(
        chapter_rows[0]["chapter"], profile=b2_phase
    )
    interaction_count = len(windows)
    if not 1 <= interaction_count <= int(
        profile.limits["b2_interaction_calls_per_chapter_cap"]
    ):
        raise LiteraryContextPipelineError(
            "B2 interaction windows exceed the outer sealed cap"
        )
    chapter_ordinal = int(chapter_rows[0]["chapter_ordinal"])

    dependencies = (
        profile.b2_phase_profile_path,
        profile.provider_profile_path,
        profile.structured_output_policy_path,
    )
    output.mkdir(parents=True, exist_ok=True)
    for source in dependencies:
        _copy_exact_file(source, output / source.name)

    profile_suffix = canonical_hash(
        {
            "outer_profile_hash": profile.profile_hash,
            "chapter_id": chapter_id,
            "b1_input_hash": real_input["input_hash"],
            "interaction_count": interaction_count,
        }
    )[:12]
    b2_body = {
        "schema_version": "literary_b2_ch1_canary_profile_v4",
        "profile_id": (
            f"literary_context_b2_{chapter_id}_{profile_suffix}"
        ),
        "b2_profile": profile.b2_phase_profile_path.name,
        "provider_profile": profile.provider_profile_path.name,
        "structured_output_policy": profile.structured_output_policy_path.name,
        "chapter_id": chapter_id,
        "role_bindings": {
            "frame": profile.role_bindings["b2_frame"],
            "interaction": profile.role_bindings["b2_interaction"],
        },
        "contract_versions": {
            "frame": profile.contract_versions["frame"],
            "interaction": profile.contract_versions["interaction"],
        },
        "limits": {
            "frame_calls": 1,
            "interaction_calls": interaction_count,
            "exception_calls": 0,
            "max_total_calls": 1 + interaction_count,
            "max_retries_per_call": 0,
            "hard_visible_token_cap": int(
                profile.limits[
                    "b2_hard_visible_token_cap_per_chapter"
                ]
            ),
        },
        "safety": {
            "source_run_may_be_historical": True,
            "certification_claim_allowed": False,
            "semantic_review_action": "persist_and_continue",
            "integrity_failure_action": "halt_before_next_call",
            "provider_fallback_allowed": False,
            "production_publish_enabled": False,
            "stop_after_chapter_id": chapter_id,
            "prior_frame_candidate_carry_required": chapter_ordinal > 1,
        },
    }
    recovery_body = {
        "schema_version": "literary_b2_recovery_live_profile_v3",
        "profile_id": (
            f"literary_context_recovery_{chapter_id}_{profile_suffix}"
        ),
        "provider_profile": profile.provider_profile_path.name,
        "structured_output_policy": profile.structured_output_policy_path.name,
        "stage_bindings": {
            "registry_recovery": {
                "provider_role_id": profile.role_bindings[
                    "registry_recovery"
                ],
                "schema_name": "literary_b2_registry_recovery_v1",
                "prompt_token_cap": profile.recovery_stage_limits[
                    "registry_recovery"
                ]["prompt_token_cap"],
                "max_output_tokens": profile.recovery_stage_limits[
                    "registry_recovery"
                ]["max_output_tokens"],
            },
            "event_review": {
                "provider_role_id": profile.role_bindings["event_review"],
                "schema_name": "literary_b2_event_review_v2",
                "prompt_token_cap": profile.recovery_stage_limits[
                    "event_review"
                ]["prompt_token_cap"],
                "max_output_tokens": profile.recovery_stage_limits[
                    "event_review"
                ]["max_output_tokens"],
            },
        },
        "generation": dict(profile.generation),
        "limits": {
            "registry_recovery_calls": int(
                profile.limits[
                    "recovery_registry_calls_per_chapter_cap"
                ]
            ),
            "event_review_calls": int(
                profile.limits["recovery_event_calls_per_chapter_cap"]
            ),
            "max_total_calls": int(
                profile.limits[
                    "recovery_registry_calls_per_chapter_cap"
                ]
            )
            + int(
                profile.limits["recovery_event_calls_per_chapter_cap"]
            ),
            "max_retries_per_call": 0,
            "hard_visible_token_cap": int(
                profile.limits[
                    "recovery_hard_visible_token_cap_per_chapter"
                ]
            ),
        },
        "safety": {
            "provider_fallback_allowed": False,
            "source_artifact_mutation_allowed": False,
            "book_global_identity_mutation_allowed": False,
            "production_publish_enabled": False,
            "stop_after_chapter_id": chapter_id,
            "event_review_contract_version": profile.contract_versions[
                "event_review"
            ],
        },
    }
    b2_path = output / "b2_profile.json"
    recovery_path = output / "recovery_profile.json"
    _write_or_verify_json(b2_path, b2_body)
    _write_or_verify_json(recovery_path, recovery_body)
    manifest_body = {
        "schema_version": GENERATED_PROFILE_SCHEMA_VERSION,
        "chapter_id": chapter_id,
        "chapter_ordinal": chapter_ordinal,
        "outer_profile_hash": profile.profile_hash,
        "b1_input_hash": real_input["input_hash"],
        "interaction_window_ids": [row["window_id"] for row in windows],
        "b2_profile_sha256": file_sha256(b2_path),
        "recovery_profile_sha256": file_sha256(recovery_path),
        "dependency_sha256": {
            source.name: file_sha256(source) for source in dependencies
        },
    }
    manifest = {
        **manifest_body,
        "generated_profile_hash": canonical_hash(manifest_body),
    }
    _write_or_verify_json(output / "generated_profile_manifest.json", manifest)
    return {
        "chapter_id": chapter_id,
        "chapter_ordinal": chapter_ordinal,
        "interaction_calls": interaction_count,
        "b2_profile_path": str(b2_path),
        "recovery_profile_path": str(recovery_path),
        "generated_profile_hash": manifest["generated_profile_hash"],
    }


def _verify_artifact_backend_v1(
    *,
    seal: Mapping[str, Any],
    report: Mapping[str, Any],
    expected_backend_mode: str,
    expected_shared_runtime_identity_hash: str | None,
    label: str,
) -> None:
    observed_mode = str(seal.get("backend_mode") or BACKEND_MODE_LEGACY)
    report_mode = str(report.get("backend_mode") or BACKEND_MODE_LEGACY)
    if observed_mode != expected_backend_mode or report_mode != expected_backend_mode:
        raise LiteraryContextPipelineError(
            f"{label} backend mode differs from the context run"
        )
    if expected_backend_mode == BACKEND_MODE_LEGACY:
        return
    identity = _object(
        seal.get("shared_runtime_identity"),
        f"{label}.shared_runtime_identity",
    )
    if (
        expected_shared_runtime_identity_hash is None
        or canonical_hash(identity) != expected_shared_runtime_identity_hash
    ):
        raise LiteraryContextPipelineError(
            f"{label} shared runtime identity differs from the context run"
        )


def build_context_chapter_checkpoint_v1(
    *,
    plan_hash: str,
    chapter_id: str,
    chapter_ordinal: int,
    b1_root: Path,
    b2_root: Path,
    recovery_root: Path,
    current_git_head: str,
    allow_historical_b1_tree_drift: bool = False,
    backend_mode: str = BACKEND_MODE_LEGACY,
    shared_runtime_identity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if backend_mode == BACKEND_MODE_SHARED_V1:
        identity = _object(
            shared_runtime_identity, "shared_runtime_identity"
        )
        shared_runtime_identity_hash = canonical_hash(identity)
    elif backend_mode == BACKEND_MODE_LEGACY:
        if shared_runtime_identity is not None:
            raise LiteraryContextPipelineError(
                "legacy checkpoint cannot bind a shared runtime"
            )
        identity = None
        shared_runtime_identity_hash = None
    else:
        raise LiteraryContextPipelineError(
            "checkpoint backend mode is outside the closed enum"
        )
    b1 = load_real_b1_run_input_v1(
        Path(b1_root).resolve(), current_git_head=current_git_head
    )
    selected = [
        row for row in b1["chapters"] if row["chapter_id"] == chapter_id
    ]
    if len(selected) != 1 or selected[0]["chapter_ordinal"] != chapter_ordinal:
        raise LiteraryContextPipelineError(
            "B1 source does not contain the checkpoint chapter"
        )
    b1_source = Path(b1_root).resolve()
    b2_source = Path(b2_root).resolve()
    recovery_source = Path(recovery_root).resolve()

    b2_seal = _verified_payload(
        b2_source / "run_seal.json", "seal_hash", "B2 run seal"
    )
    b2_artifact = _verified_payload(
        b2_source / "chapter_b2_artifact.json",
        "artifact_hash",
        "B2 chapter artifact",
    )
    b2_report = _verified_payload(
        b2_source / "live_report.json", "report_hash", "B2 live report"
    )
    current_b1_tree_hash = b2_source_tree_hash_v1(b1_source)
    sealed_b1_tree_hash = _required_string(
        b2_seal.get("source_tree_hash"), "B2 sealed source_tree_hash"
    )
    b1_tree_matches_seal = current_b1_tree_hash == sealed_b1_tree_hash
    if (
        b2_seal.get("chapter_id") != chapter_id
        or Path(str(b2_seal.get("source_run_root") or "")).resolve()
        != b1_source
        or (
            not b1_tree_matches_seal
            and not allow_historical_b1_tree_drift
        )
        or b2_seal.get("source_document_sha256")
        != b1["source_document_sha256"]
        or b2_seal.get("source_run_git_head")
        != b1["source_run_git_head"]
        or b2_seal.get("source_chapter_report_hash")
        != selected[0]["chapter_report_hash"]
        or b2_seal.get("source_prefix_bundle_hash")
        != selected[0]["prefix_bundle_hash"]
        or b2_artifact.get("chapter_id") != chapter_id
        or b2_report.get("chapter_id") != chapter_id
        or b2_report.get("chapter_artifact_hash")
        != b2_artifact["artifact_hash"]
        or b2_report.get("production_publish_performed") is not False
    ):
        raise LiteraryContextPipelineError(
            "B2 artifacts do not close over the selected B1 chapter"
        )
    _verify_artifact_backend_v1(
        seal=b2_seal,
        report=b2_report,
        expected_backend_mode=backend_mode,
        expected_shared_runtime_identity_hash=shared_runtime_identity_hash,
        label="B2",
    )

    recovery_seal = _verified_payload(
        recovery_source / "run_seal.json",
        "seal_hash",
        "recovery run seal",
    )
    recovery_report = _verified_payload(
        recovery_source / "live_report.json",
        "report_hash",
        "recovery live report",
    )
    projection = _verified_payload(
        recovery_source / "effective_b2_projection.json",
        "effective_projection_hash",
        "effective B2 projection",
    )
    if (
        recovery_seal.get("chapter_id") != chapter_id
        or Path(str(recovery_seal.get("source_b2_root") or "")).resolve()
        != b2_source
        or recovery_seal.get("source_tree_hash") != tree_hash_v1(b2_source)
        or recovery_seal.get("source_b2_artifact_hash")
        != b2_artifact["artifact_hash"]
        or recovery_report.get("status") != "complete"
        or recovery_report.get("chapter_id") != chapter_id
        or recovery_report.get("source_b2_artifact_hash")
        != b2_artifact["artifact_hash"]
        or recovery_report.get("effective_projection_hash")
        != projection["effective_projection_hash"]
        or recovery_report.get("production_publish_performed") is not False
        or projection.get("production_publish_performed") is not False
    ):
        raise LiteraryContextPipelineError(
            "recovery artifacts do not close over the selected B2 chapter"
        )
    _verify_artifact_backend_v1(
        seal=recovery_seal,
        report=recovery_report,
        expected_backend_mode=backend_mode,
        expected_shared_runtime_identity_hash=shared_runtime_identity_hash,
        label="recovery",
    )

    body = {
        "schema_version": (
            CHECKPOINT_SCHEMA_VERSION_SHARED
            if backend_mode == BACKEND_MODE_SHARED_V1
            else CHECKPOINT_SCHEMA_VERSION
        ),
        "plan_hash": _required_string(plan_hash, "plan_hash"),
        "chapter_id": chapter_id,
        "chapter_ordinal": chapter_ordinal,
        "git_head": current_git_head,
        "b1": {
            "root": str(b1_source),
            "tree_hash": current_b1_tree_hash,
            "sealed_tree_hash": sealed_b1_tree_hash,
            "tree_matches_b2_seal": b1_tree_matches_seal,
            "historical_tree_drift_allowed": (
                allow_historical_b1_tree_drift
            ),
            "input_hash": b1["input_hash"],
            "prefix_bundle_hash": selected[0]["prefix_bundle_hash"],
        },
        "b2": {
            "root": str(b2_source),
            "tree_hash": tree_hash_v1(b2_source),
            "run_seal_hash": b2_seal["seal_hash"],
            "chapter_artifact_hash": b2_artifact["artifact_hash"],
            "live_report_hash": b2_report["report_hash"],
            "calls_performed": b2_report.get("calls_performed"),
            "visible_tokens": b2_report.get("visible_tokens"),
        },
        "recovery": {
            "root": str(recovery_source),
            "tree_hash": tree_hash_v1(recovery_source),
            "run_seal_hash": recovery_seal["seal_hash"],
            "live_report_hash": recovery_report["report_hash"],
            "effective_projection_hash": projection[
                "effective_projection_hash"
            ],
            "provider_calls": recovery_report.get("provider_calls"),
            "visible_tokens": recovery_report.get("visible_tokens"),
            "pending_registry_ticket_count": recovery_report.get(
                "pending_registry_ticket_count"
            ),
            "pending_event_case_count": recovery_report.get(
                "pending_event_case_count"
            ),
        },
        "authority_boundary": {
            "b1_pending_claims_are_effective": False,
            "raw_b2_is_translator_authority": False,
            "effective_projection_is_chapter_observation_authority": True,
            "relation_phase_inference_performed": False,
        },
        "production_publish_performed": False,
    }
    if backend_mode == BACKEND_MODE_SHARED_V1:
        body["backend"] = {
            "backend_mode": BACKEND_MODE_SHARED_V1,
            "shared_runtime_identity": deepcopy(dict(identity)),
            "shared_runtime_identity_hash": shared_runtime_identity_hash,
        }
    return {**body, "checkpoint_hash": canonical_hash(body)}


def verify_context_chapter_checkpoint_v1(
    checkpoint_path: Path,
    *,
    current_git_head: str,
    expected_backend_mode: str | None = None,
    expected_shared_runtime_identity_hash: str | None = None,
) -> dict[str, Any]:
    checkpoint = _verified_payload(
        checkpoint_path, "checkpoint_hash", "context chapter checkpoint"
    )
    schema = checkpoint.get("schema_version")
    if schema not in {
        CHECKPOINT_SCHEMA_VERSION,
        CHECKPOINT_SCHEMA_VERSION_SHARED,
    }:
        raise LiteraryContextPipelineError("foreign context checkpoint schema")
    if checkpoint.get("git_head") != current_git_head:
        raise LiteraryContextPipelineError(
            "context checkpoint belongs to another Git HEAD"
        )
    if schema == CHECKPOINT_SCHEMA_VERSION_SHARED:
        backend = _object(checkpoint.get("backend"), "checkpoint.backend")
        checkpoint_backend_mode = _required_string(
            backend.get("backend_mode"), "checkpoint.backend_mode"
        )
        checkpoint_identity = _object(
            backend.get("shared_runtime_identity"),
            "checkpoint.shared_runtime_identity",
        )
        checkpoint_identity_hash = _required_string(
            backend.get("shared_runtime_identity_hash"),
            "checkpoint.shared_runtime_identity_hash",
        )
        if (
            checkpoint_backend_mode != BACKEND_MODE_SHARED_V1
            or canonical_hash(checkpoint_identity) != checkpoint_identity_hash
        ):
            raise LiteraryContextPipelineError(
                "shared checkpoint backend identity is malformed"
            )
    else:
        checkpoint_backend_mode = BACKEND_MODE_LEGACY
        checkpoint_identity = None
        checkpoint_identity_hash = None
    if (
        expected_backend_mode is not None
        and checkpoint_backend_mode != expected_backend_mode
    ):
        raise LiteraryContextPipelineError(
            "context checkpoint backend mode differs from the run plan"
        )
    if (
        expected_shared_runtime_identity_hash is not None
        and checkpoint_identity_hash != expected_shared_runtime_identity_hash
    ):
        raise LiteraryContextPipelineError(
            "context checkpoint shared runtime differs from the run plan"
        )
    rebuilt = build_context_chapter_checkpoint_v1(
        plan_hash=_required_string(checkpoint.get("plan_hash"), "plan_hash"),
        chapter_id=_required_string(checkpoint.get("chapter_id"), "chapter_id"),
        chapter_ordinal=_bounded_int(
            checkpoint.get("chapter_ordinal"),
            "chapter_ordinal",
            1,
            1000,
        ),
        b1_root=Path(
            _required_string(
                _object(checkpoint.get("b1"), "b1").get("root"), "b1.root"
            )
        ),
        b2_root=Path(
            _required_string(
                _object(checkpoint.get("b2"), "b2").get("root"), "b2.root"
            )
        ),
        recovery_root=Path(
            _required_string(
                _object(checkpoint.get("recovery"), "recovery").get("root"),
                "recovery.root",
            )
        ),
        current_git_head=current_git_head,
        allow_historical_b1_tree_drift=bool(
            _object(checkpoint.get("b1"), "b1").get(
                "historical_tree_drift_allowed"
            )
        ),
        backend_mode=checkpoint_backend_mode,
        shared_runtime_identity=checkpoint_identity,
    )
    if rebuilt["checkpoint_hash"] != checkpoint["checkpoint_hash"]:
        raise LiteraryContextPipelineError(
            "context checkpoint no longer matches source artifacts"
        )
    return checkpoint


def replay_context_pipeline_artifacts_v1(
    *,
    output_root: Path,
    b1_root: Path,
    chapter_artifacts: Sequence[Mapping[str, Any]],
    current_git_head: str,
) -> dict[str, Any]:
    output = Path(output_root).resolve()
    if output.exists():
        raise LiteraryContextPipelineError(
            f"replay output root already exists: {output}"
        )
    if not chapter_artifacts:
        raise LiteraryContextPipelineError("replay has no chapter artifacts")
    b1_source = Path(b1_root).resolve()
    source_hashes_before = {"b1": tree_hash_v1(b1_source)}
    normalized_rows: list[dict[str, Any]] = []
    for ordinal, raw in enumerate(chapter_artifacts, 1):
        chapter_id = _required_string(raw.get("chapter_id"), "chapter_id")
        b2_root = Path(_required_string(raw.get("b2_root"), "b2_root")).resolve()
        recovery_root = Path(
            _required_string(raw.get("recovery_root"), "recovery_root")
        ).resolve()
        source_hashes_before[f"b2:{chapter_id}"] = tree_hash_v1(b2_root)
        source_hashes_before[f"recovery:{chapter_id}"] = tree_hash_v1(
            recovery_root
        )
        normalized_rows.append(
            {
                "chapter_id": chapter_id,
                "chapter_ordinal": ordinal,
                "b2_root": str(b2_root),
                "recovery_root": str(recovery_root),
            }
        )

    plan_body = {
        "schema_version": REPLAY_PLAN_SCHEMA_VERSION,
        "mode": "offline_replay",
        "git_head": current_git_head,
        "b1_root": str(b1_source),
        "source_tree_hashes": source_hashes_before,
        "chapters": normalized_rows,
        "api_calls_allowed": 0,
        "production_publish_enabled": False,
    }
    plan = {**plan_body, "plan_hash": canonical_hash(plan_body)}
    output.mkdir(parents=True)
    _write_new_json(output / "replay_plan.json", plan)
    checkpoints: list[dict[str, Any]] = []
    for row in normalized_rows:
        checkpoint = build_context_chapter_checkpoint_v1(
            plan_hash=plan["plan_hash"],
            chapter_id=row["chapter_id"],
            chapter_ordinal=row["chapter_ordinal"],
            b1_root=b1_source,
            b2_root=Path(row["b2_root"]),
            recovery_root=Path(row["recovery_root"]),
            current_git_head=current_git_head,
            allow_historical_b1_tree_drift=True,
        )
        path = (
            output
            / "chapters"
            / f"ch{row['chapter_ordinal']:03d}"
            / "context_checkpoint.json"
        )
        _write_new_json(path, checkpoint)
        checkpoints.append(
            {
                "chapter_id": row["chapter_id"],
                "path": path.relative_to(output).as_posix(),
                "checkpoint_hash": checkpoint["checkpoint_hash"],
            }
        )

    source_hashes_after = {"b1": tree_hash_v1(b1_source)}
    for row in normalized_rows:
        source_hashes_after[f"b2:{row['chapter_id']}"] = tree_hash_v1(
            Path(row["b2_root"])
        )
        source_hashes_after[f"recovery:{row['chapter_id']}"] = tree_hash_v1(
            Path(row["recovery_root"])
        )
    if source_hashes_after != source_hashes_before:
        raise LiteraryContextPipelineError(
            "offline replay mutated a source artifact"
        )
    summary_body = {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "mode": "offline_replay",
        "status": "complete",
        "plan_hash": plan["plan_hash"],
        "completed_chapter_ids": [
            row["chapter_id"] for row in normalized_rows
        ],
        "chapter_checkpoints": checkpoints,
        "api_calls_performed": 0,
        "source_artifacts_mutated_by_replay": False,
        "historical_source_tree_drift": any(
            not _object(
                _read_object(
                    output / row["path"], "replay context checkpoint"
                ).get("b1"),
                "b1",
            ).get("tree_matches_b2_seal")
            for row in checkpoints
        ),
        "production_publish_performed": False,
    }
    summary = {**summary_body, "summary_hash": canonical_hash(summary_body)}
    _write_new_json(output / "run_summary.json", summary)
    return summary


def initialize_context_pipeline_run_v1(
    *,
    run_root: Path,
    document_path: Path,
    profile_path: Path,
    frozen_db: Path,
    ordered_chapter_ids: Sequence[str],
    current_git_head: str,
    backend_mode: str = BACKEND_MODE_LEGACY,
    shared_runtime: LiterarySharedRunnerBindingsV1 | None = None,
) -> dict[str, Any]:
    root = Path(run_root).resolve()
    if root.exists():
        raise LiteraryContextPipelineError(
            f"context pipeline root already exists: {root}"
        )
    profile = load_context_pipeline_profile_v1(profile_path)
    document_source = Path(document_path).resolve()
    document = _read_object(document_source, "source document")
    chapters = document.get("chapters")
    if not isinstance(chapters, list) or not chapters:
        raise LiteraryContextPipelineError("source document has no chapters")
    document_ids = [
        _required_string(row.get("chapter_id"), "document chapter_id")
        for row in chapters
        if isinstance(row, Mapping)
    ]
    selected = list(ordered_chapter_ids)
    if (
        not selected
        or len(selected) > int(profile.limits["max_chapters_per_run"])
        or selected != document_ids[: len(selected)]
    ):
        raise LiteraryContextPipelineError(
            "context pipeline requires one bounded contiguous document prefix"
        )
    frozen_hash = file_sha256(frozen_db).upper()
    if frozen_hash != FROZEN_DB_SHA256:
        raise LiteraryContextPipelineError(
            "frozen DB differs from the accepted baseline"
        )
    b1_profile = load_literary_pipeline_profile(
        profile.b1_pipeline_profile_path
    )
    backend = _backend_binding_v1(
        backend_mode=backend_mode, shared_runtime=shared_runtime
    )
    plan_body = {
        "schema_version": (
            PLAN_SCHEMA_VERSION_SHARED
            if backend_mode == BACKEND_MODE_SHARED_V1
            else PLAN_SCHEMA_VERSION
        ),
        "profile_path": str(profile.source_path),
        "profile_sha256": profile.source_sha256,
        "profile_id": profile.profile_id,
        "profile_hash": profile.profile_hash,
        "document_path": str(document_source),
        "document_sha256": file_sha256(document_source),
        "frozen_db_path": str(Path(frozen_db).resolve()),
        "frozen_db_sha256": frozen_hash,
        "ordered_chapter_ids": selected,
        "git_head": current_git_head,
        "b1_pipeline_profile_path": str(
            profile.b1_pipeline_profile_path
        ),
        "b1_pipeline_profile_sha256": file_sha256(
            profile.b1_pipeline_profile_path
        ),
        "production_publish_enabled": False,
    }
    if backend_mode == BACKEND_MODE_SHARED_V1:
        plan_body.update(backend)
    plan = {**plan_body, "plan_hash": canonical_hash(plan_body)}
    root.mkdir(parents=True)
    _write_new_json(root / "run_plan.json", plan)
    initialize_chapter_cycle_run_v1(
        run_root=root / "b1_cycle",
        document_path=document_source,
        profile_path=b1_profile.chapter_cycle_profile_path,
        frozen_db_path=Path(frozen_db).resolve(),
        ordered_chapter_ids=selected,
        stop_after_chapter_count=1,
        pipeline_profile_path=profile.b1_pipeline_profile_path,
    )
    state_body = {
        "schema_version": (
            STATE_SCHEMA_VERSION_SHARED
            if backend_mode == BACKEND_MODE_SHARED_V1
            else STATE_SCHEMA_VERSION
        ),
        "plan_hash": plan["plan_hash"],
        "status": "running",
        "completed_chapter_ids": [],
        "current_chapter_id": selected[0],
        "current_stage": "b1",
        "chapter_checkpoints": [],
        "halt_reason": None,
        "production_publish_performed": False,
    }
    if backend_mode == BACKEND_MODE_SHARED_V1:
        state_body["backend_mode"] = backend_mode
        state_body["shared_runtime_identity_hash"] = backend[
            "shared_runtime_identity_hash"
        ]
    state = {**state_body, "state_hash": canonical_hash(state_body)}
    write_checkpoint_atomic(root / "run_state.json", state)
    return state


def load_context_pipeline_state_v1(run_root: Path) -> dict[str, Any]:
    root = Path(run_root).resolve()
    plan = _load_context_plan(root)
    backend = _plan_backend_binding_v1(plan)
    state = _read_object(root / "run_state.json", "context pipeline state")
    expected_state_schema = (
        STATE_SCHEMA_VERSION_SHARED
        if backend["backend_mode"] == BACKEND_MODE_SHARED_V1
        else STATE_SCHEMA_VERSION
    )
    if state.get("schema_version") != expected_state_schema:
        raise LiteraryContextPipelineError("foreign context pipeline state")
    _verify_embedded_hash(state, "state_hash", "context pipeline state")
    if state.get("plan_hash") != plan["plan_hash"]:
        raise LiteraryContextPipelineError(
            "context pipeline state belongs to another plan"
        )
    if backend["backend_mode"] == BACKEND_MODE_SHARED_V1 and (
        state.get("backend_mode") != BACKEND_MODE_SHARED_V1
        or state.get("shared_runtime_identity_hash")
        != backend["shared_runtime_identity_hash"]
    ):
        raise LiteraryContextPipelineError(
            "shared context state backend identity differs from its plan"
        )
    completed = _string_list(
        state.get("completed_chapter_ids"), "completed_chapter_ids"
    )
    if completed != plan["ordered_chapter_ids"][: len(completed)]:
        raise LiteraryContextPipelineError(
            "context pipeline state is not a contiguous prefix"
        )
    checkpoint_rows = state.get("chapter_checkpoints")
    if (
        not isinstance(checkpoint_rows, list)
        or len(checkpoint_rows) != len(completed)
    ):
        raise LiteraryContextPipelineError(
            "context pipeline checkpoint index is stale"
        )
    for ordinal, row in enumerate(checkpoint_rows, 1):
        if not isinstance(row, Mapping):
            raise LiteraryContextPipelineError(
                "context checkpoint index row is malformed"
            )
        path = _contained_path(root, row.get("path"), "context checkpoint")
        checkpoint = verify_context_chapter_checkpoint_v1(
            path,
            current_git_head=plan["git_head"],
            expected_backend_mode=backend["backend_mode"],
            expected_shared_runtime_identity_hash=backend[
                "shared_runtime_identity_hash"
            ],
        )
        if (
            checkpoint["chapter_id"] != completed[ordinal - 1]
            or checkpoint["checkpoint_hash"] != row.get("checkpoint_hash")
        ):
            raise LiteraryContextPipelineError(
                "context checkpoint index points to a foreign chapter"
            )
    return state


def run_context_pipeline_live_v1(
    *,
    run_root: Path,
    credential_root: Path | None,
    current_git_head: str,
    usage_roots: Sequence[Path] | None = None,
    allow_resume: bool = False,
    backend_mode: str = BACKEND_MODE_LEGACY,
    shared_runtime: LiterarySharedRunnerBindingsV1 | None = None,
) -> dict[str, Any]:
    root = Path(run_root).resolve()
    plan = _load_context_plan(root)
    profile = load_context_pipeline_profile_v1(Path(plan["profile_path"]))
    if backend_mode == BACKEND_MODE_SHARED_V1 and credential_root is not None:
        raise LiteraryContextPipelineError(
            "shared context run cannot receive a legacy credential root"
        )
    if backend_mode == BACKEND_MODE_LEGACY and credential_root is None:
        raise LiteraryContextPipelineError(
            "legacy context run requires its explicit credential root"
        )
    _verify_live_plan_inputs(
        plan,
        profile,
        current_git_head=current_git_head,
        backend_mode=backend_mode,
        shared_runtime=shared_runtime,
    )
    state = load_context_pipeline_state_v1(root)
    if state["status"] == "complete":
        return write_context_pipeline_summary_v1(root)
    if state["status"] == "paused":
        if not allow_resume:
            raise LiteraryContextPipelineError(
                "paused context pipeline requires an explicit resume"
            )
        state = _save_context_state(
            root,
            state,
            status="running",
            halt_reason=None,
        )
    selected = list(plan["ordered_chapter_ids"])
    try:
        while len(state["completed_chapter_ids"]) < len(selected):
            ordinal = len(state["completed_chapter_ids"]) + 1
            chapter_id = selected[ordinal - 1]
            state = _save_context_state(
                root,
                state,
                status="running",
                current_chapter_id=chapter_id,
                current_stage="b1",
                halt_reason=None,
            )
            _run_b1_through_chapter(
                root=root,
                target_chapter_count=ordinal,
                credential_root=credential_root,
                usage_roots=usage_roots,
                backend_mode=backend_mode,
                shared_runtime=shared_runtime,
            )
            b1_root = root / "b1_cycle"
            write_literary_run_summary_v1(b1_root)

            state = _save_context_state(
                root, state, current_stage="b1_snapshot"
            )
            snapshot_root = root / "b1_snapshots" / f"ch{ordinal:03d}"
            snapshot_completed_b1_prefix_v1(
                source_run_root=b1_root,
                output_root=snapshot_root,
                chapter_count=ordinal,
                current_git_head=current_git_head,
            )

            generated = generate_chapter_runtime_profiles_v1(
                output_root=root
                / "generated_profiles"
                / f"ch{ordinal:03d}",
                profile=profile,
                b1_snapshot_root=snapshot_root,
                chapter_id=chapter_id,
                current_git_head=current_git_head,
            )

            state = _save_context_state(root, state, current_stage="b2")
            b2_root = _run_b2_chapter(
                root=root,
                chapter_ordinal=ordinal,
                snapshot_root=snapshot_root,
                b2_profile_path=Path(generated["b2_profile_path"]),
                credential_root=credential_root,
                frozen_db=Path(plan["frozen_db_path"]),
                current_git_head=current_git_head,
                max_attempts=int(
                    profile.limits["max_b2_attempts_per_chapter"]
                ),
                backend_mode=backend_mode,
                shared_runtime=shared_runtime,
            )

            state = _save_context_state(
                root, state, current_stage="recovery_event_audit"
            )
            recovery_root = _run_recovery_chapter(
                root=root,
                chapter_ordinal=ordinal,
                b2_root=b2_root,
                recovery_profile_path=Path(
                    generated["recovery_profile_path"]
                ),
                credential_root=credential_root,
                frozen_db=Path(plan["frozen_db_path"]),
                max_attempts=int(
                    profile.limits["max_recovery_attempts_per_chapter"]
                ),
                backend_mode=backend_mode,
                shared_runtime=shared_runtime,
            )

            state = _save_context_state(
                root, state, current_stage="context_checkpoint"
            )
            checkpoint = build_context_chapter_checkpoint_v1(
                plan_hash=plan["plan_hash"],
                chapter_id=chapter_id,
                chapter_ordinal=ordinal,
                b1_root=snapshot_root,
                b2_root=b2_root,
                recovery_root=recovery_root,
                current_git_head=current_git_head,
                backend_mode=backend_mode,
                shared_runtime_identity=(
                    shared_runtime.identity_payload()
                    if shared_runtime is not None
                    else None
                ),
            )
            checkpoint_path = (
                root
                / "chapters"
                / f"ch{ordinal:03d}"
                / "context_checkpoint.json"
            )
            _write_or_verify_json(checkpoint_path, checkpoint)
            checkpoint_rows = [
                *state["chapter_checkpoints"],
                {
                    "chapter_id": chapter_id,
                    "path": checkpoint_path.relative_to(root).as_posix(),
                    "checkpoint_hash": checkpoint["checkpoint_hash"],
                },
            ]
            completed = [*state["completed_chapter_ids"], chapter_id]
            state = _save_context_state(
                root,
                state,
                status=(
                    "complete"
                    if len(completed) == len(selected)
                    else "running"
                ),
                completed_chapter_ids=completed,
                chapter_checkpoints=checkpoint_rows,
                current_chapter_id=(
                    None
                    if len(completed) == len(selected)
                    else selected[len(completed)]
                ),
                current_stage=(
                    None
                    if len(completed) == len(selected)
                    else "b1"
                ),
            )
        return write_context_pipeline_summary_v1(root)
    except Exception as exc:
        current = load_context_pipeline_state_v1(root)
        _save_context_state(
            root,
            current,
            status="paused",
            halt_reason=f"{type(exc).__name__}: {exc}",
        )
        raise


def write_context_pipeline_summary_v1(run_root: Path) -> dict[str, Any]:
    root = Path(run_root).resolve()
    plan = _load_context_plan(root)
    state = load_context_pipeline_state_v1(root)
    totals = {
        "b2_calls": 0,
        "b2_visible_tokens": 0,
        "recovery_calls": 0,
        "recovery_visible_tokens": 0,
    }
    for row in state["chapter_checkpoints"]:
        checkpoint = _read_object(
            _contained_path(root, row.get("path"), "context checkpoint"),
            "context checkpoint",
        )
        totals["b2_calls"] += int(
            _object(checkpoint.get("b2"), "b2").get("calls_performed") or 0
        )
        totals["b2_visible_tokens"] += int(
            _object(checkpoint.get("b2"), "b2").get("visible_tokens") or 0
        )
        totals["recovery_calls"] += int(
            _object(checkpoint.get("recovery"), "recovery").get(
                "provider_calls"
            )
            or 0
        )
        totals["recovery_visible_tokens"] += int(
            _object(checkpoint.get("recovery"), "recovery").get(
                "visible_tokens"
            )
            or 0
        )
    body = {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "mode": "live",
        "status": state["status"],
        "plan_hash": plan["plan_hash"],
        "state_hash": state["state_hash"],
        "completed_chapter_ids": list(state["completed_chapter_ids"]),
        "chapter_checkpoints": deepcopy(state["chapter_checkpoints"]),
        "downstream_usage": totals,
        "halt_reason": state.get("halt_reason"),
        "production_publish_performed": False,
    }
    summary = {**body, "summary_hash": canonical_hash(body)}
    write_checkpoint_atomic(root / "run_summary.json", summary)
    return summary


def _run_b1_through_chapter(
    *,
    root: Path,
    target_chapter_count: int,
    credential_root: Path | None,
    usage_roots: Sequence[Path] | None,
    backend_mode: str = BACKEND_MODE_LEGACY,
    shared_runtime: LiterarySharedRunnerBindingsV1 | None = None,
) -> None:
    b1_root = root / "b1_cycle"
    state = load_chapter_cycle_state_v1(b1_root)
    completed = len(state["completed_chapter_ids"])
    if completed >= target_chapter_count:
        return
    if state["status"] != "running":
        resume_chapter_cycle_run_v1(
            run_root=b1_root,
            stop_after_chapter_count=target_chapter_count,
        )
    executor = ChapterCycleLiveExecutorV1(
        run_root=b1_root,
        plan=load_chapter_cycle_plan_v1(b1_root),
        credential_root=credential_root,
        usage_roots=tuple(usage_roots or ()) or None,
        backend_mode=backend_mode,
        shared_runtime=shared_runtime,
    )
    result = run_chapter_cycle_until_boundary_v1(
        run_root=b1_root, executor=executor
    )
    if len(result["completed_chapter_ids"]) < target_chapter_count:
        raise LiteraryContextPipelineError(
            "B1 stopped before the requested chapter checkpoint"
        )


def _run_b2_chapter(
    *,
    root: Path,
    chapter_ordinal: int,
    snapshot_root: Path,
    b2_profile_path: Path,
    credential_root: Path | None,
    frozen_db: Path,
    current_git_head: str,
    max_attempts: int,
    backend_mode: str = BACKEND_MODE_LEGACY,
    shared_runtime: LiterarySharedRunnerBindingsV1 | None = None,
) -> Path:
    chapter_root = root / "b2" / f"ch{chapter_ordinal:03d}"
    completed = _completed_attempt_root(
        chapter_root, required_file="chapter_b2_artifact.json"
    )
    if completed is not None:
        report = _verified_payload(
            completed / "live_report.json", "report_hash", "B2 live report"
        )
        _verify_attempt_backend_v1(
            attempt_root=completed,
            report=report,
            backend_mode=backend_mode,
            shared_runtime=shared_runtime,
            label="B2 completed attempt",
        )
        return completed
    attempts = _attempt_roots(chapter_root)
    if attempts:
        active = attempts[-1]
        if backend_mode == BACKEND_MODE_SHARED_V1:
            _verify_attempt_backend_v1(
                attempt_root=active,
                report=None,
                backend_mode=backend_mode,
                shared_runtime=shared_runtime,
                label="B2 resumable attempt",
            )
        if any(active.glob("**/stage_failure.json")):
            raise LiteraryContextPipelineError(
                "failed B2 attempt is immutable; an explicit retry profile is required"
            )
        execute_b2_frame_live_v1(
            output_root=active,
            credential_root=credential_root,
            frozen_db=frozen_db,
            current_git_head=current_git_head,
            backend_mode=backend_mode,
            shared_runtime=shared_runtime,
        )
        execute_b2_interactions_live_v1(
            output_root=active,
            credential_root=credential_root,
            frozen_db=frozen_db,
            current_git_head=current_git_head,
            backend_mode=backend_mode,
            shared_runtime=shared_runtime,
        )
        return active
    if max_attempts < 1:
        raise LiteraryContextPipelineError("B2 attempt cap is closed")
    output = chapter_root / "attempt_001"
    prior_b2_root = None
    if chapter_ordinal > 1:
        prior_b2_root = _completed_attempt_root(
            root / "b2" / f"ch{chapter_ordinal - 1:03d}",
            required_file="chapter_b2_artifact.json",
        )
        if prior_b2_root is None:
            raise LiteraryContextPipelineError(
                "B2 prior-frame carry lacks the preceding completed chapter"
            )
    prepare_b2_ch1_canary_v1(
        source_run_root=snapshot_root,
        output_root=output,
        canary_profile_path=b2_profile_path,
        credential_root=credential_root,
        frozen_db=frozen_db,
        current_git_head=current_git_head,
        prior_b2_root=prior_b2_root,
        backend_mode=backend_mode,
        shared_runtime=shared_runtime,
    )
    execute_b2_frame_live_v1(
        output_root=output,
        credential_root=credential_root,
        frozen_db=frozen_db,
        current_git_head=current_git_head,
        backend_mode=backend_mode,
        shared_runtime=shared_runtime,
    )
    execute_b2_interactions_live_v1(
        output_root=output,
        credential_root=credential_root,
        frozen_db=frozen_db,
        current_git_head=current_git_head,
        backend_mode=backend_mode,
        shared_runtime=shared_runtime,
    )
    return output


def _run_recovery_chapter(
    *,
    root: Path,
    chapter_ordinal: int,
    b2_root: Path,
    recovery_profile_path: Path,
    credential_root: Path | None,
    frozen_db: Path,
    max_attempts: int,
    backend_mode: str = BACKEND_MODE_LEGACY,
    shared_runtime: LiterarySharedRunnerBindingsV1 | None = None,
) -> Path:
    chapter_root = root / "b2_recovery" / f"ch{chapter_ordinal:03d}"
    completed = _completed_attempt_root(
        chapter_root, required_file="effective_b2_projection.json"
    )
    if completed is not None:
        report = _verified_payload(
            completed / "live_report.json",
            "report_hash",
            "recovery live report",
        )
        _verify_attempt_backend_v1(
            attempt_root=completed,
            report=report,
            backend_mode=backend_mode,
            shared_runtime=shared_runtime,
            label="recovery completed attempt",
        )
        return completed
    attempts = _attempt_roots(chapter_root)
    if len(attempts) >= max_attempts:
        raise LiteraryContextPipelineError(
            "recovery attempt cap was exhausted"
        )
    attempt_number = len(attempts) + 1
    output = chapter_root / f"attempt_{attempt_number:03d}"
    resume_from = attempts[-1] if attempts else None
    if resume_from is not None and backend_mode == BACKEND_MODE_SHARED_V1:
        _verify_attempt_backend_v1(
            attempt_root=resume_from,
            report=None,
            backend_mode=backend_mode,
            shared_runtime=shared_runtime,
            label="recovery resumable attempt",
        )
    run_b2_recovery_live_v1(
        repo_root=Path(__file__).resolve().parents[2],
        b2_root=b2_root,
        output_root=output,
        profile_path=recovery_profile_path,
        credential_root=credential_root,
        frozen_db=frozen_db,
        resume_from_root=resume_from,
        backend_mode=backend_mode,
        shared_runtime=shared_runtime,
    )
    return output


def _load_context_plan(root: Path) -> dict[str, Any]:
    plan = _read_object(root / "run_plan.json", "context pipeline plan")
    _plan_backend_binding_v1(plan)
    _verify_embedded_hash(plan, "plan_hash", "context pipeline plan")
    return plan


def _verify_live_plan_inputs(
    plan: Mapping[str, Any],
    profile: LiteraryContextPipelineProfile,
    *,
    current_git_head: str,
    backend_mode: str,
    shared_runtime: LiterarySharedRunnerBindingsV1 | None,
) -> None:
    expected_backend = _plan_backend_binding_v1(plan)
    actual_backend = _backend_binding_v1(
        backend_mode=backend_mode, shared_runtime=shared_runtime
    )
    if (
        plan.get("git_head") != current_git_head
        or plan.get("profile_hash") != profile.profile_hash
        or plan.get("profile_sha256") != file_sha256(profile.source_path)
        or plan.get("document_sha256")
        != file_sha256(Path(str(plan["document_path"])))
        or plan.get("frozen_db_sha256")
        != file_sha256(Path(str(plan["frozen_db_path"]))).upper()
        or plan.get("frozen_db_sha256") != FROZEN_DB_SHA256
        or plan.get("production_publish_enabled") is not False
        or expected_backend["backend_mode"] != actual_backend["backend_mode"]
        or expected_backend["shared_runtime_identity_hash"]
        != actual_backend["shared_runtime_identity_hash"]
    ):
        raise LiteraryContextPipelineError(
            "context pipeline plan inputs changed after sealing"
        )


def _save_context_state(
    root: Path, state: Mapping[str, Any], **updates: Any
) -> dict[str, Any]:
    body = deepcopy(dict(state))
    body.pop("state_hash", None)
    body.update(updates)
    result = {**body, "state_hash": canonical_hash(body)}
    write_checkpoint_atomic(root / "run_state.json", result)
    return result


def _attempt_roots(chapter_root: Path) -> list[Path]:
    if not chapter_root.is_dir():
        return []
    return sorted(
        row
        for row in chapter_root.glob("attempt_*")
        if row.is_dir()
    )


def _completed_attempt_root(
    chapter_root: Path, *, required_file: str
) -> Path | None:
    for attempt in reversed(_attempt_roots(chapter_root)):
        report_path = attempt / "live_report.json"
        if not report_path.is_file() or not (attempt / required_file).is_file():
            continue
        report = _verified_payload(
            report_path, "report_hash", "completed attempt report"
        )
        if str(report.get("status") or "").startswith("complete"):
            return attempt
    return None


def _verify_attempt_backend_v1(
    *,
    attempt_root: Path,
    report: Mapping[str, Any] | None,
    backend_mode: str,
    shared_runtime: LiterarySharedRunnerBindingsV1 | None,
    label: str,
) -> None:
    seal = _verified_payload(
        Path(attempt_root) / "run_seal.json", "seal_hash", f"{label} seal"
    )
    runtime_identity_hash = (
        canonical_hash(shared_runtime.identity_payload())
        if shared_runtime is not None
        else None
    )
    report_payload = (
        report
        if report is not None
        else {"backend_mode": seal.get("backend_mode")}
    )
    _verify_artifact_backend_v1(
        seal=seal,
        report=report_payload,
        expected_backend_mode=backend_mode,
        expected_shared_runtime_identity_hash=runtime_identity_hash,
        label=label,
    )


def _verified_payload(
    path: Path, hash_field: str, label: str
) -> dict[str, Any]:
    payload = _read_object(path, label)
    _verify_embedded_hash(payload, hash_field, label)
    return payload


def _verify_embedded_hash(
    payload: Mapping[str, Any], hash_field: str, label: str
) -> None:
    body = deepcopy(dict(payload))
    observed = _required_string(body.pop(hash_field, None), hash_field)
    if canonical_hash(body) != observed:
        raise LiteraryContextPipelineError(
            f"{label} {hash_field} mismatch"
        )


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise LiteraryContextPipelineError(
            f"cannot read {label}: {path}"
        ) from exc
    if not isinstance(value, dict):
        raise LiteraryContextPipelineError(f"{label} must be an object")
    return value


def _write_new_json(path: Path, value: Mapping[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise LiteraryContextPipelineError(
            f"refusing to overwrite immutable artifact: {target}"
        )
    target.write_text(canonical_json(dict(value)) + "\n", encoding="utf-8")


def _write_or_verify_json(path: Path, value: Mapping[str, Any]) -> None:
    target = Path(path)
    if target.is_file():
        if _read_object(target, "existing immutable JSON") != dict(value):
            raise LiteraryContextPipelineError(
                f"immutable artifact differs: {target}"
            )
        return
    _write_new_json(target, value)


def _copy_exact_file(source: Path, target: Path) -> None:
    if target.is_file():
        if file_sha256(source) != file_sha256(target):
            raise LiteraryContextPipelineError(
                f"generated dependency differs: {target}"
            )
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def _contained_path(root: Path, value: Any, label: str) -> Path:
    relative = Path(_required_string(value, label))
    if relative.is_absolute():
        raise LiteraryContextPipelineError(f"{label} must be relative")
    root_resolved = Path(root).resolve()
    path = (root_resolved / relative).resolve()
    try:
        path.relative_to(root_resolved)
    except ValueError as exc:
        raise LiteraryContextPipelineError(f"{label} escapes its root") from exc
    if not path.is_file():
        raise LiteraryContextPipelineError(f"{label} is absent: {path}")
    return path


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise LiteraryContextPipelineError(f"{label} must be an object")
    return dict(value)


def _string_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list):
        raise LiteraryContextPipelineError(f"{label} must be a list")
    return [
        _required_string(item, f"{label} item")
        for item in value
    ]


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LiteraryContextPipelineError(
            f"{label} must be a non-empty string"
        )
    return value.strip()


def _bounded_int(value: Any, label: str, low: int, high: int) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not low <= value <= high
    ):
        raise LiteraryContextPipelineError(
            f"{label} must be in [{low}, {high}]"
        )
    return value
