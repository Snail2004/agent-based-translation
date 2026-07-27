from __future__ import annotations

from copy import deepcopy

from pipeline.literary.b0_entity_prior_challenge_experiment import (
    build_hidden_corruption_set_manifest,
    evaluate_hidden_corruption,
)


def _card(card_id: str, surface: str, kind: str) -> dict[str, object]:
    return {
        "prior_card_id": card_id,
        "canonical_surface": surface,
        "stable_surfaces": [surface],
        "referent_kind": kind,
        "referential_gender": None,
        "identity_summary": "A stable referent.",
        "authority_scope": "test_verified_global_as_of_prior_scope",
        "first_supported_block_id": "syn_ch01_b001",
        "provenance_refs": [
            {"chapter_id": "syn_ch01", "block_id": "syn_ch01_b001"}
        ],
    }


def test_set_manifest_evaluates_multiple_expected_outcomes() -> None:
    correct = [_card("prior_north", "North House", "place"), _card("prior_rowan", "Mrs. Rowan", "person")]
    supplied = deepcopy(correct)
    supplied[0]["referent_kind"] = "institution"
    manifest = build_hidden_corruption_set_manifest(
        mutation_id="stress",
        correct_prior_cards=correct,
        supplied_prior_cards=supplied,
        expected_outcomes=[
            {
                "prior_card_id": "prior_north",
                "verdict": "challenge",
                "issue_code": "kind_conflict",
            },
            {
                "prior_card_id": "prior_rowan",
                "verdict": "uncertain",
                "issue_code": None,
            },
        ],
    )
    artifact_body = {
        "prior_card_dispositions": [
            {
                "prior_card_id": "prior_north",
                "verdict": "challenge",
                "issue_code": "kind_conflict",
            },
            {
                "prior_card_id": "prior_rowan",
                "verdict": "uncertain",
                "issue_code": None,
            },
        ],
        "prior_conflict_tickets": [
            {"prior_card_id": "prior_north", "issue_code": "kind_conflict"}
        ],
    }
    from pipeline.literary.checkpoint import canonical_hash

    artifact = {
        **artifact_body,
        "prior_challenge_artifact_hash": canonical_hash(artifact_body),
    }
    evaluation = evaluate_hidden_corruption(artifact, manifest)
    assert evaluation["all_expected_outcomes_detected"] is True
    assert evaluation["matched_outcome_count"] == 2
    assert evaluation["expected_ticket_detected"] is True
    assert evaluation["unrelated_ticket_count"] == 0
