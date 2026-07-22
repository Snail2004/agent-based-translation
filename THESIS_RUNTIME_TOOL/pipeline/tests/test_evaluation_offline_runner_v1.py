from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

import pytest

from pipeline.eval.common_input_v1 import (
    AdmissionPolicyIdentityV1,
    CanonicalComponentIdentityV1,
    CanonicalProjectionIdentityV1,
    CanonicalSourcePackageBindingV1,
    CommonBlockV1,
    CommonSourceSnapshotV1,
    build_common_evaluation_input,
    seal_translation_artifact,
    source_binding_to_dict,
)
from pipeline.eval.contracts_v1 import (
    CanonicalPolicy,
    ContractValidationError,
    seal_payload,
)
from pipeline.eval.offline_orchestrator_v1 import (
    build_evaluation_plan,
    seal_evaluation_run_config,
)
from pipeline.eval.offline_runner_v1 import run_offline_fixture_evaluation


RUNNER_COMMIT = "c" * 40


def _source(count: int = 2) -> CommonSourceSnapshotV1:
    return CommonSourceSnapshotV1(
        source_schema_id="CanonicalSourcePackageV1",
        source_schema_version="1.0.0",
        source_binding=CanonicalSourcePackageBindingV1(
            project_id="project",
            document_id="document",
            document=CanonicalComponentIdentityV1("1.5.0", "1" * 64),
            structure=CanonicalComponentIdentityV1("1.0.0", "2" * 64),
            asset_manifest=CanonicalComponentIdentityV1("1.0.0", "3" * 64),
            admitted_projection=CanonicalProjectionIdentityV1(
                "admitted_projection_v1", "4" * 64
            ),
            admission_policy=AdmissionPolicyIdentityV1(
                "canonical_source_admission", "1.0.0", "5" * 64
            ),
        ),
        blocks=tuple(
            CommonBlockV1(
                block_id=f"b{index + 1}",
                chapter_id="ch1",
                order_index=index,
                block_type="paragraph",
                source_text=f"source {index + 1}",
                admission="translate",
            )
            for index in range(count)
        ),
    )


def _artifact(
    source: CommonSourceSnapshotV1,
    *,
    status_overrides: dict[str, str] | None = None,
) -> dict:
    overrides = status_overrides or {}
    rows = []
    for block in source.blocks:
        status = overrides.get(block.block_id, "translated")
        rows.append(
            {
                "block_id": block.block_id,
                "status": status,
                "target_text": (
                    f"translated::{block.block_id}"
                    if status == "translated"
                    else None
                ),
                "error_code": "fixture_failure" if status == "failed" else None,
            }
        )
    counts = Counter(row["status"] for row in rows)
    return seal_translation_artifact(
        {
            "schema_id": "TranslationArtifactV1",
            "schema_version": "1.0.0",
            "artifact_id": "translation-s1",
            "created_at": "2026-07-17T00:00:00Z",
            "producer": {
                "workstream": "d2l",
                "component": "runner_test_writer",
                "component_version": "1.0.0",
                "code_commit": "a" * 40,
            },
            "source_binding": source_binding_to_dict(source.source_binding),
            "run_identity": {
                "logical_run_id": "logical-s1",
                "attempt_run_id": "attempt-s1",
                "arm_id": "s1",
                "profile_id": "technical_d2l_v1",
                "profile_config_sha256": "9" * 64,
                "source_language": "en",
                "target_language": "vi",
            },
            "translations": rows,
            "coverage": {
                "source_block_count": len(rows),
                "eligible_count": (
                    counts["translated"] + counts["missing"] + counts["failed"]
                ),
                "translated_count": counts["translated"],
                "preserved_count": 0,
                "excluded_count": 0,
                "review_held_count": 0,
                "missing_count": counts["missing"],
                "failed_count": counts["failed"],
            },
            "integrity": {"artifact_sha256": "0" * 64},
        }
    )


def _common(
    count: int = 2,
    *,
    status_overrides: dict[str, str] | None = None,
):
    source = _source(count)
    return build_common_evaluation_input(
        source,
        [_artifact(source, status_overrides=status_overrides)],
    )


