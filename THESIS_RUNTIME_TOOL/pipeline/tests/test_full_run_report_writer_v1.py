from __future__ import annotations

import copy
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
from pipeline.eval.execution_store_v1 import persist_evaluation_execution_bundle_v1
from pipeline.eval.full_run_report_v1 import seal_full_run_report
from pipeline.eval.full_run_report_writer_v1 import (
    compose_full_run_report_v1,
    persist_full_run_report_v1,
)
from pipeline.eval.offline_orchestrator_v1 import seal_evaluation_run_config


NOW = "2026-07-19T15:00:00Z"
COMMIT = "c" * 40


def _common(arm_ids: tuple[str, ...]) -> CommonEvaluationInputV1:
    blocks = (
        CommonBlockV1("b1", "ch1", 1, "paragraph", "Source one.", "translate"),
        CommonBlockV1("b2", "ch1", 2, "paragraph", "Source two.", "translate"),
    )
    arms = tuple(
        CommonArmV1(
            artifact_id=f"translation-{arm_id.lower()}",
            artifact_sha256=(str(index + 1) * 64),
            logical_run_id="translation-logical-run",
            attempt_run_id=f"translation-attempt-{arm_id.lower()}",
            arm_id=arm_id,
            profile_id="translation-profile",
            profile_config_sha256="9" * 64,
            source_language="en",
            target_language="vi",
        )
        for index, arm_id in enumerate(arm_ids)
    )
    translations = tuple(
        CommonTranslationV1(
            arm_id=arm.arm_id,
            block_id=block.block_id,
            status="translated",
            target_text=f"{arm.arm_id} translation {block.block_id}.",
            error_code=None,
        )
        for arm in arms
        for block in blocks
    )
    return CommonEvaluationInputV1(
        source_schema_id="D2LEvaluationInputV1",
        source_schema_version="1.0.0",
        source_binding=LegacyD2LSourceBindingV1(
            project_id="project",
            document_id="document",
            source_db_sha256="7" * 64,
            runtime_manifest_sha256="8" * 64,
        ),
        blocks=blocks,
        arms=arms,
        translations=translations,
    )


def _config(common: CommonEvaluationInputV1, methods: tuple[str, ...]) -> dict:
    pairwise = "pj" in methods
    return seal_evaluation_run_config(
        {
            "schema_id": "EvaluationRunConfigV1",
            "schema_version": "1.0.0",
            "config_id": "report-writer-config",
            "created_at": NOW,
            "producer": {
                "workstream": "evaluation",
                "component": "report_writer_test",
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
                    "method_id": method_id,
                    "method_version": "1.0.0",
                    "scorer_kind": "pairwise" if method_id == "pj" else "unary",
                    "profile_scope": "common",
                    "eligible_admissions": ["translate"],
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
                "context_before_blocks": 0,
                "context_after_blocks": 0,
            },
            "blinding": {"mode": "opaque_counterbalanced", "seed": "seed"},
            "retry_policy": {"max_transport_attempts": 1},
            "integrity": {"config_sha256": "0" * 64},
        }
    )


def _executor(packet: dict) -> dict:
    method_id = packet["binding"]["method_id"]
    if method_id == "sf_qe":
        active = next(
            row["text"]
            for row in packet["candidates"][0]["blocks"]
            if row["role"] == "active"
        )
        score = 90.0 if active.startswith("S1") else 60.0
        output = {"score": score}
    else:
        slot = next(
            index
            for index, candidate in enumerate(packet["candidates"])
            if next(
                row["text"]
                for row in candidate["blocks"]
                if row["role"] == "active"
            ).startswith("S1")
        )
        output = {
            "overall_verdict": f"candidate_{slot + 1}",
            "style_verdict": "tie",
            "tags": ["meaning"],
            "note": "S1 preserves more source detail.",
        }
    return {"status": "succeeded", "semantic_output": output, "error_code": None}


def _execution(
    common: CommonEvaluationInputV1, config: dict, methods: tuple[str, ...]
) -> dict:
    comparative = len(common.arms) == 2
    return execute_evaluation_plan_v1(
        common,
        config,
        _executor,
        created_at=NOW,
        runner_code_commit=COMMIT,
        baseline_arm_id="S0" if comparative else None,
        candidate_arm_id="S1" if comparative else None,
    )


def _arm_presentations(common: CommonEvaluationInputV1) -> list[dict]:
    result = []
    for arm in common.arms:
        role = "baseline" if arm.arm_id == "S0" else "candidate"
        result.append(
            {
                "arm_id": arm.arm_id,
                "role": role,
                "kind": "system",
                "label": f"{arm.arm_id} translation",
                "relative_path": f"translations/{arm.arm_id.lower()}.json",
            }
        )
    return result


