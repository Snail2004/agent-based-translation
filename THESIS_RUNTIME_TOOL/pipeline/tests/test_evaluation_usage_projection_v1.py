from __future__ import annotations

import copy
from datetime import datetime, timezone
from pathlib import Path

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
from pipeline.eval.execution_runner_v1 import execute_evaluation_plan_v1
from pipeline.eval.full_run_report_writer_v1 import compose_full_run_report_v1
from pipeline.eval.local_sf_qe_v1 import (
    SF_QE_MODEL_ID,
    persist_local_sf_qe_evidence_v1,
    prepare_local_sf_qe_v1,
    seal_local_sf_qe_evidence_v1,
)
from pipeline.eval.method_executors_v1 import EvaluationMethodExecutorV1
from pipeline.eval.offline_orchestrator_v1 import seal_evaluation_run_config
from pipeline.eval.usage_projection_v1 import (
    load_evaluation_usage_artifact_v1,
    persist_evaluation_usage_artifact_v1,
    project_evaluation_usage_v1,
    seal_evaluation_usage_artifact_v1,
    validate_evaluation_usage_artifact_v1,
)
from pipeline.llm_backend import SharedLlmAttemptLedger
from pipeline.tests.test_evaluation_method_executors_v1 import (
    _SemanticSender,
    _common as _shared_common,
    _config as _shared_config,
    _runtime as _shared_runtime,
)


NOW = "2026-07-19T00:00:00Z"
COMMIT = "a" * 40


def _common() -> CommonEvaluationInputV1:
    block = CommonBlockV1("b001", "ch1", 1, "paragraph", "Source.", "translate")
    arms = (
        CommonArmV1("artifact-s0", "1" * 64, "run", "attempt-s0", "S0", "p0", "3" * 64, "en", "vi"),
        CommonArmV1("artifact-s1", "2" * 64, "run", "attempt-s1", "S1", "p1", "4" * 64, "en", "vi"),
    )
    translations = (
        CommonTranslationV1("S0", "b001", "translated", "Target zero.", None),
        CommonTranslationV1("S1", "b001", "translated", "Target one.", None),
    )
    return CommonEvaluationInputV1(
        "D2LEvaluationInputV1",
        "1.0.0",
        LegacyD2LSourceBindingV1("project", "document", "5" * 64, "6" * 64),
        (block,),
        arms,
        translations,
    )


def _config(common: CommonEvaluationInputV1) -> dict:
    return seal_evaluation_run_config(
        {
            "schema_id": "EvaluationRunConfigV1",
            "schema_version": "1.0.0",
            "config_id": "usage-projection-test",
            "created_at": NOW,
            "producer": {
                "workstream": "evaluation",
                "component": "usage_projection_test",
                "component_version": "1.0.0",
                "code_commit": COMMIT,
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
                    "method_id": "sf_qe",
                    "method_version": "sf_qe_cometkiwi_native_x100_v1",
                    "scorer_kind": "unary",
                    "profile_scope": "common",
                    "eligible_admissions": ["translate"],
                }
            ],
            "comparison_pairs": [],
            "unit_policy": {
                "unit_kind": "block",
                "context_before_blocks": 0,
                "context_after_blocks": 0,
            },
            "blinding": {"mode": "opaque_counterbalanced", "seed": "seed"},
            "retry_policy": {"max_transport_attempts": 1},
            "integrity": {"config_sha256": "0" * 64},
        }
    )


