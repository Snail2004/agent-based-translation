from __future__ import annotations

import copy
import math

import pytest

from pipeline.eval.common_input_v1 import (
    CommonArmV1,
    CommonBlockV1,
    CommonEvaluationInputV1,
    CommonTranslationV1,
    LegacyD2LSourceBindingV1,
    source_binding_to_dict,
)
from pipeline.eval.contracts_v1 import ContractValidationError
from pipeline.eval.execution_runner_v1 import (
    execute_evaluation_plan_v1,
    seal_evaluation_execution_artifact,
    validate_evaluation_execution_artifact,
    validate_evaluation_execution_binding,
)
from pipeline.eval.offline_orchestrator_v1 import (
    build_evaluation_plan,
    seal_evaluation_run_config,
)


NOW = "2026-07-19T12:00:00Z"
COMMIT = "a" * 40


def _common(
    *,
    arm_ids: tuple[str, ...] = ("S0", "S1"),
    status_overrides: dict[tuple[str, str], str] | None = None,
) -> CommonEvaluationInputV1:
    overrides = status_overrides or {}
    blocks = (
        CommonBlockV1("b1", "ch1", 1, "paragraph", "Source one.", "translate"),
        CommonBlockV1("b2", "ch1", 2, "paragraph", "Source two.", "translate"),
    )
    arms = tuple(
        CommonArmV1(
            artifact_id=f"artifact-{arm_id.lower()}",
            artifact_sha256=(str(index + 1) * 64),
            logical_run_id="logical-run",
            attempt_run_id=f"attempt-{arm_id.lower()}",
            arm_id=arm_id,
            profile_id="profile",
            profile_config_sha256="9" * 64,
            source_language="en",
            target_language="vi",
        )
        for index, arm_id in enumerate(arm_ids)
    )
    labels = {"S0": "alpha", "S1": "beta", "S2": "gamma"}
    translations: list[CommonTranslationV1] = []
    for arm in arms:
        for block in blocks:
            status = overrides.get((arm.arm_id, block.block_id), "translated")
            translations.append(
                CommonTranslationV1(
                    arm_id=arm.arm_id,
                    block_id=block.block_id,
                    status=status,
                    target_text=(
                        f"{labels[arm.arm_id]} translation {block.block_id}."
                        if status == "translated"
                        else None
                    ),
                    error_code=None if status == "translated" else status,
                )
            )
    return CommonEvaluationInputV1(
        source_schema_id="D2LEvaluationInputV1",
        source_schema_version="1.0.0",
        source_binding=LegacyD2LSourceBindingV1(
            project_id="project",
            document_id="document",
            source_db_sha256="4" * 64,
            runtime_manifest_sha256="5" * 64,
        ),
        blocks=blocks,
        arms=arms,
        translations=tuple(translations),
    )


def _config(
    common: CommonEvaluationInputV1,
    *,
    methods: tuple[str, ...] = ("sf_qe", "sf_bt", "pj"),
) -> dict:
    pairwise = "pj" in methods
    return seal_evaluation_run_config(
        {
            "schema_id": "EvaluationRunConfigV1",
            "schema_version": "1.0.0",
            "config_id": "execution-config",
            "created_at": NOW,
            "producer": {
                "workstream": "evaluation",
                "component": "execution_test",
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
                    for arm in common.arms
                ],
            },
            "methods": [
                {
                    "method_id": method_id,
                    "method_version": "1.0.0",
                    "scorer_kind": "pairwise" if method_id == "pj" else "unary",
                    "profile_scope": "common",
                    "eligible_admissions": ["translate", "translate_structured"],
                }
                for method_id in methods
            ],
            "comparison_pairs": (
                [
                    {
                        "pair_id": "s0-v-s1",
                        "arm_1_id": "S0",
                        "arm_2_id": "S1",
                    }
                ]
                if pairwise
                else []
            ),
            "unit_policy": {
                "unit_kind": "block",
                "context_before_blocks": 1,
                "context_after_blocks": 1,
            },
            "blinding": {"mode": "opaque_counterbalanced", "seed": "seed"},
            "retry_policy": {"max_transport_attempts": 1},
            "integrity": {"config_sha256": "0" * 64},
        }
    )


def _active_text(packet: dict, slot: int = 0) -> str:
    return next(
        row["text"]
        for row in packet["candidates"][slot]["blocks"]
        if row["role"] == "active"
    )


