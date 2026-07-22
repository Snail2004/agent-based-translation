from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from pipeline.eval.common_input_v1 import CommonEvaluationInputV1
from pipeline.eval.contracts_v1 import (
    ContractValidationError,
    require_commit,
    require_enum,
    require_exact_keys,
    require_list,
    require_mapping,
    require_nullable_string,
    require_relative_path,
    require_rfc3339,
    require_sha256,
    require_string,
    require_unique,
    validate_method,
)
from pipeline.eval.execution_runner_v1 import (
    execute_evaluation_plan_v1,
    validate_evaluation_execution_binding,
)
from pipeline.eval.execution_store_v1 import (
    EvaluationExecutionBundleV1,
    load_evaluation_execution_bundle_v1,
    persist_evaluation_execution_bundle_v1,
)
from pipeline.eval.full_run_report_v1 import validate_full_run_report
from pipeline.eval.full_run_report_writer_v1 import (
    compose_full_run_report_v1,
    persist_full_run_report_v1,
)
from pipeline.eval.local_sf_qe_v1 import (
    BatchPredictorV1,
    LocalSfQePreparedV1,
    load_local_sf_qe_evidence_v1,
    persist_local_sf_qe_evidence_v1,
    prepare_local_sf_qe_v1,
)
from pipeline.eval.method_executors_v1 import (
    EvaluationMethodExecutorV1,
    SharedEvaluationRoleRunnerV1,
)
from pipeline.eval.offline_orchestrator_v1 import (
    build_evaluation_plan,
    validate_evaluation_run_config,
)
from pipeline.eval.resumable_execution_v1 import (
    EvaluationRunStateStoreV1,
    ResumableEvaluationRoleRunnerV1,
    execute_evaluation_plan_resumable_v1,
)
from pipeline.eval.scorer_input_packets_v1 import build_scorer_input_packet
from pipeline.eval.scorer_prompts_v3 import prepare_pj_prompt_presentations_v3
from pipeline.eval.usage_projection_v1 import (
    persist_evaluation_usage_artifact_v1,
    project_evaluation_usage_v1,
)
from pipeline.llm_backend import SharedLlmAttemptLedger


__all__ = [
    "EndToEndEvaluationResultV1",
    "LocalSfQeRuntimeV1",
    "run_evaluation_end_to_end_v1",
]


@dataclass(frozen=True, slots=True)
class LocalSfQeRuntimeV1:
    predictor: BatchPredictorV1
    checkpoint_sha256: str
    package_name: str
    package_version: str
    device: str
    batch_size: int
    clock: Callable[[], datetime] | None = None
    monotonic: Callable[[], float] | None = None


@dataclass(frozen=True, slots=True)
class EndToEndEvaluationResultV1:
    output_root: Path
    report_path: Path
    report: dict[str, Any]
    execution_path: Path
    execution: dict[str, Any]
    usage_path: Path | None
    local_sf_qe_path: Path | None
    reused_complete_run: bool


