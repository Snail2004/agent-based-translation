from __future__ import annotations

import copy
import hashlib
from pathlib import Path

import pytest

from pipeline.eval.contracts_v1 import CanonicalPolicy, ContractValidationError, seal_payload
from pipeline.eval.workflow_component_v1 import (
    ARM_IDS_V1,
    SCORING_HANDOFF_SCHEMA_ID,
    SCHEMA_VERSION,
    build_evaluation_artifact_index_v1,
    build_evaluation_component_event_v1,
    build_evaluation_component_manifest_v1,
    build_scoring_receipt_v1,
    scoring_input_set_sha256_v1,
    validate_evaluation_component_stream_v1,
    validate_scoring_handoff_v1,
    validate_scoring_receipt_v1,
    validate_typed_artifact_binding_v1,
)


COMMIT = "a" * 40
NOW = "2026-07-22T12:00:00Z"
WORKFLOW = "workflow_fixture_001"
RUN = "evaluation_component_run_001"
PROFILE_SHA = "b" * 64
SOURCE_HASH = "c" * 64


def _binding(ref: str, kind: str = "fixture") -> dict[str, str]:
    return {
        "artifact_ref": ref,
        "artifact_kind": kind,
        "schema_version": SCHEMA_VERSION,
        "sha256": hashlib.sha256(ref.encode()).hexdigest(),
        "sha256_kind": "physical",
    }


def _source_bindings() -> list[dict[str, object]]:
    roles = (
        "document",
        "structure_manifest",
        "asset_manifest",
        "admitted_projection",
        "normalization_receipt",
        "package_seal",
    )
    return [
        {"role": role, "binding": _binding(f"source/{role}.json", f"source_{role}")}
        for role in roles
    ]


def _translation_inputs() -> list[dict[str, object]]:
    admitted = _source_bindings()[3]["binding"]
    result = []
    for arm in ARM_IDS_V1:
        result.append(
            {
                "arm_id": arm,
                "translation_artifact": _binding(f"translations/{arm}.json", "translation_artifact_v1"),
                "producer": {
                    "component_id": "translation" if arm in {"s0", "s1"} else f"baseline_{arm}",
                    "component_run_id": f"producer_{arm}",
                },
                "coverage": {
                    "expected_block_count": 2,
                    "block_universe_sha256": SOURCE_HASH,
                    "translated_block_count": 1,
                    "preserved_block_count": 1,
                    "excluded_block_count": 0,
                    "review_held_block_count": 0,
                    "missing_block_count": 0,
                    "failed_block_count": 0,
                },
                "source_binding": copy.deepcopy(admitted),
            }
        )
    return result


def _handoff() -> dict[str, object]:
    inputs = _translation_inputs()
    draft: dict[str, object] = {
        "schema_id": SCORING_HANDOFF_SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "workflow_run_id": WORKFLOW,
        "flow_kind": "translation_evaluation_publication",
        "handoff_id": "handoff_fixture_001",
        "created_at": NOW,
        "producer": {
            "workstream": "coordination",
            "component": "neutral_workflow_relay_v1",
            "component_version": SCHEMA_VERSION,
            "code_commit": COMMIT,
        },
        "source_package_bindings": _source_bindings(),
        "optional_bindings": {"glossary": None, "context": None, "projection": None},
        "translation_inputs": inputs,
        "input_set_sha256": scoring_input_set_sha256_v1(inputs),
        "integrity": {"handoff_sha256": "0" * 64},
    }
    policy = CanonicalPolicy(
        set_like_paths=frozenset(),
        semantic_sequence_paths=frozenset({("source_package_bindings",), ("translation_inputs",)}),
    )
    return seal_payload(draft, policy=policy, hash_path=("integrity", "handoff_sha256"))


def _handoff_binding(handoff: dict[str, object]) -> dict[str, str]:
    return {
        "artifact_ref": "handoffs/scoring_handoff.json",
        "artifact_kind": "scoring_handoff_v1",
        "schema_version": SCHEMA_VERSION,
        "sha256": handoff["integrity"]["handoff_sha256"],
        "sha256_kind": "canonical:ScoringHandoffV1@1.0.0",
    }


def _manifest(handoff: dict[str, object], *, attempt: int = 1, revision: int = 1, previous: str | None = None) -> dict[str, object]:
    return build_evaluation_component_manifest_v1(
        workflow_run_id=WORKFLOW,
        component_run_id=RUN,
        component_attempt_id=f"evalcomp_attempt_{attempt:04d}",
        component_attempt_index=attempt,
        manifest_revision=revision,
        previous_manifest_sha256=previous,
        created_at=NOW,
        producer_code_commit=COMMIT,
        scoring_handoff=_handoff_binding(handoff),
        scoring_receipt_ref="handoffs/scoring_receipt.json",
        accepted_input_set_sha256=handoff["input_set_sha256"],
        evaluation_profile=_binding("profile/evaluation_v1.json", "evaluation_profile_v1"),
        stages=(
            {"stage_id": "sf_qe", "ordinal": 0, "agent": "sf_qe_runner"},
        ),
    )


