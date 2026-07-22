from __future__ import annotations

import copy
import hashlib
import json

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
from pipeline.eval.offline_orchestrator_v1 import (
    build_evaluation_plan,
    seal_evaluation_run_config,
)
from pipeline.eval.scorer_input_packets_v1 import build_scorer_input_packet
from pipeline.eval.scorer_prompts_v3 import render_sf_bt_reverse_prompt_v3
from pipeline.eval.sf_bt_stage_contracts_v1 import (
    build_sf_back_translation_result,
    build_sf_bt_semantic_judge_packet,
    seal_sf_back_translation_result,
    seal_sf_bt_semantic_judge_packet,
    validate_sf_back_translation_result,
    validate_sf_back_translation_result_binding,
    validate_sf_bt_semantic_judge_packet,
    validate_sf_bt_semantic_judge_packet_binding,
)


COMMIT = "a" * 40
NOW = "2026-07-18T00:00:00Z"
RAW_RESPONSE = '{"back_translation":"Canonical English rendering."}'
MODEL_PROFILE = {
    "provider_id": "fixture",
    "model_id": "fixture-back-translator",
    "model_version": "fixture-v1",
    "model_family": "fixture-family-a",
    "profile_sha256": "6" * 64,
}
CONTEXT_PROFILE = "bounded_neighbors"


def _rendered_prompt_sha256(packet: dict) -> str:
    return render_sf_bt_reverse_prompt_v3(
        packet, context_profile=CONTEXT_PROFILE
    ).rendered_prompt_sha256


def _common(*, middle_source: str = "English two.") -> CommonEvaluationInputV1:
    blocks = (
        CommonBlockV1("b001", "ch1", 1, "paragraph", "English one.", "translate"),
        CommonBlockV1("b002", "ch1", 2, "paragraph", middle_source, "translate"),
        CommonBlockV1("b003", "ch1", 3, "paragraph", "English three.", "translate"),
    )
    arms = (
        CommonArmV1(
            artifact_id="artifact-alpha",
            artifact_sha256="1" * 64,
            logical_run_id="logical-run",
            attempt_run_id="attempt-run",
            arm_id="S0",
            profile_id="profile",
            profile_config_sha256="3" * 64,
            source_language="en",
            target_language="vi",
        ),
        CommonArmV1(
            artifact_id="artifact-beta",
            artifact_sha256="2" * 64,
            logical_run_id="logical-run",
            attempt_run_id="attempt-run",
            arm_id="S1",
            profile_id="profile",
            profile_config_sha256="3" * 64,
            source_language="en",
            target_language="vi",
        ),
    )
    translations = tuple(
        CommonTranslationV1(
            arm_id=arm.arm_id,
            block_id=block.block_id,
            status="translated",
            target_text=f"Vietnamese {arm.arm_id} {block.block_id}.",
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
            source_db_sha256="4" * 64,
            runtime_manifest_sha256="5" * 64,
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
            "config_id": "sfbt-stage-test",
            "created_at": NOW,
            "producer": {
                "workstream": "evaluation",
                "component": "sfbt_stage_test",
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
                    "method_id": "sf_bt",
                    "method_version": "planning-v1",
                    "scorer_kind": "unary",
                    "profile_scope": "common",
                    "eligible_admissions": ["translate", "translate_structured"],
                }
            ],
            "comparison_pairs": [],
            "unit_policy": {
                "unit_kind": "block",
                "context_before_blocks": 1,
                "context_after_blocks": 1,
            },
            "blinding": {"mode": "opaque_counterbalanced", "seed": "seed"},
            "retry_policy": {"max_transport_attempts": 2},
            "integrity": {"config_sha256": "0" * 64},
        }
    )


