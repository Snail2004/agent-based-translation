from __future__ import annotations

import copy
from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pytest

from pipeline.eval.contracts_v1 import ContractValidationError
from pipeline.eval.live_pilot_runner_v1 import (
    execute_evaluation_live_pilot_v1,
    seal_evaluation_live_pilot_execution,
    validate_evaluation_live_pilot_execution,
    validate_evaluation_live_pilot_execution_binding,
)
from pipeline.eval.live_pilot_sf_qe_v1 import (
    PILOT_LOCAL_SF_QE_BINDING_SCHEMA_ID,
    prepare_evaluation_live_pilot_sf_qe_v1,
)
from pipeline.eval.local_sf_qe_v1 import SF_QE_MODEL_ID, SF_QE_REPORT_TRANSFORM_ID
from pipeline.tests.test_evaluation_live_pilot_preflight_v1 import (
    COMMIT,
    NOW,
    _build,
    _common,
    _config,
)
from pipeline.tests.test_evaluation_method_executors_v1 import (
    _SemanticSender,
    _runtime,
)


PROFILE_ID = "evaluation-live-pilot-fixture-v1"
PROFILE_SHA256 = "7" * 64
LOGICAL_RUN_ID = "evaluation-live-pilot-fixture"
ATTEMPT_RUN_ID = "evaluation-live-pilot-fixture-attempt-1"


def _digest_json(value) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _local_sf_qe_binding(preflight) -> dict:
    sf_qe_jobs = [row for row in preflight["jobs"] if row["method_id"] == "sf_qe"]
    return {
        "schema_id": PILOT_LOCAL_SF_QE_BINDING_SCHEMA_ID,
        "model_id": SF_QE_MODEL_ID,
        "report_transform_id": SF_QE_REPORT_TRANSFORM_ID,
        "checkpoint_sha256": "6" * 64,
        "package_name": "unbabel-comet",
        "package_version": "2.2.7",
        "python_version": "3.11.9",
        "device": "cpu",
        "batch_size": 8,
        "selected_job_count": len(sf_qe_jobs),
        "packet_set_sha256": _digest_json(
            [row["packet_sha256"] for row in sf_qe_jobs]
        ),
        "score_set_sha256": _digest_json([80.0 for _row in sf_qe_jobs]),
    }


class _Executor:
    def __init__(self, local_sf_qe_binding: dict, *, fail_job: int | None = None) -> None:
        self.calls = 0
        self.sf_qe_calls = 0
        self.job_ids: list[str] = []
        self.fail_job = fail_job
        self.local_sf_qe_binding = copy.deepcopy(local_sf_qe_binding)

    @property
    def execution_binding(self) -> dict:
        return {
            "evaluation_logical_run_id": LOGICAL_RUN_ID,
            "evaluation_attempt_run_id": ATTEMPT_RUN_ID,
            "evaluation_profile_id": PROFILE_ID,
            "evaluation_profile_sha256": PROFILE_SHA256,
            "local_sf_qe": copy.deepcopy(self.local_sf_qe_binding),
        }

    def __call__(self, packet: dict) -> dict:
        self.calls += 1
        self.job_ids.append(packet["binding"]["job_id"])
        method_id = packet["binding"]["method_id"]
        if method_id == "sf_qe":
            self.sf_qe_calls += 1
        if self.fail_job == self.calls:
            return {
                "status": "failed",
                "semantic_output": None,
                "error_code": "fixture_failure",
            }
        if method_id == "sf_qe":
            output = {"score": 80.0}
        elif method_id == "sf_bt":
            output = {
                "score": 75,
                "flags": [],
                "note": "fixture semantic agreement",
            }
        else:
            output = {
                "overall_verdict": "candidate_1",
                "style_verdict": "tie",
                "tags": ["meaning"],
                "note": "candidate one is more faithful",
            }
        return {"status": "succeeded", "semantic_output": output, "error_code": None}

    def assert_sf_qe_exact_cover(self) -> None:
        assert self.sf_qe_calls == self.local_sf_qe_binding["selected_job_count"]

    def begin_sf_qe_execution(self) -> None:
        assert self.sf_qe_calls in {
            0,
            self.local_sf_qe_binding["selected_job_count"],
        }
        self.sf_qe_calls = 0


