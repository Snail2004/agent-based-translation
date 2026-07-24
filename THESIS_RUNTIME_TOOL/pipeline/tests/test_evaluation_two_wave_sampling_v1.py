from __future__ import annotations

from dataclasses import replace
import copy
import hashlib
import json
from pathlib import Path

import pytest

from pipeline.eval.common_input_v1 import (
    CommonArmV1,
    CommonBlockV1,
    CommonEvaluationInputV1,
    CommonTranslationV1,
    LegacyD2LSourceBindingV1,
)
from pipeline.eval.contracts_v1 import ContractValidationError
from pipeline.eval.two_wave_sampling_v1 import (
    ARM_DISPLAY_NAMES_V1,
    METHOD_DISPLAY_NAMES_V1,
    TWO_WAVE_CHAPTER_IDS_V1,
    TWO_WAVE_WAVE_A_QUOTAS_V1,
    TWO_WAVE_WAVE_B_QUOTAS_V1,
    build_two_wave_method_stage_payload_v1,
    build_two_wave_sampling_manifest_v1,
    build_two_wave_uncertainty_decision_v1,
    build_two_wave_work_plan_v1,
    two_wave_component_stages_v1,
    two_wave_workflow_stages_v1,
    validate_two_wave_sampling_manifest_v1,
    validate_two_wave_uncertainty_decision_v1,
    validate_two_wave_work_plan_v1,
)
from pipeline.eval.two_wave_coverage_v1 import (
    build_two_wave_sample_coverage_v1,
    validate_two_wave_sample_coverage_v1,
)
from pipeline.eval.mtq5_v1 import (
    aggregate_mtq5_results_v1,
    parse_mtq5_response_v1,
    prepare_mtq5_items_v1,
)
from pipeline.eval.two_wave_runner_v1 import (
    run_two_wave_scoring_v1,
)
from pipeline.eval.workflow_component_writer_v1 import (
    EvaluationWorkflowComponentWriterV1,
    validate_evaluation_workflow_component_package_v1,
)
from pipeline.tests.test_evaluation_workflow_component_writer_v1 import (
    _context as _workflow_context,
)


COMMIT = "1" * 40
CREATED_AT = "2026-07-25T00:00:00Z"
SEED = "evaluation-five-chapter-wave-v1"
ARM_IDS = ("S0", "S1", "community", "google_nmt", "llm_lc")


def _chapter_inputs(
    *,
    block_count: int = 180,
    translated: bool = True,
) -> dict[str, CommonEvaluationInputV1]:
    result: dict[str, CommonEvaluationInputV1] = {}
    for chapter_index, chapter_id in enumerate(TWO_WAVE_CHAPTER_IDS_V1):
        blocks = tuple(
            CommonBlockV1(
                block_id=f"{chapter_id}_b{index:04d}",
                chapter_id=chapter_id,
                order_index=index,
                block_type=("paragraph" if index % 7 else "list_item"),
                source_text=("source text " * (4 + index % 31)).strip(),
                admission="translate",
            )
            for index in range(block_count)
        )
        arms = tuple(
            CommonArmV1(
                artifact_id=f"{chapter_id}_{arm_id}_artifact",
                artifact_sha256=f"{chapter_index + arm_index + 1:064x}"[-64:],
                logical_run_id=f"{chapter_id}_{arm_id}_run",
                attempt_run_id=f"{chapter_id}_{arm_id}_attempt",
                arm_id=arm_id,
                profile_id=f"profile_{arm_id}",
                profile_config_sha256=f"{chapter_index + arm_index + 101:064x}"[-64:],
                source_language="en",
                target_language="vi",
            )
            for arm_index, arm_id in enumerate(ARM_IDS)
        )
        translations = (
            tuple(
                CommonTranslationV1(
                    arm_id=arm_id,
                    block_id=block.block_id,
                    status="translated",
                    target_text=f"{arm_id} {block.block_id}",
                    error_code=None,
                )
                for block in blocks
                for arm_id in ARM_IDS
            )
            if translated
            else ()
        )
        result[chapter_id] = CommonEvaluationInputV1(
            source_schema_id="fixture",
            source_schema_version="1.0.0",
            source_binding=LegacyD2LSourceBindingV1(
                project_id="project-d2l",
                document_id="document-d2l",
                source_db_sha256="a" * 64,
                runtime_manifest_sha256=f"{chapter_index + 501:064x}"[-64:],
            ),
            blocks=blocks,
            arms=arms,
            translations=translations,
        )
    return result


def _manifest(
    inputs: dict[str, CommonEvaluationInputV1] | None = None,
) -> dict:
    return build_two_wave_sampling_manifest_v1(
        _chapter_inputs() if inputs is None else inputs,
        seed=SEED,
        created_at=CREATED_AT,
        producer_code_commit=COMMIT,
    )


def _pair_specs() -> list[tuple[str, str, str]]:
    return [
        (f"{first}__{second}", first, second)
        for first_index, first in enumerate(ARM_IDS)
        for second in ARM_IDS[first_index + 1 :]
    ]


def _method_cluster_ids(manifest: dict, stage_id: str) -> list[str]:
    wave_a_ids = list(manifest["waves"]["wave_a"]["cluster_ids"])
    if stage_id.endswith("_wave_a"):
        return wave_a_ids
    return list(manifest["waves"]["wave_b"]["cluster_ids"])[len(wave_a_ids) :]


def _method_stage_payload(
    manifest: dict,
    *,
    stage_id: str,
    coverage_sha256: str,
    delta: float,
) -> dict:
    pair_ids = [pair_id for pair_id, _, _ in _pair_specs()]
    return build_two_wave_method_stage_payload_v1(
        manifest,
        stage_id=stage_id,
        sample_coverage_sha256=coverage_sha256,
        cluster_pair_deltas={
            cluster_id: {pair_id: delta for pair_id in pair_ids}
            for cluster_id in _method_cluster_ids(manifest, stage_id)
        },
    )


