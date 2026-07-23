from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from pipeline.eval.benchmark_runner_v1 import (
    BenchmarkChapterRuntimeV1,
    BenchmarkEndToEndResultV1,
    run_benchmark_end_to_end_v1,
)
from pipeline.eval.contracts_v1 import (
    ContractValidationError,
    require_commit,
    require_exact_keys,
    require_list,
    require_mapping,
    require_nullable_string,
    require_rfc3339,
    require_sha256,
    require_string,
)
from pipeline.eval.evaluation_workflow_settings_v1 import (
    EvaluationWorkflowSettingsAuthorityV1,
    build_evaluation_workflow_settings_v1,
    validate_evaluation_workflow_settings_v1,
)
from pipeline.eval.workflow_component_v1 import validate_scoring_handoff_v1
from pipeline.eval.workflow_component_writer_v1 import (
    EvaluationWorkflowRunContextV1,
    validate_evaluation_workflow_component_package_v1,
)
from pipeline.workflow_replay.contracts_v1 import canonical_sha256
from pipeline.workflow_replay.orchestrator_v1 import (
    SnapshotObserverV1,
    WorkflowComponentPausedV1,
)


__all__ = [
    "EvaluationBenchmarkInputProviderV1",
    "EvaluationExecutorErrorV1",
    "EvaluationWorkflowExecutorRegistrationV1",
    "PreparedEvaluationBenchmarkV1",
    "RegisteredEvaluationWorkflowExecutorV1",
    "materialize_registered_evaluation_settings_v1",
    "validate_locked_evaluation_selection_v1",
]


_SCORING_HANDOFF_REF = "handoffs/scoring_handoff.json"
_SELECTION_KEYS = {
    "settings_option_id",
    "selected_chapter_ids",
    "selected_arm_ids",
    "selected_scorer_ids",
    "highlight_pair",
    "registered_option_sha256",
    "selection_sha256",
}