def run_evaluation_end_to_end_v1(
    common_input: CommonEvaluationInputV1,
    config_payload: Mapping[str, Any],
    output_root: Path,
    *,
    generated_at: str,
    producer_code_commit: str,
    evaluation_logical_run_id: str,
    evaluation_attempt_run_id: str,
    evaluation_profile_id: str,
    policy_profile_id: str | None,
    input_artifact: Mapping[str, Any],
    arm_presentations: Sequence[Mapping[str, Any]],
    method_presentations: Sequence[Mapping[str, Any]],
    baseline_arm_id: str | None = None,
    candidate_arm_id: str | None = None,
    local_sf_qe_runtime: LocalSfQeRuntimeV1 | None = None,
    llm_roles: SharedEvaluationRoleRunnerV1 | None = None,
    shared_ledger: SharedLlmAttemptLedger | None = None,
    shared_ledger_relative_path: str | None = None,
    caveats: Sequence[str] = (),
) -> EndToEndEvaluationResultV1:
    """Run or resume one sealed Evaluation attempt without choosing providers.

    The caller owns concrete model/profile values and materializes immutable
    input/translation artifacts below ``output_root`` before this function is
    entered. A committed execution is never recomputed. A complete report is
    validated and reused without invoking either local or remote scorers.
    """

    config = validate_evaluation_run_config(config_payload)
    plan = build_evaluation_plan(common_input, config)
    root = _prepare_root(output_root)
    timestamp = require_rfc3339(generated_at, path="$.generated_at")
    commit = require_commit(producer_code_commit, path="$.producer_code_commit")
    logical_run_id = require_string(
        evaluation_logical_run_id, path="$.evaluation_logical_run_id"
    )
    attempt_run_id = require_string(
        evaluation_attempt_run_id, path="$.evaluation_attempt_run_id"
    )
    profile_id = require_string(
        evaluation_profile_id, path="$.evaluation_profile_id"
    )
    policy_id = require_nullable_string(
        policy_profile_id, path="$.policy_profile_id"
    )
    input_row, arm_rows, method_rows, caveat_rows = _preflight_report_inputs(
        root=root,
        common_input=common_input,
        config=config,
        input_artifact=input_artifact,
        arm_presentations=arm_presentations,
        method_presentations=method_presentations,
        caveats=caveats,
    )
    _validate_comparison_roles(
        plan.selected_arm_ids,
        baseline_arm_id=baseline_arm_id,
        candidate_arm_id=candidate_arm_id,
    )

    report_path = root / "reports" / "full_run_report_v1.json"
    if report_path.exists():
        bundle = _load_bound_bundle(root, common_input, config, plan)
        report = validate_full_run_report(_load_json_object(report_path))
        persisted = persist_full_run_report_v1(
            output_root=root, report_payload=report
        )
        _validate_completed_report_request(
            report,
            common_input=common_input,
            config=config,
            input_artifact=input_row,
            arm_presentations=arm_rows,
            method_presentations=method_rows,
            evaluation_logical_run_id=logical_run_id,
            evaluation_attempt_run_id=attempt_run_id,
            evaluation_profile_id=profile_id,
            policy_profile_id=policy_id,
            caveats=caveat_rows,
        )
        run_state_manifest = root / "run_state" / "manifest.json"
        if run_state_manifest.is_file():
            state_store = EvaluationRunStateStoreV1.open_existing(
                root / "run_state"
            )
            with state_store.run_lock():
                status = state_store.status()
                if status["state"] != "completed":
                    if status["last_event_sequence"] > 1:
                        state_store.append_event(
                            "run_resumed", stage_id=status["current_stage_id"]
                        )
                    state_store.append_event("stage_started", stage_id="report")
                    state_store.append_event("stage_completed", stage_id="report")
                    state_store.append_event("run_completed")
        return EndToEndEvaluationResultV1(
            output_root=root,
            report_path=persisted.report_path,
            report=persisted.report,
            execution_path=bundle.execution_path,
            execution=bundle.execution,
            usage_path=_report_usage_path(root, report),
            local_sf_qe_path=_find_report_local_sf_qe_path(root, report),
            reused_complete_run=True,
        )

    manifest_path = root / "manifest.json"
    run_state_root = root / "run_state"
    run_state_manifest = run_state_root / "manifest.json"
    if not manifest_path.exists() and not run_state_manifest.exists():
        _reject_uncommitted_shared_attempts(
            shared_ledger,
            logical_run_id=logical_run_id,
            attempt_run_id=attempt_run_id,
        )

    ready_methods = {
        job.method_id for job in plan.jobs if job.status == "ready"
    }
    required_llm_methods = _required_llm_methods(
        common_input,
        plan,
        timestamp=timestamp,
        commit=commit,
    )
    _require_llm_runtime(
        root=root,
        required_llm_methods=required_llm_methods,
        llm_roles=llm_roles,
        shared_ledger=shared_ledger,
        shared_ledger_relative_path=shared_ledger_relative_path,
    )
    state_store: EvaluationRunStateStoreV1 | None = None
    effective_llm_roles = llm_roles
    if required_llm_methods:
        if llm_roles is None:
            raise ContractValidationError(
                "llm_runtime",
                "$.llm_roles",
                "resumable LLM jobs require a sealed Evaluation role runner",
            )
        state_store = EvaluationRunStateStoreV1.open_or_create(
            run_state_root,
            plan=plan,
            semantic_contract=llm_roles.semantic_contract,
            evaluation_logical_run_id=logical_run_id,
            evaluation_attempt_run_id=attempt_run_id,
            evaluation_profile_id=profile_id,
            policy_profile_id=policy_id,
            baseline_arm_id=baseline_arm_id,
            candidate_arm_id=candidate_arm_id,
            created_at=timestamp,
            producer_code_commit=commit,
        )
        effective_llm_roles = ResumableEvaluationRoleRunnerV1(
            llm_roles, state_store
        )
    elif run_state_manifest.exists():
        raise ContractValidationError(
            "run_state",
            str(run_state_manifest),
            "a persisted LLM run state cannot be reused by a plan with no LLM jobs",
        )

    lock = state_store.run_lock() if state_store is not None else nullcontext()
    with lock:
        try:
            local_payload, local_path, prepared = _resolve_local_sf_qe(
                root=root,
                common_input=common_input,
                config=config,
                plan=plan,
                timestamp=timestamp,
                commit=commit,
                runtime=local_sf_qe_runtime if not manifest_path.exists() else None,
                required="sf_qe" in ready_methods,
            )

            if manifest_path.exists():
                if state_store is not None:
                    status = state_store.status()
                    if status["state"] != "completed" and status["last_event_sequence"] > 1:
                        state_store.append_event(
                            "run_resumed", stage_id=status["current_stage_id"]
                        )
                bundle = _load_bound_bundle(root, common_input, config, plan)
                execution = bundle.execution
            else:
                scorer = (
                    prepared
                    if prepared is not None
                    else _unexpected_local_sf_qe_call
                )
                executor = EvaluationMethodExecutorV1(
                    common_input=common_input,
                    config_payload=config,
                    sf_qe_scorer=scorer,
                    llm_roles=effective_llm_roles,  # type: ignore[arg-type]
                    created_at=timestamp,
                    producer_code_commit=commit,
                )
                if state_store is None:
                    execution = execute_evaluation_plan_v1(
                        common_input,
                        config,
                        executor,
                        created_at=timestamp,
                        runner_code_commit=commit,
                        baseline_arm_id=baseline_arm_id,
                        candidate_arm_id=candidate_arm_id,
                    )
                else:
                    execution = execute_evaluation_plan_resumable_v1(
                        common_input,
                        config,
                        executor,
                        state_store,
                        created_at=timestamp,
                        runner_code_commit=commit,
                        baseline_arm_id=baseline_arm_id,
                        candidate_arm_id=candidate_arm_id,
                        finalize_run=False,
                        acquire_run_lock=False,
                    )
                if prepared is not None:
                    prepared.assert_exact_cover()
                bundle = persist_evaluation_execution_bundle_v1(
                    output_root=root,
                    config_payload=config,
                    execution_payload=execution,
                    created_at=timestamp,
                    producer_code_commit=commit,
                )

            if state_store is not None:
                state_store.append_event("stage_started", stage_id="report")
            usage_projection = project_evaluation_usage_v1(
                common_input,
                config,
                execution,
                created_at=timestamp,
                producer_code_commit=commit,
                evaluation_logical_run_id=logical_run_id,
                evaluation_attempt_run_id=attempt_run_id,
                local_sf_qe_evidence=local_payload,
                local_sf_qe_relative_path=(
                    _relative_to_root(root, local_path)
                    if local_path is not None
                    else None
                ),
                shared_ledger=shared_ledger,
                shared_ledger_relative_path=shared_ledger_relative_path,
            )
            persisted_usage = persist_evaluation_usage_artifact_v1(
                output_root=root,
                artifact_payload=usage_projection.artifact,
            )
            report = compose_full_run_report_v1(
                common_input,
                config,
                execution,
                generated_at=timestamp,
                producer_code_commit=commit,
                evaluation_logical_run_id=logical_run_id,
                evaluation_attempt_run_id=attempt_run_id,
                evaluation_profile_id=profile_id,
                policy_profile_id=policy_id,
                input_artifact=input_row,
                arm_presentations=arm_rows,
                method_presentations=method_rows,
                stage_facts=usage_projection.stage_facts,
                usage_payload=usage_projection.usage,
                usage_artifacts=(usage_projection.artifact_descriptor,),
                caveats=caveat_rows,
            )
            persisted_report = persist_full_run_report_v1(
                output_root=root,
                report_payload=report,
            )
            if state_store is not None:
                state_store.append_event("stage_completed", stage_id="report")
                state_store.append_event("run_completed")
            return EndToEndEvaluationResultV1(
                output_root=root,
                report_path=persisted_report.report_path,
                report=persisted_report.report,
                execution_path=bundle.execution_path,
                execution=bundle.execution,
                usage_path=persisted_usage.path,
                local_sf_qe_path=local_path,
                reused_complete_run=False,
            )
        except Exception:
            if state_store is not None:
                status = state_store.status()
                if status["state"] != "halted":
                    state_store.append_event(
                        "run_halted",
                        stage_id=status["current_stage_id"],
                        reason_code="end_to_end_stage_failed",
                    )
            raise