def _execute(*, fail_job: int | None = None):
    common = _common()
    config = _config(common)
    preflight = _build(common)
    executor = _Executor(_local_sf_qe_binding(preflight), fail_job=fail_job)
    artifact = execute_evaluation_live_pilot_v1(
        common,
        config,
        preflight,
        executor,
        created_at="2026-07-20T00:10:00Z",
        runner_code_commit=COMMIT,
    )
    return common, config, preflight, executor, artifact


def _validate_binding(artifact, common, config, preflight):
    return validate_evaluation_live_pilot_execution_binding(
        artifact,
        common,
        config,
        preflight,
        evaluation_logical_run_id=LOGICAL_RUN_ID,
        evaluation_attempt_run_id=ATTEMPT_RUN_ID,
        evaluation_profile_id=PROFILE_ID,
        evaluation_profile_sha256=PROFILE_SHA256,
        local_sf_qe_binding=_local_sf_qe_binding(preflight),
    )


def test_pilot_runner_executes_exact_preflight_job_cover_without_headline_claim():
    common, config, preflight, executor, artifact = _execute()

    assert executor.calls == 40
    assert executor.job_ids == [row["job_id"] for row in preflight["jobs"]]
    assert [row["job_id"] for row in artifact["jobs"]] == executor.job_ids
    assert artifact["coverage"] == {
        "selected_unit_count": 8,
        "selected_job_count": 40,
        "succeeded_job_count": 40,
        "failed_job_count": 0,
        "method_job_counts": {"pj": 8, "sf_bt": 16, "sf_qe": 16},
    }
    assert artifact["claim"] == {
        "scope": "calibration_only",
        "status": "inconclusive",
        "verdict": "INCONCLUSIVE",
        "reason_code": "pilot_not_headline_evidence",
    }
    assert artifact["binding"]["preflight_sha256"] == preflight["integrity"]["preflight_sha256"]
    assert artifact["binding"]["evaluation_profile_sha256"] == PROFILE_SHA256
    assert _validate_binding(artifact, common, config, preflight) == artifact


def test_pilot_runner_does_not_mutate_inputs_or_executor_packets():
    common = _common()
    config = _config(common)
    preflight = _build(common)
    before = (copy.deepcopy(common), copy.deepcopy(config), copy.deepcopy(preflight))

    execute_evaluation_live_pilot_v1(
        common,
        config,
        preflight,
        _Executor(_local_sf_qe_binding(preflight)),
        created_at="2026-07-20T00:10:00Z",
        runner_code_commit=COMMIT,
    )

    assert (common, config, preflight) == before


def test_failed_job_is_recorded_without_turning_pilot_into_a_verdict():
    _, _, _, executor, artifact = _execute(fail_job=7)

    assert executor.calls == 40
    assert artifact["coverage"]["succeeded_job_count"] == 39
    assert artifact["coverage"]["failed_job_count"] == 1
    failed = [row for row in artifact["jobs"] if row["status"] == "failed"]
    assert len(failed) == 1
    assert failed[0]["semantic_output"] is None
    assert failed[0]["error_code"] == "fixture_failure"
    assert artifact["claim"]["verdict"] == "INCONCLUSIVE"


def test_resealed_packet_substitution_fails_exact_binding():
    common, config, preflight, _, artifact = _execute()
    changed = copy.deepcopy(artifact)
    changed["jobs"][0]["packet_sha256"] = "8" * 64
    changed = seal_evaluation_live_pilot_execution(changed)

    validate_evaluation_live_pilot_execution(changed)
    with pytest.raises(ContractValidationError, match="stale packet"):
        _validate_binding(changed, common, config, preflight)


def test_resealed_profile_substitution_requires_external_expected_profile():
    common, config, preflight, _, artifact = _execute()
    changed = copy.deepcopy(artifact)
    changed["binding"]["evaluation_profile_sha256"] = "8" * 64
    changed["execution_id"] = "pilot-execution-foreign-profile"
    changed = seal_evaluation_live_pilot_execution(changed)

    validate_evaluation_live_pilot_execution(changed)
    with pytest.raises(ContractValidationError, match="execution references another"):
        _validate_binding(changed, common, config, preflight)