def _successful_executor(packet: dict) -> dict:
    method_id = packet["binding"]["method_id"]
    if method_id == "sf_qe":
        text = _active_text(packet)
        block_bonus = 10 if text.endswith("b2.") else 0
        score = (80 if text.startswith("beta") else 60) + block_bonus
        output = {"score": score}
    elif method_id == "sf_bt":
        text = _active_text(packet)
        output = {
            "score": 75 if text.startswith("beta") else 50,
            "flags": [],
            "note": "fixture semantic agreement",
        }
    else:
        beta_slot = 0 if _active_text(packet, 0).startswith("beta") else 1
        output = {
            "overall_verdict": f"candidate_{beta_slot + 1}",
            "style_verdict": "tie",
            "tags": ["meaning"],
            "note": "beta preserves the source more completely",
        }
    return {"status": "succeeded", "semantic_output": output, "error_code": None}


def _aggregate(artifact: dict, method_id: str) -> dict:
    return next(row for row in artifact["aggregates"] if row["method_id"] == method_id)


def test_runner_executes_all_methods_and_keeps_claim_inconclusive():
    common = _common()
    config = _config(common)

    artifact = execute_evaluation_plan_v1(
        common,
        config,
        _successful_executor,
        created_at=NOW,
        runner_code_commit=COMMIT,
        baseline_arm_id="S0",
        candidate_arm_id="S1",
    )

    assert artifact["coverage"] == {
        "planned_job_count": 10,
        "blocked_job_count": 0,
        "succeeded_job_count": 10,
        "failed_job_count": 0,
    }
    sf_qe = _aggregate(artifact, "sf_qe")
    values = {row["arm_id"]: row for row in sf_qe["arm_values"]}
    assert values["S0"]["value"] == 65
    assert values["S1"]["value"] == 85
    assert sf_qe["comparison"] == {
        "status": "available",
        "baseline_arm_id": "S0",
        "candidate_arm_id": "S1",
        "delta": 20,
        "wins": 2,
        "ties": 0,
        "losses": 0,
        "paired_denominator": 2,
    }
    sf_bt = _aggregate(artifact, "sf_bt")
    assert sf_bt["comparison"]["delta"] == 25
    pj = _aggregate(artifact, "pj")
    assert pj["comparison"]["wins"] == 2
    assert pj["comparison"]["losses"] == 0
    assert pj["comparison"]["paired_denominator"] == 2
    assert artifact["claim"]["verdict"] == "INCONCLUSIVE"
    assert artifact["claim"]["reason_codes"] == ["claim_policy_not_frozen"]


def test_executor_receives_only_blinded_packet():
    common = _common()
    config = _config(common, methods=("sf_qe",))
    seen: list[dict] = []

    def capture(packet: dict) -> dict:
        seen.append(copy.deepcopy(packet))
        assert "S0" not in repr(packet)
        assert "S1" not in repr(packet)
        return _successful_executor(packet)

    execute_evaluation_plan_v1(
        common,
        config,
        capture,
        created_at=NOW,
        runner_code_commit=COMMIT,
        baseline_arm_id="S0",
        candidate_arm_id="S1",
    )
    assert len(seen) == 4
    assert {row["candidates"][0]["slot_id"] for row in seen} == {"candidate_1"}


def test_blocked_and_failed_jobs_remain_in_denominator():
    common = _common(status_overrides={("S1", "b2"): "missing"})
    config = _config(common)

    def partly_failed(packet: dict) -> dict:
        if (
            packet["binding"]["method_id"] == "sf_qe"
            and _active_text(packet).startswith("alpha")
            and _active_text(packet).endswith("b1.")
        ):
            return {
                "status": "failed",
                "semantic_output": None,
                "error_code": "fixture_failure",
            }
        return _successful_executor(packet)

    artifact = execute_evaluation_plan_v1(
        common,
        config,
        partly_failed,
        created_at=NOW,
        runner_code_commit=COMMIT,
        baseline_arm_id="S0",
        candidate_arm_id="S1",
    )

    assert artifact["coverage"] == {
        "planned_job_count": 10,
        "blocked_job_count": 3,
        "succeeded_job_count": 6,
        "failed_job_count": 1,
    }
    sf_qe = _aggregate(artifact, "sf_qe")
    values = {row["arm_id"]: row for row in sf_qe["arm_values"]}
    assert values["S0"]["expected_count"] == 2
    assert values["S0"]["observed_count"] == 1
    assert values["S0"]["missing_count"] == 1
    assert values["S1"]["expected_count"] == 2
    assert values["S1"]["observed_count"] == 1
    assert values["S1"]["missing_count"] == 1
    assert sf_qe["status"] == "partial"
    assert sf_qe["comparison"]["status"] == "insufficient"