def _method_presentations(methods: tuple[str, ...]) -> list[dict]:
    names = {
        "sf_qe": "Semantic fidelity QE",
        "pj": "Pairwise judgment",
    }
    return [
        {
            "display_name": names[method_id],
            "method": {
                "method_id": method_id,
                "method_version": "1.0.0",
                "implementation_commit": COMMIT,
                "prompt_version": None if method_id == "sf_qe" else "pj_prompt_v3",
                "model_id": None,
            },
        }
        for method_id in methods
    ]


def _empty_usage(methods: tuple[str, ...]) -> dict:
    null_totals = {
        "request_count": None,
        "successful_request_count": None,
        "failed_request_count": None,
        "input_tokens": None,
        "cached_input_tokens": None,
        "output_tokens": None,
        "reasoning_tokens": None,
        "thought_tokens": None,
        "total_tokens": None,
        "cost_usd": None,
        "currency": None,
    }
    return {
        "status": "unavailable",
        "accounting_basis": "unavailable",
        "totals": dict(null_totals),
        "unknown_attempt_count": 0,
        "by_stage": [
            {
                "stage_id": method_id,
                "provider": None,
                "model_id": None,
                "quota_bucket_id": None,
                "credential_family": None,
                "accounting_basis": "unavailable",
                "status": "unavailable",
                **null_totals,
            }
            for method_id in methods
        ],
        "notes": ["No persisted provider usage projection was supplied."],
        "source_artifact_ids": [],
    }


def _available_usage() -> dict:
    return {
        "status": "partial",
        "accounting_basis": "provider_reported",
        "totals": {
            "request_count": 2,
            "successful_request_count": 2,
            "failed_request_count": 0,
            "input_tokens": 101,
            "cached_input_tokens": None,
            "output_tokens": 23,
            "reasoning_tokens": None,
            "thought_tokens": None,
            "total_tokens": 124,
            "cost_usd": None,
            "currency": None,
        },
        "unknown_attempt_count": 0,
        "by_stage": [
            {
                "stage_id": "sf_qe",
                "provider": "local",
                "model_id": "local-scorer",
                "quota_bucket_id": "local-evaluation",
                "credential_family": "none",
                "accounting_basis": "provider_reported",
                "status": "partial",
                "request_count": 2,
                "successful_request_count": 2,
                "failed_request_count": 0,
                "input_tokens": 101,
                "cached_input_tokens": None,
                "output_tokens": 23,
                "reasoning_tokens": None,
                "thought_tokens": None,
                "total_tokens": 124,
                "cost_usd": None,
                "currency": None,
            }
        ],
        "notes": ["Cost was not reported and remains unknown."],
        "source_artifact_ids": ["usage-ledger"],
    }


def _compose(
    common: CommonEvaluationInputV1,
    config: dict,
    execution: dict,
    methods: tuple[str, ...],
    **overrides,
) -> dict:
    args = {
        "generated_at": NOW,
        "producer_code_commit": COMMIT,
        "evaluation_logical_run_id": "evaluation-logical-run",
        "evaluation_attempt_run_id": "evaluation-attempt",
        "evaluation_profile_id": "evaluation-profile",
        "policy_profile_id": None,
        "input_artifact": {
            "artifact_id": "evaluation-input",
            "relative_path": "input/evaluation_input.json",
            "sha256": "6" * 64,
        },
        "arm_presentations": _arm_presentations(common),
        "method_presentations": _method_presentations(methods),
        "stage_facts": [
            {
                "stage_id": method_id,
                "method_id": method_id,
                "status": "complete",
                "started_at": None,
                "ended_at": None,
                "duration_ms": None,
                "attempt_run_id": "evaluation-attempt",
                "error_code": None,
            }
            for method_id in methods
        ],
        "usage_payload": _empty_usage(methods),
        "usage_artifacts": [],
        "caveats": [],
    }
    args.update(overrides)
    return compose_full_run_report_v1(common, config, execution, **args)


def test_one_arm_report_is_inspection_only_and_inputs_are_immutable() -> None:
    common = _common(("S1",))
    methods = ("sf_qe",)
    config = _config(common, methods)
    execution = _execution(common, config, methods)
    config_before = copy.deepcopy(config)
    execution_before = copy.deepcopy(execution)
    report = _compose(common, config, execution, methods)
    assert report["report_state"] == "complete"
    assert report["metrics"][0]["arm_values"][0]["value"] == 90.0
    assert report["claim"] == {
        "status": "not_applicable",
        "verdict": "NOT_APPLICABLE",
        "method_id": "claim_gate",
        "method_version": "1.0.0",
        "reason_codes": ["single_arm"],
        "source_metric_ids": ["sf_qe"],
    }
    assert config == config_before
    assert execution == execution_before


