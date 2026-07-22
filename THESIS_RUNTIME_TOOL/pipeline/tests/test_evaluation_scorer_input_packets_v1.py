from __future__ import annotations

import copy
import json
from dataclasses import asdict

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
from pipeline.eval.scorer_input_packets_v1 import (
    build_scorer_input_packet,
    seal_scorer_input_packet,
    validate_scorer_input_packet,
    validate_scorer_input_packet_binding,
)


COMMIT = "a" * 40
NOW = "2026-07-18T00:00:00Z"


def _common(
    *,
    status_overrides: dict[tuple[str, str], str] | None = None,
) -> CommonEvaluationInputV1:
    overrides = status_overrides or {}
    blocks = (
        CommonBlockV1("b001", "ch1", 1, "paragraph", "English one.", "translate"),
        CommonBlockV1("b002", "ch1", 2, "paragraph", "English two.", "translate"),
        CommonBlockV1("b003", "ch1", 3, "paragraph", "English three.", "translate"),
        CommonBlockV1("b101", "ch2", 4, "paragraph", "English next.", "translate"),
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
    labels = {"S0": "alpha", "S1": "beta"}
    translations = []
    for arm in arms:
        for block in blocks:
            status = overrides.get((arm.arm_id, block.block_id), "translated")
            translations.append(
                CommonTranslationV1(
                    arm_id=arm.arm_id,
                    block_id=block.block_id,
                    status=status,
                    target_text=(
                        f"Vietnamese {labels[arm.arm_id]} {block.block_id}."
                        if status in {"translated", "preserved"}
                        else None
                    ),
                    error_code=None if status in {"translated", "preserved"} else status,
                )
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
        translations=tuple(translations),
    )


def _config(common: CommonEvaluationInputV1) -> dict:
    return seal_evaluation_run_config(
        {
            "schema_id": "EvaluationRunConfigV1",
            "schema_version": "1.0.0",
            "config_id": "packet-config",
            "created_at": NOW,
            "producer": {
                "workstream": "evaluation",
                "component": "packet_test",
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
                    "method_version": "planning-v1",
                    "scorer_kind": "pairwise" if method_id == "pj" else "unary",
                    "profile_scope": "common",
                    "eligible_admissions": ["translate", "translate_structured"],
                }
                for method_id in ("sf_qe", "sf_bt", "pj")
            ],
            "comparison_pairs": [
                {"pair_id": "pair", "arm_1_id": "S0", "arm_2_id": "S1"}
            ],
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


def _packet(
    method_id: str,
    *,
    common: CommonEvaluationInputV1 | None = None,
    block_id: str = "b002",
    arm_id: str | None = None,
) -> tuple[dict, object, object]:
    common = common or _common()
    plan = build_evaluation_plan(common, _config(common))
    unit = next(row for row in plan.units if row.block_id == block_id)
    jobs = [
        row
        for row in plan.jobs
        if row.unit_id == unit.unit_id and row.method_id == method_id
    ]
    if arm_id is not None:
        jobs = [row for row in jobs if row.presentation_arm_ids == (arm_id,)]
    job = jobs[0]
    return (
        build_scorer_input_packet(
            common, plan, job.job_id, created_at=NOW, producer_code_commit=COMMIT
        ),
        common,
        plan,
    )


def _reseal(packet: dict) -> dict:
    return seal_scorer_input_packet(packet)


def test_sf_qe_packet_is_active_only_and_arm_blind():
    packet, common, plan = _packet("sf_qe", arm_id="S0")

    assert packet["stage"] == "quality_estimation"
    assert [row["block_id"] for row in packet["source"]["blocks"]] == ["b002"]
    assert len(packet["candidates"]) == 1
    assert [row["block_id"] for row in packet["candidates"][0]["blocks"]] == ["b002"]
    assert packet["candidates"][0]["slot_id"] == "candidate_1"
    assert "arm_id" not in json.dumps(packet, ensure_ascii=False)
    assert "logical_run_id" not in json.dumps(packet, ensure_ascii=False)
    assert validate_scorer_input_packet(packet) == packet
    assert validate_scorer_input_packet_binding(packet, common, plan) == packet


def test_sf_bt_packet_has_target_context_and_no_source_view():
    packet, _, _ = _packet("sf_bt", arm_id="S0")

    assert packet["stage"] == "back_translation"
    assert packet["source"] is None
    blocks = packet["candidates"][0]["blocks"]
    assert [row["block_id"] for row in blocks] == ["b001", "b002", "b003"]
    assert [row["role"] for row in blocks] == ["preceding", "active", "following"]
    assert "English two." not in json.dumps(packet, ensure_ascii=False)


def test_pj_packet_tracks_plan_presentation_with_opaque_slots():
    packet, common, plan = _packet("pj")
    job = next(row for row in plan.jobs if row.job_id == packet["binding"]["job_id"])
    translation_index = {
        (row.arm_id, row.block_id): row.target_text for row in common.translations
    }

    assert packet["stage"] == "pairwise_judgment"
    assert [row["role"] for row in packet["source"]["blocks"]] == [
        "preceding",
        "active",
        "following",
    ]
    for slot_index, arm_id in enumerate(job.presentation_arm_ids):
        active = next(
            row
            for row in packet["candidates"][slot_index]["blocks"]
            if row["role"] == "active"
        )
        assert active["text"] == translation_index[(arm_id, "b002")]
    assert [row["slot_id"] for row in packet["candidates"]] == [
        "candidate_1",
        "candidate_2",
    ]


def test_packet_is_deterministic_and_does_not_mutate_inputs():
    common = _common()
    plan = build_evaluation_plan(common, _config(common))
    common_before = copy.deepcopy(common)
    plan_before = copy.deepcopy(plan)
    job = next(row for row in plan.jobs if row.method_id == "pj")

    first = build_scorer_input_packet(
        common, plan, job.job_id, created_at=NOW, producer_code_commit=COMMIT
    )
    second = build_scorer_input_packet(
        common, plan, job.job_id, created_at=NOW, producer_code_commit=COMMIT
    )

    assert first == second
    assert common == common_before
    assert plan == plan_before


def test_missing_neighbor_remains_explicit_but_active_job_is_ready():
    common = _common(status_overrides={("S0", "b001"): "missing"})
    packet, _, _ = _packet("sf_bt", common=common, arm_id="S0")
    preceding = packet["candidates"][0]["blocks"][0]

    assert preceding["status"] == "missing"
    assert preceding["text"] is None
    assert packet["candidates"][0]["blocks"][1]["status"] == "translated"


def test_context_stops_at_chapter_boundary():
    packet, _, _ = _packet("pj", block_id="b003")
    assert [row["block_id"] for row in packet["source"]["blocks"]] == [
        "b002",
        "b003",
    ]


def test_blocked_active_job_cannot_build_packet():
    common = _common(status_overrides={("S0", "b002"): "failed"})
    plan = build_evaluation_plan(common, _config(common))
    unit = next(row for row in plan.units if row.block_id == "b002")
    job = next(
        row
        for row in plan.jobs
        if row.unit_id == unit.unit_id
        and row.method_id == "sf_qe"
        and row.presentation_arm_ids == ("S0",)
    )
    assert job.status == "blocked"
    with pytest.raises(ContractValidationError, match="job_not_ready"):
        build_scorer_input_packet(
            common, plan, job.job_id, created_at=NOW, producer_code_commit=COMMIT
        )


@pytest.mark.parametrize(
    "mutator,error",
    [
        (lambda p: p.update({"arm_id": "S0"}), "forbidden_runtime_data|unknown_keys"),
        (lambda p: p.update({"score": 1.0}), "forbidden_runtime_data"),
        (lambda p: p["binding"].update({"model_id": "judge"}), "unknown_keys"),
        (lambda p: p.update({"stage": "back_translation"}), "method_stage"),
        (lambda p: p["candidates"].append(copy.deepcopy(p["candidates"][0])), "duplicate"),
    ],
)
def test_resealed_tampering_fails_closed(mutator, error):
    packet, _, _ = _packet("pj")
    mutator(packet)
    packet = _reseal(packet)
    with pytest.raises(ContractValidationError, match=error):
        validate_scorer_input_packet(packet)


@pytest.mark.parametrize(
    "mutator",
    [
        lambda p: p["binding"].update({"plan_sha256": "f" * 64}),
        lambda p: p["source"]["blocks"][1].update({"text": "Foreign source."}),
        lambda p: p["candidates"][0]["blocks"][1].update(
            {"text": "Foreign translation."}
        ),
    ],
)
def test_resealed_content_or_identity_drift_fails_bound_validation(mutator):
    packet, common, plan = _packet("pj")
    mutator(packet)
    packet = _reseal(packet)
    assert validate_scorer_input_packet(packet) == packet
    with pytest.raises(ContractValidationError, match="packet_binding"):
        validate_scorer_input_packet_binding(packet, common, plan)


def test_resealed_sf_bt_source_leak_fails():
    packet, _, _ = _packet("sf_bt", arm_id="S0")
    packet["source"] = {
        "blocks": [
            {
                "block_id": "b002",
                "role": "active",
                "block_type": "paragraph",
                "status": "source",
                "text": "English two.",
            }
        ]
    }
    with pytest.raises(ContractValidationError, match="source_leak"):
        validate_scorer_input_packet(_reseal(packet))


def test_resealed_pj_context_misalignment_fails():
    packet, _, _ = _packet("pj")
    packet["candidates"][0]["blocks"][0]["block_id"] = "foreign"
    with pytest.raises(ContractValidationError, match="context_alignment"):
        validate_scorer_input_packet(_reseal(packet))


def test_resealed_nontranslated_active_candidate_fails():
    packet, _, _ = _packet("sf_qe", arm_id="S0")
    active = packet["candidates"][0]["blocks"][0]
    active["status"] = "missing"
    active["text"] = None
    with pytest.raises(ContractValidationError, match="active_candidate_status"):
        validate_scorer_input_packet(_reseal(packet))


def test_validator_does_not_mutate_payload():
    packet, _, _ = _packet("pj")
    before = copy.deepcopy(packet)
    validate_scorer_input_packet(packet)
    assert packet == before


def test_packet_has_no_private_plan_identity_fields():
    packet, _, _ = _packet("pj")
    forbidden = {
        "arm_id",
        "artifact_id",
        "logical_run_id",
        "attempt_run_id",
        "profile_id",
        "provider",
        "model_id",
        "gold",
        "oracle",
        "score",
        "winner",
    }

    def keys(value):
        if isinstance(value, dict):
            for key, child in value.items():
                yield key
                yield from keys(child)
        elif isinstance(value, list):
            for child in value:
                yield from keys(child)

    assert forbidden.isdisjoint(set(keys(packet)))


def test_fixture_dataclass_serialization_does_not_drive_packet_contract():
    packet, _, plan = _packet("pj")
    job = next(row for row in plan.jobs if row.job_id == packet["binding"]["job_id"])
    assert "presentation_arm_ids" in asdict(job)
    assert "presentation_arm_ids" not in packet
