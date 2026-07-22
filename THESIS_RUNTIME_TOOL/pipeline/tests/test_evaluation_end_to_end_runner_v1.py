from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from pipeline.eval.contracts_v1 import ContractValidationError
from pipeline.eval.end_to_end_runner_v1 import (
    LocalSfQeRuntimeV1,
    run_evaluation_end_to_end_v1,
)
from pipeline.eval.execution_runner_v1 import execute_evaluation_plan_v1
from pipeline.eval.execution_store_v1 import persist_evaluation_execution_bundle_v1
from pipeline.eval.local_sf_qe_v1 import (
    SF_QE_MODEL_ID,
    persist_local_sf_qe_evidence_v1,
    prepare_local_sf_qe_v1,
)
from pipeline.eval.method_executors_v1 import EvaluationMethodExecutorV1
from pipeline.eval.offline_orchestrator_v1 import build_evaluation_plan
from pipeline.eval.scorer_input_packets_v1 import build_scorer_input_packet
from pipeline.eval.resumable_execution_v1 import EvaluationRunHaltedV1
from pipeline.llm_backend import TransportCallError
from pipeline.tests.test_evaluation_method_executors_v1 import (
    _SemanticSender,
    _common,
    _config,
    _runtime,
)


NOW = "2026-07-19T00:00:00Z"
COMMIT = "a" * 40


class _Predictor:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail
        self.last_rows: list[dict[str, str]] = []

    def __call__(self, rows, batch_size):
        self.calls += 1
        if self.fail:
            raise AssertionError("local predictor must not be called")
        self.last_rows = [dict(row) for row in rows]
        assert batch_size == 8
        assert all(set(row) == {"src", "mt"} for row in rows)
        return [0.8 + (index % 2) * 0.1 for index, _ in enumerate(rows)]


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


def _local_runtime(predictor: _Predictor) -> LocalSfQeRuntimeV1:
    moments = iter(
        (
            datetime(2026, 7, 19, 0, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 19, 0, 0, 1, tzinfo=timezone.utc),
        )
    )
    timers = iter((4.0, 4.5))
    return LocalSfQeRuntimeV1(
        predictor=predictor,
        checkpoint_sha256="7" * 64,
        package_name="unbabel-comet",
        package_version="2.2.7",
        device="cpu",
        batch_size=8,
        clock=lambda: next(moments),
        monotonic=lambda: next(timers),
    )


def _materialize_report_inputs(root: Path, common):
    root.mkdir(parents=True, exist_ok=True)
    input_path = root / "input" / "evaluation_input.json"
    input_path.parent.mkdir(parents=True, exist_ok=True)
    input_path.write_text('{"schema_id":"fixture-input"}\n', encoding="utf-8", newline="\n")
    input_artifact = {
        "artifact_id": "evaluation-input-fixture",
        "relative_path": "input/evaluation_input.json",
        "sha256": "6" * 64,
    }
    arm_presentations = []
    for arm in common.arms:
        relative = f"translations/{arm.arm_id.lower()}.json"
        path = root / Path(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"arm_id": arm.arm_id}) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        arm_presentations.append(
            {
                "arm_id": arm.arm_id,
                "role": "baseline" if arm.arm_id == "S0" else "candidate",
                "kind": "system",
                "label": f"{arm.arm_id} translation",
                "relative_path": relative,
            }
        )
    return input_artifact, arm_presentations


def _method_presentations(config):
    names = {
        "sf_qe": "Semantic fidelity QE",
        "sf_bt": "Semantic fidelity back-translation",
        "pj": "Pairwise judgment",
    }
    prompt_versions = {
        "sf_qe": None,
        "sf_bt": "sf_bt_prompt_v3",
        "pj": "pj_prompt_v2",
    }
    models = {
        "sf_qe": SF_QE_MODEL_ID,
        "sf_bt": "evaluation-fixture-model",
        "pj": "evaluation-fixture-model",
    }
    return [
        {
            "display_name": names[row["method_id"]],
            "method": {
                "method_id": row["method_id"],
                "method_version": row["method_version"],
                "implementation_commit": COMMIT,
                "prompt_version": prompt_versions[row["method_id"]],
                "model_id": models[row["method_id"]],
            },
        }
        for row in config["methods"]
    ]