def _config(common, *, max_attempts: int = 2, seed: str = "seed-v1") -> dict:
    arm = common.arms[0]
    return seal_evaluation_run_config(
        {
            "schema_id": "EvaluationRunConfigV1",
            "schema_version": "1.0.0",
            "config_id": "runner-config",
            "created_at": "2026-07-17T00:00:00Z",
            "producer": {
                "workstream": "evaluation",
                "component": "runner_test",
                "component_version": "1.0.0",
                "code_commit": "b" * 40,
            },
            "input_binding": {
                "source_schema_id": common.source_schema_id,
                "source_schema_version": common.source_schema_version,
                "source_binding": source_binding_to_dict(common.source_binding),
                "arm_artifacts": [
                    {
                        "arm_id": arm.arm_id,
                        "translation_artifact_id": arm.artifact_id,
                        "translation_artifact_sha256": arm.artifact_sha256,
                        "logical_run_id": arm.logical_run_id,
                        "attempt_run_id": arm.attempt_run_id,
                        "profile_id": arm.profile_id,
                        "profile_config_sha256": arm.profile_config_sha256,
                    }
                ],
            },
            "methods": [
                {
                    "method_id": "fixture_method",
                    "method_version": "1.0.0",
                    "scorer_kind": "unary",
                    "profile_scope": "common",
                    "eligible_admissions": [
                        "translate",
                        "translate_structured",
                    ],
                }
            ],
            "comparison_pairs": [],
            "unit_policy": {
                "unit_kind": "block",
                "context_before_blocks": 1,
                "context_after_blocks": 1,
            },
            "blinding": {
                "mode": "opaque_counterbalanced",
                "seed": seed,
            },
            "retry_policy": {"max_transport_attempts": max_attempts},
            "integrity": {"config_sha256": "0" * 64},
        }
    )


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_stable(path: Path, payload: dict) -> bytes:
    encoded = (
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    path.write_bytes(encoded)
    return encoded


def test_runner_pauses_resumes_and_then_becomes_byte_idempotent(tmp_path: Path):
    common = _common(2)
    config = _config(common)
    root = tmp_path / "run"

    paused = run_offline_fixture_evaluation(
        common,
        config,
        root,
        runner_code_commit=RUNNER_COMMIT,
        max_jobs=1,
    )
    assert paused.status == "paused"
    assert paused.succeeded_job_count == 1
    assert paused.pending_job_count == 1
    assert paused.attempt_count == 1

    completed = run_offline_fixture_evaluation(
        common,
        config,
        root,
        runner_code_commit=RUNNER_COMMIT,
    )
    assert completed.status == "completed"
    assert completed.succeeded_job_count == 2
    assert completed.attempt_count == 2

    checkpoint_bytes = (root / "checkpoint.json").read_bytes()
    manifests = sorted(root.glob("jobs/*/attempt-*/manifest.json"))
    manifest_bytes = [path.read_bytes() for path in manifests]
    repeated = run_offline_fixture_evaluation(
        common,
        config,
        root,
        runner_code_commit=RUNNER_COMMIT,
    )
    assert repeated == completed
    assert (root / "checkpoint.json").read_bytes() == checkpoint_bytes
    assert [path.read_bytes() for path in manifests] == manifest_bytes

    request = _read(next(root.glob("jobs/*/attempt-*/request.json")))
    result = _read(next(root.glob("jobs/*/attempt-*/result.json")))
    assert request["opaque_candidate_slots"] == ["candidate_1"]
    assert "arm_id" not in json.dumps(request)
    assert "score" not in json.dumps(result).lower()


def test_blocked_input_creates_no_attempt_and_is_not_imputed(tmp_path: Path):
    common = _common(1, status_overrides={"b1": "missing"})
    summary = run_offline_fixture_evaluation(
        common,
        _config(common),
        tmp_path / "run",
        runner_code_commit=RUNNER_COMMIT,
    )

    assert summary.status == "completed"
    assert summary.ready_job_count == 0
    assert summary.blocked_job_count == 1
    assert summary.attempt_count == 0
    assert not (tmp_path / "run" / "jobs").exists()


@pytest.mark.parametrize(
    "failure_kind",
    ["transport_failure", "response_contract_failure"],
)
def test_only_declared_retryable_failures_retry_then_succeed(
    tmp_path: Path,
    failure_kind: str,
):
    common = _common(1)
    config = _config(common, max_attempts=2)
    job = build_evaluation_plan(common, config).jobs[0]
    root = tmp_path / failure_kind

    summary = run_offline_fixture_evaluation(
        common,
        config,
        root,
        runner_code_commit=RUNNER_COMMIT,
        failure_schedule={(job.job_id, 1): failure_kind},
    )

    assert summary.status == "completed"
    assert summary.succeeded_job_count == 1
    assert summary.attempt_count == 2
    manifests = [
        _read(path)
        for path in sorted(root.glob("jobs/*/attempt-*/manifest.json"))
    ]
    expected_first = (
        "transport_failed"
        if failure_kind == "transport_failure"
        else "response_contract_failed"
    )
    assert [row["status"] for row in manifests] == [expected_first, "succeeded"]


def test_exhausted_job_does_not_stop_other_independent_jobs(tmp_path: Path):
    common = _common(2)
    config = _config(common, max_attempts=2)
    plan = build_evaluation_plan(common, config)
    first_job = plan.jobs[0]

    summary = run_offline_fixture_evaluation(
        common,
        config,
        tmp_path / "run",
        runner_code_commit=RUNNER_COMMIT,
        failure_schedule={
            (first_job.job_id, 1): "transport_failure",
            (first_job.job_id, 2): "transport_failure",
        },
    )

    assert summary.status == "completed_with_exhausted"
    assert summary.exhausted_job_count == 1
    assert summary.succeeded_job_count == 1
    assert summary.attempt_count == 3


def test_resume_rebuilds_missing_checkpoint_without_reexecuting(tmp_path: Path):
    common = _common(1)
    config = _config(common)
    root = tmp_path / "run"
    first = run_offline_fixture_evaluation(
        common,
        config,
        root,
        runner_code_commit=RUNNER_COMMIT,
    )
    manifests_before = sorted(root.glob("jobs/*/attempt-*/manifest.json"))
    (root / "checkpoint.json").unlink()

    resumed = run_offline_fixture_evaluation(
        common,
        config,
        root,
        runner_code_commit=RUNNER_COMMIT,
    )

    assert resumed.status == "completed"
    assert resumed.attempt_count == first.attempt_count
    assert sorted(root.glob("jobs/*/attempt-*/manifest.json")) == manifests_before


def test_incomplete_attempt_is_preserved_and_counts_toward_cap(tmp_path: Path):
    common = _common(1)
    config = _config(common, max_attempts=2)
    plan = build_evaluation_plan(common, config)
    job = plan.jobs[0]
    root = tmp_path / "run"
    partial = root / "jobs" / job.job_id / "attempt-0001"
    partial.mkdir(parents=True)
    (partial / "request.json").write_text('{"partial":true}\n', encoding="utf-8")

    summary = run_offline_fixture_evaluation(
        common,
        config,
        root,
        runner_code_commit=RUNNER_COMMIT,
    )

    assert summary.status == "completed"
    assert summary.succeeded_job_count == 1
    assert summary.attempt_count == 2
    assert (partial / "request.json").is_file()
    assert (root / "jobs" / job.job_id / "attempt-0002" / "manifest.json").is_file()


def test_attempt_directories_must_be_contiguous_from_one(tmp_path: Path):
    common = _common(1)
    config = _config(common, max_attempts=2)
    job = build_evaluation_plan(common, config).jobs[0]
    root = tmp_path / "run"
    (root / "jobs" / job.job_id / "attempt-0002").mkdir(parents=True)

    with pytest.raises(ContractValidationError, match="attempt_sequence"):
        run_offline_fixture_evaluation(
            common,
            config,
            root,
            runner_code_commit=RUNNER_COMMIT,
        )


@pytest.mark.parametrize(
    "target",
    ["run_config", "plan", "checkpoint", "manifest", "result"],
)
def test_tampered_persisted_artifact_fails_closed(
    tmp_path: Path,
    target: str,
):
    common = _common(1)
    config = _config(common)
    root = tmp_path / target
    run_offline_fixture_evaluation(
        common,
        config,
        root,
        runner_code_commit=RUNNER_COMMIT,
    )
    if target == "run_config":
        payload = _read(root / "run_config.json")
        payload["config_id"] = "tampered"
        (root / "run_config.json").write_text(
            json.dumps(payload),
            encoding="utf-8",
        )
        expected = "immutable_conflict"
    elif target == "plan":
        payload = _read(root / "evaluation_plan.json")
        payload["plan"]["plan_id"] = "tampered"
        (root / "evaluation_plan.json").write_text(
            json.dumps(payload),
            encoding="utf-8",
        )
        expected = "plan_artifact_hash"
    elif target == "checkpoint":
        payload = _read(root / "checkpoint.json")
        payload["generation"] += 1
        (root / "checkpoint.json").write_text(
            json.dumps(payload),
            encoding="utf-8",
        )
        expected = "checkpoint_hash"
    elif target == "manifest":
        manifest_path = next(root.glob("jobs/*/attempt-*/manifest.json"))
        payload = _read(manifest_path)
        payload["failure_code"] = "tampered"
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")
        expected = "attempt_failure"
    else:
        result_path = next(root.glob("jobs/*/attempt-*/result.json"))
        result_path.write_text('{"tampered":true}\n', encoding="utf-8")
        expected = "attempt_artifact_hash"

    with pytest.raises(ContractValidationError, match=expected):
        run_offline_fixture_evaluation(
            common,
            config,
            root,
            runner_code_commit=RUNNER_COMMIT,
        )


def test_failure_schedule_rejects_non_transport_semantic_retry(tmp_path: Path):
    common = _common(1)
    config = _config(common)
    job = build_evaluation_plan(common, config).jobs[0]

    with pytest.raises(ValueError, match="unsupported fixture failure"):
        run_offline_fixture_evaluation(
            common,
            config,
            tmp_path / "run",
            runner_code_commit=RUNNER_COMMIT,
            failure_schedule={(job.job_id, 1): "low_result"},
        )


def test_resealed_semantic_artifact_tamper_still_fails_closed(tmp_path: Path):
    common = _common(1)
    config = _config(common)
    root = tmp_path / "run"
    run_offline_fixture_evaluation(
        common,
        config,
        root,
        runner_code_commit=RUNNER_COMMIT,
    )
    result_path = next(root.glob("jobs/*/attempt-*/result.json"))
    manifest_path = result_path.parent / "manifest.json"
    result = _read(result_path)
    result["score"] = 10
    result_bytes = _write_stable(result_path, result)

    manifest = _read(manifest_path)
    manifest["artifacts"]["result"]["sha256"] = hashlib.sha256(
        result_bytes
    ).hexdigest()
    manifest = seal_payload(
        manifest,
        policy=CanonicalPolicy(frozenset(), frozenset()),
        hash_path=("integrity", "manifest_sha256"),
    )
    _write_stable(manifest_path, manifest)

    with pytest.raises(ContractValidationError, match="attempt_artifact_contract"):
        run_offline_fixture_evaluation(
            common,
            config,
            root,
            runner_code_commit=RUNNER_COMMIT,
        )


@pytest.mark.parametrize("case", ["unknown_job", "blocked_job", "attempt_over_cap"])
def test_failure_schedule_must_bind_to_an_executable_plan_attempt(
    tmp_path: Path,
    case: str,
):
    status_overrides = {"b1": "missing"} if case == "blocked_job" else None
    common = _common(1, status_overrides=status_overrides)
    config = _config(common, max_attempts=2)
    plan = build_evaluation_plan(common, config)
    job = plan.jobs[0]
    if case == "unknown_job":
        key = ("job-does-not-exist", 1)
        expected = "unknown job"
    elif case == "blocked_job":
        key = (job.job_id, 1)
        expected = "blocked job"
    else:
        key = (job.job_id, 3)
        expected = "exceeds retry cap"

    with pytest.raises(ValueError, match=expected):
        run_offline_fixture_evaluation(
            common,
            config,
            tmp_path / case,
            runner_code_commit=RUNNER_COMMIT,
            failure_schedule={key: "transport_failure"},
        )


def test_same_root_rejects_config_or_runner_identity_drift(tmp_path: Path):
    common = _common(1)
    config = _config(common)
    root = tmp_path / "run"
    run_offline_fixture_evaluation(
        common,
        config,
        root,
        runner_code_commit=RUNNER_COMMIT,
    )

    with pytest.raises(ContractValidationError, match="immutable_conflict"):
        run_offline_fixture_evaluation(
            common,
            _config(common, seed="different"),
            root,
            runner_code_commit=RUNNER_COMMIT,
        )
    with pytest.raises(ContractValidationError, match="immutable_conflict"):
        run_offline_fixture_evaluation(
            common,
            config,
            root,
            runner_code_commit="d" * 40,
        )
