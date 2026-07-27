from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from pipeline.literary.b1_chapter_registry_writer_v1 import (
    _edge_identity,
    _project_prior_cards,
    verify_b1_chapter_registry_v1,
)
from pipeline.literary.checkpoint import canonical_hash
from pipeline.literary.relation_correction_overlay_v1 import (
    LiteraryRelationCorrectionError,
    apply_relation_correction_overlay_v1,
    verify_relation_correction_bundle_v1,
    verify_relation_correction_receipt_v1,
)
from pipeline.scripts.run_literary_relation_correction_v1 import main


def test_replace_relation_preserves_old_edge_and_seals_new_registry() -> None:
    registry = _registry()
    original = deepcopy(registry)
    overlay = _overlay(registry)

    corrected, normalized, receipt = apply_relation_correction_overlay_v1(
        chapter_registry=registry,
        chapter=_chapter(),
        overlay=overlay,
    )

    assert registry == original
    verify_b1_chapter_registry_v1(corrected)
    verify_relation_correction_receipt_v1(receipt)
    assert corrected["prior_cards_projection"] == registry[
        "prior_cards_projection"
    ]
    old = next(
        row
        for row in corrected["relation_edges"]
        if row["relation_edge_id"] == _wrong_edge(registry)[
            "relation_edge_id"
        ]
    )
    assert old["effective"] is False
    assert old["semantic_status"] == "human_retracted"
    replacement = next(
        row
        for row in corrected["relation_edges"]
        if row["semantic_status"] == "human_corrected"
    )
    assert replacement["relation"] == "other_kin"
    assert replacement["relation_note"] == "sibling-in-law relationship"
    assert replacement["relation_raw"] == "sibling-in-law relationship"
    assert replacement["relation_status"] == "human_other"
    assert replacement["effective"] is True
    assert corrected["human_semantic_correction_performed"] is True
    assert normalized["human_semantic_correction_performed"] is True
    assert receipt["provider_calls"] == 0
    verify_relation_correction_bundle_v1(
        source_registry=registry,
        corrected_registry=corrected,
        prior_cards=corrected["prior_cards_projection"],
        normalized_overlay=normalized,
        receipt=receipt,
    )


def test_correction_bundle_rejects_a_drifted_prior_cards_projection() -> None:
    registry = _registry()
    corrected, normalized, receipt = apply_relation_correction_overlay_v1(
        chapter_registry=registry,
        chapter=_chapter(),
        overlay=_overlay(registry),
    )
    prior_cards = deepcopy(corrected["prior_cards_projection"])
    prior_cards["cards"] = []

    with pytest.raises(
        LiteraryRelationCorrectionError,
        match="prior cards differ",
    ):
        verify_relation_correction_bundle_v1(
            source_registry=registry,
            corrected_registry=corrected,
            prior_cards=prior_cards,
            normalized_overlay=normalized,
            receipt=receipt,
        )


def test_retract_relation_without_replacement() -> None:
    registry = _registry()
    overlay = _overlay(registry)
    overlay["corrections"][0]["action"] = "retract"
    overlay["corrections"][0].pop("replacement")

    corrected, _normalized, receipt = apply_relation_correction_overlay_v1(
        chapter_registry=registry,
        chapter=_chapter(),
        overlay=overlay,
    )

    assert len(corrected["relation_edges"]) == 1
    assert corrected["relation_edges"][0]["effective"] is False
    assert receipt["replacement_relation_edge_ids"] == []


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda value: value.update(source_registry_hash="0" * 64),
            "source registry hash differs",
        ),
        (
            lambda value: value["corrections"][0].update(
                target_relation_edge_id="litrel1_unknown"
            ),
            "unknown relation edge",
        ),
        (
            lambda value: value["corrections"][0].update(
                evidence_block_ids=["bk_ch01_b999"]
            ),
            "foreign blocks",
        ),
        (
            lambda value: value["corrections"][0]["replacement"].update(
                relation="sibling_in_law_of"
            ),
            "outside the typed vocabulary",
        ),
    ],
)
def test_relation_correction_fails_closed(mutate, message: str) -> None:
    registry = _registry()
    overlay = _overlay(registry)
    mutate(overlay)

    with pytest.raises(LiteraryRelationCorrectionError, match=message):
        apply_relation_correction_overlay_v1(
            chapter_registry=registry,
            chapter=_chapter(),
            overlay=overlay,
        )


