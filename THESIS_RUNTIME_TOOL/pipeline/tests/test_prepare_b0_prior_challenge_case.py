from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.literary.b0_entity_prior_challenge_experiment import canonical_hash
from pipeline.scripts.prepare_b0_prior_challenge_case import (
    PrepareCaseError,
    prepare_case,
)


def _cards() -> dict[str, object]:
    return {
        "prior_cards": [
            {
                "prior_card_id": "prior_north_house",
                "canonical_surface": "North House",
                "stable_surfaces": ["North House"],
                "referent_kind": "place",
                "referential_gender": None,
                "identity_summary": "A named residence.",
                "authority_scope": "test_verified_global_as_of_prior_scope",
                "first_supported_block_id": "syn_ch01_b001",
                "provenance_refs": [
                    {"chapter_id": "syn_ch01", "block_id": "syn_ch01_b001"}
                ],
            }
        ]
    }


def test_prepare_case_changes_only_the_requested_field(tmp_path: Path) -> None:
    source = tmp_path / "correct.json"
    source.write_text(json.dumps(_cards()), encoding="utf-8")
    output = tmp_path / "case"
    report = prepare_case(
        correct_prior_cards_path=source,
        prior_card_id="prior_north_house",
        field="referent_kind",
        replacement_json=None,
        replacement_string="institution",
        expected_issue_code="kind_conflict",
        mutation_id="mut_kind",
        output_dir=output,
    )
    supplied = json.loads((output / "supplied_prior_cards.json").read_text())[
        "prior_cards"
    ]
    manifest = json.loads((output / "hidden_corruption_manifest.json").read_text())
    assert supplied[0]["referent_kind"] == "institution"
    assert manifest["changed_card_fields"] == ["referent_kind"]
    assert manifest["supplied_prior_cards_hash"] == canonical_hash(supplied)
    assert report["hidden_manifest_sent_to_model"] is False


def test_prepare_case_rejects_an_unknown_card(tmp_path: Path) -> None:
    source = tmp_path / "correct.json"
    source.write_text(json.dumps(_cards()), encoding="utf-8")
    with pytest.raises(PrepareCaseError, match="exactly one"):
        prepare_case(
            correct_prior_cards_path=source,
            prior_card_id="missing",
            field="referent_kind",
            replacement_json='"institution"',
            replacement_string=None,
            expected_issue_code="kind_conflict",
            mutation_id="mut_missing",
            output_dir=tmp_path / "case",
        )
