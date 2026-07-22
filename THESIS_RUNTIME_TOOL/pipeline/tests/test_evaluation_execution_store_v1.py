from __future__ import annotations

import copy
import json
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
from pipeline.eval.execution_runner_v1 import (
    execute_evaluation_plan_v1,
    seal_evaluation_execution_artifact,
)
from pipeline.eval.execution_store_v1 import (
    load_evaluation_execution_bundle_v1,
    persist_evaluation_execution_bundle_v1,
    seal_evaluation_execution_bundle_manifest,
    validate_evaluation_execution_bundle_manifest,
)
from pipeline.eval.offline_orchestrator_v1 import seal_evaluation_run_config


NOW = "2026-07-19T00:00:00Z"
COMMIT = "b" * 40


def _common() -> CommonEvaluationInputV1:
    block = CommonBlockV1(
        "b001", "ch1", 1, "paragraph", "English source.", "translate"
    )
    arm = CommonArmV1(
        artifact_id="artifact-final",
        artifact_sha256="1" * 64,
        logical_run_id="logical-run",
        attempt_run_id="translation-attempt",
        arm_id="final",
        profile_id="translation-profile",
        profile_config_sha256="2" * 64,
        source_language="en",
        target_language="vi",
    )
    return CommonEvaluationInputV1(
        source_schema_id="D2LEvaluationInputV1",
        source_schema_version="1.0.0",
        source_binding=LegacyD2LSourceBindingV1(
            project_id="project",
            document_id="document",
            source_db_sha256="3" * 64,
            runtime_manifest_sha256="4" * 64,
        ),
        blocks=(block,),
        arms=(arm,),
        translations=(
            CommonTranslationV1(
                arm_id="final",
                block_id="b001",
                status="translated",
                target_text="Bản dịch.",
                error_code=None,
            ),
        ),
    )


