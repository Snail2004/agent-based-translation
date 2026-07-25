from __future__ import annotations

import copy
import ctypes
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence
import uuid

from pipeline.eval.benchmark_aggregate_v1 import (
    compose_benchmark_run_report_v1,
    persist_benchmark_run_report_v1,
    validate_benchmark_run_report_v1,
)
from pipeline.eval.benchmark_v1 import (
    BENCHMARK_ARM_IDS_V1,
    BENCHMARK_CHAPTER_IDS_V1,
    source_read_model_sha256_v1,
    validate_benchmark_manifest_v1,
    validate_benchmark_overlay_v1,
    validate_benchmark_preflight_v1,
)
from pipeline.eval.common_input_v1 import (
    CommonEvaluationInputV1,
    CommonSourceSnapshotV1,
)
from pipeline.eval.contracts_v1 import (
    CanonicalPolicy,
    ContractValidationError,
    canonicalize,
    require_commit,
    require_enum,
    require_exact_keys,
    require_int,
    require_list,
    require_mapping,
    require_nullable_string,
    require_relative_path,
    require_rfc3339,
    require_sha256,
    require_string,
    seal_payload,
    validate_producer,
    verify_payload_hash,
)
from pipeline.eval.end_to_end_runner_v1 import (
    EndToEndEvaluationResultV1,
    LocalSfQeRuntimeV1,
    run_evaluation_end_to_end_v1,
)
from pipeline.eval.execution_runner_v1 import validate_evaluation_execution_artifact
from pipeline.eval.evaluation_workflow_settings_v1 import (
    validate_evaluation_workflow_settings_v1,
)
from pipeline.eval.full_run_report_v1 import validate_full_run_report
from pipeline.eval.method_executors_v1 import SharedEvaluationRoleRunnerV1
from pipeline.eval.offline_orchestrator_v1 import (
    build_evaluation_plan,
    validate_evaluation_run_config,
)
from pipeline.eval.workflow_component_writer_v1 import (
    EvaluationWorkflowComponentWriterV1,
    EvaluationWorkflowRunContextV1,
    benchmark_workflow_stages_v1,
)
from pipeline.eval.workflow_recovery_v1 import classify_evaluation_failure_v1
from pipeline.llm_backend import SharedLlmAttemptLedger
from pipeline.llm_backend import canonical_sha256 as shared_canonical_sha256


__all__ = [
    "BenchmarkChapterRuntimeV1",
    "BenchmarkEndToEndResultV1",
    "EvaluationWorkflowRunContextV1",
    "run_benchmark_end_to_end_v1",
    "validate_benchmark_chapter_checkpoint_v1",
    "validate_benchmark_run_status_v1",
]


RUN_MANIFEST_SCHEMA_ID = "EvaluationBenchmarkRunManifestV1"
RUN_EVENT_SCHEMA_ID = "EvaluationBenchmarkRunEventV1"
RUN_STATUS_SCHEMA_ID = "EvaluationBenchmarkRunStatusV1"
CHAPTER_CHECKPOINT_SCHEMA_ID = "EvaluationBenchmarkChapterCheckpointV1"
SCHEMA_VERSION = "1.1.0"
_MANIFEST_HASH_PATH = ("integrity", "manifest_sha256")
_EVENT_HASH_PATH = ("integrity", "event_sha256")
_CHECKPOINT_HASH_PATH = ("integrity", "checkpoint_sha256")
_MANIFEST_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(),
    semantic_sequence_paths=frozenset(
        {
            ("identity", "selected_chapter_ids"),
            ("identity", "selected_arm_ids"),
            ("identity", "selected_scorer_ids"),
            ("chapter_bindings",),
        }
    ),
)
_EVENT_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(), semantic_sequence_paths=frozenset()
)
_CHECKPOINT_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(), semantic_sequence_paths=frozenset()
)
_EVENT_TYPES = {
    "run_initialized",
    "run_resumed",
    "preflight_blocked",
    "benchmark_started",
    "chapter_started",
    "chapter_completed",
    "chapter_reused",
    "chapter_failed",
    "aggregation_started",
    "aggregation_completed",
    "run_halted",
    "run_completed",
}


@dataclass(frozen=True, slots=True)
class BenchmarkChapterRuntimeV1:
    common_input: CommonEvaluationInputV1
    config_payload: Mapping[str, Any]
    input_artifact: Mapping[str, Any]
    arm_presentations: Sequence[Mapping[str, Any]]
    method_presentations: Sequence[Mapping[str, Any]]
    local_sf_qe_runtime: LocalSfQeRuntimeV1 | None = None
    llm_roles: SharedEvaluationRoleRunnerV1 | None = None
    shared_ledger: SharedLlmAttemptLedger | None = None
    shared_ledger_relative_path: str | None = None
    caveats: Sequence[str] = ()


@dataclass(frozen=True, slots=True)
class BenchmarkEndToEndResultV1:
    output_root: Path
    status_path: Path
    status: dict[str, Any]
    report_path: Path | None
    report: dict[str, Any] | None
    chapter_results: tuple[EndToEndEvaluationResultV1, ...]
    reused_complete_run: bool
    workflow_component_root: Path | None = None


class _UsageRecordingRoleRunnerV1:
    """Thin role-runner view that projects ledger facts after each real attempt."""

    def __init__(
        self,
        base: SharedEvaluationRoleRunnerV1,
        *,
        workflow: EvaluationWorkflowComponentWriterV1,
        ledger: SharedLlmAttemptLedger,
        stage_id: str,
    ) -> None:
        self._base = base
        self._workflow = workflow
        self._ledger = ledger
        self._stage_id = stage_id

    @property
    def execution_binding(self) -> dict[str, str]:
        return self._base.execution_binding

    @property
    def cache_mode(self) -> str:
        return self._base.cache_mode

    @property
    def semantic_contract(self) -> dict[str, Any]:
        return self._base.semantic_contract

    @property
    def attempt_runtime_binding(self) -> dict[str, Any]:
        return self._base.attempt_runtime_binding

    def execute(self, **kwargs):
        logical_request_id = require_string(
            kwargs.get("logical_request_id"), path="$.logical_request_id"
        )
        try:
            result = self._base.execute(**kwargs)
        except Exception:
            self._sync(logical_request_id)
            raise
        self._sync(logical_request_id)
        return result

    def _sync(self, logical_request_id: str) -> None:
        self._workflow.sync_usage_from_ledger(
            self._ledger,
            stage_id=self._stage_id,
            current_work_id=logical_request_id,
            execution_binding=self._base.execution_binding,
        )


ChapterRunnerV1 = Callable[..., EndToEndEvaluationResultV1]


