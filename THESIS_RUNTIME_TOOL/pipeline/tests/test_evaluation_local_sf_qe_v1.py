from __future__ import annotations

import copy
from datetime import datetime, timezone
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
from pipeline.eval.execution_runner_v1 import execute_evaluation_plan_v1
from pipeline.eval.local_sf_qe_v1 import (
    SF_QE_MODEL_ID,
    SF_QE_REPORT_TRANSFORM_ID,
    load_local_sf_qe_evidence_v1,
    persist_local_sf_qe_evidence_v1,
    prepare_local_sf_qe_v1,
    seal_local_sf_qe_evidence_v1,
    validate_local_sf_qe_evidence_v1,
)
from pipeline.eval.offline_orchestrator_v1 import seal_evaluation_run_config
from pipeline.eval.method_executors_v1 import EvaluationMethodExecutorV1


NOW = "2026-07-19T00:00:00Z"
COMMIT = "a" * 40


def _common() -> CommonEvaluationInputV1:
    blocks = (
        CommonBlockV1("b001", "ch1", 1, "paragraph", "Source one.", "translate"),
        CommonBlockV1("b002", "ch1", 2, "paragraph", "Source two.", "translate"),
    )
    arms = (
        CommonArmV1(
            "artifact-s0", "1" * 64, "run", "attempt-s0", "S0", "profile-s0", "3" * 64, "en", "vi"
        ),
        CommonArmV1(
            "artifact-s1", "2" * 64, "run", "attempt-s1", "S1", "profile-s1", "4" * 64, "en", "vi"
        ),
    )
    translations = tuple(
        CommonTranslationV1(
            arm_id=arm.arm_id,
            block_id=block.block_id,
            status="translated",
            target_text=f"Target {arm.arm_id} {block.block_id}.",
            error_code=None,
        )
        for block in blocks
        for arm in arms
    )
    return CommonEvaluationInputV1(
        source_schema_id="D2LEvaluationInputV1",
        source_schema_version="1.0.0",
        source_binding=LegacyD2LSourceBindingV1(
            project_id="project",
            document_id="document",
            source_db_sha256="5" * 64,
            runtime_manifest_sha256="6" * 64,
        ),
        blocks=blocks,
        arms=arms,
        translations=translations,
    )


def _config(common: CommonEvaluationInputV1) -> dict:
    return seal_evaluation_run_config(
        {
            "schema_id": "EvaluationRunConfigV1",
            "schema_version": "1.0.0",
            "config_id": "local-sf-qe-test",
            "created_at": NOW,
            "producer": {
                "workstream": "evaluation",
                "component": "local_sf_qe_test",
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
                "context_before_blocks": 1,
                "context_after_blocks": 1,
            },
            "blinding": {"mode": "opaque_counterbalanced", "seed": "seed"},
            "retry_policy": {"max_transport_attempts": 1},
            "integrity": {"config_sha256": "0" * 64},
        }
    )


class _Clock:
    def __init__(self) -> None:
        self.values = iter(
            (
                datetime(2026, 7, 19, 0, 0, 0, tzinfo=timezone.utc),
                datetime(2026, 7, 19, 0, 0, 1, tzinfo=timezone.utc),
            )
        )

    def __call__(self) -> datetime:
        return next(self.values)


class _Timer:
    def __init__(self) -> None:
        self.values = iter((10.0, 10.25))

    def __call__(self) -> float:
        return next(self.values)


def _prepare(*, predictor=None):
    common = _common()
    config = _config(common)
    seen = []

    def default_predictor(rows, batch_size):
        seen.extend(copy.deepcopy(rows))
        assert batch_size == 8
        return [0.70, 0.80, 0.75, 0.95]

    prepared = prepare_local_sf_qe_v1(
        common,
        config,
        predictor or default_predictor,
        created_at=NOW,
        producer_code_commit=COMMIT,
        checkpoint_sha256="7" * 64,
        package_name="unbabel-comet",
        package_version="2.2.7",
        device="cpu",
        batch_size=8,
        clock=_Clock(),
        monotonic=_Timer(),
    )
    return common, config, prepared, seen


def test_batch_adapter_exposes_only_source_and_mt_and_scales_explicitly() -> None:
    common = _common()
    config = _config(common)
    common_before = copy.deepcopy(common)
    config_before = copy.deepcopy(config)
    common, config, prepared, seen = _prepare()

    assert common == common_before
    assert config == config_before
    assert len(seen) == 4
    assert all(set(row) == {"src", "mt"} for row in seen)
    assert all("S0" not in row["src"] and "S1" not in row["src"] for row in seen)
    assert prepared.evidence["model"]["model_id"] == SF_QE_MODEL_ID
    assert prepared.evidence["model"]["score_transform_id"] == SF_QE_REPORT_TRANSFORM_ID
    assert [row["native_score"] for row in prepared.evidence["rows"]] == [0.70, 0.80, 0.75, 0.95]
    assert [row["report_score_0_100"] for row in prepared.evidence["rows"]] == [70.0, 80.0, 75.0, 95.0]
    assert prepared.evidence["metering"] == {
        "started_at": "2026-07-19T00:00:00.000Z",
        "ended_at": "2026-07-19T00:00:01.000Z",
        "duration_ms": 250,
        "batch_call_count": 1,
        "item_count": 4,
    }