def _method_stage_artifact(
    manifest: dict,
    *,
    stage_id: str,
    coverage_sha256: str,
    delta: float,
    component_run_id: str = "eval-component-run",
    settings_sha256: str = "f" * 64,
) -> dict:
    active_wave = "wave_a" if stage_id.endswith("_wave_a") else "wave_b"
    body = {
        "schema_id": "EvaluationTwoWaveStageArtifactV1",
        "schema_version": "1.0.0",
        "stage_id": stage_id,
        "runner_binding": {
            "component_run_id": component_run_id,
            "sampling_manifest_sha256": manifest["integrity"]["manifest_sha256"],
            "settings_sha256": settings_sha256,
            "active_wave": active_wave,
        },
        "payload": _method_stage_payload(
            manifest,
            stage_id=stage_id,
            coverage_sha256=coverage_sha256,
            delta=delta,
        ),
    }
    canonical = json.dumps(
        body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {**body, "artifact_sha256": hashlib.sha256(canonical).hexdigest()}


def _method_artifacts(
    manifest: dict,
    *,
    completed_wave: str,
    coverage_a_sha256: str = "a" * 64,
    coverage_b_sha256: str = "b" * 64,
    btf_wave_a_delta: float = 0.2,
    mtq5_wave_a_delta: float = 0.2,
    btf_wave_b_delta: float = 0.2,
    mtq5_wave_b_delta: float = 0.2,
) -> list[dict]:
    values = {
        "btf_wave_a": (coverage_a_sha256, btf_wave_a_delta),
        "mtq5_wave_a": (coverage_a_sha256, mtq5_wave_a_delta),
        "btf_wave_b": (coverage_b_sha256, btf_wave_b_delta),
        "mtq5_wave_b": (coverage_b_sha256, mtq5_wave_b_delta),
    }
    stage_ids = (
        ("btf_wave_a", "mtq5_wave_a")
        if completed_wave == "wave_a"
        else ("btf_wave_a", "mtq5_wave_a", "btf_wave_b", "mtq5_wave_b")
    )
    return [
        _method_stage_artifact(
            manifest,
            stage_id=stage_id,
            coverage_sha256=values[stage_id][0],
            delta=values[stage_id][1],
        )
        for stage_id in stage_ids
    ]


def _coverage_hashes(completed_wave: str) -> dict[str, str]:
    hashes = {"wave_a": "a" * 64}
    if completed_wave == "wave_b":
        hashes["wave_b"] = "b" * 64
    return hashes


def test_manifest_is_deterministic_cumulative_and_exactly_preregistered() -> None:
    inputs = _chapter_inputs()
    first = _manifest(inputs)
    second = _manifest(inputs)
    assert first == second
    assert first["waves"]["wave_a"]["cluster_count"] == 50
    assert first["waves"]["wave_a"]["block_count"] == 250
    assert first["waves"]["wave_b"]["cluster_count"] == 100
    assert first["waves"]["wave_b"]["block_count"] == 500
    assert (
        first["waves"]["wave_a"]["chapter_cluster_quotas"]
        == TWO_WAVE_WAVE_A_QUOTAS_V1
    )
    assert (
        first["waves"]["wave_b"]["chapter_cluster_quotas"]
        == TWO_WAVE_WAVE_B_QUOTAS_V1
    )
    assert (
        first["waves"]["wave_b"]["cluster_ids"][:50]
        == first["waves"]["wave_a"]["cluster_ids"]
    )
    assert (
        first["waves"]["wave_b"]["block_ids"][:250]
        == first["waves"]["wave_a"]["block_ids"]
    )
    assert len(set(first["waves"]["wave_b"]["block_ids"])) == 500
    assert first["source_features"]["mode"] == "block_metadata_only"
    assert {
        row["stratum"]["term_density_band"] for row in first["clusters"]
    } == {"unknown"}


def test_sampling_is_source_only_and_ignores_translation_content() -> None:
    with_translations = _chapter_inputs(translated=True)
    without_translations = _chapter_inputs(translated=False)
    assert _manifest(with_translations) == _manifest(without_translations)

    changed = copy.deepcopy(with_translations)
    chapter_id = TWO_WAVE_CHAPTER_IDS_V1[0]
    first = changed[chapter_id].translations[0]
    changed[chapter_id] = replace(
        changed[chapter_id],
        translations=(
            replace(first, target_text="completely different target"),
            *changed[chapter_id].translations[1:],
        ),
    )
    assert _manifest(with_translations) == _manifest(changed)


def test_sealed_term_features_are_optional_but_exact_cover_when_present() -> None:
    inputs = _chapter_inputs()
    counts = {
        block.block_id: index % 5
        for common_input in inputs.values()
        for index, block in enumerate(common_input.blocks)
    }
    binding = {
        "artifact_ref": "features/source_term_counts.json",
        "artifact_kind": "source_term_counts_v1",
        "schema_version": "1.0.0",
        "sha256": "b" * 64,
        "sha256_kind": "canonical:SourceTermCountsV1@1.0.0",
    }
    manifest = build_two_wave_sampling_manifest_v1(
        inputs,
        seed=SEED,
        created_at=CREATED_AT,
        producer_code_commit=COMMIT,
        term_occurrence_counts=counts,
        term_feature_binding=binding,
    )
    assert manifest["source_features"]["mode"] == "sealed_source_term_counts_v1"
    assert {
        row["stratum"]["term_density_band"] for row in manifest["clusters"]
    } <= {"zero", "low", "medium", "high"}

    counts.pop(next(iter(counts)))
    with pytest.raises(ContractValidationError, match="feature_exact_cover"):
        build_two_wave_sampling_manifest_v1(
            inputs,
            seed=SEED,
            created_at=CREATED_AT,
            producer_code_commit=COMMIT,
            term_occurrence_counts=counts,
            term_feature_binding=binding,
        )


def test_manifest_rejects_unknown_key_hash_drift_and_foreign_block() -> None:
    manifest = _manifest()
    unknown = copy.deepcopy(manifest)
    unknown["answer"] = 42
    with pytest.raises(ContractValidationError, match="unknown_keys"):
        validate_two_wave_sampling_manifest_v1(unknown)

    drifted = copy.deepcopy(manifest)
    drifted["identity"]["seed"] = "changed"
    with pytest.raises(ContractValidationError, match="manifest_hash"):
        validate_two_wave_sampling_manifest_v1(drifted)

    foreign = copy.deepcopy(manifest)
    foreign["clusters"][0]["block_ids"][0] = "foreign"
    from pipeline.eval.contracts_v1 import seal_payload
    from pipeline.eval.two_wave_sampling_v1 import _SAMPLING_HASH_PATH, _SAMPLING_POLICY

    foreign = seal_payload(
        foreign, policy=_SAMPLING_POLICY, hash_path=_SAMPLING_HASH_PATH
    )
    with pytest.raises(ContractValidationError, match="foreign_block"):
        validate_two_wave_sampling_manifest_v1(foreign)


def test_manifest_fails_closed_when_chapter_cannot_meet_wave_b_quota() -> None:
    with pytest.raises(ContractValidationError, match="sampling_capacity"):
        _manifest(_chapter_inputs(block_count=80))


def test_uncertainty_gate_opens_wave_b_and_stops_inconclusive_after_wave_b() -> None:
    manifest = _manifest()
    wave_a_artifacts = _method_artifacts(
        manifest,
        completed_wave="wave_a",
        mtq5_wave_a_delta=0.0,
    )
    wave_a = build_two_wave_uncertainty_decision_v1(
        manifest,
        completed_wave="wave_a",
        sample_coverage_sha256s=_coverage_hashes("wave_a"),
        method_stage_artifacts=wave_a_artifacts,
        created_at=CREATED_AT,
        producer_code_commit=COMMIT,
    )
    assert wave_a["decision"] == "open_wave_b"
    assert wave_a["headline_status"] == "PENDING_WAVE_B"

    wave_b_artifacts = _method_artifacts(
        manifest,
        completed_wave="wave_b",
        mtq5_wave_a_delta=0.0,
        mtq5_wave_b_delta=0.0,
    )
    wave_b = build_two_wave_uncertainty_decision_v1(
        manifest,
        completed_wave="wave_b",
        sample_coverage_sha256s=_coverage_hashes("wave_b"),
        method_stage_artifacts=wave_b_artifacts,
        created_at=CREATED_AT,
        producer_code_commit=COMMIT,
    )
    assert wave_b["decision"] == "stop_inconclusive"
    assert wave_b["headline_status"] == "INCONCLUSIVE"


def test_uncertainty_gate_uses_direction_disagreement_and_can_stop_conclusive() -> None:
    manifest = _manifest()
    disagreement_artifacts = _method_artifacts(
        manifest,
        completed_wave="wave_a",
        mtq5_wave_a_delta=-0.2,
    )
    disagreement = build_two_wave_uncertainty_decision_v1(
        manifest,
        completed_wave="wave_a",
        sample_coverage_sha256s=_coverage_hashes("wave_a"),
        method_stage_artifacts=disagreement_artifacts,
        created_at=CREATED_AT,
        producer_code_commit=COMMIT,
    )
    assert disagreement["decision"] == "open_wave_b"
    assert any(
        reason.endswith("btf_mtq5_direction_disagrees")
        for reason in disagreement["reasons"]
    )

    conclusive_artifacts = _method_artifacts(manifest, completed_wave="wave_a")
    conclusive = build_two_wave_uncertainty_decision_v1(
        manifest,
        completed_wave="wave_a",
        sample_coverage_sha256s=_coverage_hashes("wave_a"),
        method_stage_artifacts=conclusive_artifacts,
        created_at=CREATED_AT,
        producer_code_commit=COMMIT,
    )
    assert conclusive["decision"] == "stop_conclusive"
    assert conclusive["reasons"] == []


def test_uncertainty_rejects_nonfinite_and_policy_tamper() -> None:
    manifest = _manifest()
    with pytest.raises(ContractValidationError, match="non_finite"):
        _method_stage_payload(
            manifest,
            stage_id="btf_wave_a",
            coverage_sha256="a" * 64,
            delta=float("nan"),
        )

    artifacts = _method_artifacts(manifest, completed_wave="wave_a")
    decision = build_two_wave_uncertainty_decision_v1(
        manifest,
        completed_wave="wave_a",
        sample_coverage_sha256s=_coverage_hashes("wave_a"),
        method_stage_artifacts=artifacts,
        created_at=CREATED_AT,
        producer_code_commit=COMMIT,
    )
    tampered = copy.deepcopy(decision)
    tampered["decision"] = "open_wave_b"
    with pytest.raises(ContractValidationError, match="decision_policy"):
        validate_two_wave_uncertainty_decision_v1(
            tampered,
            sampling_manifest=manifest,
            sample_coverage_sha256s=_coverage_hashes("wave_a"),
            method_stage_artifacts=artifacts,
        )


def test_uncertainty_decision_is_deterministic_and_bound_to_exact_scorer_artifacts() -> None:
    manifest = _manifest()
    artifacts = _method_artifacts(
        manifest,
        completed_wave="wave_a",
        mtq5_wave_a_delta=0.0,
    )
    first = build_two_wave_uncertainty_decision_v1(
        manifest,
        completed_wave="wave_a",
        sample_coverage_sha256s=_coverage_hashes("wave_a"),
        method_stage_artifacts=artifacts,
        created_at=CREATED_AT,
        producer_code_commit=COMMIT,
    )
    second = build_two_wave_uncertainty_decision_v1(
        manifest,
        completed_wave="wave_a",
        sample_coverage_sha256s=_coverage_hashes("wave_a"),
        method_stage_artifacts=copy.deepcopy(artifacts),
        created_at=CREATED_AT,
        producer_code_commit=COMMIT,
    )
    assert first == second
    assert first["decision"] == "open_wave_b"
    assert first["cumulative_cluster_count"] == 50
    assert {row["methods"][0]["unit_count"] for row in first["pair_evidence"]} == {50}

    foreign = _method_artifacts(
        manifest,
        completed_wave="wave_a",
        mtq5_wave_a_delta=0.2,
    )
    with pytest.raises(ContractValidationError, match="artifact_binding"):
        validate_two_wave_uncertainty_decision_v1(
            first,
            sampling_manifest=manifest,
            sample_coverage_sha256s=_coverage_hashes("wave_a"),
            method_stage_artifacts=foreign,
        )


def test_wave_b_decision_requires_wave_a_prefix_plus_exact_fifty_additions() -> None:
    manifest = _manifest()
    artifacts = _method_artifacts(
        manifest,
        completed_wave="wave_b",
        mtq5_wave_a_delta=0.0,
        mtq5_wave_b_delta=0.4,
    )
    decision = build_two_wave_uncertainty_decision_v1(
        manifest,
        completed_wave="wave_b",
        sample_coverage_sha256s=_coverage_hashes("wave_b"),
        method_stage_artifacts=artifacts,
        created_at=CREATED_AT,
        producer_code_commit=COMMIT,
    )
    assert decision["decision"] == "stop_conclusive"
    assert decision["cumulative_cluster_count"] == 100
    assert {
        method["unit_count"]
        for row in decision["pair_evidence"]
        for method in row["methods"]
    } == {100}

    with pytest.raises(ContractValidationError, match="artifact_exact_cover"):
        build_two_wave_uncertainty_decision_v1(
            manifest,
            completed_wave="wave_b",
            sample_coverage_sha256s=_coverage_hashes("wave_b"),
            method_stage_artifacts=artifacts[2:],
            created_at=CREATED_AT,
            producer_code_commit=COMMIT,
        )


def test_work_plan_reports_logical_work_without_claiming_provider_call_count() -> None:
    manifest = _manifest()
    wave_a = build_two_wave_work_plan_v1(manifest, active_wave="wave_a")
    assert wave_a["logical_work"] == {
        "dtq_full_rows": 4500,
        "terminology_full_blocks": 900,
        "btf_sampled_rows": 1250,
        "btf_incremental_rows": 1250,
        "mtq5_cluster_pair_orientations": 1000,
        "mtq5_incremental_cluster_pair_orientations": 1000,
    }
    wave_b = build_two_wave_work_plan_v1(manifest, active_wave="wave_b")
    assert wave_b["logical_work"]["btf_sampled_rows"] == 2500
    assert wave_b["logical_work"]["btf_incremental_rows"] == 1250
    assert wave_b["logical_work"]["mtq5_cluster_pair_orientations"] == 2000
    assert (
        wave_b["logical_work"]["mtq5_incremental_cluster_pair_orientations"]
        == 1000
    )
    assert wave_b["active_cluster_ids"][:50] == wave_a["active_cluster_ids"]
    assert len(wave_b["incremental_cluster_ids"]) == 50
    assert not set(wave_b["incremental_cluster_ids"]) & set(
        wave_a["active_cluster_ids"]
    )
    assert "provider_call_count" not in wave_b["logical_work"]
    validate_two_wave_work_plan_v1(wave_b, sampling_manifest=manifest)


def test_dynamic_stage_schedule_and_display_catalog_are_stable() -> None:
    stages = two_wave_workflow_stages_v1()
    assert [row["ordinal"] for row in stages] == list(range(12))
    assert [row["stage_id"] for row in stages][7:10] == [
        "btf_wave_b",
        "mtq5_wave_b",
        "uncertainty_gate_wave_b",
    ]
    assert all(row["conditional"] for row in stages[7:10])
    assert ARM_DISPLAY_NAMES_V1["S1"] == "ABT-Context"
    assert METHOD_DISPLAY_NAMES_V1 == {
        "sf_qe": "DTQ",
        "sf_bt": "BTF",
        "pj": "MTQ-5",
        "tc_occ": "TC-Occ",
        "ta_occ": "TA-Occ",
    }


def test_sample_coverage_is_exact_ready_and_rederivable() -> None:
    inputs = _chapter_inputs()
    manifest = _manifest(inputs)
    coverage = build_two_wave_sample_coverage_v1(
        inputs,
        manifest,
        active_wave="wave_a",
        created_at=CREATED_AT,
        producer_code_commit=COMMIT,
    )

    assert coverage["coverage_status"] == "ready"
    assert coverage["sample_block_ids"] == manifest["waves"]["wave_a"]["block_ids"]
    assert len(coverage["sample_block_ids"]) == 250
    assert len(coverage["input_artifacts"]) == 25
    assert [row["arm_id"] for row in coverage["arm_coverage"]] == list(ARM_IDS)
    assert all(row["translated_block_count"] == 250 for row in coverage["arm_coverage"])
    assert (
        validate_two_wave_sample_coverage_v1(
            coverage,
            sampling_manifest=manifest,
            chapter_inputs=inputs,
        )
        == coverage
    )


def test_missing_sample_row_blocks_without_replacement() -> None:
    inputs = _chapter_inputs()
    manifest = _manifest(inputs)
    missing_block_id = manifest["waves"]["wave_a"]["block_ids"][0]
    chapter_id = missing_block_id.rsplit("_b", 1)[0]
    chapter = inputs[chapter_id]
    inputs[chapter_id] = replace(
        chapter,
        translations=tuple(
            row
            for row in chapter.translations
            if not (row.arm_id == "llm_lc" and row.block_id == missing_block_id)
        ),
    )

    coverage = build_two_wave_sample_coverage_v1(
        inputs,
        manifest,
        active_wave="wave_a",
        created_at=CREATED_AT,
        producer_code_commit=COMMIT,
    )

    assert coverage["coverage_status"] == "blocked"
    assert coverage["sample_block_ids"] == manifest["waves"]["wave_a"]["block_ids"]
    llm_row = next(
        row for row in coverage["arm_coverage"] if row["arm_id"] == "llm_lc"
    )
    assert llm_row["status"] == "blocked"
    assert llm_row["translated_block_count"] == 249
    assert llm_row["unavailable_rows"] == [
        {
            "block_id": missing_block_id,
            "status": "absent",
            "error_code": None,
        }
    ]


def test_sample_coverage_rejects_hash_drift_and_wrong_arm_order() -> None:
    inputs = _chapter_inputs()
    manifest = _manifest(inputs)
    coverage = build_two_wave_sample_coverage_v1(
        inputs,
        manifest,
        active_wave="wave_a",
        created_at=CREATED_AT,
        producer_code_commit=COMMIT,
    )
    tampered = copy.deepcopy(coverage)
    tampered["arm_coverage"][0]["translated_block_count"] -= 1
    with pytest.raises(ContractValidationError):
        validate_two_wave_sample_coverage_v1(tampered)

    chapter_id = TWO_WAVE_CHAPTER_IDS_V1[0]
    inputs[chapter_id] = replace(
        inputs[chapter_id],
        arms=tuple(reversed(inputs[chapter_id].arms)),
    )
    with pytest.raises(ContractValidationError, match="arm_order"):
        build_two_wave_sample_coverage_v1(
            inputs,
            manifest,
            active_wave="wave_a",
            created_at=CREATED_AT,
            producer_code_commit=COMMIT,
        )


def test_mtq5_wave_a_exact_covers_clusters_pairs_and_orientations() -> None:
    inputs = _chapter_inputs()
    manifest = _manifest(inputs)
    coverage = build_two_wave_sample_coverage_v1(
        inputs,
        manifest,
        active_wave="wave_a",
        created_at=CREATED_AT,
        producer_code_commit=COMMIT,
    )
    items = prepare_mtq5_items_v1(
        inputs, manifest, coverage, active_wave="wave_a"
    )

    assert len(items) == 1000
    assert len({item.item_id for item in items}) == 1000
    assert {item.orientation for item in items} == {"canonical", "reversed"}
    assert all(len(item.block_ids) == 5 for item in items)
    assert all("slot_to_arm" not in item.rendered_prompt for item in items)
    assert all("arm_id" not in item.rendered_prompt for item in items)

    wave_b_coverage = build_two_wave_sample_coverage_v1(
        inputs,
        manifest,
        active_wave="wave_b",
        created_at=CREATED_AT,
        producer_code_commit=COMMIT,
    )
    cumulative_b = prepare_mtq5_items_v1(
        inputs, manifest, wave_b_coverage, active_wave="wave_b"
    )
    incremental_b = prepare_mtq5_items_v1(
        inputs,
        manifest,
        wave_b_coverage,
        active_wave="wave_b",
        incremental_only=True,
    )
    assert len(cumulative_b) == 2000
    assert len(incremental_b) == 1000
    assert not {item.cluster_id for item in items} & {
        item.cluster_id for item in incremental_b
    }


def test_mtq5_refuses_blocked_coverage_before_packet_creation() -> None:
    inputs = _chapter_inputs()
    manifest = _manifest(inputs)
    missing_block_id = manifest["waves"]["wave_a"]["block_ids"][0]
    chapter_id = missing_block_id.rsplit("_b", 1)[0]
    inputs[chapter_id] = replace(
        inputs[chapter_id],
        translations=tuple(
            row
            for row in inputs[chapter_id].translations
            if not (row.arm_id == "community" and row.block_id == missing_block_id)
        ),
    )
    coverage = build_two_wave_sample_coverage_v1(
        inputs,
        manifest,
        active_wave="wave_a",
        created_at=CREATED_AT,
        producer_code_commit=COMMIT,
    )

    with pytest.raises(ContractValidationError, match="sample_coverage"):
        prepare_mtq5_items_v1(
            inputs, manifest, coverage, active_wave="wave_a"
        )


def test_mtq5_response_contract_rejects_pseudo_precision_and_unknown_keys() -> None:
    valid = '{"c1":[5,4,3,2,1,4],"c2":[1,2,3,4,5,2],"issues":[],"note":"ok"}'
    assert parse_mtq5_response_v1(valid)["c1"] == [5, 4, 3, 2, 1, 4]
    with pytest.raises(ContractValidationError, match="score_band"):
        parse_mtq5_response_v1(
            '{"c1":[5,4,3,2,1,58],"c2":[1,2,3,4,5,2],"issues":[],"note":"x"}'
        )
    with pytest.raises(ContractValidationError, match="unknown"):
        parse_mtq5_response_v1(
            '{"c1":[5,4,3,2,1,4],"c2":[1,2,3,4,5,2],"issues":[],"note":"x","winner":"c1"}'
        )


def test_mtq5_aggregation_maps_reversed_slots_back_to_stable_arms() -> None:
    inputs = _chapter_inputs()
    manifest = _manifest(inputs)
    coverage = build_two_wave_sample_coverage_v1(
        inputs,
        manifest,
        active_wave="wave_a",
        created_at=CREATED_AT,
        producer_code_commit=COMMIT,
    )
    items = prepare_mtq5_items_v1(
        inputs, manifest, coverage, active_wave="wave_a"
    )
    arm_scores = {"S0": 1, "S1": 5, "community": 4, "google_nmt": 2, "llm_lc": 3}
    outputs = {}
    for item in items:
        slot_scores = {
            slot: arm_scores[arm_id] for slot, arm_id in item.slot_to_arm
        }
        outputs[item.item_id] = {
            "c1": [slot_scores[1]] * 6,
            "c2": [slot_scores[2]] * 6,
            "issues": [],
            "note": "fixture",
        }

    report = aggregate_mtq5_results_v1(
        sampling_manifest=manifest,
        sample_coverage=coverage,
        prepared_items=items,
        outputs=outputs,
        created_at=CREATED_AT,
    )

    summaries = {row["arm_id"]: row for row in report["arm_summaries"]}
    assert summaries["S1"]["mean_scores"]["overall"] == 5.0
    assert summaries["S0"]["mean_scores"]["overall"] == 1.0
    assert report["scope"]["logical_judgment_count"] == 1000
    assert len(report["cluster_pair_rows"]) == 500
    assert report["orientation_warning_count"] == 0


class _FakeWorkflowWriter:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.component_run_id = "eval-component-two-wave"
        self.workflow_settings = {"settings_sha256": "f" * 64}
        self._states = {
            stage["stage_id"]: "pending" for stage in two_wave_workflow_stages_v1()
        }
        self._events = []
        self._artifacts = []
        self.is_halted = False
        self.terminal_event = None

    def start_or_resume(self) -> bool:
        if not self.is_halted:
            return False
        self.is_halted = False
        self.append_event(
            "component_resumed",
            stage_id="__component__",
            agent="runner",
            severity="info",
            payload={},
        )
        return True

    def stage_state(self, stage_id: str) -> str:
        return self._states[stage_id]

    def start_stage(self, stage_id: str, *, work_total: int, work_unit: str) -> None:
        assert self._states[stage_id] in {"pending", "halted"}
        self._states[stage_id] = "running"
        self.append_event(
            "stage_start",
            stage_id=stage_id,
            agent="fake",
            severity="info",
            payload={"work_total": work_total, "work_unit": work_unit},
        )

    def progress(self, stage_id: str, **kwargs) -> None:
        self.append_event(
            "progress",
            stage_id=stage_id,
            agent="fake",
            severity="info",
            payload=dict(kwargs),
        )

    def validation_passed(self, stage_id: str, *, validator_id: str) -> dict:
        return self.append_event(
            "validation_passed",
            stage_id=stage_id,
            agent="fake",
            severity="info",
            payload={"validator_id": validator_id},
        )

    def validation_failed(
        self, stage_id: str, *, validator_id: str, reason_code: str
    ) -> dict:
        return self.append_event(
            "validation_failed",
            stage_id=stage_id,
            agent="fake",
            severity="error",
            payload={"validator_id": validator_id, "reason_code": reason_code},
        )

    def complete_stage(self, stage_id: str, *, outcome: str = "succeeded") -> None:
        self._states[stage_id] = outcome
        self.append_event(
            "stage_done",
            stage_id=stage_id,
            agent="fake",
            severity="info",
            payload={"outcome": outcome},
        )

    def append_event(self, event: str, **kwargs) -> dict:
        row = {
            "event_id": f"event-{len(self._events) + 1:04d}",
            "event": event,
            **kwargs,
        }
        self._events.append(row)
        return row

    def add_artifact(self, relative_path: str, **kwargs) -> dict:
        row = {"relative_path": relative_path, **kwargs}
        self._artifacts.append(row)
        return row

    def file_binding(
        self, relative_path: str, *, artifact_kind: str, schema_version: str
    ) -> dict:
        path = self.root / relative_path
        return {
            "artifact_ref": relative_path,
            "artifact_kind": artifact_kind,
            "schema_version": schema_version,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "sha256_kind": "physical",
        }

    def halt(self, *, reason_code: str) -> None:
        for stage_id, state in self._states.items():
            if state == "running":
                self._states[stage_id] = "halted"
        self.is_halted = True
        self.append_event(
            "component_halted",
            stage_id="__component__",
            agent="runner",
            severity="warning",
            payload={"reason_code": reason_code},
        )

    def done(self) -> None:
        self.terminal_event = "component_done"
        self.append_event(
            "component_done",
            stage_id="__component__",
            agent="runner",
            severity="info",
            payload={"outcome": "succeeded"},
        )


def _runner_inputs():
    inputs = _chapter_inputs()
    manifest = _manifest(inputs)
    coverage_a = build_two_wave_sample_coverage_v1(
        inputs,
        manifest,
        active_wave="wave_a",
        created_at=CREATED_AT,
        producer_code_commit=COMMIT,
    )
    coverage_b = build_two_wave_sample_coverage_v1(
        inputs,
        manifest,
        active_wave="wave_b",
        created_at=CREATED_AT,
        producer_code_commit=COMMIT,
    )
    return manifest, coverage_a, coverage_b


def _runner_executors(
    call_counts: dict[str, int],
    *,
    fail_once: set[str] | None = None,
    unresolved_wave_a: bool = False,
):
    failures = set() if fail_once is None else fail_once

    def make(stage_id):
        def execute(context):
            call_counts[stage_id] = call_counts.get(stage_id, 0) + 1
            if stage_id in failures:
                failures.remove(stage_id)
                raise RuntimeError("injected interruption")
            if stage_id in {
                "btf_wave_a",
                "mtq5_wave_a",
                "btf_wave_b",
                "mtq5_wave_b",
            }:
                assert context.sample_coverage is not None
                delta = 0.2
                if stage_id == "mtq5_wave_a" and unresolved_wave_a:
                    delta = 0.0
                if stage_id == "mtq5_wave_b" and unresolved_wave_a:
                    delta = 0.4
                return _method_stage_payload(
                    dict(context.sampling_manifest),
                    stage_id=stage_id,
                    coverage_sha256=context.sample_coverage["integrity"][
                        "coverage_sha256"
                    ],
                    delta=delta,
                )
            return {
                "stage_id": stage_id,
                "active_wave": context.active_wave,
                "incremental_only": context.incremental_only,
                "incremental_cluster_count": (
                    0
                    if context.work_plan is None
                    else len(context.work_plan["incremental_cluster_ids"])
                ),
            }

        return execute

    return {
        stage_id: make(stage_id)
        for stage_id in {
            "dtq_full",
            "terminology_occurrence_full",
            "btf_wave_a",
            "mtq5_wave_a",
            "btf_wave_b",
            "mtq5_wave_b",
            "aggregation",
            "report_final",
        }
    }


def test_two_wave_runner_stops_after_conclusive_wave_a_and_reuses_terminal_run(
    tmp_path: Path,
) -> None:
    manifest, coverage_a, coverage_b = _runner_inputs()
    writer = _FakeWorkflowWriter(tmp_path / "component")
    calls = {}
    executors = _runner_executors(calls)
    result = run_two_wave_scoring_v1(
        sampling_manifest=manifest,
        wave_a_coverage=coverage_a,
        wave_b_coverage=coverage_b,
        workflow_writer=writer,
        stage_executors=executors,
        generated_at=CREATED_AT,
        producer_code_commit=COMMIT,
    )

    assert result.state == "completed"
    assert result.final_wave == "wave_a"
    assert result.headline_status == "METHOD_RESULTS_AVAILABLE"
    assert calls.get("btf_wave_b", 0) == 0
    assert calls.get("mtq5_wave_b", 0) == 0
    assert all(writer.stage_state(stage) == "skipped" for stage in (
        "btf_wave_b",
        "mtq5_wave_b",
        "uncertainty_gate_wave_b",
    ))

    call_snapshot = dict(calls)
    replay = run_two_wave_scoring_v1(
        sampling_manifest=manifest,
        wave_a_coverage=coverage_a,
        wave_b_coverage=coverage_b,
        workflow_writer=writer,
        stage_executors=executors,
        generated_at=CREATED_AT,
        producer_code_commit=COMMIT,
    )
    assert calls == call_snapshot
    assert len(replay.reused_stage_ids) == 12


def test_two_wave_runner_opens_only_incremental_wave_b(tmp_path: Path) -> None:
    manifest, coverage_a, coverage_b = _runner_inputs()
    writer = _FakeWorkflowWriter(tmp_path / "component")
    calls = {}
    result = run_two_wave_scoring_v1(
        sampling_manifest=manifest,
        wave_a_coverage=coverage_a,
        wave_b_coverage=coverage_b,
        workflow_writer=writer,
        stage_executors=_runner_executors(calls, unresolved_wave_a=True),
        generated_at=CREATED_AT,
        producer_code_commit=COMMIT,
    )

    assert result.final_wave == "wave_b"
    assert result.headline_status == "METHOD_RESULTS_AVAILABLE"
    assert result.stage_results["btf_wave_b"]["incremental_only"] is True
    assert result.stage_results["btf_wave_b"]["cluster_count"] == 50
    assert result.stage_results["mtq5_wave_b"]["cluster_count"] == 50
    assert calls["btf_wave_a"] == calls["btf_wave_b"] == 1


def test_two_wave_runner_resume_does_not_repeat_accepted_stages(tmp_path: Path) -> None:
    manifest, coverage_a, coverage_b = _runner_inputs()
    writer = _FakeWorkflowWriter(tmp_path / "component")
    calls = {}
    executors = _runner_executors(calls, fail_once={"mtq5_wave_a"})
    with pytest.raises(RuntimeError, match="injected interruption"):
        run_two_wave_scoring_v1(
            sampling_manifest=manifest,
            wave_a_coverage=coverage_a,
            wave_b_coverage=coverage_b,
            workflow_writer=writer,
            stage_executors=executors,
            generated_at=CREATED_AT,
            producer_code_commit=COMMIT,
        )
    assert writer.is_halted
    before_resume = dict(calls)

    result = run_two_wave_scoring_v1(
        sampling_manifest=manifest,
        wave_a_coverage=coverage_a,
        wave_b_coverage=coverage_b,
        workflow_writer=writer,
        stage_executors=executors,
        generated_at=CREATED_AT,
        producer_code_commit=COMMIT,
    )

    assert result.state == "completed"
    for stage_id in (
        "dtq_full",
        "terminology_occurrence_full",
        "btf_wave_a",
    ):
        assert calls[stage_id] == before_resume[stage_id]
    assert calls["mtq5_wave_a"] == 2
    assert {"preflight", "sample_plan", "dtq_full", "terminology_occurrence_full", "btf_wave_a"} <= set(
        result.reused_stage_ids
    )


def test_two_wave_runner_uses_real_replay_component_writer(tmp_path: Path) -> None:
    manifest, coverage_a, coverage_b = _runner_inputs()
    context = _workflow_context(selected_scorer_ids=("sf_qe", "sf_bt", "pj"))
    writer = EvaluationWorkflowComponentWriterV1(
        tmp_path / "real_component",
        context,
        generated_at=CREATED_AT,
        producer_code_commit=COMMIT,
        stages=two_wave_component_stages_v1(),
        allow_create=True,
    )
    result = run_two_wave_scoring_v1(
        sampling_manifest=manifest,
        wave_a_coverage=coverage_a,
        wave_b_coverage=coverage_b,
        workflow_writer=writer,
        stage_executors=_runner_executors({}),
        generated_at=CREATED_AT,
        producer_code_commit=COMMIT,
    )

    package = validate_evaluation_workflow_component_package_v1(
        writer.root, context.scoring_handoff, require_terminal=True
    )
    assert result.state == "completed"
    assert package["events"][-1]["event"] == "component_done"
    assert {
        row["stage_id"]
        for row in package["events"]
        if row["event"] == "stage_start"
    } == {stage["stage_id"] for stage in two_wave_workflow_stages_v1()}


def test_two_wave_runner_blocks_before_scorers_when_wave_b_arm_is_missing(
    tmp_path: Path,
) -> None:
    inputs = _chapter_inputs()
    manifest = _manifest(inputs)
    coverage_a = build_two_wave_sample_coverage_v1(
        inputs,
        manifest,
        active_wave="wave_a",
        created_at=CREATED_AT,
        producer_code_commit=COMMIT,
    )
    missing_block_id = next(
        block_id
        for block_id in manifest["waves"]["wave_b"]["block_ids"]
        if block_id not in set(manifest["waves"]["wave_a"]["block_ids"])
    )
    chapter_id = missing_block_id.rsplit("_b", 1)[0]
    chapter = inputs[chapter_id]
    inputs[chapter_id] = replace(
        chapter,
        translations=tuple(
            row
            for row in chapter.translations
            if not (row.arm_id == "community" and row.block_id == missing_block_id)
        ),
    )
    coverage_b = build_two_wave_sample_coverage_v1(
        inputs,
        manifest,
        active_wave="wave_b",
        created_at=CREATED_AT,
        producer_code_commit=COMMIT,
    )
    calls: dict[str, int] = {}
    writer = _FakeWorkflowWriter(tmp_path / "blocked_component")

    result = run_two_wave_scoring_v1(
        sampling_manifest=manifest,
        wave_a_coverage=coverage_a,
        wave_b_coverage=coverage_b,
        workflow_writer=writer,
        stage_executors=_runner_executors(calls),
        generated_at=CREATED_AT,
        producer_code_commit=COMMIT,
    )

    assert coverage_a["coverage_status"] == "ready"
    assert coverage_b["coverage_status"] == "blocked"
    assert result.state == "halted"
    assert result.headline_status == "BLOCKED"
    assert calls == {}
    assert writer.stage_state("preflight") == "succeeded"
    assert writer.stage_state("sample_plan") == "pending"


def test_two_wave_runner_terminal_replay_rejects_tampered_stage_artifact(
    tmp_path: Path,
) -> None:
    manifest, coverage_a, coverage_b = _runner_inputs()
    writer = _FakeWorkflowWriter(tmp_path / "tamper_component")
    executors = _runner_executors({})
    run_two_wave_scoring_v1(
        sampling_manifest=manifest,
        wave_a_coverage=coverage_a,
        wave_b_coverage=coverage_b,
        workflow_writer=writer,
        stage_executors=executors,
        generated_at=CREATED_AT,
        producer_code_commit=COMMIT,
    )
    artifact_path = writer.root / "two_wave/stages/dtq_full.json"
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    artifact["payload"]["tampered"] = True
    artifact_path.write_text(
        json.dumps(artifact, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
        newline="\n",
    )

    with pytest.raises(ContractValidationError, match="artifact_hash"):
        run_two_wave_scoring_v1(
            sampling_manifest=manifest,
            wave_a_coverage=coverage_a,
            wave_b_coverage=coverage_b,
            workflow_writer=writer,
            stage_executors=executors,
            generated_at=CREATED_AT,
            producer_code_commit=COMMIT,
        )


def test_two_wave_runner_real_writer_resume_preserves_accepted_stages(
    tmp_path: Path,
) -> None:
    manifest, coverage_a, coverage_b = _runner_inputs()
    context = _workflow_context(selected_scorer_ids=("sf_qe", "sf_bt", "pj"))
    root = tmp_path / "real_resume_component"
    calls: dict[str, int] = {}
    executors = _runner_executors(calls, fail_once={"mtq5_wave_a"})
    writer = EvaluationWorkflowComponentWriterV1(
        root,
        context,
        generated_at=CREATED_AT,
        producer_code_commit=COMMIT,
        stages=two_wave_component_stages_v1(),
        allow_create=True,
    )
    with pytest.raises(RuntimeError, match="injected interruption"):
        run_two_wave_scoring_v1(
            sampling_manifest=manifest,
            wave_a_coverage=coverage_a,
            wave_b_coverage=coverage_b,
            workflow_writer=writer,
            stage_executors=executors,
            generated_at=CREATED_AT,
            producer_code_commit=COMMIT,
        )
    accepted_before_resume = dict(calls)

    reopened = EvaluationWorkflowComponentWriterV1(
        root,
        context,
        generated_at=CREATED_AT,
        producer_code_commit=COMMIT,
        stages=two_wave_component_stages_v1(),
        allow_create=False,
    )
    result = run_two_wave_scoring_v1(
        sampling_manifest=manifest,
        wave_a_coverage=coverage_a,
        wave_b_coverage=coverage_b,
        workflow_writer=reopened,
        stage_executors=executors,
        generated_at=CREATED_AT,
        producer_code_commit=COMMIT,
    )
    package = validate_evaluation_workflow_component_package_v1(
        root, context.scoring_handoff, require_terminal=True
    )

    assert result.state == "completed"
    assert {
        row["component_attempt_id"] for row in package["events"]
    } == {"evalcomp_attempt_0001", "evalcomp_attempt_0002"}
    for stage_id in (
        "dtq_full",
        "terminology_occurrence_full",
        "btf_wave_a",
    ):
        assert calls[stage_id] == accepted_before_resume[stage_id]
    assert calls["mtq5_wave_a"] == 2
