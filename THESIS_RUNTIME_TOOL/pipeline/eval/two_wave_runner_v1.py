from __future__ import annotations

from dataclasses import dataclass
import copy
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence
import uuid

from pipeline.eval.contracts_v1 import ContractValidationError
from pipeline.eval.two_wave_coverage_v1 import validate_two_wave_sample_coverage_v1
from pipeline.eval.two_wave_sampling_v1 import (
    build_two_wave_uncertainty_decision_v1,
    build_two_wave_work_plan_v1,
    two_wave_workflow_stages_v1,
    validate_two_wave_method_stage_payload_v1,
    validate_two_wave_sampling_manifest_v1,
    validate_two_wave_uncertainty_decision_v1,
)


__all__ = [
    "TwoWaveRunnerResultV1",
    "TwoWaveStageContextV1",
    "run_two_wave_scoring_v1",
]


_RUNNER_SCHEMA_VERSION = "1.0.0"
_EXECUTOR_STAGE_IDS = frozenset(
    {
        "dtq_full",
        "terminology_occurrence_full",
        "btf_wave_a",
        "mtq5_wave_a",
        "btf_wave_b",
        "mtq5_wave_b",
        "aggregation",
        "report_final",
    }
)
_WAVE_B_STAGE_IDS = (
    "btf_wave_b",
    "mtq5_wave_b",
    "uncertainty_gate_wave_b",
)


class _WorkflowWriterV1(Protocol):
    root: Path
    component_run_id: str
    workflow_settings: Mapping[str, Any]
    is_halted: bool
    terminal_event: str | None

    def start_or_resume(self) -> bool: ...

    def stage_state(self, stage_id: str) -> str: ...

    def start_stage(self, stage_id: str, *, work_total: int, work_unit: str) -> None: ...

    def progress(
        self,
        stage_id: str,
        *,
        completed: int,
        total: int,
        unit: str,
        current_work_id: str | None,
        detail: Mapping[str, Any] | None = None,
    ) -> None: ...

    def validation_passed(self, stage_id: str, *, validator_id: str) -> dict[str, Any]: ...

    def validation_failed(
        self, stage_id: str, *, validator_id: str, reason_code: str
    ) -> dict[str, Any]: ...

    def complete_stage(self, stage_id: str, *, outcome: str = "succeeded") -> None: ...

    def append_event(self, *args: Any, **kwargs: Any) -> dict[str, Any]: ...

    def add_artifact(self, *args: Any, **kwargs: Any) -> dict[str, Any]: ...

    def file_binding(
        self, relative_path: str, *, artifact_kind: str, schema_version: str
    ) -> dict[str, str]: ...

    def halt(self, *, reason_code: str) -> None: ...

    def done(self) -> None: ...


@dataclass(frozen=True, slots=True)
class TwoWaveStageContextV1:
    stage_id: str
    active_wave: str | None
    incremental_only: bool
    sampling_manifest: Mapping[str, Any]
    sample_coverage: Mapping[str, Any] | None
    work_plan: Mapping[str, Any] | None
    completed_results: Mapping[str, Mapping[str, Any]]


@dataclass(frozen=True, slots=True)
class TwoWaveRunnerResultV1:
    state: str
    headline_status: str
    final_wave: str | None
    stage_results: Mapping[str, Mapping[str, Any]]
    reused_stage_ids: tuple[str, ...]
    output_root: Path


StageExecutorV1 = Callable[[TwoWaveStageContextV1], Mapping[str, Any]]