def _preflight_report_inputs(
    *,
    root: Path,
    common_input: CommonEvaluationInputV1,
    config: Mapping[str, Any],
    input_artifact: Mapping[str, Any],
    arm_presentations: Sequence[Mapping[str, Any]],
    method_presentations: Sequence[Mapping[str, Any]],
    caveats: Sequence[str],
) -> tuple[dict[str, str], list[dict[str, str]], list[dict[str, Any]], list[str]]:
    raw_input = require_mapping(input_artifact, path="$.input_artifact")
    require_exact_keys(
        raw_input,
        required={"artifact_id", "relative_path", "sha256"},
        path="$.input_artifact",
    )
    input_row = {
        "artifact_id": require_string(
            raw_input["artifact_id"], path="$.input_artifact.artifact_id"
        ),
        "relative_path": require_relative_path(
            raw_input["relative_path"], path="$.input_artifact.relative_path"
        ),
        "sha256": require_sha256(
            raw_input["sha256"], path="$.input_artifact.sha256"
        ),
    }
    _require_artifact_file(root, input_row["relative_path"])

    arm_rows: list[dict[str, str]] = []
    for index, raw in enumerate(require_list(list(arm_presentations), path="$.arm_presentations")):
        path = f"$.arm_presentations[{index}]"
        row = require_mapping(raw, path=path)
        require_exact_keys(
            row,
            required={"arm_id", "role", "kind", "label", "relative_path"},
            path=path,
        )
        normalized = {
            "arm_id": require_string(row["arm_id"], path=f"{path}.arm_id"),
            "role": require_enum(
                row["role"],
                {"baseline", "candidate", "reference", "external_baseline"},
                path=f"{path}.role",
            ),
            "kind": require_enum(
                row["kind"],
                {"system", "human_reference", "machine_baseline"},
                path=f"{path}.kind",
            ),
            "label": require_string(row["label"], path=f"{path}.label"),
            "relative_path": require_relative_path(
                row["relative_path"], path=f"{path}.relative_path"
            ),
        }
        _require_artifact_file(root, normalized["relative_path"])
        arm_rows.append(normalized)
    require_unique([row["arm_id"] for row in arm_rows], path="$.arm_presentations")
    if {row["arm_id"] for row in arm_rows} != {arm.arm_id for arm in common_input.arms}:
        raise ContractValidationError(
            "arm_exact_cover",
            "$.arm_presentations",
            "arm presentations must exact-cover common input arms",
        )
    require_unique(
        [input_row["artifact_id"], *[arm.artifact_id for arm in common_input.arms]],
        path="$.artifacts.artifact_id",
    )
    require_unique(
        [input_row["relative_path"], *[row["relative_path"] for row in arm_rows]],
        path="$.artifacts.relative_path",
    )

    method_rows: list[dict[str, Any]] = []
    for index, raw in enumerate(
        require_list(list(method_presentations), path="$.method_presentations")
    ):
        path = f"$.method_presentations[{index}]"
        row = require_mapping(raw, path=path)
        require_exact_keys(row, required={"display_name", "method"}, path=path)
        method_rows.append(
            {
                "display_name": require_string(
                    row["display_name"], path=f"{path}.display_name"
                ),
                "method": validate_method(row["method"], path=f"{path}.method"),
            }
        )
    require_unique(
        [row["method"]["method_id"] for row in method_rows],
        path="$.method_presentations",
    )
    expected_methods = {
        row["method_id"]: row["method_version"] for row in config["methods"]
    }
    supplied_methods = {
        row["method"]["method_id"]: row["method"]["method_version"]
        for row in method_rows
    }
    if supplied_methods != expected_methods:
        raise ContractValidationError(
            "method_exact_cover",
            "$.method_presentations",
            "method presentations must exact-cover sealed config methods and versions",
        )
    caveat_rows = [
        require_string(value, path=f"$.caveats[{index}]")
        for index, value in enumerate(caveats)
    ]
    return input_row, arm_rows, method_rows, caveat_rows


