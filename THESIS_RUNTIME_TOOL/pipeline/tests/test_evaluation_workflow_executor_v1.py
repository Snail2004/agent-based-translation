from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path

import pytest

from pipeline.eval.contracts_v1 import (
    CanonicalPolicy,
    ContractValidationError,
    seal_payload,
)
from pipeline.eval.evaluation_workflow_settings_v1 import (
    EvaluationWorkflowSettingsAuthorityV1,
)
from pipeline.eval.workflow_component_writer_v1 import (
    validate_evaluation_workflow_component_package_v1,
)
from pipeline.eval.workflow_executor_v1 import (
    EvaluationWorkflowExecutorRegistrationV1,
    PreparedEvaluationBenchmarkV1,
    RegisteredEvaluationWorkflowExecutorV1,
    materialize_registered_evaluation_settings_v1,
)
from pipeline.workflow_replay.contracts_v1 import canonical_sha256
from pipeline.workflow_replay.orchestrator_v1 import WorkflowComponentPausedV1
from pipeline.tests.test_evaluation_benchmark_runner_v1 import (
    COMMIT,
    NOW,
    _Predictor,
    _manifest_and_preflight,
    _runtimes,
    _sources,
)
from pipeline.tests.test_evaluation_workflow_component_v1 import (
    _binding,
    _handoff,
)


class _Provider:
    def __init__(self, root: Path, predictor_sets: list[list[_Predictor]]) -> None:
        self.root = root
        self.predictor_sets = predictor_sets
        self.calls = 0

    def prepare(self, *, scoring_handoff, workflow_settings, output_root):
        assert output_root == self.root.resolve()
        sources = _sources()
        manifest, preflight, overlays = _manifest_and_preflight(sources)
        predictors = self.predictor_sets[min(self.calls, len(self.predictor_sets) - 1)]
        self.calls += 1
        return PreparedEvaluationBenchmarkV1(
            accepted_scoring_handoff=copy.deepcopy(scoring_handoff),
            accepted_workflow_settings=copy.deepcopy(workflow_settings),
            benchmark_manifest=manifest,
            benchmark_preflight=preflight,
            benchmark_overlays=overlays,
            chapter_runtimes=_runtimes(output_root, sources, predictors),
        )


class _ForeignEchoProvider(_Provider):
    def prepare(self, *, scoring_handoff, workflow_settings, output_root):
        prepared = super().prepare(
            scoring_handoff=scoring_handoff,
            workflow_settings=workflow_settings,
            output_root=output_root,
        )
        foreign = _reseal_handoff(
            prepared.accepted_scoring_handoff,
            handoff_id="foreign_handoff",
        )
        return replace(prepared, accepted_scoring_handoff=foreign)


def _authority() -> EvaluationWorkflowSettingsAuthorityV1:
    profile = _binding(
        "profiles/evaluation_fixture_v1.json", "evaluation_profile_v1"
    )
    return EvaluationWorkflowSettingsAuthorityV1(
        benchmark_preset=_binding(
            "presets/narrow_five_chapter_d2l_v1.json",
            "evaluation_benchmark_preset_v1",
        ),
        evaluation_config=_binding(
            "configs/evaluation_config_v1.json", "evaluation_run_config_v1"
        ),
        scorer_set=_binding(
            "scorers/sf_qe_sf_bt_pj_v1.json", "evaluation_scorer_set_v1"
        ),
        evaluation_profiles=(profile,),
        policy_profiles=(),
        shared_selections=(
            _binding(
                "selections/evaluation_five_chapter_v1.json",
                "evaluation_shared_selection_v1",
            ),
        ),
    )


def _selection(*, selection_sha256: str | None = None) -> dict[str, object]:
    basis: dict[str, object] = {
        "settings_option_id": "evaluation_workflow_settings_v1",
        "selected_chapter_ids": [
            "d2l_preliminaries",
            "d2l_linear_networks",
            "d2l_multilayer_perceptrons",
            "d2l_deep_learning_computation",
            "d2l_convolutional_neural_networks",
        ],
        "selected_arm_ids": [
            "s0",
            "s1",
            "community",
            "google_nmt",
            "llm_lc",
        ],
        "selected_scorer_ids": ["sf_qe"],
        "highlight_pair": {
            "baseline_arm_id": "s0",
            "candidate_arm_id": "s1",
        },
        "registered_option_sha256": "9" * 64,
    }
    return {
        **basis,
        "selection_sha256": (
            canonical_sha256(basis)
            if selection_sha256 is None
            else selection_sha256
        ),
    }