def _stage1(
    arm_id: str = "S0",
    *,
    middle_source: str = "English two.",
) -> tuple[dict, dict, CommonEvaluationInputV1, object]:
    common = _common(middle_source=middle_source)
    plan = build_evaluation_plan(common, _config(common))
    unit = next(row for row in plan.units if row.block_id == "b002")
    job = next(
        row
        for row in plan.jobs
        if row.unit_id == unit.unit_id
        and row.method_id == "sf_bt"
        and row.presentation_arm_ids == (arm_id,)
    )
    packet = build_scorer_input_packet(
        common,
        plan,
        job.job_id,
        created_at=NOW,
        producer_code_commit=COMMIT,
    )
    result = build_sf_back_translation_result(
        packet,
        attempt_id=f"{job.job_id}-attempt-0001",
        attempt_index=1,
        created_at=NOW,
        producer_code_commit=COMMIT,
        context_profile=CONTEXT_PROFILE,
        rendered_prompt_sha256=_rendered_prompt_sha256(packet),
        model_profile=MODEL_PROFILE,
        completion_status="complete",
        finish_reason="stop",
        raw_response_text=RAW_RESPONSE,
    )
    return packet, result, common, plan


def test_back_translation_result_binds_raw_response_and_stage1_packet():
    packet, result, _, _ = _stage1()

    assert result["output"]["back_translation"] == "Canonical English rendering."
    assert result["binding"]["stage1_packet_sha256"] == packet["integrity"]["packet_sha256"]
    assert result["prompt"]["context_profile"] == CONTEXT_PROFILE
    assert result["prompt"]["rendered_prompt_sha256"] == _rendered_prompt_sha256(
        packet
    )
    assert validate_sf_back_translation_result(result) == result
    assert (
        validate_sf_back_translation_result_binding(
            result,
            packet,
            raw_response_text=RAW_RESPONSE,
            context_profile=CONTEXT_PROFILE,
            rendered_prompt_sha256=_rendered_prompt_sha256(packet),
        )
        == result
    )


@pytest.mark.parametrize(
    "raw_response,error",
    [
        ('{"back_translation":"ok","extra":1}', "unknown_keys"),
        (
            '{"back_translation":"first","back_translation":"second"}',
            "response_json",
        ),
        ('prefix {"back_translation":"ok"}', "response_json"),
        ('{"back_translation":NaN}', "response_json"),
        ('{"back_translation":""}', "empty_string"),
        ('{"back_translation":', "response_json"),
    ],
)
def test_back_translation_response_contract_fails_closed(raw_response, error):
    packet, _, _, _ = _stage1()
    with pytest.raises(ContractValidationError, match=error):
        build_sf_back_translation_result(
            packet,
            attempt_id="attempt-1",
            attempt_index=1,
            created_at=NOW,
            producer_code_commit=COMMIT,
            context_profile=CONTEXT_PROFILE,
            rendered_prompt_sha256=_rendered_prompt_sha256(packet),
            model_profile=MODEL_PROFILE,
            completion_status="complete",
            finish_reason="stop",
            raw_response_text=raw_response,
        )


def test_back_translation_result_binding_rejects_resealed_output_or_id_drift():
    packet, result, _, _ = _stage1()

    output_drift = copy.deepcopy(result)
    output_drift["output"]["back_translation"] = "Edited between stages."
    output_drift = seal_sf_back_translation_result(output_drift)
    assert validate_sf_back_translation_result(output_drift) == output_drift
    with pytest.raises(ContractValidationError, match="raw_response_binding"):
        validate_sf_back_translation_result_binding(
            output_drift,
            packet,
            raw_response_text=RAW_RESPONSE,
            context_profile=CONTEXT_PROFILE,
            rendered_prompt_sha256=_rendered_prompt_sha256(packet),
        )

    id_drift = copy.deepcopy(result)
    id_drift["result_id"] = "sfbt-result-forged"
    id_drift = seal_sf_back_translation_result(id_drift)
    assert validate_sf_back_translation_result(id_drift) == id_drift
    with pytest.raises(ContractValidationError, match="result_id_binding"):
        validate_sf_back_translation_result_binding(
            id_drift,
            packet,
            raw_response_text=RAW_RESPONSE,
            context_profile=CONTEXT_PROFILE,
            rendered_prompt_sha256=_rendered_prompt_sha256(packet),
        )