def run_benchmark_end_to_end_v1(
    benchmark_manifest: Mapping[str, Any],
    benchmark_preflight: Mapping[str, Any],
    benchmark_overlays: Sequence[Mapping[str, Any]],
    chapter_runtimes: Mapping[str, BenchmarkChapterRuntimeV1],
    output_root: Path,
    *,
    generated_at: str,
    producer_code_commit: str,
    evaluation_logical_run_id: str,
    evaluation_attempt_run_id: str,
    evaluation_profile_id: str,
    policy_profile_id: str | None,
    baseline_arm_id: str | None = None,
    candidate_arm_id: str | None = None,
    chapter_runner: ChapterRunnerV1 = run_evaluation_end_to_end_v1,
    workflow_context: EvaluationWorkflowRunContextV1 | None = None,
) -> BenchmarkEndToEndResultV1:
    manifest = validate_benchmark_manifest_v1(benchmark_manifest)
    preflight = validate_benchmark_preflight_v1(benchmark_preflight)
    if preflight["benchmark_manifest_sha256"] != manifest["integrity"]["manifest_sha256"]:
        raise ContractValidationError(
            "manifest_binding", "$.benchmark_preflight", "preflight belongs to another benchmark"
        )
    selected_chapter_ids = tuple(
        row["chapter_id"] for row in manifest["chapters"]
    )
    selected_arm_ids = tuple(row["arm_id"] for row in manifest["arm_contracts"])
    if [row["chapter_id"] for row in preflight["chapter_checks"]] != list(
        selected_chapter_ids
    ) or [row["arm_id"] for row in preflight["arm_checks"]] != list(
        selected_arm_ids
    ):
        raise ContractValidationError(
            "preflight_scope",
            "$.benchmark_preflight",
            "preflight chapter/arm scope differs from the benchmark manifest",
        )
    workflow_settings = None
    selected_scorer_ids: tuple[str, ...] | None = None
    if workflow_context is not None:
        workflow_settings = validate_evaluation_workflow_settings_v1(
            workflow_context.workflow_settings,
            authority=workflow_context.workflow_settings_authority,
            scoring_handoff=workflow_context.scoring_handoff,
        )
        selected_scorer_ids = tuple(workflow_settings["selected_scorer_ids"])
        if tuple(workflow_settings["selected_chapter_ids"]) != selected_chapter_ids:
            raise ContractValidationError(
                "settings_scope",
                "$.workflow_settings.selected_chapter_ids",
                "workflow settings and benchmark manifest select different chapters",
            )
        if tuple(workflow_settings["selected_arm_ids"]) != tuple(
            _settings_arm_id(arm_id) for arm_id in selected_arm_ids
        ):
            raise ContractValidationError(
                "settings_scope",
                "$.workflow_settings.selected_arm_ids",
                "workflow settings and benchmark manifest select different arms",
            )
    timestamp = require_rfc3339(generated_at, path="$.generated_at")
    commit = require_commit(producer_code_commit, path="$.producer_code_commit")
    logical_run_id = require_string(
        evaluation_logical_run_id, path="$.evaluation_logical_run_id"
    )
    attempt_run_id = require_string(
        evaluation_attempt_run_id, path="$.evaluation_attempt_run_id"
    )
    profile_id = require_string(evaluation_profile_id, path="$.evaluation_profile_id")
    policy_id = require_nullable_string(policy_profile_id, path="$.policy_profile_id")
    (
        runtimes,
        scoring_contract_sha256,
        chapter_bindings,
        runtime_scorer_ids,
    ) = _validate_chapter_runtimes(
        chapter_runtimes,
        manifest=manifest,
        preflight=preflight,
        overlays=benchmark_overlays,
        expected_scorer_ids=selected_scorer_ids,
    )
    if selected_scorer_ids is None:
        selected_scorer_ids = runtime_scorer_ids
    root = output_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    state = _BenchmarkRunStateV1.open_or_create(
        root / "benchmark_state",
        manifest={
            "schema_id": RUN_MANIFEST_SCHEMA_ID,
            "schema_version": SCHEMA_VERSION,
            "created_at": timestamp,
            "producer": {
                "workstream": "evaluation",
                "component": "benchmark_runner_v1",
                "component_version": SCHEMA_VERSION,
                "code_commit": commit,
            },
            "identity": {
                "benchmark_id": manifest["benchmark_id"],
                "benchmark_manifest_sha256": manifest["integrity"]["manifest_sha256"],
                "benchmark_preflight_sha256": preflight["integrity"]["preflight_sha256"],
                "evaluation_logical_run_id": logical_run_id,
                "evaluation_attempt_run_id": attempt_run_id,
                "evaluation_profile_id": profile_id,
                "policy_profile_id": policy_id,
                "scoring_contract_sha256": scoring_contract_sha256,
                "selected_chapter_ids": list(selected_chapter_ids),
                "selected_arm_ids": list(selected_arm_ids),
                "selected_scorer_ids": list(selected_scorer_ids),
                "workflow_settings_sha256": (
                    None
                    if workflow_settings is None
                    else workflow_settings["settings_sha256"]
                ),
                "baseline_arm_id": baseline_arm_id,
                "candidate_arm_id": candidate_arm_id,
            },
            "chapter_bindings": chapter_bindings,
            "integrity": {"manifest_sha256": "0" * 64},
        },
    )
    report_path = root / "reports" / "benchmark_run_report_v1.json"
    with state.run_lock():
        workflow: EvaluationWorkflowComponentWriterV1 | None = None
        if workflow_context is not None:
            initial_status = state.status()
            workflow = EvaluationWorkflowComponentWriterV1(
                root,
                workflow_context,
                generated_at=timestamp,
                producer_code_commit=commit,
                stages=benchmark_workflow_stages_v1(selected_chapter_ids),
                allow_create=(
                    initial_status["last_event_sequence"] == 1
                    and not report_path.exists()
                ),
            )
            if not workflow.created_new:
                if workflow.is_halted:
                    workflow.start_or_resume()
                elif workflow.recovered_resume:
                    pass
                elif workflow.terminal_event is None:
                    raise ContractValidationError(
                        "component_attempt_open",
                        str(root / "events.jsonl"),
                        "an unclosed Evaluation attempt cannot be resumed implicitly",
                    )

        if workflow is not None and workflow.terminal_event == "component_failed":
            if preflight["status"] != "ready":
                return BenchmarkEndToEndResultV1(
                    root,
                    state.status_path,
                    state.status(),
                    None,
                    None,
                    (),
                    False,
                    root,
                )
            raise ContractValidationError(
                "component_terminal",
                str(root / "events.jsonl"),
                "failed workflow component requires a new component run",
            )

        if workflow is not None and workflow.stage_state("preflight") != "succeeded":
            workflow.start_stage("preflight", work_total=1, work_unit="gate")
            workflow.progress(
                "preflight",
                completed=1,
                total=1,
                unit="gate",
                current_work_id="benchmark_preflight_v1",
                detail={
                    "detail_kind": "input_arms",
                    "data": {
                        "arm_ids": [
                            _settings_arm_id(arm_id)
                            for arm_id in selected_arm_ids
                        ]
                    },
                },
            )
            if preflight["status"] == "ready":
                workflow.validation_passed(
                    "preflight", validator_id="benchmark_preflight_v1"
                )
                workflow.complete_stage("preflight")
            else:
                workflow.validation_failed(
                    "preflight",
                    validator_id="benchmark_preflight_v1",
                    reason_code="benchmark_preflight_blocked",
                )
                workflow.complete_stage("preflight", outcome="blocked")
                workflow.failed(reason_code="benchmark_preflight_blocked")

        if report_path.is_file():
            report = validate_benchmark_run_report_v1(_load_json(report_path))
            _require_completed_report_binding(report, state.manifest)
            persisted_report_path = persist_benchmark_run_report_v1(root, report)
            if state.status()["state"] != "completed":
                state.append_event("run_resumed")
                state.append_event("aggregation_started")
                state.append_event("aggregation_completed")
                state.append_event("run_completed")
            if workflow is not None:
                if workflow.terminal_event == "component_failed":
                    raise ContractValidationError(
                        "component_terminal",
                        str(root / "events.jsonl"),
                        "failed component cannot publish a completed benchmark report",
                    )
                if workflow.terminal_event is None:
                    for chapter_id in selected_chapter_ids:
                        if workflow.stage_state(f"chapter_{chapter_id}") != "succeeded":
                            raise ContractValidationError(
                                "component_coverage",
                                str(root / "events.jsonl"),
                                "completed report has an incomplete chapter replay stage",
                            )
                    if workflow.stage_state("aggregation") != "succeeded":
                        workflow.start_stage(
                            "aggregation", work_total=1, work_unit="report"
                        )
                        workflow.progress(
                            "aggregation",
                            completed=1,
                            total=1,
                            unit="report",
                            current_work_id="benchmark_run_report_v1",
                            detail={
                                "detail_kind": "aggregation_result",
                                "data": {
                                    "report": workflow.file_binding(
                                        _relative(root, persisted_report_path),
                                        artifact_kind="benchmark_run_report_v1",
                                        schema_version=report["schema_version"],
                                    ),
                                    "metric_ids": sorted(
                                        {
                                            row["method_id"]
                                            for row in report["aggregates"]
                                        }
                                    ),
                                },
                            },
                        )
                        accepted = workflow.validation_passed(
                            "aggregation", validator_id="benchmark_run_report_v1"
                        )
                        workflow.add_artifact(
                            _relative(root, persisted_report_path),
                            artifact_kind="benchmark_run_report_v1",
                            schema_version=report["schema_version"],
                            stage_id="aggregation",
                            created_by_event_id=accepted["event_id"],
                            parent_artifact_refs=("scoring_receipt.json",),
                        )
                        workflow.complete_stage("aggregation")
                    workflow.done()
                workflow.validate_package(require_terminal=True)
            return BenchmarkEndToEndResultV1(
                root,
                state.status_path,
                state.status(),
                persisted_report_path,
                report,
                (),
                True,
                root if workflow is not None else None,
            )
        if preflight["status"] != "ready":
            if state.status()["state"] != "blocked":
                state.append_event("preflight_blocked", reason_code="benchmark_preflight_blocked")
            if workflow is not None:
                workflow.validate_package(require_terminal=True)
            return BenchmarkEndToEndResultV1(
                root,
                state.status_path,
                state.status(),
                None,
                None,
                (),
                False,
                root if workflow is not None else None,
            )

        prior = state.status()
        if prior["state"] in {"running", "halted"} and prior["last_event_sequence"] > 1:
            state.append_event("run_resumed")
        state.append_event("benchmark_started")
        chapter_outputs: list[EndToEndEvaluationResultV1] = []
        aggregate_inputs: list[dict[str, Any]] = []
        workflow_active_stage: str | None = None
        try:
            for ordinal, chapter_id in enumerate(selected_chapter_ids):
                runtime = runtimes[chapter_id]
                child_root = root / "chapters" / f"{ordinal:02d}_{chapter_id}"
                workflow_active_stage = f"chapter_{chapter_id}"
                if workflow is not None:
                    workflow.start_stage(
                        workflow_active_stage, work_total=1, work_unit="chapter"
                    )
                chapter_llm_roles = runtime.llm_roles
                if workflow is not None and runtime.llm_roles is not None:
                    if runtime.shared_ledger is None:
                        raise ContractValidationError(
                            "usage_ledger_missing",
                            f"$.chapter_runtimes.{chapter_id}.shared_ledger",
                            "provider-backed Evaluation requires its shared attempt ledger",
                        )
                    chapter_llm_roles = _UsageRecordingRoleRunnerV1(
                        runtime.llm_roles,
                        workflow=workflow,
                        ledger=runtime.shared_ledger,
                        stage_id=workflow_active_stage,
                    )
                state.append_event("chapter_started", chapter_id=chapter_id)
                result = chapter_runner(
                    runtime.common_input,
                    runtime.config_payload,
                    child_root,
                    generated_at=timestamp,
                    producer_code_commit=commit,
                    evaluation_logical_run_id=f"{logical_run_id}.{chapter_id}",
                    evaluation_attempt_run_id=f"{attempt_run_id}.{chapter_id}",
                    evaluation_profile_id=profile_id,
                    policy_profile_id=policy_id,
                    input_artifact=runtime.input_artifact,
                    arm_presentations=runtime.arm_presentations,
                    method_presentations=runtime.method_presentations,
                    baseline_arm_id=baseline_arm_id,
                    candidate_arm_id=candidate_arm_id,
                    local_sf_qe_runtime=runtime.local_sf_qe_runtime,
                    llm_roles=chapter_llm_roles,
                    shared_ledger=runtime.shared_ledger,
                    shared_ledger_relative_path=runtime.shared_ledger_relative_path,
                    caveats=runtime.caveats,
                )
                if (
                    workflow is not None
                    and runtime.llm_roles is not None
                    and runtime.shared_ledger is not None
                ):
                    workflow.sync_usage_from_ledger(
                        runtime.shared_ledger,
                        stage_id=workflow_active_stage,
                        current_work_id=chapter_id,
                        execution_binding=runtime.llm_roles.execution_binding,
                    )
                checkpoint = state.persist_chapter_checkpoint(
                    chapter_id=chapter_id,
                    ordinal=ordinal,
                    report_relative_path=_relative(root, result.report_path),
                    report=result.report,
                    execution_relative_path=_relative(root, result.execution_path),
                    execution=result.execution,
                )
                state.append_event(
                    "chapter_reused" if result.reused_complete_run else "chapter_completed",
                    chapter_id=chapter_id,
                )
                if workflow is not None and workflow.stage_state(workflow_active_stage) != "succeeded":
                    workflow.progress(
                        workflow_active_stage,
                        completed=1,
                        total=1,
                        unit="chapter",
                        current_work_id=chapter_id,
                        detail={
                            "detail_kind": "chapter_scorer_progress",
                            "data": {
                                "chapter_id": chapter_id,
                                "scorer_id": None,
                                "completed": 1,
                                "total": 1,
                            },
                        },
                    )
                    accepted = workflow.validation_passed(
                        workflow_active_stage,
                        validator_id="evaluation_chapter_result_v1",
                    )
                    report_ref = _relative(root, result.report_path)
                    execution_ref = _relative(root, result.execution_path)
                    workflow.add_artifact(
                        report_ref,
                        artifact_kind="full_run_report_v1",
                        schema_version=result.report["schema_version"],
                        stage_id=workflow_active_stage,
                        created_by_event_id=accepted["event_id"],
                        parent_artifact_refs=("scoring_receipt.json",),
                    )
                    workflow.add_artifact(
                        execution_ref,
                        artifact_kind="evaluation_execution_artifact_v1",
                        schema_version=result.execution["schema_version"],
                        stage_id=workflow_active_stage,
                        created_by_event_id=accepted["event_id"],
                        parent_artifact_refs=("scoring_receipt.json",),
                    )
                    benchmark_checkpoint_ref = (
                        f"benchmark_state/chapters/{ordinal:02d}_{chapter_id}.json"
                    )
                    workflow.add_artifact(
                        benchmark_checkpoint_ref,
                        artifact_kind="benchmark_chapter_checkpoint_v1",
                        schema_version=SCHEMA_VERSION,
                        stage_id=workflow_active_stage,
                        created_by_event_id=accepted["event_id"],
                        parent_artifact_refs=(report_ref, execution_ref),
                    )
                    workflow.persist_checkpoint(
                        stage_id=workflow_active_stage,
                        work_id=chapter_id,
                        benchmark_status=state.status(),
                        chapter_checkpoint_ref=benchmark_checkpoint_ref,
                    )
                    workflow.complete_stage(workflow_active_stage)
                chapter_outputs.append(result)
                aggregate_inputs.append(
                    {
                        "chapter_id": chapter_id,
                        "report_relative_path": checkpoint["report_relative_path"],
                        "execution_relative_path": checkpoint["execution_relative_path"],
                        "report": result.report,
                        "execution": result.execution,
                        "reused_complete_run": result.reused_complete_run,
                    }
                )
                workflow_active_stage = None
            state.append_event("aggregation_started")
            workflow_active_stage = "aggregation"
            if workflow is not None:
                workflow.start_stage("aggregation", work_total=1, work_unit="report")
            report = compose_benchmark_run_report_v1(
                manifest,
                preflight,
                aggregate_inputs,
                generated_at=timestamp,
                producer_code_commit=commit,
                evaluation_logical_run_id=logical_run_id,
                evaluation_attempt_run_id=attempt_run_id,
                evaluation_profile_id=profile_id,
                policy_profile_id=policy_id,
                scoring_contract_sha256=scoring_contract_sha256,
                selected_scorer_ids=selected_scorer_ids,
                workflow_settings_sha256=(
                    None
                    if workflow_settings is None
                    else workflow_settings["settings_sha256"]
                ),
                baseline_arm_id=baseline_arm_id,
                candidate_arm_id=candidate_arm_id,
            )
            persisted = persist_benchmark_run_report_v1(root, report)
            state.append_event("aggregation_completed")
            state.append_event("run_completed")
            if workflow is not None:
                workflow.progress(
                    "aggregation",
                    completed=1,
                    total=1,
                    unit="report",
                    current_work_id="benchmark_run_report_v1",
                    detail={
                        "detail_kind": "aggregation_result",
                        "data": {
                            "report": workflow.file_binding(
                                _relative(root, persisted),
                                artifact_kind="benchmark_run_report_v1",
                                schema_version=report["schema_version"],
                            ),
                            "metric_ids": sorted(
                                {row["method_id"] for row in report["aggregates"]}
                            ),
                        },
                    },
                )
                accepted = workflow.validation_passed(
                    "aggregation", validator_id="benchmark_run_report_v1"
                )
                workflow.add_artifact(
                    _relative(root, persisted),
                    artifact_kind="benchmark_run_report_v1",
                    schema_version=report["schema_version"],
                    stage_id="aggregation",
                    created_by_event_id=accepted["event_id"],
                    parent_artifact_refs=("scoring_receipt.json",),
                )
                workflow.complete_stage("aggregation")
                workflow.done()
                workflow.validate_package(require_terminal=True)
            workflow_active_stage = None
            return BenchmarkEndToEndResultV1(
                root,
                state.status_path,
                state.status(),
                persisted,
                report,
                tuple(chapter_outputs),
                False,
                root if workflow is not None else None,
            )
        except Exception as exc:
            current = state.status()
            chapter_id = current["current_chapter_id"]
            if chapter_id is not None:
                state.append_event(
                    "chapter_failed", chapter_id=chapter_id, reason_code=_exception_code(exc)
                )
            state.append_event("run_halted", reason_code=_exception_code(exc))
            if workflow is not None and workflow.terminal_event is None and not workflow.is_halted:
                active_stage = workflow_active_stage
                classification = classify_evaluation_failure_v1(exc)
                reason_code = classification.reason_code
                work_id = chapter_id or active_stage
                incident_id = workflow.record_internal_incident(
                    exc,
                    category=classification.category,
                    reason_code=reason_code,
                    stage_id=active_stage,
                    work_id=work_id,
                )
                checkpoint_binding = None
                if active_stage is not None and workflow.stage_state(active_stage) == "running":
                    checkpoint_binding = workflow.persist_checkpoint(
                        stage_id=active_stage,
                        work_id=work_id,
                        benchmark_status=state.status(),
                    )
                if classification.category == "integrity":
                    workflow.failed(
                        reason_code=reason_code,
                        reason_category="integrity",
                        incident_id=incident_id,
                        checkpoint=checkpoint_binding,
                        current_stage_id=active_stage,
                        current_work_id=work_id,
                    )
                else:
                    workflow.halt(
                        reason_code=reason_code,
                        reason_category=classification.category,
                        incident_id=incident_id,
                        checkpoint=checkpoint_binding,
                        current_stage_id=active_stage,
                        current_work_id=work_id,
                    )
                workflow.validate_package()
            raise