class EvaluationExecutorErrorV1(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class PreparedEvaluationBenchmarkV1:
    """Registered, fully materialized inputs for the existing benchmark runner."""

    accepted_scoring_handoff: Mapping[str, Any]
    accepted_workflow_settings: Mapping[str, Any]
    benchmark_manifest: Mapping[str, Any]
    benchmark_preflight: Mapping[str, Any]
    benchmark_overlays: Sequence[Mapping[str, Any]]
    chapter_runtimes: Mapping[str, BenchmarkChapterRuntimeV1]


class EvaluationBenchmarkInputProviderV1(Protocol):
    """Resolve typed handoff bindings without directory scans or semantic inference."""

    def prepare(
        self,
        *,
        scoring_handoff: Mapping[str, Any],
        workflow_settings: Mapping[str, Any],
        output_root: Path,
    ) -> PreparedEvaluationBenchmarkV1: ...


@dataclass(frozen=True, slots=True)
class EvaluationWorkflowExecutorRegistrationV1:
    """Server-owned facts sealed before the neutral orchestrator invokes Evaluation."""

    workflow_run_id: str
    component_run_id: str
    output_root: Path
    generated_at: str
    producer_code_commit: str
    evaluation_logical_run_id: str
    evaluation_attempt_run_id: str
    evaluation_profile_id: str
    evaluation_profile_ref: str
    policy_profile_id: str | None
    policy_profile_ref: str | None
    shared_selection_ref: str
    settings_option_id: str
    registered_option_sha256: str
    locked_selection: Mapping[str, Any]
    settings_authority: EvaluationWorkflowSettingsAuthorityV1
    materialized_workflow_settings: Mapping[str, Any]


class RegisteredEvaluationWorkflowExecutorV1:
    """Thin production adapter from ScoringHandoffV1 to the benchmark runner."""

    def __init__(
        self,
        registration: EvaluationWorkflowExecutorRegistrationV1,
        input_provider: EvaluationBenchmarkInputProviderV1,
        *,
        runner: Callable[..., BenchmarkEndToEndResultV1] = run_benchmark_end_to_end_v1,
    ) -> None:
        if not isinstance(registration, EvaluationWorkflowExecutorRegistrationV1):
            raise TypeError(
                "registration must be EvaluationWorkflowExecutorRegistrationV1"
            )
        self._registration = registration
        self._input_provider = input_provider
        self._runner = runner
        self._workflow_run_id = require_string(
            registration.workflow_run_id, path="$.registration.workflow_run_id"
        )
        self._component_run_id = require_string(
            registration.component_run_id, path="$.registration.component_run_id"
        )
        self._output_root = Path(registration.output_root).resolve()
        self._generated_at = require_rfc3339(
            registration.generated_at, path="$.registration.generated_at"
        )
        self._producer_code_commit = require_commit(
            registration.producer_code_commit,
            path="$.registration.producer_code_commit",
        )
        self._evaluation_logical_run_id = require_string(
            registration.evaluation_logical_run_id,
            path="$.registration.evaluation_logical_run_id",
        )
        self._evaluation_attempt_run_id = require_string(
            registration.evaluation_attempt_run_id,
            path="$.registration.evaluation_attempt_run_id",
        )
        self._evaluation_profile_id = require_string(
            registration.evaluation_profile_id,
            path="$.registration.evaluation_profile_id",
        )
        self._evaluation_profile_ref = require_string(
            registration.evaluation_profile_ref,
            path="$.registration.evaluation_profile_ref",
        )
        self._policy_profile_id = require_nullable_string(
            registration.policy_profile_id,
            path="$.registration.policy_profile_id",
        )
        self._policy_profile_ref = require_nullable_string(
            registration.policy_profile_ref,
            path="$.registration.policy_profile_ref",
        )
        if (self._policy_profile_id is None) != (
            self._policy_profile_ref is None
        ):
            raise ContractValidationError(
                "policy_profile_binding",
                "$.registration",
                "policy profile id and artifact reference must be both set or both null",
            )
        self._shared_selection_ref = require_string(
            registration.shared_selection_ref,
            path="$.registration.shared_selection_ref",
        )
        self._settings_option_id = require_string(
            registration.settings_option_id,
            path="$.registration.settings_option_id",
        )
        self._registered_option_sha256 = require_sha256(
            registration.registered_option_sha256,
            path="$.registration.registered_option_sha256",
        )
        self._locked_selection = validate_locked_evaluation_selection_v1(
            registration.locked_selection,
            expected_settings_option_id=self._settings_option_id,
            expected_registered_option_sha256=self._registered_option_sha256,
        )
        self._materialized_workflow_settings = copy.deepcopy(
            registration.materialized_workflow_settings
        )

    def materialize_settings(
        self, scoring_handoff: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Return the deterministic settings expected for this sealed registration."""

        return materialize_registered_evaluation_settings_v1(
            scoring_handoff=scoring_handoff,
            settings_authority=self._registration.settings_authority,
            locked_selection=self._registration.locked_selection,
            settings_option_id=self._registration.settings_option_id,
            registered_option_sha256=self._registration.registered_option_sha256,
            evaluation_profile_ref=self._registration.evaluation_profile_ref,
            policy_profile_ref=self._registration.policy_profile_ref,
            shared_selection_ref=self._registration.shared_selection_ref,
        )

    def execute(
        self,
        scoring_handoff: Mapping[str, Any],
        observer: SnapshotObserverV1,
    ) -> Path:
        handoff = validate_scoring_handoff_v1(copy.deepcopy(scoring_handoff))
        if handoff["workflow_run_id"] != self._workflow_run_id:
            raise ContractValidationError(
                "workflow_binding",
                "$.scoring_handoff.workflow_run_id",
                "registered Evaluation executor belongs to another workflow",
            )
        settings = validate_evaluation_workflow_settings_v1(
            copy.deepcopy(self._materialized_workflow_settings),
            authority=self._registration.settings_authority,
            scoring_handoff=handoff,
        )
        expected_settings = self.materialize_settings(handoff)
        if settings != expected_settings:
            raise ContractValidationError(
                "settings_materialization",
                "$.registration.materialized_workflow_settings",
                "registered settings differ from the deterministic locked selection",
            )
        prepared = self._input_provider.prepare(
            scoring_handoff=copy.deepcopy(handoff),
            workflow_settings=copy.deepcopy(settings),
            output_root=self._output_root,
        )
        if not isinstance(prepared, PreparedEvaluationBenchmarkV1):
            raise TypeError(
                "input provider must return PreparedEvaluationBenchmarkV1"
            )
        accepted_handoff = validate_scoring_handoff_v1(
            copy.deepcopy(prepared.accepted_scoring_handoff)
        )
        if accepted_handoff != handoff:
            raise ContractValidationError(
                "handoff_echo",
                "$.prepared.accepted_scoring_handoff",
                "input provider must echo the exact accepted scoring handoff",
            )
        accepted_settings = validate_evaluation_workflow_settings_v1(
            copy.deepcopy(prepared.accepted_workflow_settings),
            authority=self._registration.settings_authority,
            scoring_handoff=handoff,
        )
        if accepted_settings != settings:
            raise ContractValidationError(
                "settings_echo",
                "$.prepared.accepted_workflow_settings",
                "input provider must echo the exact materialized workflow settings",
            )
        context = EvaluationWorkflowRunContextV1(
            workflow_run_id=self._workflow_run_id,
            component_run_id=self._component_run_id,
            scoring_handoff=handoff,
            scoring_handoff_artifact_ref=_SCORING_HANDOFF_REF,
            evaluation_profile=settings["evaluation_profile_ref"],
            workflow_settings=settings,
            workflow_settings_authority=self._registration.settings_authority,
        )
        baseline_arm_id, candidate_arm_id = _runner_highlight_pair(
            settings["highlight_pair"]
        )
        try:
            result = self._runner(
                copy.deepcopy(prepared.benchmark_manifest),
                copy.deepcopy(prepared.benchmark_preflight),
                copy.deepcopy(list(prepared.benchmark_overlays)),
                dict(prepared.chapter_runtimes),
                self._output_root,
                generated_at=self._generated_at,
                producer_code_commit=self._producer_code_commit,
                evaluation_logical_run_id=self._evaluation_logical_run_id,
                evaluation_attempt_run_id=self._evaluation_attempt_run_id,
                evaluation_profile_id=self._evaluation_profile_id,
                policy_profile_id=self._policy_profile_id,
                baseline_arm_id=baseline_arm_id,
                candidate_arm_id=candidate_arm_id,
                workflow_context=context,
            )
        except Exception:
            self._publish_non_success_snapshot(observer, handoff)
            raise
        if result.output_root.resolve() != self._output_root:
            raise EvaluationExecutorErrorV1(
                "evaluation_output_root",
                "benchmark runner returned a foreign output root",
            )
        package = validate_evaluation_workflow_component_package_v1(
            self._output_root,
            handoff,
            require_terminal=True,
        )
        if package["events"][-1]["event"] != "component_done" or result.report is None:
            observer(self._output_root, True)
            raise EvaluationExecutorErrorV1(
                "evaluation_terminal_without_report",
                "Evaluation terminated without an accepted benchmark report",
            )
        return self._output_root

    def _publish_non_success_snapshot(
        self,
        observer: SnapshotObserverV1,
        handoff: Mapping[str, Any],
    ) -> None:
        manifest_path = self._output_root / "component_manifest.json"
        if not manifest_path.is_file():
            return
        try:
            package = validate_evaluation_workflow_component_package_v1(
                self._output_root,
                handoff,
            )
        except ContractValidationError:
            return
        terminal_event = package["events"][-1]["event"]
        if terminal_event == "component_halted":
            observer(self._output_root, False)
            raise WorkflowComponentPausedV1("evaluation")
        if terminal_event == "component_failed":
            observer(self._output_root, True)


def materialize_registered_evaluation_settings_v1(
    *,
    scoring_handoff: Mapping[str, Any],
    settings_authority: EvaluationWorkflowSettingsAuthorityV1,
    locked_selection: Mapping[str, Any],
    settings_option_id: str,
    registered_option_sha256: str,
    evaluation_profile_ref: str,
    policy_profile_ref: str | None,
    shared_selection_ref: str,
) -> dict[str, Any]:
    """Purely materialize Settings 1.1 from a sealed handoff and registration."""

    if not isinstance(settings_authority, EvaluationWorkflowSettingsAuthorityV1):
        raise TypeError(
            "settings_authority must be EvaluationWorkflowSettingsAuthorityV1"
        )
    locked = validate_locked_evaluation_selection_v1(
        locked_selection,
        expected_settings_option_id=settings_option_id,
        expected_registered_option_sha256=registered_option_sha256,
    )
    return build_evaluation_workflow_settings_v1(
        authority=settings_authority,
        scoring_handoff=copy.deepcopy(scoring_handoff),
        evaluation_profile_ref=evaluation_profile_ref,
        policy_profile_ref=policy_profile_ref,
        shared_selection_ref=shared_selection_ref,
        highlight_pair=locked["highlight_pair"],
        selected_chapter_ids=locked["selected_chapter_ids"],
        selected_arm_ids=locked["selected_arm_ids"],
        selected_scorer_ids=locked["selected_scorer_ids"],
    )


def validate_locked_evaluation_selection_v1(
    value: Mapping[str, Any],
    *,
    expected_settings_option_id: str,
    expected_registered_option_sha256: str,
) -> dict[str, Any]:
    row = require_mapping(value, path="$.locked_selection")
    require_exact_keys(row, required=_SELECTION_KEYS, path="$.locked_selection")
    option_id = require_string(
        row["settings_option_id"],
        path="$.locked_selection.settings_option_id",
    )
    option_sha = require_sha256(
        row["registered_option_sha256"],
        path="$.locked_selection.registered_option_sha256",
    )
    if option_id != require_string(
        expected_settings_option_id, path="$.expected_settings_option_id"
    ):
        raise ContractValidationError(
            "settings_option_binding",
            "$.locked_selection.settings_option_id",
            "locked selection names another settings option",
        )
    if option_sha != require_sha256(
        expected_registered_option_sha256,
        path="$.expected_registered_option_sha256",
    ):
        raise ContractValidationError(
            "settings_option_binding",
            "$.locked_selection.registered_option_sha256",
            "locked selection names another registered option revision",
        )
    chapters = _string_list(
        row["selected_chapter_ids"], path="$.locked_selection.selected_chapter_ids"
    )
    arms = _string_list(
        row["selected_arm_ids"], path="$.locked_selection.selected_arm_ids"
    )
    scorers = _string_list(
        row["selected_scorer_ids"], path="$.locked_selection.selected_scorer_ids"
    )
    highlight = copy.deepcopy(row["highlight_pair"])
    if highlight is not None:
        highlight_row = require_mapping(
            highlight, path="$.locked_selection.highlight_pair"
        )
        require_exact_keys(
            highlight_row,
            required={"baseline_arm_id", "candidate_arm_id"},
            path="$.locked_selection.highlight_pair",
        )
        highlight = {
            "baseline_arm_id": require_string(
                highlight_row["baseline_arm_id"],
                path="$.locked_selection.highlight_pair.baseline_arm_id",
            ),
            "candidate_arm_id": require_string(
                highlight_row["candidate_arm_id"],
                path="$.locked_selection.highlight_pair.candidate_arm_id",
            ),
        }
    basis = {
        "settings_option_id": option_id,
        "selected_chapter_ids": chapters,
        "selected_arm_ids": arms,
        "selected_scorer_ids": scorers,
        "highlight_pair": highlight,
        "registered_option_sha256": option_sha,
    }
    selection_sha = require_sha256(
        row["selection_sha256"], path="$.locked_selection.selection_sha256"
    )
    if canonical_sha256(basis) != selection_sha:
        raise ContractValidationError(
            "selection_hash",
            "$.locked_selection.selection_sha256",
            "locked Evaluation selection hash drift",
        )
    return {**basis, "selection_sha256": selection_sha}


def _string_list(value: Any, *, path: str) -> list[str]:
    rows = require_list(value, path=path)
    result = [
        require_string(item, path=f"{path}[{index}]")
        for index, item in enumerate(rows)
    ]
    if not result or len(set(result)) != len(result):
        raise ContractValidationError(
            "selection_values", path, "selection must be non-empty and unique"
        )
    return result


def _runner_highlight_pair(
    value: Mapping[str, Any] | None,
) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    arm_map = {"s0": "S0", "s1": "S1"}

    def convert(arm_id: str) -> str:
        return arm_map.get(arm_id, arm_id)

    return (
        convert(value["baseline_arm_id"]),
        convert(value["candidate_arm_id"]),
    )
