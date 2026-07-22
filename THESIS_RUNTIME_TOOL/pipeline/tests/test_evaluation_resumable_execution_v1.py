from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest

from pipeline.eval.contracts_v1 import ContractValidationError
from pipeline.eval.method_executors_v1 import EvaluationMethodExecutorV1
from pipeline.eval.offline_orchestrator_v1 import build_evaluation_plan
from pipeline.eval.resumable_execution_v1 import (
    EvaluationRunHaltedV1,
    EvaluationRunStateStoreV1,
    ResumableEvaluationRoleRunnerV1,
    execute_evaluation_plan_resumable_v1,
)
from pipeline.llm_backend import TransportCallError
from pipeline.tests.test_evaluation_method_executors_v1 import (
    COMMIT,
    NOW,
    _SemanticSender,
    _common,
    _config,
    _runtime,
)


class _FailOnCallSender(_SemanticSender):
    def __init__(self, fail_on_call: int) -> None:
        super().__init__()
        self.fail_on_call = fail_on_call

    def send(self, request):
        if self.calls + 1 == self.fail_on_call:
            self.calls += 1
            raise TransportCallError(
                code="http_500",
                status_code=500,
                safe_message="fixture provider failure",
            )
        return super().send(request)


class _ContractDriftRoleRunner:
    def __init__(self, base) -> None:
        self._base = base

    @property
    def semantic_contract(self):
        value = self._base.semantic_contract
        value["roles"][0]["generation"]["temperature"] = 0.9
        return value

    @property
    def execution_binding(self):
        return self._base.execution_binding

    @property
    def attempt_runtime_binding(self):
        return self._base.attempt_runtime_binding

    @property
    def cache_mode(self):
        return self._base.cache_mode

    def execute(self, **kwargs):
        return self._base.execute(**kwargs)


def _store(
    root: Path,
    base_roles,
    common,
    config,
    *,
    evaluation_profile_id: str = "evaluation-fixture-profile",
    baseline_arm_id: str | None = None,
    candidate_arm_id: str | None = None,
) -> EvaluationRunStateStoreV1:
    plan = build_evaluation_plan(common, config)
    return EvaluationRunStateStoreV1.open_or_create(
        root,
        plan=plan,
        semantic_contract=base_roles.semantic_contract,
        evaluation_logical_run_id="evaluation_fixture_run",
        evaluation_attempt_run_id="evaluation_fixture_attempt",
        evaluation_profile_id=evaluation_profile_id,
        policy_profile_id=None,
        baseline_arm_id=baseline_arm_id,
        candidate_arm_id=candidate_arm_id,
        created_at=NOW,
        producer_code_commit=COMMIT,
        clock=lambda: NOW,
    )


def _executor(common, config, roles) -> EvaluationMethodExecutorV1:
    return EvaluationMethodExecutorV1(
        common_input=common,
        config_payload=config,
        sf_qe_scorer=lambda _source, _target: 80.0,
        llm_roles=roles,
        created_at=NOW,
        producer_code_commit=COMMIT,
    )


def _base_roles(
    tmp_path: Path,
    sender,
    common,
    config,
    *,
    distribution_suffix: str | None = None,
):
    executor, ledger = _runtime(
        tmp_path,
        sender,
        common,
        config,
        distribution_suffix=distribution_suffix,
    )
    return executor._llm_roles, ledger  # Test-only access to the injected role runner.


def test_sf_bt_resumes_after_second_physical_call_without_repeating_accepted_call(
    tmp_path: Path,
) -> None:
    common = _common()
    config = _config(common, methods=("sf_bt",))
    first_sender = _FailOnCallSender(fail_on_call=2)
    runtime_root = tmp_path / "shared-runtime"
    first_base, _ = _base_roles(runtime_root, first_sender, common, config)
    state_root = tmp_path / "run-state"
    store = _store(state_root, first_base, common, config)
    first_roles = ResumableEvaluationRoleRunnerV1(first_base, store)

    with pytest.raises(EvaluationRunHaltedV1, match="http_500"):
        execute_evaluation_plan_resumable_v1(
            common,
            config,
            _executor(common, config, first_roles),
            store,
            created_at=NOW,
            runner_code_commit=COMMIT,
        )

    assert first_sender.calls == 2
    assert store.status()["state"] == "halted"
    assert store.status()["accepted_call_count"] == 1
    assert store.status()["completed_job_count"] == 0

    second_sender = _SemanticSender()
    second_base, _ = _base_roles(
        runtime_root,
        second_sender,
        common,
        config,
        distribution_suffix="row2",
    )
    reopened = _store(state_root, second_base, common, config)
    second_roles = ResumableEvaluationRoleRunnerV1(second_base, reopened)
    execution = execute_evaluation_plan_resumable_v1(
        common,
        config,
        _executor(common, config, second_roles),
        reopened,
        created_at=NOW,
        runner_code_commit=COMMIT,
    )

    assert execution["coverage"]["succeeded_job_count"] == 6
    assert second_sender.calls == 11
    assert reopened.status()["state"] == "completed"
    assert reopened.status()["accepted_call_count"] == 12
    assert reopened.status()["completed_job_count"] == 6
    events = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((state_root / "events").glob("*.json"))
    ]
    assert any(row["event_type"] == "call_reused" for row in events)
    assert any(row["event_type"] == "run_resumed" for row in events)


