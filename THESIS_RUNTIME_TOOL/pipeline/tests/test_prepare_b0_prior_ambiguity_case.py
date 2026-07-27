from __future__ import annotations

import json
from pathlib import Path

from pipeline.scripts.prepare_b0_prior_ambiguity_case import prepare_ambiguity_case


def _card(card_id: str, surface: str, gender: str) -> dict[str, object]:
    return {
        "prior_card_id": card_id,
        "canonical_surface": surface,
        "stable_surfaces": [surface, "Rowan"],
        "referent_kind": "person",
        "referential_gender": gender,
        "identity_summary": "A person bearing the Rowan surname.",
        "authority_scope": "test_verified_global_as_of_prior_scope",
        "first_supported_block_id": "syn_ch01_b001",
        "provenance_refs": [
            {"chapter_id": "syn_ch01", "block_id": "syn_ch01_b001"}
        ],
    }


def test_prepare_ambiguity_case_appends_without_merging(tmp_path: Path) -> None:
    base = tmp_path / "base.json"
    additional = tmp_path / "additional.json"
    base.write_text(
        json.dumps({"prior_cards": [_card("prior_mr", "Mr. Rowan", "masculine")]}),
        encoding="utf-8",
    )
    additional.write_text(
        json.dumps({"prior_card": _card("prior_mrs", "Mrs. Rowan", "feminine")}),
        encoding="utf-8",
    )
    report = prepare_ambiguity_case(
        base_prior_cards_path=base,
        additional_card_path=additional,
        output_dir=tmp_path / "case",
    )
    supplied = json.loads((tmp_path / "case/supplied_prior_cards.json").read_text())
    assert [row["prior_card_id"] for row in supplied["prior_cards"]] == [
        "prior_mr",
        "prior_mrs",
    ]
    assert report["combined_prior_card_count"] == 2
    assert report["hidden_oracle_sent_to_model"] is False
