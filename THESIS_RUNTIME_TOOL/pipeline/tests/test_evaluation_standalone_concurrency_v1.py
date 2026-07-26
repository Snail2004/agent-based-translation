from __future__ import annotations

import json
from pathlib import Path
import threading
import time

import pytest

from pipeline.eval.contracts_v1 import ContractValidationError
from pipeline.eval.standalone_concurrency_v1 import (
    StandaloneEvaluationTaskV1,
    StandaloneTaskFailureV1,
    build_standalone_concurrency_profile_v1,
    build_standalone_task_plan_v1,
    run_standalone_evaluation_tasks_v1,
)


SHA_A = "a" * 64
SHA_B = "b" * 64


def _profile():
    return build_standalone_concurrency_profile_v1(
        profile_id="d2l_five_chapter_cli_v1",
        assignment_sha256=SHA_A,
        max_in_flight=3,
        lanes=(
            {"lane_id": "local", "authority_kind": "local_cpu", "worker_limit": 1},
            {"lane_id": "shop", "authority_kind": "physical_quota_bucket", "worker_limit": 1},
            {"lane_id": "ckey", "authority_kind": "physical_quota_bucket", "worker_limit": 1},
        ),
    )


def _tasks():
    return (
        StandaloneEvaluationTaskV1("qe", 1, "sf_qe", "local", SHA_A, SHA_B),
        StandaloneEvaluationTaskV1("reverse", 2, "sf_bt_reverse", "shop", SHA_A, SHA_B),
        StandaloneEvaluationTaskV1("judge", 3, "sf_bt_semantic", "ckey", SHA_A, SHA_B, ("reverse",)),
        StandaloneEvaluationTaskV1("mtq", 4, "mtq5_orientation", "ckey", SHA_A, SHA_B),
    )


def _plan(profile, tasks=None):
    return build_standalone_task_plan_v1(
        plan_id="d2l_five_chapter_plan_v1",
        profile=profile,
        tasks=_tasks() if tasks is None else tasks,
    )


def test_lanes_overlap_but_each_physical_bucket_stays_serial(tmp_path: Path) -> None:
    profile = _profile()
    plan = _plan(profile)
    lock = threading.Lock()
    active_by_lane = {"local": 0, "shop": 0, "ckey": 0}
    maxima = dict(active_by_lane)
    total_active = 0
    max_total = 0

    def execute(task, _attempt, dependencies):
        nonlocal total_active, max_total
        with lock:
            active_by_lane[task.lane_id] += 1
            maxima[task.lane_id] = max(maxima[task.lane_id], active_by_lane[task.lane_id])
            total_active += 1
            max_total = max(max_total, total_active)
        if task.task_id == "judge":
            assert dependencies["reverse"] == b'{"task":"reverse"}'
        time.sleep(0.04)
        with lock:
            active_by_lane[task.lane_id] -= 1
            total_active -= 1
        return json.dumps({"task": task.task_id}, separators=(",", ":")).encode()

    result = run_standalone_evaluation_tasks_v1(
        output_root=tmp_path / "run",
        profile=profile,
        plan=plan,
        executor=execute,
        resume=False,
    )

    assert result.state == "completed"
    assert max_total >= 2
    assert maxima == {"local": 1, "shop": 1, "ckey": 1}
    assert result.accepted_task_ids == ("qe", "reverse", "judge", "mtq")


def test_resume_reuses_accepted_tasks_and_only_retries_failed_work(tmp_path: Path) -> None:
    profile = _profile()
    plan = _plan(profile)
    first_calls: list[str] = []

    def first(task, _attempt, _dependencies):
        first_calls.append(task.task_id)
        if task.task_id == "reverse":
            raise StandaloneTaskFailureV1("fixture_network_failure")
        return b'{"accepted":true}'

    first_result = run_standalone_evaluation_tasks_v1(
        output_root=tmp_path / "run",
        profile=profile,
        plan=plan,
        executor=first,
        resume=False,
    )
    assert first_result.state == "halted"
    preserved = {
        path.relative_to(tmp_path / "run"): path.read_bytes()
        for path in (tmp_path / "run" / "receipts").rglob("*.json")
        if b'"status":"accepted"' in path.read_bytes()
    }
    second_calls: list[tuple[str, int]] = []

    def second(task, attempt, dependencies):
        second_calls.append((task.task_id, attempt))
        if task.task_id == "judge":
            assert dependencies["reverse"] == b'{"accepted":true}'
        return b'{"accepted":true}'

    second_result = run_standalone_evaluation_tasks_v1(
        output_root=tmp_path / "run",
        profile=profile,
        plan=plan,
        executor=second,
        resume=True,
    )
    assert second_result.state == "completed"
    assert ("reverse", 2) in second_calls
    assert "qe" not in {task_id for task_id, _ in second_calls}
    for relative, payload in preserved.items():
        assert (tmp_path / "run" / relative).read_bytes() == payload


def test_resume_rejects_profile_or_plan_drift(tmp_path: Path) -> None:
    profile = _profile()
    plan = _plan(profile)
    run_standalone_evaluation_tasks_v1(
        output_root=tmp_path / "run",
        profile=profile,
        plan=plan,
        executor=lambda task, attempt, deps: b"{}",
        resume=False,
    )
    drifted = build_standalone_concurrency_profile_v1(
        profile_id="d2l_five_chapter_cli_v1",
        assignment_sha256="c" * 64,
        max_in_flight=3,
        lanes=profile["lanes"],
    )
    with pytest.raises(ContractValidationError, match="profile_binding|immutable_drift"):
        run_standalone_evaluation_tasks_v1(
            output_root=tmp_path / "run",
            profile=drifted,
            plan=_plan(drifted),
            executor=lambda task, attempt, deps: b"{}",
            resume=True,
        )


def test_physical_bucket_cannot_claim_multiple_workers() -> None:
    with pytest.raises(ContractValidationError, match="quota_concurrency"):
        build_standalone_concurrency_profile_v1(
            profile_id="bad",
            assignment_sha256=SHA_A,
            max_in_flight=2,
            lanes=(
                {"lane_id": "ckey", "authority_kind": "physical_quota_bucket", "worker_limit": 2},
            ),
        )


def test_tampered_accepted_artifact_fails_before_resume_provider_work(tmp_path: Path) -> None:
    profile = _profile()
    plan = _plan(profile)
    root = tmp_path / "run"
    run_standalone_evaluation_tasks_v1(
        output_root=root,
        profile=profile,
        plan=plan,
        executor=lambda task, attempt, deps: b"{}",
        resume=False,
    )
    artifact = next((root / "artifacts").rglob("*.json"))
    artifact.write_bytes(b'{"tampered":true}')
    called = False

    def should_not_run(task, attempt, deps):
        nonlocal called
        called = True
        return b"{}"

    with pytest.raises(ContractValidationError, match="artifact_hash"):
        run_standalone_evaluation_tasks_v1(
            output_root=root,
            profile=profile,
            plan=plan,
            executor=should_not_run,
            resume=True,
        )
    assert called is False