def _run_args(root: Path, common, config):
    input_artifact, arms = _materialize_report_inputs(root, common)
    return {
        "generated_at": NOW,
        "producer_code_commit": COMMIT,
        "evaluation_logical_run_id": "evaluation_fixture_run",
        "evaluation_attempt_run_id": "evaluation_fixture_attempt",
        "evaluation_profile_id": "evaluation-fixture-profile",
        "policy_profile_id": None,
        "input_artifact": input_artifact,
        "arm_presentations": arms,
        "method_presentations": _method_presentations(config),
        "baseline_arm_id": "S0",
        "candidate_arm_id": "S1",
        "shared_ledger_relative_path": "usage/attempt_ledger.sqlite3",
        "caveats": ("Fixture transport; no live provider was called.",),
    }


def test_full_zero_api_path_persists_execution_usage_and_report(tmp_path: Path) -> None:
    common = _common()
    config = _config(common)
    root = tmp_path / "run"
    args = _run_args(root, common, config)
    common_before = copy.deepcopy(common)
    config_before = copy.deepcopy(config)
    args_before = copy.deepcopy(args)
    predictor = _Predictor()
    sender = _SemanticSender()
    fixture_executor, ledger = _runtime(root / "usage", sender, common, config)

    result = run_evaluation_end_to_end_v1(
        common,
        config,
        root,
        **args,
        local_sf_qe_runtime=_local_runtime(predictor),
        llm_roles=fixture_executor._llm_roles,
        shared_ledger=ledger,
    )

    assert predictor.calls == 1
    assert len(predictor.last_rows) == 6
    assert sender.calls == 18
    assert result.report_path == root / "reports" / "full_run_report_v1.json"
    assert result.report["report_state"] == "complete"
    assert result.report["claim"]["verdict"] == "INCONCLUSIVE"
    assert result.report["usage"]["totals"]["request_count"] == 19
    assert result.report["usage"]["totals"]["cost_usd"] is None
    assert result.report["usage"]["unknown_attempt_count"] == 19
    assert result.execution_path.is_file()
    assert result.usage_path is not None and result.usage_path.is_file()
    assert result.local_sf_qe_path is not None and result.local_sf_qe_path.is_file()
    assert result.reused_complete_run is False
    assert common == common_before
    assert config == config_before
    assert args == args_before


def test_complete_run_reuses_without_any_scorer_call(tmp_path: Path) -> None:
    common = _common()
    config = _config(common)
    root = tmp_path / "run"
    args = _run_args(root, common, config)
    first_predictor = _Predictor()
    sender = _SemanticSender()
    fixture_executor, ledger = _runtime(root / "usage", sender, common, config)
    first = run_evaluation_end_to_end_v1(
        common,
        config,
        root,
        **args,
        local_sf_qe_runtime=_local_runtime(first_predictor),
        llm_roles=fixture_executor._llm_roles,
        shared_ledger=ledger,
    )
    calls_before = sender.calls

    second = run_evaluation_end_to_end_v1(
        common,
        config,
        root,
        **args,
        local_sf_qe_runtime=_local_runtime(_Predictor(fail=True)),
        llm_roles=None,
        shared_ledger=None,
    )

    assert sender.calls == calls_before
    assert second.reused_complete_run is True
    assert second.report == first.report
    assert second.execution == first.execution


def test_end_to_end_run_resumes_after_provider_failure_and_finishes_report(
    tmp_path: Path,
) -> None:
    common = _common()
    config = _config(common, methods=("sf_bt",))
    root = tmp_path / "run"
    args = _run_args(root, common, config)
    first_sender = _FailOnCallSender(fail_on_call=2)
    first_executor, first_ledger = _runtime(
        root / "usage", first_sender, common, config
    )

    with pytest.raises(EvaluationRunHaltedV1, match="http_500"):
        run_evaluation_end_to_end_v1(
            common,
            config,
            root,
            **args,
            local_sf_qe_runtime=None,
            llm_roles=first_executor._llm_roles,
            shared_ledger=first_ledger,
        )

    assert first_sender.calls == 2
    assert not (root / "manifest.json").exists()
    assert not (root / "reports" / "full_run_report_v1.json").exists()
    halted = json.loads(
        (root / "run_state" / "status.json").read_text(encoding="utf-8")
    )
    assert halted["state"] == "halted"
    assert halted["accepted_call_count"] == 1

    second_sender = _SemanticSender()
    second_executor, second_ledger = _runtime(
        root / "usage",
        second_sender,
        common,
        config,
        distribution_suffix="row2",
    )
    result = run_evaluation_end_to_end_v1(
        common,
        config,
        root,
        **args,
        local_sf_qe_runtime=None,
        llm_roles=second_executor._llm_roles,
        shared_ledger=second_ledger,
    )

    assert second_sender.calls == 11
    assert result.report["report_state"] == "complete"
    assert result.report["usage"]["totals"]["request_count"] == 13
    completed = json.loads(
        (root / "run_state" / "status.json").read_text(encoding="utf-8")
    )
    assert completed["state"] == "completed"
    assert completed["accepted_call_count"] == 12
    assert completed["completed_job_count"] == 6


