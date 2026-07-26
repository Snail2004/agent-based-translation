from __future__ import annotations

import copy
from pathlib import Path

import pytest

import pipeline.eval.workflow_repair_v1 as repair_module
from pipeline.eval.contracts_v1 import (
    ContractValidationError,
    canonical_sha256,
    seal_payload,
)
from pipeline.eval.workflow_recovery_v1 import (
    EvaluationWorkflowRecoveryStoreV1,
    build_evaluation_recovery_assignment_v1,
    build_evaluation_work_descriptor_v1,
)
from pipeline.eval.workflow_repair_v1 import (
    build_evaluation_repair_plan_v1,
    build_evaluation_repair_receipt_v1,
    repair_plan_path_v1,
    repair_receipt_path_v1,
    validate_evaluation_repair_plan_v1,
    validate_evaluation_repair_receipt_v1,
)


HASH = "a" * 64
NOW = "2026-07-26T12:00:00Z"
SOURCE_COMMIT = "b" * 40
REPAIR_COMMIT = "c" * 40


def _assignment():
    return build_evaluation_recovery_assignment_v1(
        workflow_run_id="workflow_repair_fixture",
        component_run_id="evalcomp_repair_fixture",
        input_set_sha256=HASH,
        settings_sha256=HASH,
        evaluation_profile_sha256=HASH,
        stage_plan_sha256=HASH,
        sampling_sha256=HASH,
        semantic_contract_sha256=HASH,
    )


def _descriptor(stage_id: str):
    return build_evaluation_work_descriptor_v1(
        stage_id=stage_id,
        chapter_id=stage_id,
        scorer_id="sf_qe",
        arm_ids=["s0", "s1"],
        presentation_id=f"presentation_{stage_id}",
        orientation="forward",
        input_bindings=[{"artifact_ref": "handoff.json", "sha256": HASH}],
        evaluation_profile_sha256=HASH,
        prompt_sha256=HASH,
        schema_sha256=HASH,
        validator_sha256=HASH,
        model_id="fixture-model",
        provider_family="fixture",
        output_mode="prompt_validated",
        logical_request_id=f"logical_{stage_id}",
    )


def _artifact(ref: str, digest: str, *, kind: str = "full_run_report_v1"):
    return {
        "artifact_kind": kind,
        "artifact_ref": ref,
        "schema_version": "1.0.0",
        "sha256": digest,
        "sha256_kind": "physical",
    }


def _fixture(tmp_path: Path):
    assignment = _assignment()
    store = EvaluationWorkflowRecoveryStoreV1(
        tmp_path / "component",
        assignment=assignment,
        generated_at=NOW,
        producer_code_commit=SOURCE_COMMIT,
    )
    accepted_one = store.begin_work(
        _descriptor("chapter_one"),
        component_attempt_id="evalcomp_attempt_0001",
        component_attempt_index=1,
    )
    halted = store.begin_work(
        _descriptor("chapter_two"),
        component_attempt_id="evalcomp_attempt_0001",
        component_attempt_index=1,
    )
    accepted_three = store.begin_work(
        _descriptor("chapter_three"),
        component_attempt_id="evalcomp_attempt_0001",
        component_attempt_index=1,
    )
    old_one = _artifact("chapters/01/reports/old.json", "1" * 64)
    old_three = _artifact("chapters/03/reports/old.json", "3" * 64)
    store.accept_work(
        work_id=accepted_one,
        component_attempt_id="evalcomp_attempt_0001",
        component_attempt_index=1,
        artifact_binding=old_one,
    )
    store.accept_work(
        work_id=accepted_three,
        component_attempt_id="evalcomp_attempt_0001",
        component_attempt_index=1,
        artifact_binding=old_three,
    )
    store.mark_halted(
        work_id=halted,
        component_attempt_id="evalcomp_attempt_0001",
        component_attempt_index=1,
        category="operational",
        incident_id=None,
        reason_code="implementation_bug",
    )
    plan = build_evaluation_repair_plan_v1(
        assignment=assignment,
        ledger=store.ledger,
        source_component_attempt_id="evalcomp_attempt_0001",
        source_component_attempt_index=1,
        assignment_sha256=assignment["integrity"]["assignment_sha256"],
        pre_repair_checkpoint_sha256=None,
        reason_code="implementation_repair",
        authorized_by="evaluation_server",
        authorization_id="repair_authorization_001",
        authorized_at=NOW,
        source_code_commit=SOURCE_COMMIT,
        repair_code_commit=REPAIR_COMMIT,
        affected_work_ids=[accepted_one, halted],
    )
    result_one = {
        "work_id": accepted_one,
        "previous_artifact": old_one,
        "result_artifact": _artifact(
            "chapters/01/repairs/r/reports/new.json", "2" * 64
        ),
        "report_artifact": _artifact(
            "chapters/01/repairs/r/reports/new.json", "2" * 64
        ),
        "execution_artifact": _artifact(
            "chapters/01/repairs/r/execution/new.json",
            "4" * 64,
            kind="evaluation_execution_artifact_v1",
        ),
    }
    result_two = {
        "work_id": halted,
        "previous_artifact": None,
        "result_artifact": _artifact(
            "chapters/02/repairs/r/reports/new.json", "5" * 64
        ),
        "report_artifact": _artifact(
            "chapters/02/repairs/r/reports/new.json", "5" * 64
        ),
        "execution_artifact": _artifact(
            "chapters/02/repairs/r/execution/new.json",
            "6" * 64,
            kind="evaluation_execution_artifact_v1",
        ),
    }
    receipt = build_evaluation_repair_receipt_v1(
        plan=plan,
        component_attempt_id="evalcomp_attempt_0002",
        component_attempt_index=2,
        repaired_results=[result_one, result_two],
        current_accepted_artifacts=[
            {"work_id": accepted_three, "artifact": old_three},
            {"work_id": accepted_one, "artifact": result_one["result_artifact"]},
            {"work_id": halted, "artifact": result_two["result_artifact"]},
        ],
        completed_at=NOW,
    )
    return assignment, store, plan, receipt, accepted_one, halted, accepted_three