def test_back_translation_result_rejects_wrong_prompt_or_incomplete_transport():
    packet, result, _, _ = _stage1()

    wrong_prompt = copy.deepcopy(result)
    wrong_prompt["prompt"]["prompt_sha256"] = "f" * 64
    with pytest.raises(ContractValidationError, match="enum"):
        validate_sf_back_translation_result(
            seal_sf_back_translation_result(wrong_prompt)
        )

    incomplete = copy.deepcopy(result)
    incomplete["transport"]["completion_status"] = "truncated"
    with pytest.raises(ContractValidationError, match="enum"):
        validate_sf_back_translation_result(
            seal_sf_back_translation_result(incomplete)
        )

    with pytest.raises(ContractValidationError, match="stage1_prompt_binding"):
        validate_sf_back_translation_result_binding(
            result,
            packet,
            raw_response_text=RAW_RESPONSE,
            context_profile="no_context",
            rendered_prompt_sha256=_rendered_prompt_sha256(packet),
        )

    with pytest.raises(ContractValidationError, match="stage1_prompt_binding"):
        validate_sf_back_translation_result_binding(
            result,
            packet,
            raw_response_text=RAW_RESPONSE,
            context_profile=CONTEXT_PROFILE,
            rendered_prompt_sha256="f" * 64,
        )


def test_semantic_packet_binds_source_and_back_translation_in_opaque_slots():
    stage1_packet, stage1_result, common, plan = _stage1()

    packet = build_sf_bt_semantic_judge_packet(
        common,
        plan,
        stage1_packet,
        stage1_result,
        stage1_raw_response_text=RAW_RESPONSE,
        stage1_context_profile=CONTEXT_PROFILE,
        stage1_rendered_prompt_sha256=_rendered_prompt_sha256(stage1_packet),
        presentation_id="canonical",
        source_first=True,
        created_at=NOW,
        producer_code_commit=COMMIT,
    )

    assert packet["binding"]["source_slot_id"] == "passage_a"
    assert packet["binding"]["back_translation_slot_id"] == "passage_b"
    assert [row["text"] for row in packet["passages"]] == [
        "English two.",
        "Canonical English rendering.",
    ]
    rendered = json.dumps(packet, ensure_ascii=False)
    assert "arm_id" not in rendered
    assert "model_profile" not in rendered
    assert validate_sf_bt_semantic_judge_packet(packet) == packet
    assert (
        validate_sf_bt_semantic_judge_packet_binding(
            packet,
            common,
            plan,
            stage1_packet,
            stage1_result,
            stage1_raw_response_text=RAW_RESPONSE,
            stage1_context_profile=CONTEXT_PROFILE,
            stage1_rendered_prompt_sha256=_rendered_prompt_sha256(stage1_packet),
        )
        == packet
    )


def test_semantic_packet_reverse_presentation_swaps_text_not_provenance():
    stage1_packet, stage1_result, common, plan = _stage1()

    packet = build_sf_bt_semantic_judge_packet(
        common,
        plan,
        stage1_packet,
        stage1_result,
        stage1_raw_response_text=RAW_RESPONSE,
        stage1_context_profile=CONTEXT_PROFILE,
        stage1_rendered_prompt_sha256=_rendered_prompt_sha256(stage1_packet),
        presentation_id="reverse",
        source_first=False,
        created_at=NOW,
        producer_code_commit=COMMIT,
    )

    assert packet["binding"]["source_slot_id"] == "passage_b"
    assert [row["text"] for row in packet["passages"]] == [
        "Canonical English rendering.",
        "English two.",
    ]