def _config(common: CommonEvaluationInputV1, *, config_id: str = "store-test") -> dict:
    arm = common.arms[0]
    return seal_evaluation_run_config(
        {
            "schema_id": "EvaluationRunConfigV1",
            "schema_version": "1.0.0",
            "config_id": config_id,
            "created_at": NOW,
            "producer": {
                "workstream": "evaluation",
                "component": "execution_store_test",
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
                ],
            },
            "methods": [
                {
                    "method_id": "sf_qe",
                    "method_version": "store-v1",
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


def _payloads(*, config_id: str = "store-test") -> tuple[dict, dict]:
    common = _common()
    config = _config(common, config_id=config_id)
    execution = execute_evaluation_plan_v1(
        common,
        config,
        lambda _packet: {
            "status": "succeeded",
            "semantic_output": {"score": 80.0},
            "error_code": None,
        },
        created_at=NOW,
        runner_code_commit=COMMIT,
    )
    return config, execution


def test_atomic_bundle_persistence_is_content_addressed_and_idempotent(tmp_path: Path) -> None:
    config, execution = _payloads()
    config_before = copy.deepcopy(config)
    execution_before = copy.deepcopy(execution)
    first = persist_evaluation_execution_bundle_v1(
        output_root=tmp_path / "run",
        config_payload=config,
        execution_payload=execution,
        created_at=NOW,
        producer_code_commit=COMMIT,
    )
    first_bytes = {
        path: path.read_bytes()
        for path in (first.manifest_path, first.config_path, first.execution_path)
    }
    second = persist_evaluation_execution_bundle_v1(
        output_root=tmp_path / "run",
        config_payload=config,
        execution_payload=execution,
        created_at=NOW,
        producer_code_commit=COMMIT,
    )
    assert first.reused is False
    assert second.reused is True
    assert second.manifest == first.manifest
    assert all(path.read_bytes() == data for path, data in first_bytes.items())
    assert config == config_before
    assert execution == execution_before
    assert list((tmp_path / "run").rglob("*.tmp")) == []


def test_load_revalidates_every_artifact_and_binding(tmp_path: Path) -> None:
    config, execution = _payloads()
    persisted = persist_evaluation_execution_bundle_v1(
        output_root=tmp_path / "run",
        config_payload=config,
        execution_payload=execution,
        created_at=NOW,
        producer_code_commit=COMMIT,
    )
    loaded = load_evaluation_execution_bundle_v1(output_root=tmp_path / "run")
    assert loaded.config == persisted.config
    assert loaded.execution == persisted.execution
    assert loaded.manifest == persisted.manifest


def test_interrupted_pre_manifest_write_resumes_without_overwriting(tmp_path: Path) -> None:
    config, execution = _payloads()
    config_path = (
        tmp_path
        / "run"
        / "config"
        / f"{config['integrity']['config_sha256']}.json"
    )
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    persisted = persist_evaluation_execution_bundle_v1(
        output_root=tmp_path / "run",
        config_payload=config,
        execution_payload=execution,
        created_at=NOW,
        producer_code_commit=COMMIT,
    )
    assert persisted.reused is False
    assert persisted.manifest_path.exists()
    assert persisted.execution_path.exists()


def test_noncanonical_existing_bytes_are_not_accepted_as_same_artifact(
    tmp_path: Path,
) -> None:
    config, execution = _payloads()
    config_path = (
        tmp_path
        / "run"
        / "config"
        / f"{config['integrity']['config_sha256']}.json"
    )
    config_path.parent.mkdir(parents=True)
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    with pytest.raises(ContractValidationError, match="canonical requested bytes"):
        persist_evaluation_execution_bundle_v1(
            output_root=tmp_path / "run",
            config_payload=config,
            execution_payload=execution,
            created_at=NOW,
            producer_code_commit=COMMIT,
        )


def test_tampered_persisted_execution_fails_closed(tmp_path: Path) -> None:
    config, execution = _payloads()
    persisted = persist_evaluation_execution_bundle_v1(
        output_root=tmp_path / "run",
        config_payload=config,
        execution_payload=execution,
        created_at=NOW,
        producer_code_commit=COMMIT,
    )
    tampered = json.loads(persisted.execution_path.read_text(encoding="utf-8"))
    tampered["coverage"]["succeeded_job_count"] = 0
    persisted.execution_path.write_text(
        json.dumps(tampered), encoding="utf-8", newline="\n"
    )
    with pytest.raises(ContractValidationError):
        load_evaluation_execution_bundle_v1(output_root=tmp_path / "run")


def test_foreign_config_execution_pair_is_rejected_before_writing(tmp_path: Path) -> None:
    config, _ = _payloads(config_id="config-a")
    _, execution = _payloads(config_id="config-b")
    with pytest.raises(ContractValidationError, match="config_binding"):
        persist_evaluation_execution_bundle_v1(
            output_root=tmp_path / "run",
            config_payload=config,
            execution_payload=execution,
            created_at=NOW,
            producer_code_commit=COMMIT,
        )
    assert not (tmp_path / "run").exists()


def test_resealed_manifest_cannot_redirect_content_addressed_path(tmp_path: Path) -> None:
    config, execution = _payloads()
    persisted = persist_evaluation_execution_bundle_v1(
        output_root=tmp_path / "run",
        config_payload=config,
        execution_payload=execution,
        created_at=NOW,
        producer_code_commit=COMMIT,
    )
    drift = copy.deepcopy(persisted.manifest)
    drift["artifacts"]["execution"]["relative_path"] = "execution/other.json"
    drift = seal_evaluation_execution_bundle_manifest(drift)
    with pytest.raises(ContractValidationError, match="content-addressed"):
        validate_evaluation_execution_bundle_manifest(drift)


def test_existing_immutable_artifact_conflict_is_not_replaced(tmp_path: Path) -> None:
    config, execution = _payloads()
    persisted = persist_evaluation_execution_bundle_v1(
        output_root=tmp_path / "run",
        config_payload=config,
        execution_payload=execution,
        created_at=NOW,
        producer_code_commit=COMMIT,
    )
    original = persisted.execution_path.read_bytes()
    conflicting = copy.deepcopy(execution)
    conflicting["producer"]["component"] = "execution_runner_v1_fixture_variant"
    conflicting = seal_evaluation_execution_artifact(conflicting)
    conflicting_path = (
        tmp_path
        / "run"
        / "execution"
        / f"{conflicting['integrity']['artifact_sha256']}.json"
    )
    conflicting_path.write_text("{}\n", encoding="utf-8", newline="\n")
    with pytest.raises(ContractValidationError, match="immutable_conflict"):
        persist_evaluation_execution_bundle_v1(
            output_root=tmp_path / "run",
            config_payload=config,
            execution_payload=conflicting,
            created_at=NOW,
            producer_code_commit=COMMIT,
        )
    assert persisted.execution_path.read_bytes() == original


def test_committed_manifest_rejects_another_execution_without_orphan_write(
    tmp_path: Path,
) -> None:
    config, execution = _payloads()
    persisted = persist_evaluation_execution_bundle_v1(
        output_root=tmp_path / "run",
        config_payload=config,
        execution_payload=execution,
        created_at=NOW,
        producer_code_commit=COMMIT,
    )
    another = copy.deepcopy(execution)
    another["producer"]["component"] = "execution_runner_v1_second_execution"
    another = seal_evaluation_execution_artifact(another)
    another_path = (
        tmp_path
        / "run"
        / "execution"
        / f"{another['integrity']['artifact_sha256']}.json"
    )
    with pytest.raises(ContractValidationError, match="immutable_conflict"):
        persist_evaluation_execution_bundle_v1(
            output_root=tmp_path / "run",
            config_payload=config,
            execution_payload=another,
            created_at=NOW,
            producer_code_commit=COMMIT,
        )
    assert persisted.manifest_path.exists()
    assert not another_path.exists()