def _resolve_local_sf_qe(
    *,
    root: Path,
    common_input: CommonEvaluationInputV1,
    config: Mapping[str, Any],
    plan: Any,
    timestamp: str,
    commit: str,
    runtime: LocalSfQeRuntimeV1 | None,
    required: bool,
) -> tuple[dict[str, Any] | None, Path | None, LocalSfQePreparedV1 | None]:
    if not required:
        return None, None, None
    matches: list[tuple[dict[str, Any], Path]] = []
    directory = root / "local_sf_qe"
    if directory.exists():
        for path in sorted(directory.glob("*.json")):
            evidence = load_local_sf_qe_evidence_v1(path)
            binding = evidence["binding"]
            if (
                binding["project_id"] == plan.project_id
                and binding["document_id"] == plan.document_id
                and binding["config_sha256"] == plan.config_sha256
                and binding["input_set_sha256"] == plan.input_set_sha256
                and binding["plan_sha256"] == plan.plan_sha256
            ):
                matches.append((evidence, path.resolve()))
    if len(matches) > 1:
        raise ContractValidationError(
            "local_sf_qe_ambiguous",
            str(directory),
            "multiple local SF-QE artifacts match the same sealed plan",
        )
    if matches:
        evidence, path = matches[0]
        return evidence, path, LocalSfQePreparedV1(evidence)
    if runtime is None:
        raise ContractValidationError(
            "local_sf_qe_runtime",
            "$.local_sf_qe_runtime",
            "ready SF-QE jobs require a predictor or one persisted matching artifact",
        )
    prepared = prepare_local_sf_qe_v1(
        common_input,
        config,
        runtime.predictor,
        created_at=timestamp,
        producer_code_commit=commit,
        checkpoint_sha256=runtime.checkpoint_sha256,
        package_name=runtime.package_name,
        package_version=runtime.package_version,
        device=runtime.device,
        batch_size=runtime.batch_size,
        clock=runtime.clock,
        monotonic=runtime.monotonic,
    )
    persisted = persist_local_sf_qe_evidence_v1(
        output_root=root, evidence_payload=prepared.evidence
    )
    return persisted.evidence, persisted.path, prepared


