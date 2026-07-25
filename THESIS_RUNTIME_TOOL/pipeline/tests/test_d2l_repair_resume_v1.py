from __future__ import annotations

from copy import deepcopy

import pytest

from pipeline.prepass.d2l_console_replay_contract_v1 import canonical_sha256
from pipeline.prepass.d2l_repair_resume_v1 import (
    CHAIN_REPAIR_SCOPE_POLICY_ID,
    CHAIN_SCHEMA_VERSION,
    D2LRepairResumeError,
    LEGACY_CHAIN_REPAIR_SCOPE_POLICY_ID,
    LEGACY_CHAIN_SCHEMA_VERSION,
    LEGACY_REPAIR_SCOPE_POLICY_ID,
    LEGACY_SCHEMA_VERSION,
    REPAIR_SCOPE_POLICY_ID,
    SCHEMA_VERSION,
    build_chain_repair_receipt,
    build_repair_receipt,
    is_chain_repair_schema_version,
    validate_chain_repair_paths,
    validate_mechanical_repair_paths,
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
            (
                "THESIS_RUNTIME_TOOL/pipeline/prepass/"
                "d2l_translation_component_runner_v1.py"
            ),
            (
                "THESIS_RUNTIME_TOOL/pipeline/tests/"
                "test_d2l_translation_component_runner_v1.py"
            ),
        ],
        created_at="2026-07-25T00:00:00Z",
    )


def _reseal(value: dict) -> dict:
    value = deepcopy(value)
    value.pop("integrity", None)
    value["integrity"] = {"payload_sha256": canonical_sha256(value)}
    return value


def _legacy_receipt() -> dict:
    receipt = _receipt()
    receipt["schema_version"] = LEGACY_SCHEMA_VERSION
    receipt["repair_scope_policy_id"] = LEGACY_REPAIR_SCOPE_POLICY_ID
    return _reseal(receipt)


def _chain_receipt(*, changed_paths: list[str] | None = None) -> dict:
    return build_chain_repair_receipt(
        workflow_run_id="wf_repair",
        component_run_id="tr_repair",
        previous_component_attempt_id=5,
        stage_id="b1_candidate_discovery",
        checkpoint_ref="checkpoints/checkpoint_a5_b1.json",
        checkpoint_sha256="A" * 64,
        reason_code="runtime_infrastructure_sync",
        sealed_code_revision="1" * 40,
        baseline_code_revision="2" * 40,
        effective_code_revision="3" * 40,
        parent_repair_artifact_ref="art_component_repair_a0005",
        parent_repair_receipt_ref="runtime/repair_receipts/repair_a0005.json",
        parent_repair_receipt_sha256="B" * 64,
        parent_effective_code_revision="2" * 40,
        semantic_contract_sha256="C" * 64,
        runner_plan_sha256="D" * 64,
        git_delta_sha256="E" * 64,
        changed_paths=changed_paths
        or [
            "THESIS_RUNTIME_TOOL/app/backend/services/thesis_runs.py",
            (
                "THESIS_RUNTIME_TOOL/pipeline/prepass/"
                "d2l_translation_component_runner_v1.py"
            ),
        ],
        created_at="2026-07-25T00:00:00Z",
    )


def _legacy_chain_receipt() -> dict:
    receipt = _chain_receipt()
    receipt["schema_version"] = LEGACY_CHAIN_SCHEMA_VERSION
    receipt["repair_scope_policy_id"] = LEGACY_CHAIN_REPAIR_SCOPE_POLICY_ID
    return _reseal(receipt)


def test_repair_receipt_is_exact_and_hash_bound() -> None:
    receipt = _receipt()
    assert validate_repair_receipt(receipt) == receipt
    assert receipt["next_component_attempt_id"] == 3
    assert receipt["schema_version"] == SCHEMA_VERSION
    assert receipt["repair_scope_policy_id"] == REPAIR_SCOPE_POLICY_ID

    for field in ("reason_code", "semantic_contract_sha256", "runner_plan_sha256"):
        tampered = deepcopy(receipt)
        tampered[field] = "F" * 64 if field.endswith("sha256") else "changed"
        with pytest.raises(D2LRepairResumeError, match="payload hash drift"):
            validate_repair_receipt(tampered)


def test_repair_receipt_rejects_revision_or_path_ambiguity() -> None:
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

def test_active_policy_uses_paths_only_as_canonical_audit_evidence() -> None:
    paths = sorted([
        "README.md",
        "THESIS_RUNTIME_TOOL/app/backend/services/project_runtime.py",
        "THESIS_RUNTIME_TOOL/app/prototype/future/view.jsx",
        "THESIS_RUNTIME_TOOL/config/profiles/example.json",
        "THESIS_RUNTIME_TOOL/pipeline/translate/prompt.py",
        "THESIS_RUNTIME_TOOL/pipeline/workflow_replay/relay_v1.py",
        "THESIS_RUNTIME_TOOL/tests/example.py",
        "docs/architecture.md",
    ])

    assert validate_mechanical_repair_paths(paths) == paths
    assert validate_chain_repair_paths(paths) == paths