def _registration(
    root: Path,
    *,
    selection=None,
    materialized_settings=None,
    producer_code_commit: str = COMMIT,
):
    locked_selection = _selection() if selection is None else selection
    if materialized_settings is None:
        materialized_settings = materialize_registered_evaluation_settings_v1(
            scoring_handoff=_handoff(),
            settings_authority=_authority(),
            locked_selection=(
                _selection()
                if selection is not None
                and selection.get("selection_sha256") == "f" * 64
                else locked_selection
            ),
            settings_option_id="evaluation_workflow_settings_v1",
            registered_option_sha256="9" * 64,
            evaluation_profile_ref="profiles/evaluation_fixture_v1.json",
            policy_profile_ref=None,
            shared_selection_ref="selections/evaluation_five_chapter_v1.json",
        )
    return EvaluationWorkflowExecutorRegistrationV1(
        workflow_run_id=_handoff()["workflow_run_id"],
        component_run_id="evalcomp_production_fixture_001",
        output_root=root,
        generated_at=NOW,
        producer_code_commit=producer_code_commit,
        evaluation_logical_run_id="evaluation_production_fixture",
        evaluation_attempt_run_id="evaluation_production_attempt_fixture",
        evaluation_profile_id="evaluation_fixture_v1",
        evaluation_profile_ref="profiles/evaluation_fixture_v1.json",
        policy_profile_id=None,
        policy_profile_ref=None,
        shared_selection_ref="selections/evaluation_five_chapter_v1.json",
        settings_option_id="evaluation_workflow_settings_v1",
        registered_option_sha256="9" * 64,
        locked_selection=locked_selection,
        settings_authority=_authority(),
        materialized_workflow_settings=materialized_settings,
    )


def _reseal_handoff(
    handoff,
    *,
    workflow_run_id: str | None = None,
    handoff_id: str | None = None,
):
    draft = copy.deepcopy(handoff)
    if workflow_run_id is not None:
        draft["workflow_run_id"] = workflow_run_id
    if handoff_id is not None:
        draft["handoff_id"] = handoff_id
    return seal_payload(
        draft,
        policy=CanonicalPolicy(
            set_like_paths=frozenset(),
            semantic_sequence_paths=frozenset(
                {("source_package_bindings",), ("translation_inputs",)}
            ),
        ),
        hash_path=("integrity", "handoff_sha256"),
    )