def run_two_wave_scoring_v1(
    *,
    sampling_manifest: Mapping[str, Any],
    wave_a_coverage: Mapping[str, Any],
    wave_b_coverage: Mapping[str, Any],
    workflow_writer: _WorkflowWriterV1,
    stage_executors: Mapping[str, StageExecutorV1],
    generated_at: str,
    producer_code_commit: str,
) -> TwoWaveRunnerResultV1:
    manifest = validate_two_wave_sampling_manifest_v1(sampling_manifest)
    coverage_a = validate_two_wave_sample_coverage_v1(
        wave_a_coverage, sampling_manifest=manifest
    )
    coverage_b = validate_two_wave_sample_coverage_v1(
        wave_b_coverage, sampling_manifest=manifest
    )
    if coverage_a["active_wave"] != "wave_a" or coverage_b["active_wave"] != "wave_b":
        raise ContractValidationError(
            "coverage_wave",
            "$.coverage",
            "runner requires one exact Wave A and one exact Wave B coverage artifact",
        )
    if set(stage_executors) != _EXECUTOR_STAGE_IDS:
        raise ContractValidationError(
            "executor_exact_cover",
            "$.stage_executors",
            f"expected exact stage executors {sorted(_EXECUTOR_STAGE_IDS)!r}",
        )
    settings_sha256 = workflow_writer.workflow_settings.get("settings_sha256")
    if not _is_sha256(settings_sha256):
        raise ContractValidationError(
            "settings_binding",
            "$.workflow_writer.workflow_settings.settings_sha256",
            "two-wave runner requires sealed Evaluation settings",
        )
    if Path(workflow_writer.root).resolve() != workflow_writer.root.resolve():
        raise ContractValidationError(
            "output_root",
            "$.workflow_writer.root",
            "workflow writer root must be absolute and resolved",
        )

    if workflow_writer.terminal_event == "component_failed":
        raise ContractValidationError(
            "terminal_component",
            str(workflow_writer.root),
            "failed Evaluation component cannot Resume",
        )
    if workflow_writer.terminal_event == "component_done":
        loaded = _load_existing_results(
            workflow_writer,
            manifest=manifest,
            wave_a_coverage=coverage_a,
            wave_b_coverage=coverage_b,
            settings_sha256=settings_sha256,
        )
        final_wave, headline = _final_outcome(loaded)
        return TwoWaveRunnerResultV1(
            state="completed",
            headline_status=headline,
            final_wave=final_wave,
            stage_results=loaded,
            reused_stage_ids=tuple(loaded),
            output_root=workflow_writer.root,
        )
    if workflow_writer.is_halted:
        workflow_writer.start_or_resume()

    work_plan_a = build_two_wave_work_plan_v1(manifest, active_wave="wave_a")
    work_plan_b = build_two_wave_work_plan_v1(manifest, active_wave="wave_b")
    results: dict[str, Mapping[str, Any]] = {}
    reused: list[str] = []

    try:
        results["preflight"] = _run_stage(
            workflow_writer,
            stage_id="preflight",
            work_total=25,
            work_unit="chapter_arm_cell",
            payload_factory=lambda: {
                "wave_a_coverage_status": coverage_a["coverage_status"],
                "wave_a_coverage_sha256": coverage_a["integrity"]["coverage_sha256"],
                "wave_b_coverage_status": coverage_b["coverage_status"],
                "wave_b_coverage_sha256": coverage_b["integrity"]["coverage_sha256"],
                "status": (
                    "ready"
                    if coverage_a["coverage_status"] == "ready"
                    and coverage_b["coverage_status"] == "ready"
                    else "blocked"
                ),
            },
            runner_binding=_runner_binding(
                workflow_writer,
                manifest,
                settings_sha256=settings_sha256,
                active_wave=None,
            ),
            reused_stage_ids=reused,
        )
        if results["preflight"]["status"] != "ready":
            return _halted_result(
                workflow_writer,
                results,
                reused,
                reason_code="two_wave_sample_coverage_blocked",
            )

        results["sample_plan"] = _run_stage(
            workflow_writer,
            stage_id="sample_plan",
            work_total=100,
            work_unit="cluster",
            payload_factory=lambda: {
                "sampling_manifest_sha256": manifest["integrity"]["manifest_sha256"],
                "wave_a_work_plan_sha256": work_plan_a["integrity"][
                    "work_plan_sha256"
                ],
                "wave_b_work_plan_sha256": work_plan_b["integrity"][
                    "work_plan_sha256"
                ],
                "wave_a_cluster_count": len(work_plan_a["active_cluster_ids"]),
                "wave_b_cluster_count": len(work_plan_b["active_cluster_ids"]),
                "wave_b_incremental_cluster_count": len(
                    work_plan_b["incremental_cluster_ids"]
                ),
            },
            runner_binding=_runner_binding(
                workflow_writer,
                manifest,
                settings_sha256=settings_sha256,
                active_wave=None,
            ),
            reused_stage_ids=reused,
        )

        for stage_id in ("dtq_full", "terminology_occurrence_full"):
            work_total, work_unit = _work_total(stage_id, work_plan_a)
            results[stage_id] = _run_executor_stage(
                workflow_writer,
                stage_id=stage_id,
                active_wave=None,
                incremental_only=False,
                coverage=None,
                work_plan=work_plan_a,
                manifest=manifest,
                completed_results=results,
                executor=stage_executors[stage_id],
                work_total=work_total,
                work_unit=work_unit,
                settings_sha256=settings_sha256,
                reused_stage_ids=reused,
            )

        for stage_id in ("btf_wave_a", "mtq5_wave_a"):
            work_total, work_unit = _work_total(stage_id, work_plan_a)
            results[stage_id] = _run_executor_stage(
                workflow_writer,
                stage_id=stage_id,
                active_wave="wave_a",
                incremental_only=False,
                coverage=coverage_a,
                work_plan=work_plan_a,
                manifest=manifest,
                completed_results=results,
                executor=stage_executors[stage_id],
                work_total=work_total,
                work_unit=work_unit,
                settings_sha256=settings_sha256,
                reused_stage_ids=reused,
            )

        results["uncertainty_gate_wave_a"] = _run_stage(
            workflow_writer,
            stage_id="uncertainty_gate_wave_a",
            work_total=10,
            work_unit="arm_pair",
            payload_factory=lambda: _build_uncertainty_decision(
                workflow_writer,
                manifest=manifest,
                completed_wave="wave_a",
                sample_coverage_sha256s={
                    "wave_a": coverage_a["integrity"]["coverage_sha256"],
                },
                settings_sha256=settings_sha256,
                created_at=generated_at,
                producer_code_commit=producer_code_commit,
            ),
            runner_binding=_runner_binding(
                workflow_writer,
                manifest,
                settings_sha256=settings_sha256,
                active_wave="wave_a",
            ),
            reused_stage_ids=reused,
        )
        results["uncertainty_gate_wave_a"] = _validate_uncertainty_result(
            workflow_writer,
            results["uncertainty_gate_wave_a"],
            manifest=manifest,
            completed_wave="wave_a",
            sample_coverage_sha256s={
                "wave_a": coverage_a["integrity"]["coverage_sha256"],
            },
            settings_sha256=settings_sha256,
        )
        wave_a_decision = results["uncertainty_gate_wave_a"]["decision"]
        if wave_a_decision == "open_wave_b":
            if coverage_b["coverage_status"] != "ready":
                return _halted_result(
                    workflow_writer,
                    results,
                    reused,
                    reason_code="wave_b_sample_coverage_blocked",
                )
            for stage_id in ("btf_wave_b", "mtq5_wave_b"):
                work_total, work_unit = _work_total(stage_id, work_plan_b)
                results[stage_id] = _run_executor_stage(
                    workflow_writer,
                    stage_id=stage_id,
                    active_wave="wave_b",
                    incremental_only=True,
                    coverage=coverage_b,
                    work_plan=work_plan_b,
                    manifest=manifest,
                    completed_results=results,
                    executor=stage_executors[stage_id],
                    work_total=work_total,
                    work_unit=work_unit,
                    settings_sha256=settings_sha256,
                    reused_stage_ids=reused,
                )
            results["uncertainty_gate_wave_b"] = _run_stage(
                workflow_writer,
                stage_id="uncertainty_gate_wave_b",
                work_total=10,
                work_unit="arm_pair",
                payload_factory=lambda: _build_uncertainty_decision(
                    workflow_writer,
                    manifest=manifest,
                    completed_wave="wave_b",
                    sample_coverage_sha256s={
                        "wave_a": coverage_a["integrity"]["coverage_sha256"],
                        "wave_b": coverage_b["integrity"]["coverage_sha256"],
                    },
                    settings_sha256=settings_sha256,
                    created_at=generated_at,
                    producer_code_commit=producer_code_commit,
                ),
                runner_binding=_runner_binding(
                    workflow_writer,
                    manifest,
                    settings_sha256=settings_sha256,
                    active_wave="wave_b",
                ),
                reused_stage_ids=reused,
            )
            results["uncertainty_gate_wave_b"] = _validate_uncertainty_result(
                workflow_writer,
                results["uncertainty_gate_wave_b"],
                manifest=manifest,
                completed_wave="wave_b",
                sample_coverage_sha256s={
                    "wave_a": coverage_a["integrity"]["coverage_sha256"],
                    "wave_b": coverage_b["integrity"]["coverage_sha256"],
                },
                settings_sha256=settings_sha256,
            )
            final_wave = "wave_b"
        else:
            for stage_id in _WAVE_B_STAGE_IDS:
                results[stage_id] = _run_skipped_stage(
                    workflow_writer,
                    stage_id=stage_id,
                    reason_code="wave_a_conclusive",
                    runner_binding=_runner_binding(
                        workflow_writer,
                        manifest,
                        settings_sha256=settings_sha256,
                        active_wave="wave_b",
                    ),
                    reused_stage_ids=reused,
                )
            final_wave = "wave_a"

        for stage_id in ("aggregation", "report_final"):
            work_total, work_unit = _work_total(
                stage_id, work_plan_b if final_wave == "wave_b" else work_plan_a
            )
            results[stage_id] = _run_executor_stage(
                workflow_writer,
                stage_id=stage_id,
                active_wave=final_wave,
                incremental_only=False,
                coverage=coverage_b if final_wave == "wave_b" else coverage_a,
                work_plan=work_plan_b if final_wave == "wave_b" else work_plan_a,
                manifest=manifest,
                completed_results=results,
                executor=stage_executors[stage_id],
                work_total=work_total,
                work_unit=work_unit,
                settings_sha256=settings_sha256,
                reused_stage_ids=reused,
            )
        workflow_writer.done()
        _, headline = _final_outcome(results)
        return TwoWaveRunnerResultV1(
            state="completed",
            headline_status=headline,
            final_wave=final_wave,
            stage_results=copy.deepcopy(results),
            reused_stage_ids=tuple(reused),
            output_root=workflow_writer.root,
        )
    except Exception as exc:
        if workflow_writer.terminal_event is None and not workflow_writer.is_halted:
            _persist_runner_checkpoint(
                workflow_writer,
                stage_id=_active_stage_id(workflow_writer),
                manifest_sha256=manifest["integrity"]["manifest_sha256"],
                settings_sha256=settings_sha256,
                completed_stage_ids=tuple(results),
            )
            workflow_writer.halt(reason_code=type(exc).__name__)
        raise