def _event(
    manifest: dict[str, object],
    seq: int,
    previous: str | None,
    event: str,
    payload: dict[str, object],
    *,
    attempt: int = 1,
    stage: str = "__component__",
    agent: str = "runner",
    detail: dict[str, object] | None = None,
) -> dict[str, object]:
    return build_evaluation_component_event_v1(
        manifest,
        component_seq=seq,
        component_attempt_id=f"evalcomp_attempt_{attempt:04d}",
        component_attempt_index=attempt,
        ts=NOW,
        stage_id=stage,
        agent=agent,
        event=event,
        severity="info" if event not in {"validation_failed", "component_failed"} else "error",
        payload=payload,
        previous_event_sha256=previous,
        detail=detail,
    )


def _normal_events(manifest: dict[str, object]) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    previous = None
    rows = (
        ("component_started", {"stage_count": 1}, "__component__", "runner"),
        ("stage_start", {"work_total": 2, "work_unit": "block"}, "sf_qe", "sf_qe_runner"),
        ("progress", {"completed": 1, "total": 2, "unit": "block", "current_work_id": "b001"}, "sf_qe", "sf_qe_runner"),
        ("validation_passed", {"validator_id": "full_run_report_v1"}, "sf_qe", "sf_qe_runner"),
        ("stage_done", {"outcome": "succeeded"}, "sf_qe", "sf_qe_runner"),
        ("component_done", {"outcome": "succeeded"}, "__component__", "runner"),
    )
    for seq, (event_type, payload, stage, agent) in enumerate(rows, start=1):
        current = _event(manifest, seq, previous, event_type, payload, stage=stage, agent=agent)
        events.append(current)
        previous = current["integrity"]["event_sha256"]
    return events


def test_valid_five_arm_handoff_and_receipt_echoes_exact_rows() -> None:
    handoff = _handoff()
    accepted = validate_scoring_handoff_v1(handoff)
    receipt = build_scoring_receipt_v1(
        accepted,
        handoff_artifact_ref="handoffs/scoring_handoff.json",
        evaluation_component_run_id=RUN,
        evaluation_component_attempt_id="evalcomp_attempt_0001",
        accepted_at=NOW,
        producer_code_commit=COMMIT,
        status="accepted",
    )
    assert receipt["accepted_translation_inputs"] == accepted["translation_inputs"]
    assert receipt["accepted_input_set_sha256"] == accepted["input_set_sha256"]
    validate_scoring_receipt_v1(receipt, handoff=accepted)


def test_handoff_rejects_missing_or_reordered_arm() -> None:
    handoff = _handoff()
    handoff["translation_inputs"] = handoff["translation_inputs"][:-1]
    with pytest.raises(ContractValidationError, match="exact ordered arms|requires seven"):
        validate_scoring_handoff_v1(handoff)


def test_d2l_two_arm_fragment_cannot_be_used_as_final_handoff() -> None:
    """The relay must compose the final five-arm object before Evaluation accepts it."""
    fragment = {
        "schema": "scoring_handoff_fragment_v1",
        "workflow_run_id": WORKFLOW,
        "translation_inputs": _translation_inputs()[:2],
    }
    with pytest.raises(ContractValidationError, match="missing required keys"):
        validate_scoring_handoff_v1(fragment)


def test_handoff_and_binding_are_closed_and_path_contained() -> None:
    handoff = _handoff()
    handoff["unexpected"] = True
    with pytest.raises(ContractValidationError, match="unknown keys"):
        validate_scoring_handoff_v1(handoff)
    with pytest.raises(ContractValidationError, match="unsafe_path"):
        validate_typed_artifact_binding_v1(
            {
                "artifact_ref": "C:/outside.json",
                "artifact_kind": "fixture",
                "schema_version": SCHEMA_VERSION,
                "sha256": "a" * 64,
                "sha256_kind": "physical",
            },
            path="$.artifact",
        )


def test_handoff_rejects_relay_or_evaluation_as_translation_producer() -> None:
    handoff = _handoff()
    handoff["translation_inputs"][0]["producer"]["component_id"] = "evaluation"
    with pytest.raises(ContractValidationError, match="cannot author"):
        validate_scoring_handoff_v1(handoff)