def _require_llm_runtime(
    *,
    root: Path,
    required_llm_methods: set[str],
    llm_roles: SharedEvaluationRoleRunnerV1 | None,
    shared_ledger: SharedLlmAttemptLedger | None,
    shared_ledger_relative_path: str | None,
) -> None:
    if shared_ledger is not None:
        if shared_ledger_relative_path is None:
            raise ContractValidationError(
                "llm_usage_path",
                "$.shared_ledger_relative_path",
                "shared attempt ledger needs one run-relative persisted path",
            )
        relative = require_relative_path(
            shared_ledger_relative_path, path="$.shared_ledger_relative_path"
        )
        expected = _contained_path(root, relative)
        if shared_ledger.path != expected or not expected.is_file():
            raise ContractValidationError(
                "llm_usage_path",
                "$.shared_ledger_relative_path",
                "declared ledger path must identify the supplied physical ledger",
            )
    elif shared_ledger_relative_path is not None and required_llm_methods:
        raise ContractValidationError(
            "llm_runtime",
            "$.shared_ledger",
            "a ledger path without its ledger cannot support ready LLM jobs",
        )
    if not required_llm_methods:
        return
    if llm_roles is None or shared_ledger is None:
        raise ContractValidationError(
            "llm_runtime",
            "$.llm_roles",
            "ready SF-BT/PJ jobs require one sealed role runner and its attempt ledger",
        )