def test_two_arm_report_keeps_candidate_advantage_inconclusive() -> None:
    common = _common(("S0", "S1"))
    methods = ("sf_qe", "pj")
    config = _config(common, methods)
    execution = _execution(common, config, methods)
    report = _compose(common, config, execution, methods)
    metrics = {row["metric_id"]: row for row in report["metrics"]}
    assert metrics["sf_qe"]["comparison"]["delta"] == 30.0
    assert metrics["pj"]["comparison"]["wins"] == 2
    assert report["claim"]["verdict"] == "INCONCLUSIVE"
    assert report["claim"]["reason_codes"] == ["claim_policy_not_frozen"]


def test_arm_presentations_must_exact_cover_common_arms() -> None:
    common = _common(("S0", "S1"))
    methods = ("sf_qe",)
    config = _config(common, methods)
    execution = _execution(common, config, methods)
    with pytest.raises(ContractValidationError, match="arm_exact_cover"):
        _compose(
            common,
            config,
            execution,
            methods,
            arm_presentations=_arm_presentations(common)[:1],
        )


def test_method_metadata_must_match_execution_version() -> None:
    common = _common(("S1",))
    methods = ("sf_qe",)
    config = _config(common, methods)
    execution = _execution(common, config, methods)
    presentations = _method_presentations(methods)
    presentations[0]["method"]["method_version"] = "foreign"
    with pytest.raises(ContractValidationError, match="method_version"):
        _compose(
            common,
            config,
            execution,
            methods,
            method_presentations=presentations,
        )


def test_foreign_common_input_with_same_arm_id_cannot_reuse_execution() -> None:
    common = _common(("S1",))
    methods = ("sf_qe",)
    config = _config(common, methods)
    execution = _execution(common, config, methods)
    foreign = copy.deepcopy(common)
    foreign_arm = foreign.arms[0]
    foreign = CommonEvaluationInputV1(
        source_schema_id=foreign.source_schema_id,
        source_schema_version=foreign.source_schema_version,
        source_binding=foreign.source_binding,
        blocks=foreign.blocks,
        arms=(
            CommonArmV1(
                artifact_id=foreign_arm.artifact_id,
                artifact_sha256="f" * 64,
                logical_run_id=foreign_arm.logical_run_id,
                attempt_run_id=foreign_arm.attempt_run_id,
                arm_id=foreign_arm.arm_id,
                profile_id=foreign_arm.profile_id,
                profile_config_sha256=foreign_arm.profile_config_sha256,
                source_language=foreign_arm.source_language,
                target_language=foreign_arm.target_language,
            ),
        ),
        translations=foreign.translations,
    )
    with pytest.raises(ContractValidationError):
        _compose(foreign, config, execution, methods)


def test_usage_stages_must_exact_cover_execution_methods() -> None:
    common = _common(("S1",))
    methods = ("sf_qe",)
    config = _config(common, methods)
    execution = _execution(common, config, methods)
    usage = _empty_usage(methods)
    usage["by_stage"] = []
    with pytest.raises(ContractValidationError, match="usage_stage_exact_cover"):
        _compose(common, config, execution, methods, usage_payload=usage)


def test_persisted_usage_facts_and_unknown_cost_are_preserved() -> None:
    common = _common(("S1",))
    methods = ("sf_qe",)
    config = _config(common, methods)
    execution = _execution(common, config, methods)
    usage = _available_usage()
    usage_before = copy.deepcopy(usage)
    methods_with_model = _method_presentations(methods)
    methods_with_model[0]["method"]["model_id"] = "local-scorer"
    report = _compose(
        common,
        config,
        execution,
        methods,
        method_presentations=methods_with_model,
        usage_payload=usage,
        usage_artifacts=[
            {
                "artifact_id": "usage-ledger",
                "relative_path": "usage/ledger.json",
                "sha256": "d" * 64,
            }
        ],
    )
    assert report["usage"]["totals"]["input_tokens"] == 101
    assert report["usage"]["totals"]["total_tokens"] == 124
    assert report["usage"]["totals"]["cost_usd"] is None
    assert report["integrity"]["source_usage_artifact_ids"] == ["usage-ledger"]
    assert usage == usage_before


def test_available_usage_without_persisted_ledger_is_rejected() -> None:
    common = _common(("S1",))
    methods = ("sf_qe",)
    config = _config(common, methods)
    execution = _execution(common, config, methods)
    usage = _available_usage()
    usage["source_artifact_ids"] = []
    with pytest.raises(ContractValidationError, match="usage_provenance"):
        _compose(common, config, execution, methods, usage_payload=usage)