@pytest.mark.parametrize(
    "paths",
    [
        [],
        ["../outside.py"],
        ["/absolute.py"],
        ["C:/absolute.py"],
        [r"THESIS_RUNTIME_TOOL\app\prototype\app.jsx"],
        ["THESIS_RUNTIME_TOOL//app.jsx"],
        ["./THESIS_RUNTIME_TOOL/app.jsx"],
        ["THESIS_RUNTIME_TOOL/./app.jsx"],
        ["THESIS_RUNTIME_TOOL/app/../app.jsx"],
        ["b.py", "a.py"],
        ["a.py", "a.py"],
        ["A.py", "a.py"],
    ],
)
def test_active_policy_rejects_noncanonical_audit_paths(paths: list[str]) -> None:
    with pytest.raises(D2LRepairResumeError, match="changed_paths"):
        validate_mechanical_repair_paths(paths)
    with pytest.raises(D2LRepairResumeError, match="changed_paths"):
        validate_chain_repair_paths(paths)


def test_active_direct_and_chain_receipts_accept_arbitrary_canonical_paths() -> None:
    direct = build_repair_receipt(
        workflow_run_id="wf_repair",
        component_run_id="tr_repair",
        previous_component_attempt_id=2,
        stage_id="b1_candidate_discovery",
        checkpoint_ref="checkpoints/checkpoint_a2_b1.json",
        checkpoint_sha256="A" * 64,
        reason_code="reviewed_revision",
        baseline_code_revision="1" * 40,
        effective_code_revision="2" * 40,
        semantic_contract_sha256="B" * 64,
        runner_plan_sha256="C" * 64,
        git_delta_sha256="D" * 64,
        changed_paths=["THESIS_RUNTIME_TOOL/pipeline/translate/prompt.py"],
        created_at="2026-07-25T00:00:00Z",
    )
    chain = _chain_receipt(
        changed_paths=["THESIS_RUNTIME_TOOL/config/new_profile.json"]
    )
    assert validate_repair_receipt(direct) == direct
    assert validate_repair_receipt(chain) == chain
    assert chain["schema_version"] == CHAIN_SCHEMA_VERSION
    assert chain["repair_scope_policy_id"] == CHAIN_REPAIR_SCOPE_POLICY_ID
    assert is_chain_repair_schema_version(chain["schema_version"])


def test_legacy_receipts_remain_byte_compatible_and_keep_closed_scope() -> None:
    direct = _legacy_receipt()
    chain = _legacy_chain_receipt()
    assert validate_repair_receipt(direct) == direct
    assert validate_repair_receipt(chain) == chain
    assert is_chain_repair_schema_version(LEGACY_CHAIN_SCHEMA_VERSION)

    legacy_direct_drift = deepcopy(direct)
    legacy_direct_drift["changed_paths"] = [
        "THESIS_RUNTIME_TOOL/pipeline/translate/prompt.py"
    ]
    legacy_direct_drift = _reseal(legacy_direct_drift)
    with pytest.raises(D2LRepairResumeError, match="closed mechanical scope"):
        validate_repair_receipt(legacy_direct_drift)

    legacy_chain_drift = deepcopy(chain)
    legacy_chain_drift["changed_paths"] = [
        "THESIS_RUNTIME_TOOL/pipeline/translate/prompt.py"
    ]
    legacy_chain_drift = _reseal(legacy_chain_drift)
    with pytest.raises(D2LRepairResumeError, match="closed chained scope"):
        validate_repair_receipt(legacy_chain_drift)


@pytest.mark.parametrize(
    "unknown_schema",
    [
        "d2l_component_repair_receipt_v2_unknown",
        "d2l_component_repair_receipt_v5_unknown",
    ],
)
def test_unknown_repair_schema_or_policy_is_rejected(
    unknown_schema: str,
) -> None:
    unknown = deepcopy(_chain_receipt())
    unknown["schema_version"] = unknown_schema
    unknown = _reseal(unknown)
    with pytest.raises(D2LRepairResumeError, match="schema is invalid"):
        validate_repair_receipt(unknown)

    wrong_policy = deepcopy(_chain_receipt())
    wrong_policy["repair_scope_policy_id"] = LEGACY_CHAIN_REPAIR_SCOPE_POLICY_ID
    wrong_policy = _reseal(wrong_policy)
    with pytest.raises(D2LRepairResumeError, match="scope policy is invalid"):
        validate_repair_receipt(wrong_policy)


def test_existing_reviewed_paths_remain_valid_audit_evidence() -> None:
    paths = sorted([
        "THESIS_RUNTIME_TOOL/app/backend/services/project_runtime.py",
        "THESIS_RUNTIME_TOOL/app/backend/tests/test_project_runtime.py",
        "THESIS_RUNTIME_TOOL/pipeline/prepass/d2l_project_campaign_v2.py",
        "THESIS_RUNTIME_TOOL/pipeline/prepass/d2l_repair_resume_v1.py",
        "THESIS_RUNTIME_TOOL/pipeline/prepass/d2l_translation_component_runner_v1.py",
        "THESIS_RUNTIME_TOOL/pipeline/scripts/run_d2l_project_campaign.py",
        "THESIS_RUNTIME_TOOL/pipeline/tests/test_d2l_project_campaign_v2.py",
        "THESIS_RUNTIME_TOOL/pipeline/tests/test_d2l_repair_resume_v1.py",
        "THESIS_RUNTIME_TOOL/pipeline/tests/test_d2l_translation_component_runner_v1.py",
    ])

    assert validate_mechanical_repair_paths(paths) == paths
    assert validate_chain_repair_paths(paths) == paths

    receipt = _chain_receipt()
    tampered = deepcopy(receipt)
    tampered["parent_repair_artifact_ref"] = "foreign"
    with pytest.raises(D2LRepairResumeError, match="payload hash drift"):
        validate_repair_receipt(tampered)
