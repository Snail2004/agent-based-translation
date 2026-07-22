from __future__ import annotations

import copy
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
from pipeline.eval.live_pilot_preflight_v1 import (
    EVALUATION_LIVE_PILOT_CANARY_SCHEMA_VERSION,
    build_evaluation_live_pilot_canary_preflight,
    build_evaluation_live_pilot_preflight,
    seal_evaluation_live_pilot_preflight,
    validate_evaluation_live_pilot_preflight,
    validate_evaluation_live_pilot_preflight_binding,
)
from pipeline.eval.offline_orchestrator_v1 import seal_evaluation_run_config


NOW = "2026-07-20T00:00:00Z"
COMMIT = "a" * 40


def _common(*, equal_arms: bool = False) -> CommonEvaluationInputV1:
    blocks = tuple(
        CommonBlockV1(
            block_id=f"b{index:03d}",
            chapter_id="ch-mlp",
            order_index=index,
            block_type="paragraph",
            source_text=(f"Source block {index}. " + ("x" * (index * 17))),
            admission="translate",
        )
        for index in range(1, 17)
    )
    arms = (
        CommonArmV1(
            "artifact-s0",
            "1" * 64,
            "run-mlp",
            "attempt-s0",
            "S0",
            "profile-s0",
            "3" * 64,
            "en",
            "vi",
        ),
        CommonArmV1(
            "artifact-s1",
            "2" * 64,
            "run-mlp",
            "attempt-s1",
            "S1",
            "profile-s1",
            "4" * 64,
            "en",
            "vi",
        ),
    )
    translations = tuple(
        CommonTranslationV1(
            arm_id=arm.arm_id,
            block_id=block.block_id,
            status="translated",
            target_text=(
                f"Ban dich chung {block.block_id}."
                if equal_arms
                else f"Ban dich {arm.arm_id} {block.block_id}."
            ),
            error_code=None,
        )
        for arm in arms
        for block in blocks
    )
    return CommonEvaluationInputV1(
        source_schema_id="D2LEvaluationInputV1",
        source_schema_version="1.0.0",
        source_binding=LegacyD2LSourceBindingV1(
            project_id="project-mlp",
            document_id="document-mlp",
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
            "config_id": "live-pilot-preflight-fixture",
            "created_at": NOW,
            "producer": {
                "workstream": "evaluation",
                "component": "live_pilot_preflight_test",
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
                    "method_version": version,
                    "scorer_kind": "pairwise" if method_id == "pj" else "unary",
                    "profile_scope": "common",
                    "eligible_admissions": ["translate", "translate_structured"],
                }
                for method_id, version in (
                    ("sf_qe", "local-v1"),
                    ("sf_bt", "prompt-v3"),
                    ("pj", "prompt-v2"),
                )
            ],
            "comparison_pairs": [
                {"pair_id": "s0-v-s1", "arm_1_id": "S0", "arm_2_id": "S1"}
            ],
            "unit_policy": {
                "unit_kind": "block",
                "context_before_blocks": 1,
                "context_after_blocks": 1,
            },
            "blinding": {"mode": "opaque_counterbalanced", "seed": "fixture-blind"},
            "retry_policy": {"max_transport_attempts": 1},
            "integrity": {"config_sha256": "0" * 64},
        }
    )


def _build(common: CommonEvaluationInputV1, *, seed: str = "pilot-seed") -> dict:
    return build_evaluation_live_pilot_preflight(
        common,
        _config(common),
        created_at=NOW,
        producer_code_commit=COMMIT,
        selection_seed=seed,
        requested_unit_count=8,
    )


def _build_canary(
    common: CommonEvaluationInputV1, *, seed: str = "canary-seed"
) -> dict:
    return build_evaluation_live_pilot_canary_preflight(
        common,
        _config(common),
        created_at=NOW,
        producer_code_commit=COMMIT,
        selection_seed=seed,
    )