def _required_llm_methods(
    common_input: CommonEvaluationInputV1,
    plan: Any,
    *,
    timestamp: str,
    commit: str,
) -> set[str]:
    required = {
        job.method_id
        for job in plan.jobs
        if job.status == "ready" and job.method_id == "sf_bt"
    }
    for job in plan.jobs:
        if job.status != "ready" or job.method_id != "pj":
            continue
        packet = build_scorer_input_packet(
            common_input,
            plan,
            job.job_id,
            created_at=timestamp,
            producer_code_commit=commit,
        )
        if not prepare_pj_prompt_presentations_v3(packet).mechanical_equal:
            required.add("pj")
            break
    return required


def _reject_uncommitted_shared_attempts(
    ledger: SharedLlmAttemptLedger | None,
    *,
    logical_run_id: str,
    attempt_run_id: str,
) -> None:
    if ledger is None:
        return
    matches = [
        row
        for row in ledger.list_records("seal")
        if row["workstream"] == "evaluation"
        and row["run_id"] == logical_run_id
        and row["attempt_run_id"] == attempt_run_id
    ]
    if matches:
        raise ContractValidationError(
            "uncommitted_shared_attempts",
            "$.shared_ledger",
            "attempt ledger has Evaluation calls but no committed execution; "
            "use a new attempt or an explicit recovery procedure",
        )


def _load_bound_bundle(
    root: Path,
    common_input: CommonEvaluationInputV1,
    config: Mapping[str, Any],
    plan: Any,
) -> EvaluationExecutionBundleV1:
    bundle = load_evaluation_execution_bundle_v1(output_root=root)
    if bundle.config != config:
        raise ContractValidationError(
            "resume_config",
            str(bundle.config_path),
            "persisted config differs from the requested sealed config",
        )
    validate_evaluation_execution_binding(bundle.execution, common_input, plan)
    return bundle


def _validate_completed_report_request(
    report: Mapping[str, Any],
    *,
    common_input: CommonEvaluationInputV1,
    config: Mapping[str, Any],
    input_artifact: Mapping[str, str],
    arm_presentations: Sequence[Mapping[str, str]],
    method_presentations: Sequence[Mapping[str, Any]],
    evaluation_logical_run_id: str,
    evaluation_attempt_run_id: str,
    evaluation_profile_id: str,
    policy_profile_id: str | None,
    caveats: Sequence[str],
) -> None:
    identity = report["identity"]
    expected_attempts = _ordered_unique(
        [arm.attempt_run_id for arm in common_input.arms]
        + [evaluation_attempt_run_id]
    )
    if (
        identity["project_id"] != common_input.project_id
        or identity["document_id"] != common_input.document_id
        or identity["logical_run_id"] != evaluation_logical_run_id
        or identity["profile_id"] != evaluation_profile_id
        or identity["attempt_run_ids"] != expected_attempts
        or identity["input_manifest_sha256"] != input_artifact["sha256"]
        or report["report_method"]["policy_profile_id"] != policy_profile_id
        or report["integrity"]["evaluation_config_sha256"]
        != config["integrity"]["config_sha256"]
    ):
        raise ContractValidationError(
            "completed_run_binding",
            "$.identity",
            "completed report belongs to another requested run or profile",
        )
    input_matches = [
        row
        for row in report["artifacts"]
        if row["artifact_id"] == input_artifact["artifact_id"]
        and row["relative_path"] == input_artifact["relative_path"]
        and row["sha256"] == input_artifact["sha256"]
        and row["kind"] == "evaluation_input"
    ]
    if len(input_matches) != 1:
        raise ContractValidationError(
            "completed_input_artifact",
            "$.artifacts",
            "completed report references another input artifact",
        )
    presentations = {row["arm_id"]: row for row in arm_presentations}
    report_arms = {row["arm_id"]: row for row in report["arms"]}
    for arm in common_input.arms:
        presentation = presentations[arm.arm_id]
        expected = {
            "arm_id": arm.arm_id,
            "role": presentation["role"],
            "kind": presentation["kind"],
            "label": presentation["label"],
            "translation_artifact_id": arm.artifact_id,
            "translation_sha256": arm.artifact_sha256,
        }
        if report_arms.get(arm.arm_id) != expected:
            raise ContractValidationError(
                "completed_arm_binding",
                "$.arms",
                "completed report uses another arm presentation",
            )
        artifact_matches = [
            row
            for row in report["artifacts"]
            if row["artifact_id"] == arm.artifact_id
            and row["relative_path"] == presentation["relative_path"]
            and row["sha256"] == arm.artifact_sha256
        ]
        if len(artifact_matches) != 1:
            raise ContractValidationError(
                "completed_arm_artifact",
                "$.artifacts",
                "completed report references another translation artifact path",
            )
    expected_methods = {
        row["method"]["method_id"]: {
            "display_name": row["display_name"],
            "method": row["method"],
        }
        for row in method_presentations
    }
    for metric in report["metrics"]:
        method_id = metric["method"]["method_id"]
        expected = expected_methods.get(method_id)
        if expected is None or metric["display_name"] != expected["display_name"] or metric[
            "method"
        ] != expected["method"]:
            raise ContractValidationError(
                "completed_method_binding",
                "$.metrics",
                "completed report uses another method presentation",
            )
    if not set(caveats).issubset(set(report["caveats"])):
        raise ContractValidationError(
            "completed_caveats",
            "$.caveats",
            "completed report omits a requested caveat",
        )