def _run_executor_stage(
    writer: _WorkflowWriterV1,
    *,
    stage_id: str,
    active_wave: str | None,
    incremental_only: bool,
    coverage: Mapping[str, Any] | None,
    work_plan: Mapping[str, Any],
    manifest: Mapping[str, Any],
    completed_results: Mapping[str, Mapping[str, Any]],
    executor: StageExecutorV1,
    work_total: int,
    work_unit: str,
    settings_sha256: str,
    reused_stage_ids: list[str],
) -> Mapping[str, Any]:
    context = TwoWaveStageContextV1(
        stage_id=stage_id,
        active_wave=active_wave,
        incremental_only=incremental_only,
        sampling_manifest=manifest,
        sample_coverage=coverage,
        work_plan=work_plan,
        completed_results=copy.deepcopy(completed_results),
    )
    payload = _run_stage(
        writer,
        stage_id=stage_id,
        work_total=work_total,
        work_unit=work_unit,
        payload_factory=lambda: _validate_executor_payload(
            executor(context),
            stage_id=stage_id,
            manifest=manifest,
            coverage=coverage,
        ),
        runner_binding=_runner_binding(
            writer,
            manifest,
            settings_sha256=settings_sha256,
            active_wave=active_wave,
        ),
        reused_stage_ids=reused_stage_ids,
    )
    return _validate_executor_payload(
        payload,
        stage_id=stage_id,
        manifest=manifest,
        coverage=coverage,
    )