def test_semantic_packet_canonicalizes_source_text_before_hashing():
    decomposed = "To\u0302\u0301i uu."
    stage1_packet, stage1_result, common, plan = _stage1(
        middle_source=decomposed
    )

    packet = build_sf_bt_semantic_judge_packet(
        common,
        plan,
        stage1_packet,
        stage1_result,
        stage1_raw_response_text=RAW_RESPONSE,
        stage1_context_profile=CONTEXT_PROFILE,
        stage1_rendered_prompt_sha256=_rendered_prompt_sha256(stage1_packet),
        presentation_id="canonical",
        source_first=True,
        created_at=NOW,
        producer_code_commit=COMMIT,
    )

    source_row = next(
        row
        for row in packet["passages"]
        if row["slot_id"] == packet["binding"]["source_slot_id"]
    )
    assert source_row["text"] == "Tối uu."
    assert (
        validate_sf_bt_semantic_judge_packet_binding(
            packet,
            common,
            plan,
            stage1_packet,
            stage1_result,
            stage1_raw_response_text=RAW_RESPONSE,
            stage1_context_profile=CONTEXT_PROFILE,
            stage1_rendered_prompt_sha256=_rendered_prompt_sha256(stage1_packet),
        )
        == packet
    )


def test_semantic_packet_bound_validation_rejects_resealed_source_drift():
    stage1_packet, stage1_result, common, plan = _stage1()
    packet = build_sf_bt_semantic_judge_packet(
        common,
        plan,
        stage1_packet,
        stage1_result,
        stage1_raw_response_text=RAW_RESPONSE,
        stage1_context_profile=CONTEXT_PROFILE,
        stage1_rendered_prompt_sha256=_rendered_prompt_sha256(stage1_packet),
        presentation_id="canonical",
        source_first=True,
        created_at=NOW,
        producer_code_commit=COMMIT,
    )
    drift = copy.deepcopy(packet)
    source_slot = drift["binding"]["source_slot_id"]
    source_row = next(row for row in drift["passages"] if row["slot_id"] == source_slot)
    source_row["text"] = "Foreign source."
    foreign_sha256 = hashlib.sha256(source_row["text"].encode("utf-8")).hexdigest()
    source_row["text_sha256"] = foreign_sha256
    drift["binding"]["source_text_sha256"] = foreign_sha256
    drift = seal_sf_bt_semantic_judge_packet(drift)

    assert validate_sf_bt_semantic_judge_packet(drift) == drift
    with pytest.raises(ContractValidationError, match="semantic_packet_binding"):
        validate_sf_bt_semantic_judge_packet_binding(
            drift,
            common,
            plan,
            stage1_packet,
            stage1_result,
            stage1_raw_response_text=RAW_RESPONSE,
            stage1_context_profile=CONTEXT_PROFILE,
            stage1_rendered_prompt_sha256=_rendered_prompt_sha256(stage1_packet),
        )


def test_semantic_packet_rejects_cross_arm_stage1_substitution():
    packet_s0, result_s0, common, plan = _stage1("S0")
    packet_s1, _, _, _ = _stage1("S1")

    with pytest.raises(ContractValidationError, match="stage1_binding"):
        build_sf_bt_semantic_judge_packet(
            common,
            plan,
            packet_s1,
            result_s0,
            stage1_raw_response_text=RAW_RESPONSE,
            stage1_context_profile=CONTEXT_PROFILE,
            stage1_rendered_prompt_sha256=_rendered_prompt_sha256(packet_s1),
            presentation_id="canonical",
            source_first=True,
            created_at=NOW,
            producer_code_commit=COMMIT,
        )

    assert packet_s0["binding"]["job_id"] != packet_s1["binding"]["job_id"]


def test_contract_builders_do_not_mutate_inputs():
    stage1_packet, stage1_result, common, plan = _stage1()
    originals = copy.deepcopy((stage1_packet, stage1_result, common, plan))

    build_sf_bt_semantic_judge_packet(
        common,
        plan,
        stage1_packet,
        stage1_result,
        stage1_raw_response_text=RAW_RESPONSE,
        stage1_context_profile=CONTEXT_PROFILE,
        stage1_rendered_prompt_sha256=_rendered_prompt_sha256(stage1_packet),
        presentation_id="canonical",
        source_first=True,
        created_at=NOW,
        producer_code_commit=COMMIT,
    )

    assert (stage1_packet, stage1_result, common, plan) == originals