def _validate_comparison_roles(
    selected_arm_ids: Sequence[str],
    *,
    baseline_arm_id: str | None,
    candidate_arm_id: str | None,
) -> None:
    selected = set(selected_arm_ids)
    for path, value in (
        ("$.baseline_arm_id", baseline_arm_id),
        ("$.candidate_arm_id", candidate_arm_id),
    ):
        if value is not None and value not in selected:
            raise ContractValidationError(
                "comparison_arm", path, "comparison role references a foreign arm"
            )
    if (baseline_arm_id is None) != (candidate_arm_id is None):
        raise ContractValidationError(
            "comparison_pair",
            "$",
            "baseline and candidate must be supplied together",
        )
    if baseline_arm_id is not None and baseline_arm_id == candidate_arm_id:
        raise ContractValidationError(
            "comparison_pair", "$", "baseline and candidate must differ"
        )


def _prepare_root(path: Path) -> Path:
    root = Path(path)
    root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir():
        raise ContractValidationError(
            "output_root", str(root), "evaluation output root is not a directory"
        )
    return root.resolve()


def _require_artifact_file(root: Path, relative_path: str) -> Path:
    path = _contained_path(root, relative_path)
    if not path.is_file():
        raise ContractValidationError(
            "missing_artifact",
            str(path),
            "required input or translation artifact is absent before scoring",
        )
    return path


def _contained_path(root: Path, relative_path: str) -> Path:
    normalized = require_relative_path(relative_path, path="$.relative_path")
    path = (root / Path(*normalized.split("/"))).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ContractValidationError(
            "path_escape", str(path), "artifact path escapes Evaluation run root"
        ) from exc
    return path


def _relative_to_root(root: Path, path: Path) -> str:
    try:
        relative = path.resolve().relative_to(root)
    except ValueError as exc:
        raise ContractValidationError(
            "artifact_path", str(path), "artifact is outside Evaluation run root"
        ) from exc
    return relative.as_posix()


def _report_usage_path(root: Path, report: Mapping[str, Any]) -> Path | None:
    rows = [row for row in report["artifacts"] if row["kind"] == "usage_ledger"]
    if not rows:
        return None
    if len(rows) != 1:
        raise ContractValidationError(
            "usage_artifact", "$.artifacts", "runner expects one usage projection"
        )
    return _contained_path(root, rows[0]["relative_path"])


def _find_report_local_sf_qe_path(
    root: Path, report: Mapping[str, Any]
) -> Path | None:
    if not any(row["method"]["method_id"] == "sf_qe" for row in report["metrics"]):
        return None
    directory = root / "local_sf_qe"
    rows = sorted(directory.glob("*.json")) if directory.exists() else []
    return rows[0].resolve() if len(rows) == 1 else None


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractValidationError(
            "artifact_json", str(path), "artifact is not readable finite UTF-8 JSON"
        ) from exc
    if not isinstance(value, dict):
        raise ContractValidationError(
            "artifact_json", str(path), "artifact root must be an object"
        )
    return value


def _unexpected_local_sf_qe_call(_source_text: str, _target_text: str) -> float:
    raise ContractValidationError(
        "sf_qe_runtime", "$", "execution requested an unprovisioned local SF-QE call"
    )


def _ordered_unique(values: Sequence[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result