def _validate_executor_payload(
    value: Any,
    *,
    stage_id: str,
    manifest: Mapping[str, Any],
    coverage: Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    payload = _require_payload(value, stage_id=stage_id)
    if stage_id not in {"btf_wave_a", "mtq5_wave_a", "btf_wave_b", "mtq5_wave_b"}:
        return payload
    if coverage is None:
        raise ContractValidationError(
            "coverage_binding",
            f"$.stages.{stage_id}",
            "sampled method stage requires an exact coverage artifact",
        )
    return validate_two_wave_method_stage_payload_v1(
        payload,
        sampling_manifest=manifest,
        expected_stage_id=stage_id,
        expected_coverage_sha256=coverage["integrity"]["coverage_sha256"],
    )


def _run_stage(
    writer: _WorkflowWriterV1,
    *,
    stage_id: str,
    work_total: int,
    work_unit: str,
    payload_factory: Callable[[], Mapping[str, Any]],
    runner_binding: Mapping[str, Any],
    reused_stage_ids: list[str],
) -> Mapping[str, Any]:
    state = writer.stage_state(stage_id)
    if state in {"succeeded", "skipped"}:
        artifact = _load_stage_artifact(
            writer, stage_id=stage_id, runner_binding=runner_binding
        )
        reused_stage_ids.append(stage_id)
        return artifact["payload"]
    if state == "running":
        _persist_runner_checkpoint(
            writer,
            stage_id=stage_id,
            manifest_sha256=runner_binding["sampling_manifest_sha256"],
            settings_sha256=runner_binding["settings_sha256"],
            completed_stage_ids=(),
        )
        writer.halt(reason_code="interrupted_running_stage")
        writer.start_or_resume()
    writer.start_stage(stage_id, work_total=work_total, work_unit=work_unit)
    try:
        payload = _require_payload(payload_factory(), stage_id=stage_id)
        artifact = _persist_stage_artifact(
            writer,
            stage_id=stage_id,
            runner_binding=runner_binding,
            payload=payload,
        )
        passed = writer.validation_passed(
            stage_id, validator_id="evaluation_two_wave_stage_artifact_v1"
        )
        writer.add_artifact(
            _stage_relative_path(stage_id),
            artifact_kind="evaluation_two_wave_stage_artifact_v1",
            schema_version=_RUNNER_SCHEMA_VERSION,
            stage_id=stage_id,
            created_by_event_id=passed["event_id"],
            parent_artifact_refs=("workflow_settings.json", "scoring_receipt.json"),
        )
        writer.progress(
            stage_id,
            completed=work_total,
            total=work_total,
            unit=work_unit,
            current_work_id=None,
        )
        _persist_runner_checkpoint(
            writer,
            stage_id=stage_id,
            manifest_sha256=runner_binding["sampling_manifest_sha256"],
            settings_sha256=runner_binding["settings_sha256"],
            completed_stage_ids=(stage_id,),
        )
        writer.complete_stage(stage_id, outcome="succeeded")
        return artifact["payload"]
    except Exception as exc:
        writer.validation_failed(
            stage_id,
            validator_id="evaluation_two_wave_stage_artifact_v1",
            reason_code=type(exc).__name__,
        )
        raise


def _run_skipped_stage(
    writer: _WorkflowWriterV1,
    *,
    stage_id: str,
    reason_code: str,
    runner_binding: Mapping[str, Any],
    reused_stage_ids: list[str],
) -> Mapping[str, Any]:
    state = writer.stage_state(stage_id)
    if state == "skipped":
        artifact = _load_stage_artifact(
            writer, stage_id=stage_id, runner_binding=runner_binding
        )
        reused_stage_ids.append(stage_id)
        return artifact["payload"]
    if state != "pending":
        raise ContractValidationError(
            "stage_state",
            f"$.stages.{stage_id}",
            f"cannot skip stage in state {state!r}",
        )
    writer.start_stage(stage_id, work_total=0, work_unit="skipped")
    payload = {"status": "skipped", "reason_code": reason_code}
    artifact = _persist_stage_artifact(
        writer,
        stage_id=stage_id,
        runner_binding=runner_binding,
        payload=payload,
    )
    passed = writer.validation_passed(
        stage_id, validator_id="evaluation_two_wave_stage_artifact_v1"
    )
    writer.add_artifact(
        _stage_relative_path(stage_id),
        artifact_kind="evaluation_two_wave_stage_artifact_v1",
        schema_version=_RUNNER_SCHEMA_VERSION,
        stage_id=stage_id,
        created_by_event_id=passed["event_id"],
        parent_artifact_refs=("workflow_settings.json", "scoring_receipt.json"),
    )
    _persist_runner_checkpoint(
        writer,
        stage_id=stage_id,
        manifest_sha256=runner_binding["sampling_manifest_sha256"],
        settings_sha256=runner_binding["settings_sha256"],
        completed_stage_ids=(stage_id,),
    )
    writer.complete_stage(stage_id, outcome="skipped")
    return artifact["payload"]


def _halted_result(
    writer: _WorkflowWriterV1,
    results: Mapping[str, Mapping[str, Any]],
    reused: Sequence[str],
    *,
    reason_code: str,
) -> TwoWaveRunnerResultV1:
    latest_stage = next(reversed(results), "preflight")
    writer.halt(reason_code=reason_code)
    return TwoWaveRunnerResultV1(
        state="halted",
        headline_status="BLOCKED",
        final_wave=None,
        stage_results=copy.deepcopy(results),
        reused_stage_ids=tuple(reused),
        output_root=writer.root,
    )


def _persist_stage_artifact(
    writer: _WorkflowWriterV1,
    *,
    stage_id: str,
    runner_binding: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    body = {
        "schema_id": "EvaluationTwoWaveStageArtifactV1",
        "schema_version": _RUNNER_SCHEMA_VERSION,
        "stage_id": stage_id,
        "runner_binding": dict(runner_binding),
        "payload": copy.deepcopy(dict(payload)),
    }
    artifact = {**body, "artifact_sha256": _canonical_sha256(body)}
    path = writer.root / _stage_relative_path(stage_id)
    _write_immutable_json(path, artifact)
    return _validate_stage_artifact(
        artifact, stage_id=stage_id, runner_binding=runner_binding
    )


def _load_stage_artifact(
    writer: _WorkflowWriterV1,
    *,
    stage_id: str,
    runner_binding: Mapping[str, Any] | None,
) -> dict[str, Any]:
    path = writer.root / _stage_relative_path(stage_id)
    if not path.is_file():
        raise ContractValidationError(
            "missing_stage_artifact", str(path), "completed stage artifact is missing"
        )
    return _validate_stage_artifact(
        _load_json(path), stage_id=stage_id, runner_binding=runner_binding
    )


def _validate_stage_artifact(
    value: Mapping[str, Any],
    *,
    stage_id: str,
    runner_binding: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractValidationError("type", "$stage_artifact", "expected object")
    required = {
        "schema_id",
        "schema_version",
        "stage_id",
        "runner_binding",
        "payload",
        "artifact_sha256",
    }
    if set(value) != required:
        raise ContractValidationError(
            "keys", "$stage_artifact", "stage artifact has missing or unknown keys"
        )
    if (
        value["schema_id"] != "EvaluationTwoWaveStageArtifactV1"
        or value["schema_version"] != _RUNNER_SCHEMA_VERSION
        or value["stage_id"] != stage_id
    ):
        raise ContractValidationError(
            "stage_binding", "$stage_artifact", "stage artifact identity mismatch"
        )
    observed_binding = value["runner_binding"]
    if not isinstance(observed_binding, Mapping):
        raise ContractValidationError(
            "type", "$stage_artifact.runner_binding", "expected object"
        )
    if runner_binding is not None and dict(observed_binding) != dict(runner_binding):
        raise ContractValidationError(
            "runner_binding",
            "$stage_artifact.runner_binding",
            "stage artifact belongs to another sealed run",
        )
    payload = _require_payload(value["payload"], stage_id=stage_id)
    body = {
        "schema_id": value["schema_id"],
        "schema_version": value["schema_version"],
        "stage_id": value["stage_id"],
        "runner_binding": dict(observed_binding),
        "payload": copy.deepcopy(dict(payload)),
    }
    if value["artifact_sha256"] != _canonical_sha256(body):
        raise ContractValidationError(
            "artifact_hash",
            "$stage_artifact.artifact_sha256",
            "stage artifact hash mismatch",
        )
    return {**body, "artifact_sha256": value["artifact_sha256"]}


def _persist_runner_checkpoint(
    writer: _WorkflowWriterV1,
    *,
    stage_id: str,
    manifest_sha256: str,
    settings_sha256: str,
    completed_stage_ids: Sequence[str],
) -> dict[str, Any]:
    completed = _completed_stage_ids(writer, completed_stage_ids)
    body = {
        "schema_id": "EvaluationTwoWaveRunnerCheckpointV1",
        "schema_version": _RUNNER_SCHEMA_VERSION,
        "component_run_id": writer.component_run_id,
        "sampling_manifest_sha256": manifest_sha256,
        "settings_sha256": settings_sha256,
        "stage_id": stage_id,
        "completed_stage_ids": list(completed),
    }
    checkpoint = {**body, "checkpoint_sha256": _canonical_sha256(body)}
    relative_path = f"two_wave/checkpoints/{checkpoint['checkpoint_sha256']}.json"
    _write_immutable_json(writer.root / relative_path, checkpoint)
    binding = writer.file_binding(
        relative_path,
        artifact_kind="evaluation_two_wave_runner_checkpoint_v1",
        schema_version=_RUNNER_SCHEMA_VERSION,
    )
    event = writer.append_event(
        "checkpoint",
        stage_id=stage_id,
        agent=_stage_agent(stage_id),
        severity="info",
        payload={"checkpoint": binding, "work_id": stage_id},
    )
    writer.add_artifact(
        relative_path,
        artifact_kind="evaluation_two_wave_runner_checkpoint_v1",
        schema_version=_RUNNER_SCHEMA_VERSION,
        stage_id=stage_id,
        created_by_event_id=event["event_id"],
        parent_artifact_refs=("scoring_receipt.json",),
    )
    return checkpoint


def _completed_stage_ids(
    writer: _WorkflowWriterV1,
    additional_stage_ids: Sequence[str],
) -> tuple[str, ...]:
    additional = set(additional_stage_ids)
    declared = two_wave_workflow_stages_v1()
    known = {stage["stage_id"] for stage in declared}
    if not additional.issubset(known):
        raise ContractValidationError(
            "checkpoint_stage",
            "$.completed_stage_ids",
            "checkpoint names an unknown completed stage",
        )
    return tuple(
        stage["stage_id"]
        for stage in declared
        if writer.stage_state(stage["stage_id"]) in {"succeeded", "skipped"}
        or stage["stage_id"] in additional
    )


def _stage_agent(stage_id: str) -> str:
    for stage in two_wave_workflow_stages_v1():
        if stage["stage_id"] == stage_id:
            return str(stage["agent"])
    raise ContractValidationError(
        "checkpoint_stage",
        "$.stage_id",
        "checkpoint stage is not declared in the two-wave schedule",
    )


def _method_stage_ids(completed_wave: str) -> tuple[str, ...]:
    if completed_wave == "wave_a":
        return ("btf_wave_a", "mtq5_wave_a")
    if completed_wave == "wave_b":
        return ("btf_wave_a", "mtq5_wave_a", "btf_wave_b", "mtq5_wave_b")
    raise ContractValidationError(
        "completed_wave",
        "$.completed_wave",
        "completed wave must be wave_a or wave_b",
    )


def _load_method_stage_artifacts(
    writer: _WorkflowWriterV1,
    *,
    manifest: Mapping[str, Any],
    completed_wave: str,
    settings_sha256: str,
) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    for stage_id in _method_stage_ids(completed_wave):
        active_wave = "wave_a" if stage_id.endswith("_wave_a") else "wave_b"
        artifacts.append(
            _load_stage_artifact(
                writer,
                stage_id=stage_id,
                runner_binding=_runner_binding(
                    writer,
                    manifest,
                    settings_sha256=settings_sha256,
                    active_wave=active_wave,
                ),
            )
        )
    return artifacts


def _build_uncertainty_decision(
    writer: _WorkflowWriterV1,
    *,
    manifest: Mapping[str, Any],
    completed_wave: str,
    sample_coverage_sha256s: Mapping[str, str],
    settings_sha256: str,
    created_at: str,
    producer_code_commit: str,
) -> dict[str, Any]:
    artifacts = _load_method_stage_artifacts(
        writer,
        manifest=manifest,
        completed_wave=completed_wave,
        settings_sha256=settings_sha256,
    )
    return build_two_wave_uncertainty_decision_v1(
        manifest,
        completed_wave=completed_wave,
        sample_coverage_sha256s=sample_coverage_sha256s,
        method_stage_artifacts=artifacts,
        created_at=created_at,
        producer_code_commit=producer_code_commit,
    )


def _validate_uncertainty_result(
    writer: _WorkflowWriterV1,
    value: Mapping[str, Any],
    *,
    manifest: Mapping[str, Any],
    completed_wave: str,
    sample_coverage_sha256s: Mapping[str, str],
    settings_sha256: str,
) -> dict[str, Any]:
    artifacts = _load_method_stage_artifacts(
        writer,
        manifest=manifest,
        completed_wave=completed_wave,
        settings_sha256=settings_sha256,
    )
    accepted = validate_two_wave_uncertainty_decision_v1(
        value,
        sampling_manifest=manifest,
        sample_coverage_sha256s=sample_coverage_sha256s,
        method_stage_artifacts=artifacts,
    )
    if accepted["completed_wave"] != completed_wave:
        raise ContractValidationError(
            "completed_wave",
            "$.decision.completed_wave",
            "uncertainty decision belongs to another wave",
        )
    return accepted


def _load_existing_results(
    writer: _WorkflowWriterV1,
    *,
    manifest: Mapping[str, Any],
    wave_a_coverage: Mapping[str, Any],
    wave_b_coverage: Mapping[str, Any],
    settings_sha256: str,
) -> dict[str, Mapping[str, Any]]:
    results: dict[str, Mapping[str, Any]] = {}
    artifacts: dict[str, Mapping[str, Any]] = {}
    manifest_sha256 = manifest["integrity"]["manifest_sha256"]
    for stage in two_wave_workflow_stages_v1():
        stage_id = stage["stage_id"]
        if writer.stage_state(stage_id) not in {"succeeded", "skipped"}:
            raise ContractValidationError(
                "terminal_stage",
                f"$.stages.{stage_id}",
                "completed component contains a nonterminal stage",
            )
        artifact = _load_stage_artifact(
            writer,
            stage_id=stage_id,
            runner_binding=None,
        )
        binding = artifact["runner_binding"]
        if (
            binding.get("sampling_manifest_sha256") != manifest_sha256
            or binding.get("settings_sha256") != settings_sha256
        ):
            raise ContractValidationError(
                "runner_binding",
                f"$.stages.{stage_id}",
                "completed stage belongs to another run identity",
            )
        results[stage_id] = artifact["payload"]
        artifacts[stage_id] = artifact

    results["uncertainty_gate_wave_a"] = _validate_uncertainty_result(
        writer,
        results["uncertainty_gate_wave_a"],
        manifest=manifest,
        completed_wave="wave_a",
        sample_coverage_sha256s={
            "wave_a": wave_a_coverage["integrity"]["coverage_sha256"],
        },
        settings_sha256=settings_sha256,
    )
    if results["uncertainty_gate_wave_a"]["decision"] == "open_wave_b":
        for stage_id in _WAVE_B_STAGE_IDS:
            if writer.stage_state(stage_id) != "succeeded":
                raise ContractValidationError(
                    "wave_schedule",
                    f"$.stages.{stage_id}",
                    "an opened Wave B must complete every Wave B stage",
                )
        results["uncertainty_gate_wave_b"] = _validate_uncertainty_result(
            writer,
            results["uncertainty_gate_wave_b"],
            manifest=manifest,
            completed_wave="wave_b",
            sample_coverage_sha256s={
                "wave_a": wave_a_coverage["integrity"]["coverage_sha256"],
                "wave_b": wave_b_coverage["integrity"]["coverage_sha256"],
            },
            settings_sha256=settings_sha256,
        )
        final_wave = "wave_b"
    else:
        for stage_id in _WAVE_B_STAGE_IDS:
            if writer.stage_state(stage_id) != "skipped":
                raise ContractValidationError(
                    "wave_schedule",
                    f"$.stages.{stage_id}",
                    "a conclusive Wave A must skip every Wave B stage",
                )
        final_wave = "wave_a"

    expected_active_waves: dict[str, str | None] = {
        "preflight": None,
        "sample_plan": None,
        "dtq_full": None,
        "terminology_occurrence_full": None,
        "btf_wave_a": "wave_a",
        "mtq5_wave_a": "wave_a",
        "uncertainty_gate_wave_a": "wave_a",
        "btf_wave_b": "wave_b",
        "mtq5_wave_b": "wave_b",
        "uncertainty_gate_wave_b": "wave_b",
        "aggregation": final_wave,
        "report_final": final_wave,
    }
    for stage_id, artifact in artifacts.items():
        expected_binding = _runner_binding(
            writer,
            manifest,
            settings_sha256=settings_sha256,
            active_wave=expected_active_waves[stage_id],
        )
        if dict(artifact["runner_binding"]) != expected_binding:
            raise ContractValidationError(
                "runner_binding",
                f"$.stages.{stage_id}",
                "completed stage belongs to another sealed runner identity",
            )
    return results


def _runner_binding(
    writer: _WorkflowWriterV1,
    manifest: Mapping[str, Any],
    *,
    settings_sha256: str,
    active_wave: str | None,
) -> dict[str, Any]:
    return {
        "component_run_id": writer.component_run_id,
        "sampling_manifest_sha256": manifest["integrity"]["manifest_sha256"],
        "settings_sha256": settings_sha256,
        "active_wave": active_wave,
    }


def _work_total(stage_id: str, plan: Mapping[str, Any]) -> tuple[int, str]:
    logical = plan["logical_work"]
    if stage_id == "dtq_full":
        return logical["dtq_full_rows"], "arm_block"
    if stage_id == "terminology_occurrence_full":
        return logical["terminology_full_blocks"], "source_block"
    if stage_id.startswith("btf_"):
        return logical["btf_incremental_rows"], "arm_block"
    if stage_id.startswith("mtq5_"):
        return logical["mtq5_incremental_cluster_pair_orientations"], "judgment"
    if stage_id == "aggregation":
        return 5, "method"
    if stage_id == "report_final":
        return 1, "report"
    raise ContractValidationError("stage_id", "$.stage_id", f"unknown stage {stage_id}")


def _require_payload(value: Any, *, stage_id: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractValidationError(
            "stage_payload",
            f"$.stages.{stage_id}",
            "stage executor must return an object",
        )
    try:
        _canonical_bytes(value)
    except (TypeError, ValueError) as exc:
        raise ContractValidationError(
            "stage_payload",
            f"$.stages.{stage_id}",
            "stage payload must be finite canonical JSON",
        ) from exc
    return copy.deepcopy(dict(value))


def _final_outcome(
    results: Mapping[str, Mapping[str, Any]]
) -> tuple[str | None, str]:
    wave_b = results.get("uncertainty_gate_wave_b")
    if wave_b and wave_b.get("status") != "skipped":
        return "wave_b", str(wave_b["headline_status"])
    wave_a = results.get("uncertainty_gate_wave_a")
    if wave_a is not None:
        return "wave_a", str(wave_a["headline_status"])
    return None, "BLOCKED"


def _active_stage_id(writer: _WorkflowWriterV1) -> str:
    for stage in reversed(two_wave_workflow_stages_v1()):
        if writer.stage_state(stage["stage_id"]) in {"running", "halted"}:
            return stage["stage_id"]
    for stage in reversed(two_wave_workflow_stages_v1()):
        if writer.stage_state(stage["stage_id"]) in {"succeeded", "skipped"}:
            return stage["stage_id"]
    return "preflight"


def _stage_relative_path(stage_id: str) -> str:
    return f"two_wave/stages/{stage_id}.json"


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _write_immutable_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    if path.exists():
        if path.read_bytes() != encoded:
            raise ContractValidationError(
                "immutable_conflict", str(path), "existing artifact bytes differ"
            )
        return
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractValidationError(
            "artifact_read", str(path), "cannot read JSON artifact"
        ) from exc
    if not isinstance(value, dict):
        raise ContractValidationError("type", str(path), "expected JSON object")
    return value


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )
