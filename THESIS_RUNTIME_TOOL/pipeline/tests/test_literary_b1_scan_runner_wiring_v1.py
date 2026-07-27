"""The scan runner's own plumbing, exercised without a provider call.

A unit suite can be entirely green while the CLI path is broken: the argument
existed, the loader accepted it, and nothing connected the two.  That is what
happened here, and it only surfaced when a real run was attempted.  These tests
call the runner's functions the way the CLI does.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from pipeline.literary.b1_cross_chapter_decision_ledger_v1 import (
    empty_decision_ledger_v1,
    project_reconciled_b1_registry_v1,
)
from pipeline.literary.b1_scan_v1 import _validate_prior_cards
from pipeline.scripts import run_literary_b1_scan_v1 as runner


def test_every_canary_argument_reaches_the_runner() -> None:
    # The CLI forwards args by keyword; a parser option with no matching
    # parameter fails only at runtime, after the operator has already queued
    # a live run.
    args = runner.build_parser().parse_args(
        [
            "canary",
            "--output-root", "out",
            "--capability-root", "cap",
            "--run-id", "r",
            "--attempt-run-id", "a",
        ]
    )
    accepted = set(inspect.signature(runner._run_canary).parameters)
    # names main() renames or consumes itself rather than forwarding verbatim
    renamed = {
        "command",
            "chapter",
            "document",
            "epub",
        "source",
        "without_roster",
        "runtime_profile",
        "prior_cards",
        "prior_registry_root",
        "previous_summary_root",
        "credential_env",
        "credential_file",
    }
    missing = {name for name in vars(args) if name not in renamed and name not in accepted}
    assert not missing, f"CLI options never reach _run_canary: {sorted(missing)}"


def _write_projection_root(tmp_path: Path) -> Path:
    registry_path = (
        Path(__file__).resolve().parents[2]
        / "data"
        / "reports"
        / "literary_b1_chapter_registry_wh_ch01_20260721_2"
        / "chapter_registry.json"
    )
    if not registry_path.is_file():
        pytest.skip("real chapter registry evidence is absent in this checkout")
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    ledger = empty_decision_ledger_v1(book_id="wuthering_heights")
    projection = project_reconciled_b1_registry_v1(registries=[registry], ledger=ledger)
    root = tmp_path / "reconciled"
    root.mkdir()
    (root / "decision_ledger.json").write_text(
        json.dumps(ledger, ensure_ascii=False), encoding="utf-8"
    )
    (root / "reconciled_projection.json").write_text(
        json.dumps(projection, ensure_ascii=False), encoding="utf-8"
    )
    (root / "source_registry_00.json").write_text(
        json.dumps(registry, ensure_ascii=False), encoding="utf-8"
    )
    return root


def test_projection_root_produces_reconciled_prior_cards(tmp_path: Path) -> None:
    root = _write_projection_root(tmp_path)
    cards, lineage = runner._load_prior_cards_context(
        prior_cards_path=None,
        prior_registry_root=None,
        prior_projection_root=root,
    )
    assert lineage["prior_input_kind"] == "reconciled_cross_chapter_projection"
    assert lineage["projection_hash"] and lineage["decision_ledger_hash"]
    assert len(cards) == 8
    for card in cards:
        assert card["prior_card_id"] and card["stable_surfaces"]
        assert "member_card_ids" not in card
        assert all(
            "member_card_id" not in claim
            for claim in card.get("profile_claims") or []
        )
    assert _validate_prior_cards(cards) == cards


def test_projection_root_refuses_a_mismatched_ledger(tmp_path: Path) -> None:
    root = _write_projection_root(tmp_path)
    projection = json.loads((root / "reconciled_projection.json").read_text(encoding="utf-8"))
    projection["ledger_hash"] = "0" * 64
    (root / "reconciled_projection.json").write_text(
        json.dumps(projection, ensure_ascii=False), encoding="utf-8"
    )
    with pytest.raises(SystemExit, match="does not match its decision ledger"):
        runner._load_prior_cards_context(
            prior_cards_path=None, prior_registry_root=None, prior_projection_root=root
        )


def test_projection_root_refuses_an_incomplete_registry_set(tmp_path: Path) -> None:
    # The projection was built from a set of sealed registries. Rebuilding the
    # prior cards from a different set would silently change what the next
    # chapter sees, so the runner must refuse rather than improvise.
    root = _write_projection_root(tmp_path)
    (root / "source_registry_00.json").unlink()
    with pytest.raises(SystemExit, match="do not match the ones the projection"):
        runner._load_prior_cards_context(
            prior_cards_path=None, prior_registry_root=None, prior_projection_root=root
        )


def test_no_prior_input_is_still_legal_for_a_first_chapter() -> None:
    cards, lineage = runner._load_prior_cards_context(
        prior_cards_path=None, prior_registry_root=None, prior_projection_root=None
    )
    assert cards == []
    assert lineage["prior_input_kind"] == "none"


def test_writer_prior_cards_envelope_is_accepted_as_standalone_input(
    tmp_path: Path,
) -> None:
    path = tmp_path / "prior_cards.json"
    card = {
        "prior_card_id": "b0ent_example",
        "canonical_surface": "Example",
        "stable_surfaces": ["Example"],
    }
    path.write_text(
        json.dumps(
            {
                "schema_version": "literary_b1_prior_cards_v1",
                "chapter_id": "book_ch01",
                "cards": [card],
            }
        ),
        encoding="utf-8",
    )

    cards, lineage = runner._load_prior_cards_context(
        prior_cards_path=path,
        prior_registry_root=None,
        prior_projection_root=None,
    )

    assert cards == [card]
    assert lineage["prior_input_kind"] == "standalone_cards"


def test_writer_prior_cards_envelope_rejects_unknown_schema(tmp_path: Path) -> None:
    path = tmp_path / "prior_cards.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "foreign_prior_cards",
                "chapter_id": "book_ch01",
                "cards": [],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit, match="unknown schema"):
        runner._load_prior_cards_context(
            prior_cards_path=path,
            prior_registry_root=None,
            prior_projection_root=None,
        )