def validate_benchmark_run_status_v1(value: Mapping[str, Any]) -> dict[str, Any]:
    path = "$"
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "schema_id", "schema_version", "state", "current_chapter_id",
            "completed_chapter_count", "failed_chapter_count", "chapter_states",
            "last_event_sequence", "last_event_sha256", "reason_code", "updated_at",
            "manifest_sha256", "selected_chapter_ids",
        },
        path=path,
    )
    selected_chapter_ids = _validate_selected_ids(
        row["selected_chapter_ids"],
        allowed=BENCHMARK_CHAPTER_IDS_V1,
        minimum=1,
        path="$.selected_chapter_ids",
    )
    chapter_states_row = require_mapping(row["chapter_states"], path="$.chapter_states")
    require_exact_keys(
        chapter_states_row,
        required=set(selected_chapter_ids),
        path="$.chapter_states",
    )
    chapter_states = {
        chapter_id: require_enum(
            chapter_states_row[chapter_id], {"pending", "running", "completed", "failed"},
            path=f"$.chapter_states.{chapter_id}",
        )
        for chapter_id in selected_chapter_ids
    }
    current = require_nullable_string(row["current_chapter_id"], path="$.current_chapter_id")
    if current is not None and current not in selected_chapter_ids:
        raise ContractValidationError("chapter_id", "$.current_chapter_id", "foreign current chapter")
    result = {
        "schema_id": require_enum(row["schema_id"], {RUN_STATUS_SCHEMA_ID}, path="$.schema_id"),
        "schema_version": require_enum(row["schema_version"], {SCHEMA_VERSION}, path="$.schema_version"),
        "state": require_enum(row["state"], {"initialized", "running", "blocked", "halted", "completed"}, path="$.state"),
        "current_chapter_id": current,
        "completed_chapter_count": require_int(row["completed_chapter_count"], path="$.completed_chapter_count", minimum=0),
        "failed_chapter_count": require_int(row["failed_chapter_count"], path="$.failed_chapter_count", minimum=0),
        "selected_chapter_ids": list(selected_chapter_ids),
        "chapter_states": chapter_states,
        "last_event_sequence": require_int(row["last_event_sequence"], path="$.last_event_sequence", minimum=1),
        "last_event_sha256": require_sha256(row["last_event_sha256"], path="$.last_event_sha256"),
        "reason_code": require_nullable_string(row["reason_code"], path="$.reason_code"),
        "updated_at": require_rfc3339(row["updated_at"], path="$.updated_at"),
        "manifest_sha256": require_sha256(row["manifest_sha256"], path="$.manifest_sha256"),
    }
    if result["completed_chapter_count"] != sum(value == "completed" for value in chapter_states.values()):
        raise ContractValidationError("coverage", "$.completed_chapter_count", "chapter status count drift")
    if result["failed_chapter_count"] != sum(value == "failed" for value in chapter_states.values()):
        raise ContractValidationError("coverage", "$.failed_chapter_count", "chapter failure count drift")
    return result