def test_preflight_selects_source_strata_and_locks_call_envelope():
    common = _common()
    before = copy.deepcopy(common)

    artifact = _build(common)

    assert common == before
    selected = artifact["selection"]["selected_units"]
    assert len(selected) == 8
    assert [row["order_index"] for row in selected] == sorted(
        row["order_index"] for row in selected
    )
    assert {stratum: sum(row["length_stratum"] == stratum for row in selected)
            for stratum in range(4)} == {0: 2, 1: 2, 2: 2, 3: 2}

    workload = artifact["workload"]
    assert workload["selected_plan_job_count"] == 40
    assert workload["method_job_counts"] == {"pj": 8, "sf_bt": 16, "sf_qe": 16}
    assert workload["physical_call_counts"] == {
        "pj_judge": 16,
        "qualification_probe_call_cap": 3,
        "sf_bt_back_translation": 16,
        "sf_bt_semantic_judge": 16,
        "sf_qe_local_rows": 16,
        "total_api_calls": 48,
    }
    assert workload["token_envelope"]["rendered_prompt_count"] == 32
    assert workload["token_envelope"]["deferred_prompt_count"] == 16
    assert workload["token_envelope"]["reserved_max_prompt_tokens"] == 576_000
    assert workload["token_envelope"]["reserved_max_completion_tokens"] == 81_920
    assert workload["token_envelope"]["reserved_max_total_tokens"] == 657_920
    assert workload["token_envelope"]["cost_cap_usd"] is None

    serialized = json.dumps(
        artifact, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    assert "Source block" not in serialized
    assert "Ban dich" not in serialized
    assert "gold" not in serialized.lower()
    assert "human_reference" not in serialized.lower()


def test_preflight_is_deterministic_for_exact_seed_and_input():
    common = _common()
    first = _build(common, seed="same-seed")
    second = _build(common, seed="same-seed")

    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert first["integrity"]["preflight_sha256"] == second["integrity"]["preflight_sha256"]


def test_quota_safe_canary_spans_tertiles_and_locks_eighteen_calls():
    common = _common()
    before = copy.deepcopy(common)

    artifact = _build_canary(common)

    assert common == before
    assert artifact["schema_version"] == EVALUATION_LIVE_PILOT_CANARY_SCHEMA_VERSION
    assert artifact["producer"]["component"] == "live_pilot_canary_preflight_v1"
    assert artifact["selection"]["algorithm"] == "source_length_tertile_hash_v1"
    assert artifact["selection"]["requested_unit_count"] == 3
    assert len(artifact["selection"]["selected_units"]) == 3
    assert {
        row["length_stratum"] for row in artifact["selection"]["selected_units"]
    } == {0, 1, 2}
    assert artifact["workload"]["selected_plan_job_count"] == 15
    assert artifact["workload"]["method_job_counts"] == {
        "pj": 3,
        "sf_bt": 6,
        "sf_qe": 6,
    }
    assert artifact["workload"]["physical_call_counts"] == {
        "pj_judge": 6,
        "qualification_probe_call_cap": 3,
        "sf_bt_back_translation": 6,
        "sf_bt_semantic_judge": 6,
        "sf_qe_local_rows": 6,
        "total_api_calls": 18,
    }
    assert artifact["workload"]["token_envelope"]["rendered_prompt_count"] == 12
    assert artifact["workload"]["token_envelope"]["deferred_prompt_count"] == 6
    assert artifact["workload"]["token_envelope"]["reserved_max_prompt_tokens"] == 216_000
    assert artifact["workload"]["token_envelope"]["reserved_max_completion_tokens"] == 30_720
    assert artifact["workload"]["token_envelope"]["reserved_max_total_tokens"] == 246_720

    validate_evaluation_live_pilot_preflight_binding(
        artifact, common, _config(common)
    )


def test_legacy_preflight_still_rejects_three_units():
    common = _common()

    with pytest.raises(ContractValidationError, match="must be >= 4"):
        build_evaluation_live_pilot_preflight(
            common,
            _config(common),
            created_at=NOW,
            producer_code_commit=COMMIT,
            selection_seed="legacy-three-units",
            requested_unit_count=3,
        )


def test_canary_cannot_be_relabelled_as_legacy_after_reseal():
    changed = copy.deepcopy(_build_canary(_common()))
    changed["schema_version"] = "1.0.0"
    changed = seal_evaluation_live_pilot_preflight(changed)

    with pytest.raises(ContractValidationError):
        validate_evaluation_live_pilot_preflight(changed)


def test_equal_candidates_skip_pairwise_api_calls_mechanically():
    artifact = _build(_common(equal_arms=True))

    workload = artifact["workload"]
    assert workload["physical_call_counts"]["pj_judge"] == 0
    assert workload["physical_call_counts"]["total_api_calls"] == 32
    assert workload["token_envelope"]["reserved_max_completion_tokens"] == 73_728
    pj_jobs = [row for row in artifact["jobs"] if row["method_id"] == "pj"]
    assert len(pj_jobs) == 8
    assert all(row["mechanical_equal"] and not row["prompts"] for row in pj_jobs)


def test_closed_contract_rejects_unknown_key_after_valid_reseal():
    artifact = _build(_common())
    changed = copy.deepcopy(artifact)
    changed["selection"]["answer_hint"] = "S1"
    changed = seal_evaluation_live_pilot_preflight(changed)

    with pytest.raises(ContractValidationError, match="unknown keys"):
        validate_evaluation_live_pilot_preflight(changed)


def test_binding_validator_rejects_resealed_seed_substitution():
    common = _common()
    config = _config(common)
    artifact = _build(common)
    changed = copy.deepcopy(artifact)
    changed["selection"]["seed"] = "foreign-seed"
    changed = seal_evaluation_live_pilot_preflight(changed)

    validate_evaluation_live_pilot_preflight(changed)
    with pytest.raises(ContractValidationError, match="exact input"):
        validate_evaluation_live_pilot_preflight_binding(changed, common, config)


def test_preflight_rejects_insufficient_fully_ready_units():
    common = _common()
    translations = list(common.translations)
    for index, row in enumerate(translations):
        if row.arm_id == "S1" and row.block_id.startswith("b00"):
            translations[index] = CommonTranslationV1(
                row.arm_id,
                row.block_id,
                "failed",
                None,
                "fixture_failure",
            )
    incomplete = CommonEvaluationInputV1(
        common.source_schema_id,
        common.source_schema_version,
        common.source_binding,
        common.blocks,
        common.arms,
        tuple(translations),
    )

    with pytest.raises(ContractValidationError, match="fully ready"):
        _build(incomplete)


def test_preflight_rejects_config_with_transport_retry():
    common = _common()
    config = _config(common)
    changed = copy.deepcopy(config)
    changed["retry_policy"]["max_transport_attempts"] = 2
    changed = seal_evaluation_run_config(changed)

    with pytest.raises(ContractValidationError, match="exactly one sealed"):
        build_evaluation_live_pilot_preflight(
            common,
            changed,
            created_at=NOW,
            producer_code_commit=COMMIT,
            selection_seed="pilot-seed",
            requested_unit_count=8,
        )
