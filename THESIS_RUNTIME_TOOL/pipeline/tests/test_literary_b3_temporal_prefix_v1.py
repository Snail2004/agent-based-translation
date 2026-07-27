from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.literary.b3_temporal_prefix_v1 import (
    B3_TEMPORAL_CHAPTER_ARTIFACT_SCHEMA_VERSION_V1,
    B3TemporalPrefixError,
    build_b3_temporal_prefix_v1,
    fold_b3_temporal_batch_artifact_v1,
)
from pipeline.literary.checkpoint import canonical_hash


def _state(*, source_blocks: list[str], value: str = "serving") -> dict:
    return {
        "state_id": "state_1",
        "semantic_key": "state_key_1",
        "state_domain": "role",
        "subject_referent_refs": ["entity_1"],
        "counterpart_referent_refs": [],
        "state_value": value,
        "lifecycle_status": "open",
        "authority_status": "effective",
        "source_block_ids": source_blocks,
    }


def _write_artifact(
    root: Path,
    chapter_id: str,
    states: list[dict],
    *,
    pending_cases: list[dict] | None = None,
) -> None:
    root.mkdir()
    body = {
        "schema_version": B3_TEMPORAL_CHAPTER_ARTIFACT_SCHEMA_VERSION_V1,
        "chapter_id": chapter_id,
        "effective_state_projection": states,
        "pending_cases": pending_cases or [],
        "resolved_cases": [],
        "closed_prior_state_ids": [],
        "identity_mutation_performed": False,
        "production_publish_performed": False,
    }
    body["artifact_hash"] = canonical_hash(body)
    (root / "chapter_temporal_artifact.json").write_text(
        json.dumps(body, ensure_ascii=False),
        encoding="utf-8",
    )


def _pending_case(
    *,
    review_route: str,
    reason_codes: list[str],
    reason: str = "Identity must be resolved before applying the state.",
) -> dict:
    return {
        "pending_case_id": "pending_1",
        "chapter_id": "ch1",
        "review_route": review_route,
        "reason": reason,
        "reason_codes": reason_codes,
        "authority_status": "pending_review",
        "proposed_action": None,
    }


@pytest.mark.parametrize(
    ("reason_codes", "rerouted_review_route"),
    [
        (["referent_identity_not_confirmed"], "temporal_review"),
        (
            [
                "referent_identity_not_confirmed",
                "stable_claim_domain_requires_review",
            ],
            "stable_claim_review",
        ),
    ],
)
def test_prefix_accepts_typed_identity_review_reroute(
    tmp_path: Path,
    reason_codes: list[str],
    rerouted_review_route: str,
) -> None:
    prior = _pending_case(
        review_route="identity_review",
        reason_codes=reason_codes,
    )
    rerouted = dict(prior)
    rerouted["review_route"] = rerouted_review_route
    first_root = tmp_path / "ch1"
    second_root = tmp_path / "ch2"
    _write_artifact(first_root, "ch1", [], pending_cases=[prior])
    _write_artifact(second_root, "ch2", [], pending_cases=[rerouted])

    prefix = build_b3_temporal_prefix_v1([first_root, second_root])

    assert prefix["pending_cases"] == [rerouted]


def test_prefix_rejects_untyped_or_rewritten_identity_review_reroute(
    tmp_path: Path,
) -> None:
    prior = _pending_case(
        review_route="identity_review",
        reason_codes=["referent_identity_not_confirmed"],
    )
    wrong_route = dict(prior)
    wrong_route["review_route"] = "stable_claim_review"
    first_root = tmp_path / "ch1"
    wrong_route_root = tmp_path / "ch2_wrong_route"
    _write_artifact(first_root, "ch1", [], pending_cases=[prior])
    _write_artifact(
        wrong_route_root,
        "ch2_wrong_route",
        [],
        pending_cases=[wrong_route],
    )

    with pytest.raises(B3TemporalPrefixError, match="changed across chapters"):
        build_b3_temporal_prefix_v1([first_root, wrong_route_root])

    rewritten = dict(prior)
    rewritten["review_route"] = "temporal_review"
    rewritten["reason"] = "Rewritten prose must not pass as a typed reroute."
    rewritten_root = tmp_path / "ch2_rewritten"
    _write_artifact(
        rewritten_root,
        "ch2_rewritten",
        [],
        pending_cases=[rewritten],
    )

    with pytest.raises(B3TemporalPrefixError, match="changed across chapters"):
        build_b3_temporal_prefix_v1([first_root, rewritten_root])


def test_prefix_accepts_cumulative_state_evidence(tmp_path: Path) -> None:
    first = _state(source_blocks=["ch1_b1"])
    second = _state(source_blocks=["ch1_b1", "ch2_b1"])
    second["observations"] = [
        {"state_value": "serving", "source_block_ids": ["ch1_b1"]},
        {"state_value": "serving", "source_block_ids": ["ch2_b1"]},
    ]
    second["observation_count"] = 2
    first_root = tmp_path / "ch1"
    second_root = tmp_path / "ch2"
    _write_artifact(first_root, "ch1", [first])
    _write_artifact(second_root, "ch2", [second])

    prefix = build_b3_temporal_prefix_v1([first_root, second_root])

    assert prefix["effective_open_states"] == [second]


def test_prefix_rejects_cumulative_state_rewrite_or_evidence_loss(
    tmp_path: Path,
) -> None:
    first = _state(source_blocks=["ch1_b1"])
    changed = _state(source_blocks=["ch1_b1"], value="unrelated")
    first_root = tmp_path / "ch1"
    changed_root = tmp_path / "ch2"
    _write_artifact(first_root, "ch1", [first])
    _write_artifact(changed_root, "ch2", [changed])

    with pytest.raises(B3TemporalPrefixError, match="changed across chapters"):
        build_b3_temporal_prefix_v1([first_root, changed_root])

    first_with_observation = _state(source_blocks=["ch1_b1"])
    first_with_observation["observations"] = [
        {"state_value": "serving", "source_block_ids": ["ch1_b1"]}
    ]
    first_with_observation["observation_count"] = 1
    lost = _state(source_blocks=["ch2_b1"])
    lost["observations"] = [
        {"state_value": "serving", "source_block_ids": ["ch2_b1"]}
    ]
    lost["observation_count"] = 1
    first_root = tmp_path / "ch1_with_observation"
    lost_root = tmp_path / "ch2_lost_observation"
    _write_artifact(first_root, "ch1", [first_with_observation])
    _write_artifact(lost_root, "ch2", [lost])

    with pytest.raises(
        B3TemporalPrefixError, match="cumulative evidence was removed"
    ):
        build_b3_temporal_prefix_v1([first_root, lost_root])


def test_batch_fold_rehydrates_code_owned_consolidation_ids() -> None:
    prior = _state(source_blocks=["ch1_b1"])
    prior["consolidated_state_ids"] = ["state_absorbed"]
    model_visible = dict(prior)
    model_visible.pop("consolidated_state_ids")
    body = {
        "schema_version": "literary_b3_temporal_artifact_v1",
        "effective_state_projection": [model_visible],
        "pending_cases": [],
        "closed_prior_state_ids": [],
    }
    batch = {**body, "artifact_hash": canonical_hash(body)}

    effective, pending = fold_b3_temporal_batch_artifact_v1(
        effective_states=[prior],
        pending_cases=[],
        batch_artifact=batch,
    )

    assert effective == [prior]
    assert pending == []