def _reseal_plan(value):
    draft = copy.deepcopy(value)
    draft["integrity"]["plan_sha256"] = "0" * 64
    material = copy.deepcopy(draft)
    material["repair_id"] = ""
    material["integrity"]["plan_sha256"] = "0" * 64
    draft["repair_id"] = "evalrepair_" + canonical_sha256(
        material, policy=repair_module._POLICY
    )[:32]
    return seal_payload(
        draft,
        policy=repair_module._POLICY,
        hash_path=("integrity", "plan_sha256"),
    )


def _reseal_receipt(value):
    draft = copy.deepcopy(value)
    draft["integrity"]["receipt_sha256"] = "0" * 64
    return seal_payload(
        draft,
        policy=repair_module._POLICY,
        hash_path=("integrity", "receipt_sha256"),
    )


def test_repair_plan_and_receipt_bind_exact_work_partition(tmp_path: Path) -> None:
    assignment, _store, plan, receipt, accepted_one, halted, accepted_three = (
        _fixture(tmp_path)
    )
    assert validate_evaluation_repair_plan_v1(
        plan, assignment=assignment
    )["affected_work_ids"] == [accepted_one, halted]
    validated = validate_evaluation_repair_receipt_v1(receipt, plan=plan)
    assert validated["superseded_work_ids"] == [accepted_one]
    assert {
        row["work_id"] for row in validated["current_accepted_artifacts"]
    } == {accepted_one, halted, accepted_three}


def test_resealed_foreign_semantic_plan_is_rejected(tmp_path: Path) -> None:
    assignment, _store, plan, _receipt, *_ = _fixture(tmp_path)
    foreign = copy.deepcopy(plan)
    foreign["semantic_bindings"]["settings_sha256"] = "f" * 64
    foreign = _reseal_plan(foreign)
    with pytest.raises(ContractValidationError, match="semantic"):
        validate_evaluation_repair_plan_v1(foreign, assignment=assignment)


def test_receipt_rejects_duplicate_current_contribution(tmp_path: Path) -> None:
    _assignment_value, _store, plan, receipt, *_ = _fixture(tmp_path)
    duplicate = copy.deepcopy(receipt)
    duplicate["current_accepted_artifacts"].append(
        copy.deepcopy(duplicate["current_accepted_artifacts"][0])
    )
    duplicate = _reseal_receipt(duplicate)
    with pytest.raises(ContractValidationError, match="repeated|duplicate"):
        validate_evaluation_repair_receipt_v1(duplicate, plan=plan)


def test_receipt_rejects_changed_unaffected_artifact(tmp_path: Path) -> None:
    _assignment_value, _store, plan, receipt, *_ = _fixture(tmp_path)
    changed = copy.deepcopy(receipt)
    changed["unaffected_accepted_artifacts"][0]["artifact"]["sha256"] = "9" * 64
    changed = _reseal_receipt(changed)
    with pytest.raises(ContractValidationError, match="unaffected"):
        validate_evaluation_repair_receipt_v1(changed, plan=plan)


def test_plan_rejects_second_repair_of_superseded_work(tmp_path: Path) -> None:
    assignment, store, plan, _receipt, accepted_one, *_ = _fixture(tmp_path)
    store.supersede_work(
        work_id=accepted_one,
        component_attempt_id="evalcomp_attempt_0002",
        component_attempt_index=2,
        repair_id=plan["repair_id"],
        repair_plan_ref=f"recovery/repairs/{plan['repair_id']}/plan.json",
        repair_plan_sha256=plan["integrity"]["plan_sha256"],
        repair_code_commit=REPAIR_COMMIT,
        previous_artifact=plan["prior_accepted_artifacts"][0]["artifact"],
    )
    with pytest.raises(ContractValidationError, match="already superseded"):
        build_evaluation_repair_plan_v1(
            assignment=assignment,
            ledger=store.ledger,
            source_component_attempt_id="evalcomp_attempt_0002",
            source_component_attempt_index=2,
            assignment_sha256=assignment["integrity"]["assignment_sha256"],
            pre_repair_checkpoint_sha256=None,
            reason_code="implementation_repair",
            authorized_by="evaluation_server",
            authorization_id="repair_authorization_002",
            authorized_at=NOW,
            source_code_commit=REPAIR_COMMIT,
            repair_code_commit="d" * 40,
            affected_work_ids=[accepted_one],
        )


def test_repair_paths_reject_path_traversal_ids(tmp_path: Path) -> None:
    with pytest.raises(ContractValidationError, match="invalid Evaluation repair ID"):
        repair_plan_path_v1(tmp_path, "../escape")
    with pytest.raises(ContractValidationError, match="invalid Evaluation repair ID"):
        repair_receipt_path_v1(tmp_path, "repair/escape")