def validate_benchmark_chapter_checkpoint_v1(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    return _validate_checkpoint(value)


class _BenchmarkRunStateV1:
    def __init__(self, root: Path, manifest: Mapping[str, Any]) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.events_root = self.root / "events"
        self.checkpoints_root = self.root / "chapters"
        self.events_root.mkdir(parents=True, exist_ok=True)
        self.checkpoints_root.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.root / "manifest.json"
        self.status_path = self.root / "status.json"
        self.lock_path = self.root / "runner.lock"
        sealed = _seal_manifest(manifest)
        if self.manifest_path.exists():
            persisted = _validate_manifest(_load_json(self.manifest_path))
            if persisted != sealed:
                raise ContractValidationError(
                    "resume_binding", str(self.manifest_path), "benchmark run manifest differs"
                )
            self.manifest = persisted
        else:
            self.manifest = sealed
            _write_immutable_json(self.manifest_path, sealed)
        self._checkpoints = self._audit_chapter_checkpoints()
        self._events = self._audit_events()
        if not self._events:
            self.append_event("run_initialized")
        else:
            self._write_status()

    @classmethod
    def open_or_create(cls, root: Path, *, manifest: Mapping[str, Any]) -> "_BenchmarkRunStateV1":
        return cls(root, manifest)

    @contextmanager
    def run_lock(self) -> Iterator[None]:
        _recover_dead_lock(self.lock_path)
        try:
            descriptor = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise ContractValidationError(
                "run_locked", str(self.lock_path), "another benchmark runner owns this run"
            ) from exc
        try:
            os.write(descriptor, f"{os.getpid()}\n".encode("ascii"))
            os.close(descriptor)
            yield
        finally:
            try:
                os.close(descriptor)
            except OSError:
                pass
            self.lock_path.unlink(missing_ok=True)

    def append_event(
        self,
        event_type: str,
        *,
        chapter_id: str | None = None,
        reason_code: str | None = None,
    ) -> dict[str, Any]:
        event = require_enum(event_type, _EVENT_TYPES, path="$.event_type")
        selected_chapter_ids = tuple(
            row["chapter_id"] for row in self.manifest["chapter_bindings"]
        )
        if chapter_id is not None and chapter_id not in selected_chapter_ids:
            raise ContractValidationError("chapter_id", "$.chapter_id", "foreign benchmark chapter")
        sequence = len(self._events) + 1
        previous = self._events[-1]["integrity"]["event_sha256"] if self._events else None
        draft = {
            "schema_id": RUN_EVENT_SCHEMA_ID,
            "schema_version": SCHEMA_VERSION,
            "sequence": sequence,
            "occurred_at": self.manifest["created_at"],
            "event_type": event,
            "chapter_id": chapter_id,
            "reason_code": reason_code,
            "previous_event_sha256": previous,
            "manifest_sha256": self.manifest["integrity"]["manifest_sha256"],
            "integrity": {"event_sha256": "0" * 64},
        }
        sealed = seal_payload(draft, policy=_EVENT_POLICY, hash_path=_EVENT_HASH_PATH)
        normalized = _validate_event(sealed)
        _write_immutable_json(self.events_root / f"{sequence:08d}.json", normalized)
        self._events.append(normalized)
        self._write_status()
        return normalized

    def status(self) -> dict[str, Any]:
        return validate_benchmark_run_status_v1(_load_json(self.status_path))

    def persist_chapter_checkpoint(
        self,
        *,
        chapter_id: str,
        ordinal: int,
        report_relative_path: str,
        report: Mapping[str, Any],
        execution_relative_path: str,
        execution: Mapping[str, Any],
    ) -> dict[str, Any]:
        report_row = validate_full_run_report(report)
        execution_row = validate_evaluation_execution_artifact(execution)
        draft = {
            "schema_id": CHAPTER_CHECKPOINT_SCHEMA_ID,
            "schema_version": SCHEMA_VERSION,
            "chapter_id": chapter_id,
            "ordinal": ordinal,
            "manifest_sha256": self.manifest["integrity"]["manifest_sha256"],
            "report_relative_path": report_relative_path,
            "report_sha256": report_row["integrity"]["report_sha256"],
            "execution_relative_path": execution_relative_path,
            "execution_sha256": execution_row["integrity"]["artifact_sha256"],
            "integrity": {"checkpoint_sha256": "0" * 64},
        }
        sealed = seal_payload(draft, policy=_CHECKPOINT_POLICY, hash_path=_CHECKPOINT_HASH_PATH)
        normalized = _validate_checkpoint(sealed)
        _write_immutable_json(self.checkpoints_root / f"{ordinal:02d}_{chapter_id}.json", normalized)
        if ordinal == len(self._checkpoints):
            self._checkpoints.append(normalized)
        elif ordinal < len(self._checkpoints) and self._checkpoints[ordinal] != normalized:
            raise ContractValidationError(
                "checkpoint_conflict",
                str(self.checkpoints_root / f"{ordinal:02d}_{chapter_id}.json"),
                "persisted chapter checkpoint differs from the resumed result",
            )
        elif ordinal > len(self._checkpoints):
            raise ContractValidationError(
                "checkpoint_sequence",
                str(self.checkpoints_root / f"{ordinal:02d}_{chapter_id}.json"),
                "chapter checkpoint cannot skip an earlier source chapter",
            )
        return normalized

    def _audit_chapter_checkpoints(self) -> list[dict[str, Any]]:
        checkpoints: list[dict[str, Any]] = []
        selected_chapter_ids = tuple(
            row["chapter_id"] for row in self.manifest["chapter_bindings"]
        )
        for ordinal, path in enumerate(sorted(self.checkpoints_root.glob("*.json"))):
            if ordinal >= len(selected_chapter_ids):
                raise ContractValidationError(
                    "checkpoint_sequence",
                    str(path),
                    "benchmark contains more checkpoints than selected chapters",
                )
            row = _validate_checkpoint(_load_json(path))
            expected_chapter = selected_chapter_ids[ordinal]
            expected_name = f"{ordinal:02d}_{expected_chapter}.json"
            if path.name != expected_name or row["ordinal"] != ordinal:
                raise ContractValidationError(
                    "checkpoint_sequence",
                    str(path),
                    "chapter checkpoints must form a contiguous source-order prefix",
                )
            if row["chapter_id"] != expected_chapter:
                raise ContractValidationError(
                    "checkpoint_sequence",
                    str(path),
                    "checkpoint chapter differs from its source-order position",
                )
            if row["manifest_sha256"] != self.manifest["integrity"]["manifest_sha256"]:
                raise ContractValidationError(
                    "manifest_binding",
                    str(path),
                    "chapter checkpoint belongs to another benchmark run",
                )
            checkpoints.append(row)
        return checkpoints

    def _audit_events(self) -> list[dict[str, Any]]:
        paths = sorted(self.events_root.glob("*.json"))
        events = []
        previous = None
        for index, path in enumerate(paths, start=1):
            if path.name != f"{index:08d}.json":
                raise ContractValidationError("event_sequence", str(path), "event file sequence has a gap")
            row = _validate_event(_load_json(path))
            if row["sequence"] != index or row["previous_event_sha256"] != previous:
                raise ContractValidationError("event_chain", str(path), "event hash chain drift")
            if row["manifest_sha256"] != self.manifest["integrity"]["manifest_sha256"]:
                raise ContractValidationError("manifest_binding", str(path), "event belongs to another run")
            previous = row["integrity"]["event_sha256"]
            events.append(row)
        return events

    def _write_status(self) -> None:
        status = _project_status(self.manifest, self._events)
        _write_json_atomic(self.status_path, status)


def _validate_chapter_runtimes(
    runtimes: Mapping[str, BenchmarkChapterRuntimeV1],
    *,
    manifest: Mapping[str, Any],
    preflight: Mapping[str, Any],
    overlays: Sequence[Mapping[str, Any]],
    expected_scorer_ids: Sequence[str] | None,
) -> tuple[
    dict[str, BenchmarkChapterRuntimeV1],
    str,
    list[dict[str, Any]],
    tuple[str, ...],
]:
    selected_chapter_ids = tuple(
        row["chapter_id"] for row in manifest["chapters"]
    )
    selected_arm_ids = tuple(row["arm_id"] for row in manifest["arm_contracts"])
    if tuple(runtimes) != selected_chapter_ids:
        raise ContractValidationError(
            "chapter_exact_cover",
            "$.chapter_runtimes",
            "runtime map must exact-cover selected chapters in order",
        )
    manifest_by_id = {row["chapter_id"]: row for row in manifest["chapters"]}
    overlay_by_key = _validate_runtime_overlays(overlays, preflight=preflight)
    result = {}
    contracts = []
    bindings = []
    resolved_scorer_ids: tuple[str, ...] | None = None
    for ordinal, chapter_id in enumerate(selected_chapter_ids):
        runtime = runtimes[chapter_id]
        runtime_path = f"$.chapter_runtimes.{chapter_id}"
        if runtime.llm_roles is not None:
            if runtime.shared_ledger is None:
                raise ContractValidationError(
                    "usage_ledger_missing",
                    f"{runtime_path}.shared_ledger",
                    "provider-backed Evaluation requires its shared attempt ledger",
                )
            if runtime.shared_ledger_relative_path is None:
                raise ContractValidationError(
                    "usage_ledger_path",
                    f"{runtime_path}.shared_ledger_relative_path",
                    "provider-backed Evaluation requires the persisted ledger path",
                )
            require_relative_path(
                runtime.shared_ledger_relative_path,
                path=f"{runtime_path}.shared_ledger_relative_path",
            )
        elif runtime.shared_ledger is not None or runtime.shared_ledger_relative_path is not None:
            raise ContractValidationError(
                "usage_ledger_without_roles",
                runtime_path,
                "a shared attempt ledger cannot be attached without provider-backed roles",
            )
        common = runtime.common_input
        if {row.chapter_id for row in common.blocks} != {chapter_id}:
            raise ContractValidationError("chapter_binding", f"$.chapter_runtimes.{chapter_id}", "common input chapter differs")
        source = CommonSourceSnapshotV1(
            common.source_schema_id,
            common.source_schema_version,
            common.source_binding,
            common.blocks,
        )
        source_sha = source_read_model_sha256_v1(source)
        if source_sha != manifest_by_id[chapter_id]["source_read_model_sha256"]:
            raise ContractValidationError("source_binding", f"$.chapter_runtimes.{chapter_id}", "source read-model hash differs")
        if tuple(arm.arm_id for arm in common.arms) != selected_arm_ids:
            raise ContractValidationError(
                "arm_binding",
                f"$.chapter_runtimes.{chapter_id}",
                "chapter does not exact-cover selected arms",
            )
        if preflight["status"] == "ready":
            _validate_common_overlay_binding(
                common,
                chapter_id=chapter_id,
                overlay_by_key=overlay_by_key,
                selected_arm_ids=selected_arm_ids,
            )
        config = validate_evaluation_run_config(runtime.config_payload)
        config_scorer_ids = tuple(row["method_id"] for row in config["methods"])
        if resolved_scorer_ids is None:
            resolved_scorer_ids = config_scorer_ids
        elif config_scorer_ids != resolved_scorer_ids:
            raise ContractValidationError(
                "scorer_scope",
                f"$.chapter_runtimes.{chapter_id}.config_payload.methods",
                "chapter scorer methods differ across the selected run",
            )
        if (
            expected_scorer_ids is not None
            and config_scorer_ids != tuple(expected_scorer_ids)
        ):
            raise ContractValidationError(
                "scorer_scope",
                f"$.chapter_runtimes.{chapter_id}.config_payload.methods",
                "chapter scorer methods differ from sealed workflow settings",
            )
        plan = build_evaluation_plan(common, config)
        method_presentations = copy.deepcopy(list(runtime.method_presentations))
        semantic_contract = runtime.llm_roles.semantic_contract if runtime.llm_roles is not None else None
        contract = {
            "methods": config["methods"],
            "comparison_pairs": config["comparison_pairs"],
            "unit_policy": config["unit_policy"],
            "blinding": config["blinding"],
            "retry_policy": config["retry_policy"],
            "method_presentations": method_presentations,
            "llm_semantic_contract": semantic_contract,
        }
        contracts.append(shared_canonical_sha256(contract))
        bindings.append(
            {
                "chapter_id": chapter_id,
                "ordinal": ordinal,
                "source_read_model_sha256": source_sha,
                "config_sha256": config["integrity"]["config_sha256"],
                "input_set_sha256": plan.input_set_sha256,
                "plan_sha256": plan.plan_sha256,
            }
        )
        result[chapter_id] = runtime
    if len(set(contracts)) != 1:
        raise ContractValidationError(
            "scoring_contract_drift", "$.chapter_runtimes", "scoring policy or model contract differs by chapter"
        )
    assert resolved_scorer_ids is not None
    return result, contracts[0], bindings, resolved_scorer_ids


def _validate_runtime_overlays(
    values: Sequence[Mapping[str, Any]],
    *,
    preflight: Mapping[str, Any],
) -> dict[tuple[str, str], dict[str, Any]]:
    expected = {
        (arm["arm_id"], chapter["chapter_id"]): chapter["overlay_sha256"]
        for arm in preflight["arm_checks"]
        for chapter in arm["chapter_checks"]
        if chapter["overlay_sha256"] is not None
    }
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for index, raw in enumerate(values):
        overlay = validate_benchmark_overlay_v1(raw)
        key = (overlay["arm"]["arm_id"], overlay["source"]["chapter_id"])
        if key in result:
            raise ContractValidationError(
                "duplicate_overlay",
                f"$.benchmark_overlays[{index}]",
                f"duplicate benchmark overlay {key}",
            )
        if key not in expected:
            raise ContractValidationError(
                "foreign_overlay",
                f"$.benchmark_overlays[{index}]",
                f"overlay {key} is absent from the sealed preflight",
            )
        if overlay["integrity"]["overlay_sha256"] != expected[key]:
            raise ContractValidationError(
                "overlay_binding",
                f"$.benchmark_overlays[{index}].integrity.overlay_sha256",
                "overlay differs from the artifact approved by preflight",
            )
        result[key] = overlay
    if preflight["status"] == "ready" and set(result) != set(expected):
        raise ContractValidationError(
            "overlay_exact_cover",
            "$.benchmark_overlays",
            "ready scoring requires the exact selected preflight-approved overlays",
        )
    return result


def _validate_common_overlay_binding(
    common: CommonEvaluationInputV1,
    *,
    chapter_id: str,
    overlay_by_key: Mapping[tuple[str, str], Mapping[str, Any]],
    selected_arm_ids: Sequence[str],
) -> None:
    arm_by_id = {row.arm_id: row for row in common.arms}
    translation_by_key = {
        (row.arm_id, row.block_id): row for row in common.translations
    }
    if len(translation_by_key) != len(common.translations):
        raise ContractValidationError(
            "translation_duplicate",
            f"$.chapter_runtimes.{chapter_id}.common_input.translations",
            "duplicate arm/block translation row",
        )
    expected_translation_count = len(common.blocks) * len(common.arms)
    if len(common.translations) != expected_translation_count:
        raise ContractValidationError(
            "translation_exact_cover",
            f"$.chapter_runtimes.{chapter_id}.common_input.translations",
            "runtime translations do not exact-cover every arm/block cell",
        )
    for arm_id in selected_arm_ids:
        overlay = overlay_by_key[(arm_id, chapter_id)]
        common_arm = arm_by_id[arm_id]
        overlay_arm = overlay["arm"]
        expected_arm = {
            "logical_run_id": overlay_arm["logical_run_id"],
            "attempt_run_id": overlay_arm["attempt_run_id"],
            "profile_id": overlay_arm["profile_id"],
            "profile_config_sha256": overlay_arm["profile_config_sha256"],
            "source_language": overlay_arm["source_language"],
            "target_language": overlay_arm["target_language"],
        }
        observed_arm = {
            "logical_run_id": common_arm.logical_run_id,
            "attempt_run_id": common_arm.attempt_run_id,
            "profile_id": common_arm.profile_id,
            "profile_config_sha256": common_arm.profile_config_sha256,
            "source_language": common_arm.source_language,
            "target_language": common_arm.target_language,
        }
        if observed_arm != expected_arm:
            raise ContractValidationError(
                "overlay_arm_binding",
                f"$.chapter_runtimes.{chapter_id}.common_input.arms.{arm_id}",
                "runtime arm identity differs from its preflight overlay",
            )
        permitted_artifacts = {
            (
                overlay["overlay_id"],
                overlay["integrity"]["overlay_sha256"],
            ),
            (
                overlay_arm["evidence_artifact_id"],
                overlay_arm["evidence_sha256"],
            ),
        }
        if (common_arm.artifact_id, common_arm.artifact_sha256) not in permitted_artifacts:
            raise ContractValidationError(
                "overlay_artifact_binding",
                f"$.chapter_runtimes.{chapter_id}.common_input.arms.{arm_id}",
                "runtime arm is not the overlay or immutable evidence sealed by preflight",
            )
        for block, overlay_row in zip(common.blocks, overlay["rows"], strict=True):
            if overlay_row["block_id"] != block.block_id:
                raise ContractValidationError(
                    "overlay_block_binding",
                    f"$.chapter_runtimes.{chapter_id}.common_input.blocks.{block.block_id}",
                    "runtime and overlay block order differ",
                )
            observed = translation_by_key.get((arm_id, block.block_id))
            if observed is None:
                raise ContractValidationError(
                    "translation_exact_cover",
                    f"$.chapter_runtimes.{chapter_id}.common_input.translations",
                    f"missing runtime translation for {arm_id}/{block.block_id}",
                )
            expected_status, expected_target, expected_error = _overlay_runtime_value(
                block, overlay_row
            )
            if (
                observed.status != expected_status
                or observed.target_text != expected_target
                or observed.error_code != expected_error
            ):
                raise ContractValidationError(
                    "overlay_translation_binding",
                    f"$.chapter_runtimes.{chapter_id}.common_input.translations.{arm_id}.{block.block_id}",
                    "runtime translation differs from its preflight-approved overlay",
                )


def _overlay_runtime_value(block: Any, overlay_row: Mapping[str, Any]) -> tuple[str, str | None, str | None]:
    status = overlay_row["alignment_status"]
    if status == "aligned":
        if block.admission in {"translate", "translate_structured"}:
            return "translated", overlay_row["target_text"], None
        if block.admission == "preserve":
            return "preserved", block.source_text, None
        if block.admission == "exclude":
            return "excluded", None, None
        raise ContractValidationError(
            "source_admission",
            f"$.blocks.{block.block_id}.admission",
            "review-required source rows cannot enter a ready benchmark",
        )
    return status, None, overlay_row["error_code"] if status == "failed" else None


def _seal_manifest(value: Mapping[str, Any]) -> dict[str, Any]:
    draft = copy.deepcopy(dict(value))
    draft["integrity"] = {"manifest_sha256": "0" * 64}
    return _validate_manifest(
        seal_payload(draft, policy=_MANIFEST_POLICY, hash_path=_MANIFEST_HASH_PATH)
    )


def _validate_manifest(value: Mapping[str, Any]) -> dict[str, Any]:
    row = require_mapping(value, path="$manifest")
    require_exact_keys(row, required={"schema_id", "schema_version", "created_at", "producer", "identity", "chapter_bindings", "integrity"}, path="$manifest")
    identity = require_mapping(row["identity"], path="$manifest.identity")
    require_exact_keys(
        identity,
        required={
            "benchmark_id",
            "benchmark_manifest_sha256",
            "benchmark_preflight_sha256",
            "evaluation_logical_run_id",
            "evaluation_attempt_run_id",
            "evaluation_profile_id",
            "policy_profile_id",
            "scoring_contract_sha256",
            "selected_chapter_ids",
            "selected_arm_ids",
            "selected_scorer_ids",
            "workflow_settings_sha256",
            "baseline_arm_id",
            "candidate_arm_id",
        },
        path="$manifest.identity",
    )
    selected_chapter_ids = _validate_selected_ids(
        identity["selected_chapter_ids"],
        allowed=BENCHMARK_CHAPTER_IDS_V1,
        minimum=1,
        path="$manifest.identity.selected_chapter_ids",
    )
    selected_arm_ids = _validate_selected_ids(
        identity["selected_arm_ids"],
        allowed=BENCHMARK_ARM_IDS_V1,
        minimum=2,
        path="$manifest.identity.selected_arm_ids",
    )
    selected_scorer_ids = _validate_selected_ids(
        identity["selected_scorer_ids"],
        allowed=("sf_qe", "sf_bt", "pj"),
        minimum=1,
        path="$manifest.identity.selected_scorer_ids",
    )
    bindings = []
    for ordinal, raw in enumerate(require_list(row["chapter_bindings"], path="$manifest.chapter_bindings")):
        path = f"$manifest.chapter_bindings[{ordinal}]"
        if ordinal >= len(selected_chapter_ids):
            raise ContractValidationError(
                "chapter_exact_cover",
                "$manifest.chapter_bindings",
                "chapter bindings exceed the selected chapter scope",
            )
        item = require_mapping(raw, path=path)
        require_exact_keys(item, required={"chapter_id", "ordinal", "source_read_model_sha256", "config_sha256", "input_set_sha256", "plan_sha256"}, path=path)
        observed = require_int(item["ordinal"], path=f"{path}.ordinal", minimum=0)
        if observed != ordinal:
            raise ContractValidationError("chapter_order", f"{path}.ordinal", "chapter order drift")
        bindings.append({
            "chapter_id": require_enum(item["chapter_id"], {selected_chapter_ids[ordinal]}, path=f"{path}.chapter_id"),
            "ordinal": observed,
            "source_read_model_sha256": require_sha256(item["source_read_model_sha256"], path=f"{path}.source_read_model_sha256"),
            "config_sha256": require_sha256(item["config_sha256"], path=f"{path}.config_sha256"),
            "input_set_sha256": require_sha256(item["input_set_sha256"], path=f"{path}.input_set_sha256"),
            "plan_sha256": require_sha256(item["plan_sha256"], path=f"{path}.plan_sha256"),
        })
    if len(bindings) != len(selected_chapter_ids):
        raise ContractValidationError(
            "chapter_exact_cover",
            "$manifest.chapter_bindings",
            "chapter bindings must exact-cover the selected chapter scope",
        )
    baseline = require_nullable_string(identity["baseline_arm_id"], path="$manifest.identity.baseline_arm_id")
    candidate = require_nullable_string(identity["candidate_arm_id"], path="$manifest.identity.candidate_arm_id")
    if (baseline is None) != (candidate is None) or (
        baseline is not None
        and (
            baseline == candidate
            or baseline not in selected_arm_ids
            or candidate not in selected_arm_ids
        )
    ):
        raise ContractValidationError("comparison_binding", "$manifest.identity", "invalid comparison pair")
    normalized = {
        "schema_id": require_enum(row["schema_id"], {RUN_MANIFEST_SCHEMA_ID}, path="$manifest.schema_id"),
        "schema_version": require_enum(row["schema_version"], {SCHEMA_VERSION}, path="$manifest.schema_version"),
        "created_at": require_rfc3339(row["created_at"], path="$manifest.created_at"),
        "producer": _validate_runner_producer(row["producer"]),
        "identity": {
            "benchmark_id": require_string(identity["benchmark_id"], path="$manifest.identity.benchmark_id"),
            "benchmark_manifest_sha256": require_sha256(identity["benchmark_manifest_sha256"], path="$manifest.identity.benchmark_manifest_sha256"),
            "benchmark_preflight_sha256": require_sha256(identity["benchmark_preflight_sha256"], path="$manifest.identity.benchmark_preflight_sha256"),
            "evaluation_logical_run_id": require_string(identity["evaluation_logical_run_id"], path="$manifest.identity.evaluation_logical_run_id"),
            "evaluation_attempt_run_id": require_string(identity["evaluation_attempt_run_id"], path="$manifest.identity.evaluation_attempt_run_id"),
            "evaluation_profile_id": require_string(identity["evaluation_profile_id"], path="$manifest.identity.evaluation_profile_id"),
            "policy_profile_id": require_nullable_string(identity["policy_profile_id"], path="$manifest.identity.policy_profile_id"),
            "scoring_contract_sha256": require_sha256(identity["scoring_contract_sha256"], path="$manifest.identity.scoring_contract_sha256"),
            "selected_chapter_ids": list(selected_chapter_ids),
            "selected_arm_ids": list(selected_arm_ids),
            "selected_scorer_ids": list(selected_scorer_ids),
            "workflow_settings_sha256": _nullable_sha(
                identity["workflow_settings_sha256"],
                "$manifest.identity.workflow_settings_sha256",
            ),
            "baseline_arm_id": baseline,
            "candidate_arm_id": candidate,
        },
        "chapter_bindings": bindings,
        "integrity": _one_hash(row["integrity"], "manifest_sha256", "$manifest.integrity"),
    }
    if not verify_payload_hash(normalized, policy=_MANIFEST_POLICY, hash_path=_MANIFEST_HASH_PATH):
        raise ContractValidationError("manifest_hash", "$manifest.integrity.manifest_sha256", "manifest hash drift")
    canonical = canonicalize(normalized, policy=_MANIFEST_POLICY)
    assert isinstance(canonical, dict)
    return canonical


def _validate_event(value: Mapping[str, Any]) -> dict[str, Any]:
    row = require_mapping(value, path="$event")
    require_exact_keys(row, required={"schema_id", "schema_version", "sequence", "occurred_at", "event_type", "chapter_id", "reason_code", "previous_event_sha256", "manifest_sha256", "integrity"}, path="$event")
    chapter = require_nullable_string(row["chapter_id"], path="$event.chapter_id")
    if chapter is not None and chapter not in BENCHMARK_CHAPTER_IDS_V1:
        raise ContractValidationError("chapter_id", "$event.chapter_id", "foreign chapter")
    normalized = {
        "schema_id": require_enum(row["schema_id"], {RUN_EVENT_SCHEMA_ID}, path="$event.schema_id"),
        "schema_version": require_enum(row["schema_version"], {SCHEMA_VERSION}, path="$event.schema_version"),
        "sequence": require_int(row["sequence"], path="$event.sequence", minimum=1),
        "occurred_at": require_rfc3339(row["occurred_at"], path="$event.occurred_at"),
        "event_type": require_enum(row["event_type"], _EVENT_TYPES, path="$event.event_type"),
        "chapter_id": chapter,
        "reason_code": require_nullable_string(row["reason_code"], path="$event.reason_code"),
        "previous_event_sha256": _nullable_sha(row["previous_event_sha256"], "$event.previous_event_sha256"),
        "manifest_sha256": require_sha256(row["manifest_sha256"], path="$event.manifest_sha256"),
        "integrity": _one_hash(row["integrity"], "event_sha256", "$event.integrity"),
    }
    if not verify_payload_hash(normalized, policy=_EVENT_POLICY, hash_path=_EVENT_HASH_PATH):
        raise ContractValidationError("event_hash", "$event.integrity.event_sha256", "event hash drift")
    return normalized


def _validate_checkpoint(value: Mapping[str, Any]) -> dict[str, Any]:
    row = require_mapping(value, path="$checkpoint")
    require_exact_keys(row, required={"schema_id", "schema_version", "chapter_id", "ordinal", "manifest_sha256", "report_relative_path", "report_sha256", "execution_relative_path", "execution_sha256", "integrity"}, path="$checkpoint")
    ordinal = require_int(row["ordinal"], path="$checkpoint.ordinal", minimum=0)
    if ordinal >= len(BENCHMARK_CHAPTER_IDS_V1):
        raise ContractValidationError(
            "chapter_order",
            "$checkpoint.ordinal",
            "checkpoint ordinal is outside the registered chapter universe",
        )
    normalized = {
        "schema_id": require_enum(row["schema_id"], {CHAPTER_CHECKPOINT_SCHEMA_ID}, path="$checkpoint.schema_id"),
        "schema_version": require_enum(row["schema_version"], {SCHEMA_VERSION}, path="$checkpoint.schema_version"),
        "chapter_id": require_enum(row["chapter_id"], set(BENCHMARK_CHAPTER_IDS_V1), path="$checkpoint.chapter_id"),
        "ordinal": ordinal,
        "manifest_sha256": require_sha256(row["manifest_sha256"], path="$checkpoint.manifest_sha256"),
        "report_relative_path": require_relative_path(row["report_relative_path"], path="$checkpoint.report_relative_path"),
        "report_sha256": require_sha256(row["report_sha256"], path="$checkpoint.report_sha256"),
        "execution_relative_path": require_relative_path(row["execution_relative_path"], path="$checkpoint.execution_relative_path"),
        "execution_sha256": require_sha256(row["execution_sha256"], path="$checkpoint.execution_sha256"),
        "integrity": _one_hash(row["integrity"], "checkpoint_sha256", "$checkpoint.integrity"),
    }
    if not verify_payload_hash(normalized, policy=_CHECKPOINT_POLICY, hash_path=_CHECKPOINT_HASH_PATH):
        raise ContractValidationError("checkpoint_hash", "$checkpoint.integrity.checkpoint_sha256", "checkpoint hash drift")
    return normalized


def _project_status(manifest: Mapping[str, Any], events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    selected_chapter_ids = tuple(
        row["chapter_id"] for row in manifest["chapter_bindings"]
    )
    states = {chapter_id: "pending" for chapter_id in selected_chapter_ids}
    state = "initialized"
    current = None
    reason = None
    for event in events:
        event_type = event["event_type"]
        chapter = event["chapter_id"]
        if event_type in {"benchmark_started", "run_resumed", "aggregation_started", "aggregation_completed"}:
            state = "running"
            current = None
            reason = None
        elif event_type == "preflight_blocked":
            state = "blocked"
            reason = event["reason_code"]
        elif event_type == "chapter_started":
            state = "running"
            current = chapter
            states[chapter] = "running"
            reason = None
        elif event_type in {"chapter_completed", "chapter_reused"}:
            states[chapter] = "completed"
            current = None
        elif event_type == "chapter_failed":
            states[chapter] = "failed"
            current = chapter
            reason = event["reason_code"]
        elif event_type == "run_halted":
            state = "halted"
            reason = event["reason_code"]
        elif event_type == "run_completed":
            state = "completed"
            current = None
            reason = None
    last = events[-1]
    return validate_benchmark_run_status_v1(
        {
            "schema_id": RUN_STATUS_SCHEMA_ID,
            "schema_version": SCHEMA_VERSION,
            "state": state,
            "current_chapter_id": current,
            "completed_chapter_count": sum(value == "completed" for value in states.values()),
            "failed_chapter_count": sum(value == "failed" for value in states.values()),
            "selected_chapter_ids": list(selected_chapter_ids),
            "chapter_states": states,
            "last_event_sequence": last["sequence"],
            "last_event_sha256": last["integrity"]["event_sha256"],
            "reason_code": reason,
            "updated_at": last["occurred_at"],
            "manifest_sha256": manifest["integrity"]["manifest_sha256"],
        }
    )


def _require_completed_report_binding(report: Mapping[str, Any], manifest: Mapping[str, Any]) -> None:
    identity = report["identity"]
    expected = manifest["identity"]
    pairs = {
        "benchmark_id": expected["benchmark_id"],
        "benchmark_manifest_sha256": expected["benchmark_manifest_sha256"],
        "benchmark_preflight_sha256": expected["benchmark_preflight_sha256"],
        "evaluation_logical_run_id": expected["evaluation_logical_run_id"],
        "evaluation_attempt_run_id": expected["evaluation_attempt_run_id"],
        "evaluation_profile_id": expected["evaluation_profile_id"],
        "policy_profile_id": expected["policy_profile_id"],
        "scoring_contract_sha256": expected["scoring_contract_sha256"],
        "selected_chapter_ids": expected["selected_chapter_ids"],
        "selected_arm_ids": expected["selected_arm_ids"],
        "selected_scorer_ids": expected["selected_scorer_ids"],
        "workflow_settings_sha256": expected["workflow_settings_sha256"],
        "baseline_arm_id": expected["baseline_arm_id"],
        "candidate_arm_id": expected["candidate_arm_id"],
    }
    if any(identity[key] != value for key, value in pairs.items()):
        raise ContractValidationError("completed_run_binding", "$.report.identity", "completed benchmark report belongs to another request")


def _validate_runner_producer(value: Any) -> dict[str, str]:
    row = validate_producer(value, path="$manifest.producer", workstream="evaluation")
    if row["component"] != "benchmark_runner_v1" or row["component_version"] != SCHEMA_VERSION:
        raise ContractValidationError("producer", "$manifest.producer", "unexpected benchmark runner producer")
    return row


def _one_hash(value: Any, field: str, path: str) -> dict[str, str]:
    row = require_mapping(value, path=path)
    require_exact_keys(row, required={field}, path=path)
    return {field: require_sha256(row[field], path=f"{path}.{field}")}


def _nullable_sha(value: Any, path: str) -> str | None:
    return None if value is None else require_sha256(value, path=path)


def _validate_selected_ids(
    value: Any,
    *,
    allowed: Sequence[str],
    minimum: int,
    path: str,
) -> tuple[str, ...]:
    rows = tuple(
        require_string(item, path=f"{path}[{index}]")
        for index, item in enumerate(require_list(value, path=path))
    )
    if len(rows) < minimum:
        raise ContractValidationError(
            "selection_size",
            path,
            f"selection requires at least {minimum} item(s)",
        )
    if len(rows) != len(set(rows)):
        raise ContractValidationError(
            "selection_duplicate", path, "selection items must be unique"
        )
    positions = {item: index for index, item in enumerate(allowed)}
    if any(item not in positions for item in rows):
        raise ContractValidationError(
            "settings_selection", path, "selection contains an unregistered item"
        )
    if tuple(sorted(rows, key=positions.__getitem__)) != rows:
        raise ContractValidationError(
            "selection_order", path, "selection must preserve server-owned order"
        )
    return rows


def _settings_arm_id(arm_id: str) -> str:
    return arm_id.lower() if arm_id in {"S0", "S1"} else arm_id


def _relative(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise ContractValidationError("path_escape", str(path), "artifact escapes benchmark root") from exc


def _exception_code(exc: Exception) -> str:
    if isinstance(exc, ContractValidationError):
        return exc.code
    return type(exc).__name__.lower()


def _recover_dead_lock(path: Path) -> None:
    if not path.exists():
        return
    try:
        pid = int(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError) as exc:
        raise ContractValidationError("run_lock", str(path), "benchmark lock is malformed") from exc
    if _process_alive(pid):
        return
    path.unlink(missing_ok=True)


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"), parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ContractValidationError("checkpoint_json", str(path), "invalid checkpoint JSON") from exc


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"


def _write_immutable_json(path: Path, value: Mapping[str, Any]) -> None:
    encoded = _json_bytes(value)
    if path.exists():
        if path.read_bytes() != encoded:
            raise ContractValidationError("immutable_conflict", str(path), "immutable checkpoint differs")
        return
    _write_bytes_atomic(path, encoded)


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    _write_bytes_atomic(path, _json_bytes(value))


def _write_bytes_atomic(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("xb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()