def test_handoff_rejects_source_binding_drift_and_coverage_drift() -> None:
    handoff = _handoff()
    handoff["translation_inputs"][2]["source_binding"] = _binding("source/other.json")
    with pytest.raises(ContractValidationError, match="source binding"):
        validate_scoring_handoff_v1(handoff)


def test_handoff_rejects_resealed_input_set_hash_drift() -> None:
    handoff = _handoff()
    handoff["translation_inputs"][0]["translation_artifact"]["sha256"] = "d" * 64
    with pytest.raises(ContractValidationError, match="input set hash"):
        validate_scoring_handoff_v1(handoff)


def test_receipt_rejects_changed_row_even_when_self_hash_is_resealed() -> None:
    handoff = validate_scoring_handoff_v1(_handoff())
    receipt = build_scoring_receipt_v1(
        handoff,
        handoff_artifact_ref="handoffs/scoring_handoff.json",
        evaluation_component_run_id=RUN,
        evaluation_component_attempt_id="evalcomp_attempt_0001",
        accepted_at=NOW,
        producer_code_commit=COMMIT,
        status="accepted",
    )
    receipt["accepted_translation_inputs"][0]["translation_artifact"]["sha256"] = "e" * 64
    policy = CanonicalPolicy(set_like_paths=frozenset(), semantic_sequence_paths=frozenset({("accepted_translation_inputs",)}))
    resealed = seal_payload(receipt, policy=policy, hash_path=("integrity", "receipt_sha256"))
    with pytest.raises(ContractValidationError, match="input set hash|receipt echo"):
        validate_scoring_receipt_v1(resealed, handoff=handoff)


def test_receipt_rejects_foreign_workflow_even_with_valid_receipt_hash() -> None:
    handoff = validate_scoring_handoff_v1(_handoff())
    receipt = build_scoring_receipt_v1(
        handoff,
        handoff_artifact_ref="handoffs/scoring_handoff.json",
        evaluation_component_run_id=RUN,
        evaluation_component_attempt_id="evalcomp_attempt_0001",
        accepted_at=NOW,
        producer_code_commit=COMMIT,
        status="accepted",
    )
    receipt["workflow_run_id"] = "foreign_workflow"
    policy = CanonicalPolicy(
        set_like_paths=frozenset(),
        semantic_sequence_paths=frozenset({("accepted_translation_inputs",)}),
    )
    resealed = seal_payload(receipt, policy=policy, hash_path=("integrity", "receipt_sha256"))
    with pytest.raises(ContractValidationError, match="foreign workflow"):
        validate_scoring_receipt_v1(resealed, handoff=handoff)


def test_component_stream_has_contiguous_hash_chained_events() -> None:
    handoff = validate_scoring_handoff_v1(_handoff())
    manifest = _manifest(handoff)
    events = _normal_events(manifest)
    normalized = validate_evaluation_component_stream_v1(manifest, events)
    assert [row["component_seq"] for row in normalized] == list(range(1, 7))
    assert normalized[-1]["event"] == "component_done"


def test_input_arm_progress_accepts_only_canonical_registered_subset() -> None:
    handoff = validate_scoring_handoff_v1(_handoff())
    manifest = _manifest(handoff)
    event = _event(
        manifest,
        1,
        None,
        "component_started",
        {"stage_count": 1},
        detail={
            "detail_kind": "input_arms",
            "data": {"arm_ids": ["s0", "s1", "google_nmt"]},
        },
    )
    assert event["detail"]["data"]["arm_ids"] == ["s0", "s1", "google_nmt"]

    with pytest.raises(ContractValidationError, match="arm_order"):
        _event(
            manifest,
            1,
            None,
            "component_started",
            {"stage_count": 1},
            detail={
                "detail_kind": "input_arms",
                "data": {"arm_ids": ["google_nmt", "s0"]},
            },
        )


def test_component_stream_rejects_sequence_gap_and_chain_bypass() -> None:
    handoff = validate_scoring_handoff_v1(_handoff())
    manifest = _manifest(handoff)
    events = _normal_events(manifest)
    events[2]["component_seq"] = 99
    with pytest.raises(ContractValidationError, match="contiguous|identity drift"):
        validate_evaluation_component_stream_v1(manifest, events)

    events = _normal_events(manifest)
    events[2]["previous_event_sha256"] = None
    with pytest.raises(ContractValidationError, match="hash chain|identity drift"):
        validate_evaluation_component_stream_v1(manifest, events)