def _prepared_execution():
    common = _common()
    config = _config(common)
    moments = iter(
        (
            datetime(2026, 7, 19, 0, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 19, 0, 0, 1, tzinfo=timezone.utc),
        )
    )
    timer = iter((2.0, 2.5))
    prepared = prepare_local_sf_qe_v1(
        common,
        config,
        lambda rows, batch: [0.75, 0.85],
        created_at=NOW,
        producer_code_commit=COMMIT,
        checkpoint_sha256="7" * 64,
        package_name="unbabel-comet",
        package_version="2.2.7",
        device="cpu",
        batch_size=8,
        clock=lambda: next(moments),
        monotonic=lambda: next(timer),
    )
    executor = EvaluationMethodExecutorV1(
        common_input=common,
        config_payload=config,
        sf_qe_scorer=prepared,
        llm_roles=None,
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
    return common, config, prepared, execution


def _projection(tmp_path: Path):
    common, config, prepared, execution = _prepared_execution()
    local = persist_local_sf_qe_evidence_v1(
        output_root=tmp_path, evidence_payload=prepared.evidence
    )
    projection = project_evaluation_usage_v1(
        common,
        config,
        execution,
        created_at=NOW,
        producer_code_commit=COMMIT,
        evaluation_logical_run_id="evaluation-run",
        evaluation_attempt_run_id="evaluation-attempt",
        local_sf_qe_evidence=local.evidence,
        local_sf_qe_relative_path=local.path.relative_to(tmp_path).as_posix(),
    )
    return common, config, execution, local, projection


def test_local_projection_keeps_unknown_tokens_and_cost_null(tmp_path: Path) -> None:
    common, config, execution, local, projection = _projection(tmp_path)
    assert projection.stage_facts == (
        {
            "stage_id": "sf_qe.local_scorer",
            "method_id": "sf_qe",
            "status": "complete",
            "started_at": "2026-07-19T00:00:00.000Z",
            "ended_at": "2026-07-19T00:00:01.000Z",
            "duration_ms": 500,
            "attempt_run_id": "evaluation-attempt",
            "error_code": None,
        },
    )
    assert projection.usage["status"] == "partial"
    assert projection.usage["accounting_basis"] == "local_metered"
    assert projection.usage["totals"]["request_count"] == 1
    assert projection.usage["totals"]["input_tokens"] is None
    assert projection.usage["totals"]["cost_usd"] is None
    assert projection.usage["unknown_attempt_count"] == 1
    assert projection.artifact["source_records"] == [
        {
            "kind": "local_sf_qe_evidence",
            "record_id": local.evidence["artifact_id"],
            "record_sha256": local.evidence["integrity"]["artifact_sha256"],
            "relative_path": local.path.relative_to(tmp_path).as_posix(),
        }
    ]
    assert common.project_id == projection.artifact["binding"]["project_id"]
    assert config["integrity"]["config_sha256"] == projection.artifact["binding"]["config_sha256"]
    assert execution["integrity"]["artifact_sha256"] == projection.artifact["binding"]["execution_sha256"]


def test_projection_persists_and_full_report_accepts_exact_facts(tmp_path: Path) -> None:
    common, config, execution, _, projection = _projection(tmp_path)
    persisted = persist_evaluation_usage_artifact_v1(
        output_root=tmp_path, artifact_payload=projection.artifact
    )
    reused = persist_evaluation_usage_artifact_v1(
        output_root=tmp_path, artifact_payload=projection.artifact
    )
    assert persisted.reused is False and reused.reused is True
    assert load_evaluation_usage_artifact_v1(persisted.path) == projection.artifact
    report = compose_full_run_report_v1(
        common,
        config,
        execution,
        generated_at=NOW,
        producer_code_commit=COMMIT,
        evaluation_logical_run_id="evaluation-run",
        evaluation_attempt_run_id="evaluation-attempt",
        evaluation_profile_id="evaluation-profile",
        policy_profile_id=None,
        input_artifact={
            "artifact_id": "evaluation-input",
            "relative_path": "input/evaluation_input.json",
            "sha256": "8" * 64,
        },
        arm_presentations=[
            {
                "arm_id": arm.arm_id,
                "role": "baseline" if arm.arm_id == "S0" else "candidate",
                "kind": "system",
                "label": arm.arm_id,
                "relative_path": f"translations/{arm.arm_id.lower()}.json",
            }
            for arm in common.arms
        ],
        method_presentations=[
            {
                "display_name": "Semantic fidelity QE",
                "method": {
                    "method_id": "sf_qe",
                    "method_version": "sf_qe_cometkiwi_native_x100_v1",
                    "implementation_commit": COMMIT,
                    "prompt_version": None,
                    "model_id": SF_QE_MODEL_ID,
                },
            }
        ],
        stage_facts=projection.stage_facts,
        usage_payload=projection.usage,
        usage_artifacts=[projection.artifact_descriptor],
    )
    assert report["usage"] == projection.usage
    assert report["metrics"][0]["arm_values"][0]["value"] == 75.0
    assert report["metrics"][0]["arm_values"][1]["value"] == 85.0
    assert report["claim"]["status"] == "insufficient"
    assert report["claim"]["verdict"] == "INCONCLUSIVE"


def test_foreign_local_evidence_and_empty_shared_ledger_fail_or_stay_explicit(
    tmp_path: Path,
) -> None:
    common, config, prepared, execution = _prepared_execution()
    foreign = copy.deepcopy(prepared.evidence)
    foreign["binding"]["project_id"] = "other-project"
    foreign = seal_local_sf_qe_evidence_v1(foreign)
    with pytest.raises(ContractValidationError, match="foreign project_id"):
        project_evaluation_usage_v1(
            common,
            config,
            execution,
            created_at=NOW,
            producer_code_commit=COMMIT,
            evaluation_logical_run_id="evaluation-run",
            evaluation_attempt_run_id="evaluation-attempt",
            local_sf_qe_evidence=foreign,
            local_sf_qe_relative_path="local_sf_qe/foreign.json",
        )

    empty_ledger = SharedLlmAttemptLedger(tmp_path / "shared.sqlite3")
    projection = project_evaluation_usage_v1(
        common,
        config,
        execution,
        created_at=NOW,
        producer_code_commit=COMMIT,
        evaluation_logical_run_id="evaluation-run",
        evaluation_attempt_run_id="evaluation-attempt",
        local_sf_qe_evidence=prepared.evidence,
        local_sf_qe_relative_path="local_sf_qe/local.json",
        shared_ledger=empty_ledger,
        shared_ledger_relative_path="usage/shared.sqlite3",
    )
    assert len(projection.stage_facts) == 1


def test_usage_contract_rejects_unknown_key_hash_and_identity_tamper(tmp_path: Path) -> None:
    _, _, _, _, projection = _projection(tmp_path)
    unknown = copy.deepcopy(projection.artifact)
    unknown["verdict"] = "better"
    with pytest.raises(ContractValidationError, match="unknown keys"):
        validate_evaluation_usage_artifact_v1(unknown)

    stale = copy.deepcopy(projection.artifact)
    stale["usage"]["totals"]["request_count"] = 99
    with pytest.raises(ContractValidationError, match="artifact ID differs"):
        validate_evaluation_usage_artifact_v1(stale)

    resealed = copy.deepcopy(projection.artifact)
    resealed["binding"]["execution_sha256"] = "f" * 64
    resealed = seal_evaluation_usage_artifact_v1(resealed)
    with pytest.raises(ContractValidationError, match="artifact ID differs"):
        validate_evaluation_usage_artifact_v1(resealed)


def test_shared_attempt_ledger_projects_each_semantic_role_without_cost_guessing(
    tmp_path: Path,
) -> None:
    common = _shared_common()
    config = _shared_config(common, methods=("sf_bt", "pj"))
    executor, ledger = _shared_runtime(tmp_path, _SemanticSender(), common, config)
    execution = execute_evaluation_plan_v1(
        common,
        config,
        executor,
        created_at=NOW,
        runner_code_commit=COMMIT,
        baseline_arm_id="S0",
        candidate_arm_id="S1",
    )
    projection = project_evaluation_usage_v1(
        common,
        config,
        execution,
        created_at=NOW,
        producer_code_commit=COMMIT,
        evaluation_logical_run_id="evaluation_fixture_run",
        evaluation_attempt_run_id="evaluation_fixture_attempt",
        shared_ledger=ledger,
        shared_ledger_relative_path="usage/shared_attempts.sqlite3",
    )
    assert [row["stage_id"] for row in projection.stage_facts] == [
        "sf_bt.back_translation",
        "sf_bt.semantic_judge",
        "pj.judge",
    ]
    assert all(row["status"] == "complete" for row in projection.stage_facts)
    by_stage = {row["stage_id"]: row for row in projection.usage["by_stage"]}
    assert by_stage["sf_bt.back_translation"]["request_count"] == 6
    assert by_stage["sf_bt.semantic_judge"]["request_count"] == 6
    assert by_stage["pj.judge"]["request_count"] == 6
    assert projection.usage["totals"]["request_count"] == 18
    assert projection.usage["totals"]["successful_request_count"] == 18
    assert projection.usage["totals"]["input_tokens"] == 720
    assert projection.usage["totals"]["output_tokens"] == 216
    assert projection.usage["totals"]["total_tokens"] == 936
    assert projection.usage["totals"]["cost_usd"] is None
    assert projection.usage["status"] == "partial"
    assert projection.usage["unknown_attempt_count"] == 18
    assert {row["kind"] for row in projection.artifact["source_records"]} >= {
        "shared_seal",
        "shared_usage",
    }

    wrong_run = project_evaluation_usage_v1(
        common,
        config,
        execution,
        created_at=NOW,
        producer_code_commit=COMMIT,
        evaluation_logical_run_id="other_run",
        evaluation_attempt_run_id="other_attempt",
        shared_ledger=ledger,
        shared_ledger_relative_path="usage/shared_attempts.sqlite3",
    )
    wrong_run_status = {row["stage_id"]: row["status"] for row in wrong_run.stage_facts}
    assert wrong_run_status == {
        "sf_bt.back_translation": "not_run",
        "sf_bt.semantic_judge": "not_run",
        "pj.judge": "not_applicable",
    }
    assert wrong_run.usage["status"] == "unavailable"