def test_prepared_scorer_requires_plan_order_and_exact_cover() -> None:
    _, _, prepared, seen = _prepare()
    with pytest.raises(ContractValidationError, match="next sealed plan row"):
        prepared(seen[1]["src"], seen[1]["mt"])

    _, _, prepared, seen = _prepare()
    for row, expected in zip(seen, (70.0, 80.0, 75.0, 95.0)):
        assert prepared(row["src"], row["mt"]) == expected
    prepared.assert_exact_cover()
    with pytest.raises(ContractValidationError, match="extra request"):
        prepared(seen[0]["src"], seen[0]["mt"])

    _, _, incomplete, seen = _prepare()
    incomplete(seen[0]["src"], seen[0]["mt"])
    with pytest.raises(ContractValidationError, match="not every sealed"):
        incomplete.assert_exact_cover()


def test_prepared_batch_drives_the_existing_execution_runner() -> None:
    common, config, prepared, _ = _prepare()
    executor = EvaluationMethodExecutorV1(
        common_input=common,
        config_payload=config,
        sf_qe_scorer=prepared,
        llm_roles=None,  # The sealed config contains no provider-backed method.
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
    aggregate = execution["aggregates"][0]
    assert aggregate["unit"] == "score_0_100"
    assert aggregate["arm_values"] == [
        {
            "arm_id": "S0",
            "value": 72.5,
            "numerator": 145.0,
            "denominator": 2,
            "expected_count": 2,
            "observed_count": 2,
            "missing_count": 0,
        },
        {
            "arm_id": "S1",
            "value": 87.5,
            "numerator": 175.0,
            "denominator": 2,
            "expected_count": 2,
            "observed_count": 2,
            "missing_count": 0,
        },
    ]


@pytest.mark.parametrize(
    ("scores", "error"),
    [
        ([0.1], "result count"),
        ([0.1, 0.2, 0.3, float("nan")], "within \\[0, 1\\]"),
        ([0.1, 0.2, 0.3, 1.01], "within \\[0, 1\\]"),
        ([0.1, 0.2, 0.3, True], "must be numeric"),
    ],
)
def test_predictor_failures_do_not_become_scores(scores, error) -> None:
    with pytest.raises(ContractValidationError, match=error):
        _prepare(predictor=lambda _rows, _batch: scores)


def test_contract_rejects_unknown_key_transform_tamper_and_reseal() -> None:
    _, _, prepared, _ = _prepare()
    unknown = copy.deepcopy(prepared.evidence)
    unknown["answer"] = 100
    with pytest.raises(ContractValidationError, match="unknown keys"):
        validate_local_sf_qe_evidence_v1(unknown)

    tampered = copy.deepcopy(prepared.evidence)
    tampered["rows"][0]["report_score_0_100"] = 99.0
    with pytest.raises(ContractValidationError, match="exactly equal"):
        validate_local_sf_qe_evidence_v1(tampered)

    resealed = copy.deepcopy(prepared.evidence)
    resealed["rows"][0]["native_score"] = 0.1
    resealed["rows"][0]["report_score_0_100"] = 10.0
    resealed = seal_local_sf_qe_evidence_v1(resealed)
    with pytest.raises(ContractValidationError, match="artifact ID differs"):
        validate_local_sf_qe_evidence_v1(resealed)


def test_persistence_is_canonical_immutable_and_strict(tmp_path: Path) -> None:
    _, _, prepared, _ = _prepare()
    first = persist_local_sf_qe_evidence_v1(
        output_root=tmp_path, evidence_payload=prepared.evidence
    )
    second = persist_local_sf_qe_evidence_v1(
        output_root=tmp_path, evidence_payload=prepared.evidence
    )
    assert first.reused is False
    assert second.reused is True
    assert first.path == second.path
    assert first.path.read_bytes().endswith(b"\n")
    assert load_local_sf_qe_evidence_v1(first.path) == prepared.evidence

    first.path.write_text('{"schema_id":"x","schema_id":"y"}\n', encoding="utf-8")
    with pytest.raises(ContractValidationError, match="strict JSON"):
        load_local_sf_qe_evidence_v1(first.path)


def test_contract_rejects_nonfinite_json_even_if_parser_is_permissive(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"value": "placeholder"}).replace('"placeholder"', "NaN"), encoding="utf-8")
    with pytest.raises(ContractValidationError, match="strict JSON"):
        load_local_sf_qe_evidence_v1(path)