def test_cli_writes_drop_in_registry_without_provider_call(
    tmp_path: Path,
) -> None:
    registry = _registry()
    document = {
        "schema_version": "literary_source_document_v1",
        "document_id": "book",
        "chapters": [_chapter()],
    }
    registry_path = tmp_path / "registry.json"
    document_path = tmp_path / "document.json"
    overlay_path = tmp_path / "overlay.json"
    output = tmp_path / "corrected"
    _write(registry_path, registry)
    _write(document_path, document)
    _write(overlay_path, _overlay(registry))

    assert main(
        [
            "--registry",
            str(registry_path),
            "--document",
            str(document_path),
            "--overlay",
            str(overlay_path),
            "--out-dir",
            str(output),
        ]
    ) == 0
    corrected = json.loads(
        (output / "chapter_registry.json").read_text(encoding="utf-8")
    )
    receipt = json.loads(
        (output / "relation_correction_receipt.json").read_text(
            encoding="utf-8"
        )
    )
    verify_b1_chapter_registry_v1(corrected)
    verify_relation_correction_receipt_v1(receipt)
    assert receipt["provider_calls"] == 0


def _chapter() -> dict:
    return {
        "chapter_id": "bk_ch01",
        "blocks": [
            {
                "block_id": "bk_ch01_b001",
                "order_index": 1,
                "clean_text": "Ada married into Ben's family.",
            }
        ],
    }


def _card(entity_id: str, surface: str) -> dict:
    return {
        "entity_id": entity_id,
        "canonical_surface": surface,
        "stable_surfaces": [surface],
        "record_class": "named_entity_candidate",
        "record_state": "chapter_confirmed",
        "referent_kind": {
            "value": "person",
            "basis": "explicit_textual",
            "semantic_status": "auditor_reviewed",
            "effective": True,
        },
        "identity_summary": {
            "text": f"{surface} is a person.",
            "semantic_status": "auditor_reviewed",
            "authority_scope": "chapter_provisional",
        },
        "distinguishing_note": None,
        "claims": [],
        "aliases": [],
        "address_forms_used": [],
        "first_seen": {
            "chapter_id": "bk_ch01",
            "block_id": "bk_ch01_b001",
            "order_index": 1,
        },
        "support_block_ids": ["bk_ch01_b001"],
        "presence_history": [
            {
                "chapter_id": "bk_ch01",
                "presence_basis": "direct_presence",
                "semantic_status": "auditor_reviewed",
                "source_block_ids": ["bk_ch01_b001"],
            }
        ],
        "source_refs": [f"scan:{entity_id}"],
        "chapter_authority": True,
        "identity_authority": False,
        "book_authority": False,
    }


def _registry() -> dict:
    cards = [_card("b0ent_ada", "Ada"), _card("b0ent_ben", "Ben")]
    edge = {
        "relation_family": "sibling_of",
        "relation": "sibling_of",
        "relation_variants": ["sibling_of"],
        "source_entity_id": "b0ent_ada",
        "target_entity_id": "b0ent_ben",
        "chapter_id": "bk_ch01",
        "anchor_block_ids": ["bk_ch01_b001"],
        "semantic_status": "auditor_reviewed",
        "effective": True,
        "source_component_ids": ["b1lac_wrong"],
        "validity_scope": "as_of_chapter",
    }
    edge["relation_edge_id"] = (
        "litrel1_" + canonical_hash(_edge_identity(edge))[:20]
    )
    body = {
        "schema_version": "literary_b1_chapter_registry_v1",
        "chapter_id": "bk_ch01",
        "lineage": {},
        "cards": cards,
        "relation_edges": [edge],
        "glossary_entries": [],
        "prior_cards_projection": {
            "schema_version": "literary_b1_prior_cards_v1",
            "chapter_id": "bk_ch01",
            "cards": _project_prior_cards(cards),
        },
        "dormant_observations": [],
        "pending_reviews": [],
        "diagnostics": [],
        "curation_log": {},
        "within_chapter_identity_merges": [],
        "id_alias_table": [],
        "chapter_authority_granted": True,
        "identity_authority_granted": False,
        "book_authority_granted": False,
        "database_mutation_performed": False,
        "production_publish_performed": False,
        "metrics": {
            "relation_edge_count": 1,
            "structural_contradiction_count": 0,
        },
    }
    registry = {**body, "registry_hash": canonical_hash(body)}
    verify_b1_chapter_registry_v1(registry)
    return registry


def _wrong_edge(registry: dict) -> dict:
    return registry["relation_edges"][0]


def _overlay(registry: dict) -> dict:
    return {
        "schema_version": "literary_relation_correction_overlay_v1",
        "chapter_id": "bk_ch01",
        "source_registry_hash": registry["registry_hash"],
        "corrections": [
            {
                "action": "replace",
                "target_relation_edge_id": _wrong_edge(registry)[
                    "relation_edge_id"
                ],
                "evidence_block_ids": ["bk_ch01_b001"],
                "correction_note": (
                    "The source states an in-law relation, not siblinghood."
                ),
                "replacement": {
                    "component_kind": "kinship_link",
                    "relation": "other_kin",
                    "source_entity_id": "b0ent_ada",
                    "target_entity_id": "b0ent_ben",
                    "anchor_block_ids": ["bk_ch01_b001"],
                    "relation_note": "sibling-in-law relationship",
                },
            }
        ],
    }


def _write(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
