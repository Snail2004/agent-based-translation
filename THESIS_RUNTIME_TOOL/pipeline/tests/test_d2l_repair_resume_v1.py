from __future__ import annotations

from copy import deepcopy

import pytest

from pipeline.prepass.d2l_repair_resume_v1 import (
    D2LRepairResumeError,
    build_repair_receipt,
    validate_repair_receipt,
)


def _receipt() -> dict:
    return build_repair_receipt(
        workflow_run_id="wf_repair",
        component_run_id="tr_repair",
        previous_component_attempt_id=2,
        stage_id="b1_candidate_discovery",
        checkpoint_ref="checkpoints/checkpoint_a2_b1.json",
        checkpoint_sha256="A" * 64,
        reason_code="json_envelope_compatibility_fix",
        baseline_code_revision="1" * 40,
        effective_code_revision="2" * 40,
        semantic_contract_sha256="B" * 64,
        runner_plan_sha256="C" * 64,
        git_delta_sha256="D" * 64,
        changed_paths=[
            "THESIS_RUNTIME_TOOL/pipeline/prepass/live.py",
            "THESIS_RUNTIME_TOOL/pipeline/tests/test_live.py",
        ],
        created_at="2026-07-25T00:00:00Z",
    )


def test_repair_receipt_is_exact_and_hash_bound() -> None:
    receipt = _receipt()
    assert validate_repair_receipt(receipt) == receipt
    assert receipt["next_component_attempt_id"] == 3

    tampered = deepcopy(receipt)
    tampered["reason_code"] = "changed"
    with pytest.raises(D2LRepairResumeError, match="payload hash drift"):
        validate_repair_receipt(tampered)


def test_repair_receipt_rejects_semantic_or_identity_ambiguity() -> None:
    same_revision = deepcopy(_receipt())
    same_revision["effective_code_revision"] = same_revision[
        "baseline_code_revision"
    ]
    with pytest.raises(D2LRepairResumeError, match="must change"):
        validate_repair_receipt(same_revision)

    unsafe_path = deepcopy(_receipt())
    unsafe_path["changed_paths"] = ["../outside.py"]
    with pytest.raises(D2LRepairResumeError, match="changed_paths"):
        validate_repair_receipt(unsafe_path)