def test_declared_method_model_must_match_unique_usage_evidence() -> None:
    common = _common(("S1",))
    methods = ("sf_qe",)
    config = _config(common, methods)
    execution = _execution(common, config, methods)
    methods_with_wrong_model = _method_presentations(methods)
    methods_with_wrong_model[0]["method"]["model_id"] = "wrong-model"
    with pytest.raises(ContractValidationError, match="method_model_evidence"):
        _compose(
            common,
            config,
            execution,
            methods,
            method_presentations=methods_with_wrong_model,
            usage_payload=_available_usage(),
            usage_artifacts=[
                {
                    "artifact_id": "usage-ledger",
                    "relative_path": "usage/ledger.json",
                    "sha256": "d" * 64,
                }
            ],
        )


def test_one_method_can_publish_multiple_usage_stages_without_fake_model_id() -> None:
    common = _common(("S1",))
    methods = ("sf_qe",)
    config = _config(common, methods)
    execution = _execution(common, config, methods)
    usage = _available_usage()
    first = usage["by_stage"][0]
    first["stage_id"] = "sf_qe.extract"
    first["model_id"] = "model-a"
    second = copy.deepcopy(first)
    second["stage_id"] = "sf_qe.judge"
    second["model_id"] = "model-b"
    usage["by_stage"] = [first, second]
    report = _compose(
        common,
        config,
        execution,
        methods,
        stage_facts=[
            {
                "stage_id": stage_id,
                "method_id": "sf_qe",
                "status": "complete",
                "started_at": None,
                "ended_at": None,
                "duration_ms": None,
                "attempt_run_id": "evaluation-attempt",
                "error_code": None,
            }
            for stage_id in ("sf_qe.extract", "sf_qe.judge")
        ],
        usage_payload=usage,
        usage_artifacts=[
            {
                "artifact_id": "usage-ledger",
                "relative_path": "usage/ledger.json",
                "sha256": "d" * 64,
            }
        ],
    )
    assert report["metrics"][0]["method"]["model_id"] is None
    assert [row["stage_id"] for row in report["usage"]["by_stage"]] == [
        "sf_qe.extract",
        "sf_qe.judge",
    ]


def _materialize_report_inputs(
    root: Path, common: CommonEvaluationInputV1, report: dict
) -> None:
    for artifact in report["artifacts"]:
        if artifact["kind"] == "metric_report":
            continue
        path = root / Path(*artifact["relative_path"].split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"fixture {artifact['artifact_id']}\n", encoding="utf-8")


def test_fixed_path_writer_is_idempotent_and_bundle_bound(tmp_path: Path) -> None:
    common = _common(("S1",))
    methods = ("sf_qe",)
    config = _config(common, methods)
    execution = _execution(common, config, methods)
    root = tmp_path / "run"
    persist_evaluation_execution_bundle_v1(
        output_root=root,
        config_payload=config,
        execution_payload=execution,
        created_at=NOW,
        producer_code_commit=COMMIT,
    )
    report = _compose(common, config, execution, methods)
    _materialize_report_inputs(root, common, report)
    first = persist_full_run_report_v1(output_root=root, report_payload=report)
    second = persist_full_run_report_v1(output_root=root, report_payload=report)
    assert first.report_path == root / "reports" / "full_run_report_v1.json"
    assert first.reused is False
    assert second.reused is True
    assert first.report_path.read_bytes() == second.report_path.read_bytes()


def test_writer_rejects_missing_present_artifact(tmp_path: Path) -> None:
    common = _common(("S1",))
    methods = ("sf_qe",)
    config = _config(common, methods)
    execution = _execution(common, config, methods)
    root = tmp_path / "run"
    persist_evaluation_execution_bundle_v1(
        output_root=root,
        config_payload=config,
        execution_payload=execution,
        created_at=NOW,
        producer_code_commit=COMMIT,
    )
    report = _compose(common, config, execution, methods)
    with pytest.raises(ContractValidationError, match="missing_artifact"):
        persist_full_run_report_v1(output_root=root, report_payload=report)


def test_writer_refuses_conflicting_report_bytes(tmp_path: Path) -> None:
    common = _common(("S1",))
    methods = ("sf_qe",)
    config = _config(common, methods)
    execution = _execution(common, config, methods)
    root = tmp_path / "run"
    persist_evaluation_execution_bundle_v1(
        output_root=root,
        config_payload=config,
        execution_payload=execution,
        created_at=NOW,
        producer_code_commit=COMMIT,
    )
    report = _compose(common, config, execution, methods)
    _materialize_report_inputs(root, common, report)
    persist_full_run_report_v1(output_root=root, report_payload=report)
    conflicting = copy.deepcopy(report)
    conflicting["caveats"] = [*conflicting["caveats"], "different persisted fact"]
    conflicting = seal_full_run_report(conflicting)
    with pytest.raises(ContractValidationError, match="immutable_conflict"):
        persist_full_run_report_v1(output_root=root, report_payload=conflicting)