def test_missing_input_fails_before_local_or_shared_semantic_work(tmp_path: Path) -> None:
    common = _common()
    config = _config(common)
    root = tmp_path / "run"
    args = _run_args(root, common, config)
    (root / "translations" / "s1.json").unlink()
    predictor = _Predictor()
    sender = _SemanticSender()
    fixture_executor, ledger = _runtime(root / "usage", sender, common, config)

    with pytest.raises(ContractValidationError, match="missing_artifact"):
        run_evaluation_end_to_end_v1(
            common,
            config,
            root,
            **args,
            local_sf_qe_runtime=_local_runtime(predictor),
            llm_roles=fixture_executor._llm_roles,
            shared_ledger=ledger,
        )
    assert predictor.calls == 0
    assert sender.calls == 0
    assert not (root / "manifest.json").exists()


def test_declared_ledger_path_must_match_supplied_physical_ledger(
    tmp_path: Path,
) -> None:
    common = _common()
    config = _config(common)
    root = tmp_path / "run"
    args = _run_args(root, common, config)
    args["shared_ledger_relative_path"] = "usage/another.sqlite3"
    predictor = _Predictor()
    sender = _SemanticSender()
    fixture_executor, ledger = _runtime(root / "usage", sender, common, config)

    with pytest.raises(ContractValidationError, match="declared ledger path"):
        run_evaluation_end_to_end_v1(
            common,
            config,
            root,
            **args,
            local_sf_qe_runtime=_local_runtime(predictor),
            llm_roles=fixture_executor._llm_roles,
            shared_ledger=ledger,
        )
    assert predictor.calls == 0
    assert sender.calls == 0


def test_committed_execution_resumes_to_report_without_repeating_models(
    tmp_path: Path,
) -> None:
    common = _common()
    config = _config(common)
    root = tmp_path / "run"
    args = _run_args(root, common, config)
    predictor = _Predictor()
    prepared = prepare_local_sf_qe_v1(
        common,
        config,
        _local_runtime(predictor).predictor,
        created_at=NOW,
        producer_code_commit=COMMIT,
        checkpoint_sha256="7" * 64,
        package_name="unbabel-comet",
        package_version="2.2.7",
        device="cpu",
        batch_size=8,
    )
    persist_local_sf_qe_evidence_v1(output_root=root, evidence_payload=prepared.evidence)
    sender = _SemanticSender()
    fixture_executor, ledger = _runtime(root / "usage", sender, common, config)
    executor = EvaluationMethodExecutorV1(
        common_input=common,
        config_payload=config,
        sf_qe_scorer=prepared,
        llm_roles=fixture_executor._llm_roles,
        created_at=NOW,
        producer_code_commit=COMMIT,
    )
    execution = execute_evaluation_plan_v1(
        common,
        config,
        executor,
        created_at=NOW,
        runner_code_commit=COMMIT,
        baseline_arm_id="S0",
        candidate_arm_id="S1",
    )
    prepared.assert_exact_cover()
    persist_evaluation_execution_bundle_v1(
        output_root=root,
        config_payload=config,
        execution_payload=execution,
        created_at=NOW,
        producer_code_commit=COMMIT,
    )
    calls_before = sender.calls

    result = run_evaluation_end_to_end_v1(
        common,
        config,
        root,
        **args,
        local_sf_qe_runtime=_local_runtime(_Predictor(fail=True)),
        llm_roles=fixture_executor._llm_roles,
        shared_ledger=ledger,
    )

    assert calls_before == 18
    assert sender.calls == calls_before
    assert result.report_path.is_file()
    assert result.reused_complete_run is False


def test_uncommitted_shared_attempt_halts_before_local_batch(tmp_path: Path) -> None:
    common = _common()
    config = _config(common)
    root = tmp_path / "run"
    args = _run_args(root, common, config)
    predictor = _Predictor()
    sender = _SemanticSender()
    fixture_executor, ledger = _runtime(root / "usage", sender, common, config)
    plan = build_evaluation_plan(common, config)
    job = next(job for job in plan.jobs if job.method_id == "sf_bt" and job.status == "ready")
    packet = build_scorer_input_packet(
        common, plan, job.job_id, created_at=NOW, producer_code_commit=COMMIT
    )
    fixture_executor(packet)
    calls_before = sender.calls

    with pytest.raises(ContractValidationError, match="uncommitted_shared_attempts"):
        run_evaluation_end_to_end_v1(
            common,
            config,
            root,
            **args,
            local_sf_qe_runtime=_local_runtime(predictor),
            llm_roles=fixture_executor._llm_roles,
            shared_ledger=ledger,
        )
    assert calls_before == 2
    assert sender.calls == calls_before
    assert predictor.calls == 0
    assert not (root / "manifest.json").exists()


def test_report_reuse_rejects_different_profile_without_calls(tmp_path: Path) -> None:
    common = _common()
    config = _config(common)
    root = tmp_path / "run"
    args = _run_args(root, common, config)
    sender = _SemanticSender()
    fixture_executor, ledger = _runtime(root / "usage", sender, common, config)
    run_evaluation_end_to_end_v1(
        common,
        config,
        root,
        **args,
        local_sf_qe_runtime=_local_runtime(_Predictor()),
        llm_roles=fixture_executor._llm_roles,
        shared_ledger=ledger,
    )
    calls_before = sender.calls
    drift = dict(args)
    drift["evaluation_profile_id"] = "another-profile"

    with pytest.raises(ContractValidationError, match="completed_run_binding"):
        run_evaluation_end_to_end_v1(
            common,
            config,
            root,
            **drift,
            local_sf_qe_runtime=None,
            llm_roles=None,
            shared_ledger=None,
        )
    assert sender.calls == calls_before


def test_pj_mechanical_ties_need_no_llm_runtime_or_ledger(tmp_path: Path) -> None:
    common = _common(equal_candidates=True)
    config = _config(common, methods=("pj",))
    root = tmp_path / "run"
    args = _run_args(root, common, config)
    args["method_presentations"][0]["method"]["model_id"] = None

    result = run_evaluation_end_to_end_v1(
        common,
        config,
        root,
        **args,
        local_sf_qe_runtime=None,
        llm_roles=None,
        shared_ledger=None,
    )

    assert result.report["report_state"] == "complete"
    assert result.report["usage"]["status"] == "not_applicable"
    assert result.report["usage"]["totals"]["request_count"] is None
    assert result.report["stages"][0]["status"] == "not_applicable"


def test_committed_execution_with_missing_local_evidence_never_rescores(
    tmp_path: Path,
) -> None:
    common = _common()
    config = _config(common)
    root = tmp_path / "run"
    args = _run_args(root, common, config)
    prepared = prepare_local_sf_qe_v1(
        common,
        config,
        _Predictor(),
        created_at=NOW,
        producer_code_commit=COMMIT,
        checkpoint_sha256="7" * 64,
        package_name="unbabel-comet",
        package_version="2.2.7",
        device="cpu",
        batch_size=8,
    )
    sender = _SemanticSender()
    fixture_executor, ledger = _runtime(root / "usage", sender, common, config)
    executor = EvaluationMethodExecutorV1(
        common_input=common,
        config_payload=config,
        sf_qe_scorer=prepared,
        llm_roles=fixture_executor._llm_roles,
        created_at=NOW,
        producer_code_commit=COMMIT,
    )
    execution = execute_evaluation_plan_v1(
        common,
        config,
        executor,
        created_at=NOW,
        runner_code_commit=COMMIT,
        baseline_arm_id="S0",
        candidate_arm_id="S1",
    )
    persist_evaluation_execution_bundle_v1(
        output_root=root,
        config_payload=config,
        execution_payload=execution,
        created_at=NOW,
        producer_code_commit=COMMIT,
    )
    calls_before = sender.calls
    failing_predictor = _Predictor(fail=True)

    with pytest.raises(ContractValidationError, match="local_sf_qe_runtime"):
        run_evaluation_end_to_end_v1(
            common,
            config,
            root,
            **args,
            local_sf_qe_runtime=_local_runtime(failing_predictor),
            llm_roles=fixture_executor._llm_roles,
            shared_ledger=ledger,
        )
    assert failing_predictor.calls == 0
    assert sender.calls == calls_before