def test_resume_increments_component_attempt_but_keeps_component_run() -> None:
    handoff = validate_scoring_handoff_v1(_handoff())
    manifest_one = _manifest(handoff)
    first: list[dict[str, object]] = []
    previous = None
    for seq, (event_type, payload, stage, agent) in enumerate(
        (
            ("component_started", {"stage_count": 1}, "__component__", "runner"),
            ("stage_start", {"work_total": 2, "work_unit": "block"}, "sf_qe", "sf_qe_runner"),
            ("component_halted", {"reason_code": "process_interrupted", "resume_available": True}, "__component__", "runner"),
        ),
        start=1,
    ):
        current = _event(manifest_one, seq, previous, event_type, payload, stage=stage, agent=agent)
        first.append(current)
        previous = current["integrity"]["event_sha256"]
    manifest_two = _manifest(
        handoff,
        attempt=2,
        revision=2,
        previous=manifest_one["integrity"]["manifest_sha256"],
    )
    resumed = _event(
        manifest_two,
        4,
        previous,
        "component_resumed",
        {
            "resumed_from_attempt_id": "evalcomp_attempt_0001",
            "checkpoint": _binding("checkpoints/cp-1.json", "checkpoint_v1"),
        },
        attempt=2,
    )
    previous = resumed["integrity"]["event_sha256"]
    done_stage = _event(
        manifest_two,
        5,
        previous,
        "stage_start",
        {"work_total": 2, "work_unit": "block"},
        attempt=2,
        stage="sf_qe",
        agent="sf_qe_runner",
    )
    previous = done_stage["integrity"]["event_sha256"]
    final = _event(
        manifest_two,
        6,
        previous,
        "stage_done",
        {"outcome": "succeeded"},
        attempt=2,
        stage="sf_qe",
        agent="sf_qe_runner",
    )
    previous = final["integrity"]["event_sha256"]
    terminal = _event(manifest_two, 7, previous, "component_done", {"outcome": "succeeded"}, attempt=2)
    normalized = validate_evaluation_component_stream_v1(
        manifest_two,
        [*first, resumed, done_stage, final, terminal],
        manifest_revisions=(manifest_one,),
    )
    assert normalized[0]["component_run_id"] == normalized[-1]["component_run_id"] == RUN
    assert normalized[0]["component_attempt_index"] == 1
    assert normalized[3]["component_attempt_index"] == 2


def test_resume_cannot_reuse_logical_request_or_physical_attempt_as_component_attempt() -> None:
    handoff = validate_scoring_handoff_v1(_handoff())
    manifest = _manifest(handoff)
    events = _normal_events(manifest)
    events[0]["component_attempt_id"] = "logical-request-1"
    with pytest.raises(ContractValidationError, match="component_attempt_id|attempt ID"):
        validate_evaluation_component_stream_v1(manifest, events)


def test_terminal_component_rejects_later_event() -> None:
    handoff = validate_scoring_handoff_v1(_handoff())
    manifest = _manifest(handoff)
    events = _normal_events(manifest)
    previous = events[-1]["integrity"]["event_sha256"]
    later = _event(manifest, 7, previous, "progress", {"completed": 1, "total": 1, "unit": "block", "current_work_id": "b001"}, stage="sf_qe", agent="sf_qe_runner")
    with pytest.raises(ContractValidationError, match="terminal"):
        validate_evaluation_component_stream_v1(manifest, [*events, later])


def test_artifact_index_rejects_unknown_parent_and_absolute_path(tmp_path: Path) -> None:
    handoff = validate_scoring_handoff_v1(_handoff())
    manifest = _manifest(handoff)
    index = build_evaluation_artifact_index_v1(
        manifest,
        generated_at=NOW,
        producer_code_commit=COMMIT,
        artifacts=(
            {
                "artifact": _binding("reports/score.json", "score_report_v1"),
                "stage_id": "sf_qe",
                "created_by_event_id": "evalevt_" + "1" * 32,
                "parent_artifact_refs": [],
            },
        ),
    )
    assert index["artifacts"][0]["artifact"]["artifact_ref"] == "reports/score.json"
    broken = copy.deepcopy(index)
    broken["artifacts"][0]["parent_artifact_refs"] = ["missing.json"]
    policy = CanonicalPolicy(
        set_like_paths=frozenset({("artifacts", "*", "parent_artifact_refs")} ),
        semantic_sequence_paths=frozenset({("artifacts",)}),
    )
    broken = seal_payload(broken, policy=policy, hash_path=("integrity", "artifact_index_sha256"))
    with pytest.raises(ContractValidationError, match="unknown parent"):
        from pipeline.eval.workflow_component_v1 import validate_evaluation_artifact_index_v1
        validate_evaluation_artifact_index_v1(broken, manifest=manifest)