def test_completed_job_checkpoint_prevents_all_provider_calls_on_resume(
    tmp_path: Path,
) -> None:
    common = _common()
    config = _config(common, methods=("sf_bt",))
    first_sender = _SemanticSender()
    first_base, _ = _base_roles(tmp_path / "first-runtime", first_sender, common, config)
    state_root = tmp_path / "run-state"
    store = _store(state_root, first_base, common, config)
    execution = execute_evaluation_plan_resumable_v1(
        common,
        config,
        _executor(
            common,
            config,
            ResumableEvaluationRoleRunnerV1(first_base, store),
        ),
        store,
        created_at=NOW,
        runner_code_commit=COMMIT,
    )
    assert first_sender.calls == 12

    second_sender = _SemanticSender()
    second_base, _ = _base_roles(
        tmp_path / "second-runtime", second_sender, common, config
    )
    reopened = _store(state_root, second_base, common, config)
    replay = execute_evaluation_plan_resumable_v1(
        common,
        config,
        _executor(
            common,
            config,
            ResumableEvaluationRoleRunnerV1(second_base, reopened),
        ),
        reopened,
        created_at=NOW,
        runner_code_commit=COMMIT,
    )

    assert replay == execution
    assert second_sender.calls == 0


def test_semantic_contract_drift_requires_a_new_run(tmp_path: Path) -> None:
    common = _common()
    config = _config(common, methods=("sf_bt",))
    base, _ = _base_roles(tmp_path / "runtime", _SemanticSender(), common, config)
    store = _store(tmp_path / "run-state", base, common, config)

    with pytest.raises(ContractValidationError, match="semantic_contract"):
        ResumableEvaluationRoleRunnerV1(_ContractDriftRoleRunner(base), store)


@pytest.mark.parametrize(
    ("changed"),
    ("profile", "comparison"),
)
def test_report_binding_drift_requires_a_new_run(
    tmp_path: Path, changed: str
) -> None:
    common = _common()
    config = _config(common, methods=("sf_bt",))
    base, _ = _base_roles(tmp_path / "runtime", _SemanticSender(), common, config)
    state_root = tmp_path / "run-state"
    _store(state_root, base, common, config)

    kwargs = {}
    if changed == "profile":
        kwargs["evaluation_profile_id"] = "another-profile"
    else:
        kwargs["baseline_arm_id"] = "s0"
        kwargs["candidate_arm_id"] = "s1"

    with pytest.raises(ContractValidationError, match="run-state manifest differs"):
        _store(state_root, base, common, config, **kwargs)


def test_missing_status_projection_is_rebuilt_from_immutable_events(
    tmp_path: Path,
) -> None:
    common = _common()
    config = _config(common, methods=("sf_bt",))
    base, _ = _base_roles(tmp_path / "runtime", _SemanticSender(), common, config)
    state_root = tmp_path / "run-state"
    store = _store(state_root, base, common, config)
    expected = store.status()
    (state_root / "status.json").unlink()

    reopened = _store(state_root, base, common, config)
    assert reopened.status() == expected


def test_tampered_call_checkpoint_fails_closed_before_reuse(tmp_path: Path) -> None:
    common = _common()
    config = _config(common, methods=("sf_bt",))
    sender = _FailOnCallSender(fail_on_call=2)
    base, _ = _base_roles(tmp_path / "runtime", sender, common, config)
    state_root = tmp_path / "run-state"
    store = _store(state_root, base, common, config)
    roles = ResumableEvaluationRoleRunnerV1(base, store)
    with pytest.raises(EvaluationRunHaltedV1):
        execute_evaluation_plan_resumable_v1(
            common,
            config,
            _executor(common, config, roles),
            store,
            created_at=NOW,
            runner_code_commit=COMMIT,
        )

    call_path = next((state_root / "calls").glob("*.json"))
    row = json.loads(call_path.read_text(encoding="utf-8"))
    row["outcome"]["response_text"] = '{"back_translation":"tampered"}'
    call_path.write_text(json.dumps(row), encoding="utf-8", newline="\n")

    with pytest.raises(ContractValidationError):
        _store(state_root, base, common, config).load_accepted_call(
            row["binding"]
        )


def test_nonfinite_and_duplicate_json_checkpoint_inputs_fail_closed(
    tmp_path: Path,
) -> None:
    common = _common()
    config = _config(common, methods=("sf_bt",))
    base, _ = _base_roles(tmp_path / "runtime", _SemanticSender(), common, config)
    state_root = tmp_path / "run-state"
    _store(state_root, base, common, config)

    status_path = state_root / "status.json"
    status_path.write_text('{"x":NaN}', encoding="utf-8", newline="\n")
    with pytest.raises(ContractValidationError, match="checkpoint JSON"):
        _store(state_root, base, common, config)

    status_path.write_text('{"x":1,"x":2}', encoding="utf-8", newline="\n")
    with pytest.raises(ContractValidationError, match="checkpoint JSON"):
        _store(state_root, base, common, config)


def test_live_process_lock_rejects_a_second_runner(tmp_path: Path) -> None:
    common = _common()
    config = _config(common, methods=("sf_bt",))
    base, _ = _base_roles(tmp_path / "runtime", _SemanticSender(), common, config)
    state_root = tmp_path / "run-state"
    store = _store(state_root, base, common, config)
    (state_root / "runner.lock").write_text(
        f"{os.getpid()}\n", encoding="ascii", newline="\n"
    )

    with pytest.raises(ContractValidationError, match="another process owns"):
        with store.run_lock():
            raise AssertionError("lock must not be acquired")