def test_executor_materializes_settings_and_completes_component(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evaluation"
    provider = _Provider(
        root,
        [[_Predictor(0.5) for _ in range(5)]],
    )
    observed = []
    executor = RegisteredEvaluationWorkflowExecutorV1(
        _registration(root), provider
    )

    result_root = executor.execute(
        _handoff(), lambda path, terminal: observed.append((path, terminal))
    )

    assert result_root == root.resolve()
    assert observed == []
    package = validate_evaluation_workflow_component_package_v1(
        root, _handoff(), require_terminal=True
    )
    assert package["events"][-1]["event"] == "component_done"
    assert package["workflow_settings"]["schema_version"] == "1.1.0"
    assert package["workflow_settings"]["selected_scorer_ids"] == ["sf_qe"]
    assert package["receipt"]["status"] == "accepted"
    assert (root / "reports" / "benchmark_run_report_v1.json").is_file()


def test_executor_rejects_selection_hash_drift_before_provider(
    tmp_path: Path,
) -> None:
    provider = _Provider(
        tmp_path / "evaluation",
        [[_Predictor(0.5) for _ in range(5)]],
    )
    with pytest.raises(ContractValidationError, match="selection hash drift"):
        RegisteredEvaluationWorkflowExecutorV1(
            _registration(
                tmp_path / "evaluation",
                selection=_selection(selection_sha256="f" * 64),
            ),
            provider,
        )
    assert provider.calls == 0


def test_executor_rejects_foreign_provider_handoff_echo(tmp_path: Path) -> None:
    root = tmp_path / "evaluation"
    provider = _ForeignEchoProvider(
        root,
        [[_Predictor(0.5) for _ in range(5)]],
    )
    executor = RegisteredEvaluationWorkflowExecutorV1(
        _registration(root), provider
    )

    with pytest.raises(ContractValidationError, match="exact accepted scoring handoff"):
        executor.execute(_handoff(), lambda _path, _terminal: None)
    assert not (root / "component_manifest.json").exists()


def test_executor_halts_then_resumes_same_component_run(tmp_path: Path) -> None:
    root = tmp_path / "evaluation"
    first = [_Predictor(0.5) for _ in range(4)] + [_Predictor(0.5, fail=True)]
    second = [_Predictor(0.5, fail=True) for _ in range(4)] + [_Predictor(0.5)]
    provider = _Provider(root, [first, second])
    observed = []
    executor = RegisteredEvaluationWorkflowExecutorV1(
        _registration(root), provider
    )

    with pytest.raises(WorkflowComponentPausedV1):
        executor.execute(
            _handoff(), lambda path, terminal: observed.append((path, terminal))
        )
    assert observed == [(root.resolve(), False)]
    halted = validate_evaluation_workflow_component_package_v1(
        root, _handoff()
    )
    assert halted["events"][-1]["event"] == "component_halted"
    assert halted["manifest"]["component_attempt_index"] == 1

    observed.clear()
    assert executor.execute(
        _handoff(), lambda path, terminal: observed.append((path, terminal))
    ) == root.resolve()
    assert observed == []
    completed = validate_evaluation_workflow_component_package_v1(
        root, _handoff(), require_terminal=True
    )
    assert completed["events"][-1]["event"] == "component_done"
    assert completed["manifest"]["component_attempt_index"] == 2
    assert [predictor.calls for predictor in second[:4]] == [0, 0, 0, 0]
    assert second[-1].calls == 1


def test_executor_selectively_repairs_exact_work_set_without_replaying_unaffected(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evaluation"
    initial_predictors = [_Predictor(0.5) for _ in range(4)] + [
        _Predictor(0.5, fail=True)
    ]
    first_executor = RegisteredEvaluationWorkflowExecutorV1(
        _registration(root),
        _Provider(root, [initial_predictors]),
    )
    with pytest.raises(WorkflowComponentPausedV1):
        first_executor.execute(_handoff(), lambda _path, _terminal: None)

    partial = validate_evaluation_workflow_component_package_v1(
        root, _handoff()
    )
    ledger_rows = {
        row["stage_id"]: row
        for row in partial["recovery"]["ledger"]["works"]
    }
    accepted_stage = "chapter_d2l_deep_learning_computation"
    halted_stage = "chapter_d2l_convolutional_neural_networks"
    affected_work_ids = [
        ledger_rows[accepted_stage]["work_id"],
        ledger_rows[halted_stage]["work_id"],
    ]
    old_artifact_ref = ledger_rows[accepted_stage]["accepted_artifact"][
        "artifact_ref"
    ]
    old_artifact_bytes = (root / old_artifact_ref).read_bytes()

    repaired_predictors = [
        _Predictor(0.75, fail=True),
        _Predictor(0.75, fail=True),
        _Predictor(0.75, fail=True),
        _Predictor(0.75),
        _Predictor(0.75),
    ]
    repaired_executor = RegisteredEvaluationWorkflowExecutorV1(
        _registration(root, producer_code_commit="e" * 40),
        _Provider(root, [repaired_predictors]),
    )
    plan = repaired_executor.build_repair_plan(
        _handoff(),
        affected_work_ids=affected_work_ids,
        reason_code="implementation_repair",
        authorized_by="evaluation_server",
        authorization_id="repair_authorization_fixture_001",
        authorized_at=NOW,
    )

    assert plan["source_code_commit"] == COMMIT
    assert plan["repair_code_commit"] == "e" * 40
    assert plan["affected_work_ids"] == affected_work_ids
    assert plan["supersede_work_ids"] == [affected_work_ids[0]]
    assert repaired_executor.execute(
        _handoff(),
        lambda _path, _terminal: None,
        repair_plan=plan,
    ) == root.resolve()

    assert [item.calls for item in repaired_predictors] == [0, 0, 0, 1, 1]
    assert (root / old_artifact_ref).read_bytes() == old_artifact_bytes
    completed = validate_evaluation_workflow_component_package_v1(
        root, _handoff(), require_terminal=True
    )
    assert completed["manifest"]["component_attempt_index"] == 2
    repairs = completed["recovery"]["repairs"]
    assert len(repairs) == 1
    receipt = repairs[0]["receipt"]
    assert receipt is not None
    assert receipt["rerun_work_ids"] == affected_work_ids
    assert {
        row["work_id"] for row in receipt["current_accepted_artifacts"]
    } == {
        row["work_id"] for row in plan["unaffected_accepted_artifacts"]
    } | set(affected_work_ids)


def test_executor_resumes_interrupted_repair_without_replaying_repaired_prefix(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evaluation"
    initial_predictors = [_Predictor(0.5) for _ in range(4)] + [
        _Predictor(0.5, fail=True)
    ]
    first_executor = RegisteredEvaluationWorkflowExecutorV1(
        _registration(root),
        _Provider(root, [initial_predictors]),
    )
    with pytest.raises(WorkflowComponentPausedV1):
        first_executor.execute(_handoff(), lambda _path, _terminal: None)

    partial = validate_evaluation_workflow_component_package_v1(
        root, _handoff()
    )
    ledger_rows = {
        row["stage_id"]: row for row in partial["recovery"]["ledger"]["works"]
    }
    affected_work_ids = [
        ledger_rows["chapter_d2l_deep_learning_computation"]["work_id"],
        ledger_rows["chapter_d2l_convolutional_neural_networks"]["work_id"],
    ]
    old_artifact_ref = ledger_rows[
        "chapter_d2l_deep_learning_computation"
    ]["accepted_artifact"]["artifact_ref"]
    old_artifact_bytes = (root / old_artifact_ref).read_bytes()

    repair_attempt_two = [
        _Predictor(0.75, fail=True),
        _Predictor(0.75, fail=True),
        _Predictor(0.75, fail=True),
        _Predictor(0.75),
        _Predictor(0.75, fail=True),
    ]
    repair_attempt_three = [
        _Predictor(0.8, fail=True),
        _Predictor(0.8, fail=True),
        _Predictor(0.8, fail=True),
        _Predictor(0.8, fail=True),
        _Predictor(0.8),
    ]
    repaired_executor = RegisteredEvaluationWorkflowExecutorV1(
        _registration(root, producer_code_commit="e" * 40),
        _Provider(root, [repair_attempt_two, repair_attempt_three]),
    )
    plan = repaired_executor.build_repair_plan(
        _handoff(),
        affected_work_ids=affected_work_ids,
        reason_code="implementation_repair",
        authorized_by="evaluation_server",
        authorization_id="repair_authorization_fixture_002",
        authorized_at=NOW,
    )

    with pytest.raises(WorkflowComponentPausedV1):
        repaired_executor.execute(
            _handoff(),
            lambda _path, _terminal: None,
            repair_plan=plan,
        )
    repair_halted = validate_evaluation_workflow_component_package_v1(
        root, _handoff()
    )
    assert repair_halted["manifest"]["component_attempt_index"] == 2
    assert repair_halted["events"][-1]["event"] == "component_halted"
    assert [item.calls for item in repair_attempt_two] == [0, 0, 0, 1, 1]

    assert repaired_executor.execute(
        _handoff(),
        lambda _path, _terminal: None,
        repair_plan=plan,
    ) == root.resolve()
    assert [item.calls for item in repair_attempt_three] == [0, 0, 0, 0, 1]
    assert (root / old_artifact_ref).read_bytes() == old_artifact_bytes

    completed = validate_evaluation_workflow_component_package_v1(
        root, _handoff(), require_terminal=True
    )
    assert completed["manifest"]["component_attempt_index"] == 3
    receipt = completed["recovery"]["repairs"][0]["receipt"]
    assert receipt is not None
    assert receipt["component_attempt_index"] == 3
    assert receipt["rerun_work_ids"] == affected_work_ids
    accepted = completed["recovery"]["ledger"]["accepted_work_ids"]
    assert len(accepted) == len(set(accepted))


def test_executor_rejects_foreign_workflow_before_provider(tmp_path: Path) -> None:
    root = tmp_path / "evaluation"
    provider = _Provider(
        root,
        [[_Predictor(0.5) for _ in range(5)]],
    )
    executor = RegisteredEvaluationWorkflowExecutorV1(
        _registration(root), provider
    )
    foreign = _reseal_handoff(_handoff(), workflow_run_id="foreign_workflow")

    with pytest.raises(ContractValidationError, match="another workflow"):
        executor.execute(foreign, lambda _path, _terminal: None)
    assert provider.calls == 0


def test_executor_rejects_valid_but_different_materialized_settings(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evaluation"
    provider = _Provider(
        root,
        [[_Predictor(0.5) for _ in range(5)]],
    )
    different_selection = _selection()
    different_selection["highlight_pair"] = {
        "baseline_arm_id": "s1",
        "candidate_arm_id": "community",
    }
    different_basis = {
        key: copy.deepcopy(value)
        for key, value in different_selection.items()
        if key != "selection_sha256"
    }
    different_selection["selection_sha256"] = canonical_sha256(different_basis)
    different_settings = materialize_registered_evaluation_settings_v1(
        scoring_handoff=_handoff(),
        settings_authority=_authority(),
        locked_selection=different_selection,
        settings_option_id="evaluation_workflow_settings_v1",
        registered_option_sha256="9" * 64,
        evaluation_profile_ref="profiles/evaluation_fixture_v1.json",
        policy_profile_ref=None,
        shared_selection_ref="selections/evaluation_five_chapter_v1.json",
    )
    executor = RegisteredEvaluationWorkflowExecutorV1(
        _registration(root, materialized_settings=different_settings),
        provider,
    )

    with pytest.raises(
        ContractValidationError,
        match="registered settings differ from the deterministic locked selection",
    ):
        executor.execute(_handoff(), lambda _path, _terminal: None)
    assert provider.calls == 0