def test_one_arm_run_does_not_fabricate_comparison():
    common = _common(arm_ids=("S0",))
    config = _config(common, methods=("sf_qe", "sf_bt"))
    artifact = execute_evaluation_plan_v1(
        common,
        config,
        _successful_executor,
        created_at=NOW,
        runner_code_commit=COMMIT,
    )
    assert all(
        row["comparison"]["status"] == "not_applicable"
        for row in artifact["aggregates"]
    )
    assert artifact["claim"]["verdict"] == "INCONCLUSIVE"


@pytest.mark.parametrize(
    "observation",
    [
        {
            "status": "succeeded",
            "semantic_output": {"score": math.nan},
            "error_code": None,
        },
        {
            "status": "succeeded",
            "semantic_output": {"score": 101},
            "error_code": None,
        },
        {
            "status": "succeeded",
            "semantic_output": {"score": 50, "unexpected": True},
            "error_code": None,
        },
    ],
)
def test_sf_qe_invalid_observations_fail_closed(observation: dict):
    common = _common(arm_ids=("S0",))
    config = _config(common, methods=("sf_qe",))
    with pytest.raises(ContractValidationError):
        execute_evaluation_plan_v1(
            common,
            config,
            lambda _packet: observation,
            created_at=NOW,
            runner_code_commit=COMMIT,
        )


def test_method_specific_output_contracts_fail_closed():
    common = _common()
    sf_bt_config = _config(common, methods=("sf_bt",))
    with pytest.raises(ContractValidationError):
        execute_evaluation_plan_v1(
            common,
            sf_bt_config,
            lambda _packet: {
                "status": "succeeded",
                "semantic_output": {"score": 63, "flags": [], "note": "invalid band"},
                "error_code": None,
            },
            created_at=NOW,
            runner_code_commit=COMMIT,
        )

    pj_config = _config(common, methods=("pj",))
    with pytest.raises(ContractValidationError):
        execute_evaluation_plan_v1(
            common,
            pj_config,
            lambda _packet: {
                "status": "succeeded",
                "semantic_output": {
                    "overall_verdict": "S1",
                    "style_verdict": "tie",
                    "tags": [],
                    "note": "arm identity leaked",
                },
                "error_code": None,
            },
            created_at=NOW,
            runner_code_commit=COMMIT,
            baseline_arm_id="S0",
            candidate_arm_id="S1",
        )


def test_artifact_is_deterministic_detached_and_inputs_are_immutable():
    common = _common()
    config = _config(common)
    config_before = copy.deepcopy(config)
    first = execute_evaluation_plan_v1(
        common,
        config,
        _successful_executor,
        created_at=NOW,
        runner_code_commit=COMMIT,
        baseline_arm_id="S0",
        candidate_arm_id="S1",
    )
    second = execute_evaluation_plan_v1(
        common,
        config,
        _successful_executor,
        created_at=NOW,
        runner_code_commit=COMMIT,
        baseline_arm_id="S0",
        candidate_arm_id="S1",
    )
    assert first == second
    assert config == config_before
    first["jobs"][0]["status"] = "failed"
    assert second["jobs"][0]["status"] == "succeeded"


def test_resealed_tampered_aggregate_is_rejected():
    common = _common()
    config = _config(common)
    artifact = execute_evaluation_plan_v1(
        common,
        config,
        _successful_executor,
        created_at=NOW,
        runner_code_commit=COMMIT,
        baseline_arm_id="S0",
        candidate_arm_id="S1",
    )
    tampered = copy.deepcopy(artifact)
    tampered["aggregates"][0]["arm_values"][0]["value"] = 99
    resealed = seal_evaluation_execution_artifact(tampered)
    with pytest.raises(ContractValidationError, match="aggregates"):
        validate_evaluation_execution_artifact(resealed)


def test_binding_rejects_foreign_plan():
    common = _common()
    config = _config(common)
    artifact = execute_evaluation_plan_v1(
        common,
        config,
        _successful_executor,
        created_at=NOW,
        runner_code_commit=COMMIT,
        baseline_arm_id="S0",
        candidate_arm_id="S1",
    )
    foreign_config = copy.deepcopy(config)
    foreign_config["blinding"]["seed"] = "other-seed"
    foreign_config = seal_evaluation_run_config(foreign_config)
    foreign_plan = build_evaluation_plan(common, foreign_config)
    with pytest.raises(ContractValidationError):
        validate_evaluation_execution_binding(artifact, common, foreign_plan)