def test_resealed_local_checkpoint_substitution_requires_external_expected_binding():
    common, config, preflight, _, artifact = _execute()
    changed = copy.deepcopy(artifact)
    changed["binding"]["local_sf_qe"]["checkpoint_sha256"] = "8" * 64
    changed["execution_id"] = "pilot-execution-foreign-checkpoint"
    changed = seal_evaluation_live_pilot_execution(changed)

    validate_evaluation_live_pilot_execution(changed)
    with pytest.raises(ContractValidationError, match="execution references another"):
        _validate_binding(changed, common, config, preflight)


def test_resealed_sf_qe_score_substitution_fails_bound_score_set():
    _, _, _, _, artifact = _execute()
    changed = copy.deepcopy(artifact)
    sf_qe_row = next(row for row in changed["jobs"] if row["method_id"] == "sf_qe")
    sf_qe_row["semantic_output"]["score"] = 79.0
    changed = seal_evaluation_live_pilot_execution(changed)

    with pytest.raises(ContractValidationError, match="score set"):
        validate_evaluation_live_pilot_execution(changed)


def test_unknown_key_is_rejected_even_after_reseal():
    _, _, _, _, artifact = _execute()
    changed = copy.deepcopy(artifact)
    changed["claim"]["winner"] = "S1"
    changed = seal_evaluation_live_pilot_execution(changed)

    with pytest.raises(ContractValidationError, match="unknown keys"):
        validate_evaluation_live_pilot_execution(changed)


def test_public_observation_validator_rejects_nonfinite_score():
    from pipeline.eval.execution_runner_v1 import (
        validate_evaluation_job_observation_v1,
    )

    with pytest.raises(ContractValidationError, match="finite"):
        validate_evaluation_job_observation_v1(
            {
                "status": "succeeded",
                "semantic_output": {"score": float("nan")},
                "error_code": None,
            },
            method_id="sf_qe",
        )


def test_pilot_runner_uses_shared_backend_and_exact_cache_for_resume(
    tmp_path: Path,
) -> None:
    common = _common()
    common = replace(
        common,
        translations=tuple(
            replace(
                row,
                target_text=(
                    f"Ban dich thu nhat {row.block_id}."
                    if row.arm_id == "S0"
                    else f"Ban dich thu hai {row.block_id}."
                ),
            )
            for row in common.translations
        ),
    )
    config = _config(common)
    preflight = _build(common)
    sender = _SemanticSender()
    local_sf_qe = prepare_evaluation_live_pilot_sf_qe_v1(
        common,
        config,
        preflight,
        _CometPredictor(),
        batch_size=8,
    )
    executor, ledger = _runtime(
        tmp_path,
        sender,
        common,
        config,
        cache_mode="read_write",
        sf_qe_scorer=local_sf_qe,
    )

    first = execute_evaluation_live_pilot_v1(
        common,
        config,
        preflight,
        executor,
        created_at="2026-07-20T00:10:00Z",
        runner_code_commit=COMMIT,
    )
    first_provider_calls = sender.calls
    second = execute_evaluation_live_pilot_v1(
        common,
        config,
        preflight,
        executor,
        created_at="2026-07-20T00:10:00Z",
        runner_code_commit=COMMIT,
    )

    assert first == second
    assert all(
        first["binding"][key] == value
        for key, value in executor.execution_binding.items()
    )
    assert first_provider_calls == 48
    assert sender.calls == first_provider_calls
    assert sender.pj_calls == 16
    assert ledger.count("usage") == 48
    assert ledger.count("cache") == 96
    assert all("S0" not in prompt and "S1" not in prompt for prompt in sender.prompts)


class _CometPredictor:
    checkpoint_sha256 = "6" * 64

    def describe_runtime(self):
        return {
            "schema_id": "CometKiwiRuntimeDescriptionV1",
            "package_name": "unbabel-comet",
            "package_version": "2.2.7",
            "python_version": "3.11.9",
            "device": "cpu",
            "checkpoint_sha256": self.checkpoint_sha256,
        }

    def __call__(self, rows, batch_size):
        assert batch_size == 8
        return [0.8 for _row in rows]
