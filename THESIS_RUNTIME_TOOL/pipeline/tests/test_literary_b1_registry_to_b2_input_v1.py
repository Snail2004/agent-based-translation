from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from pipeline.literary.b1_chapter_registry_writer_v1 import (
    REGISTRY_SCHEMA_VERSION,
    _project_prior_cards,
)
from pipeline.literary.b1_registry_to_b2_input_v1 import (
    B1RegistryToB2InputError,
    build_b2_registry_input_package_v1,
    load_b2_registry_input_package_v1,
    verify_b2_registry_input_package_v1,
    write_b2_registry_input_package_v1,
)
from pipeline.literary.b2_context_v1 import (
    build_candidate_packet_v1,
    load_b2_phase_a_profile,
)
from pipeline.literary.checkpoint import canonical_hash


ROOT = Path(__file__).resolve().parents[2]
PROFILE = ROOT / "pipeline" / "configs" / "literary_b2_slim_phase_a_profile_v1.json"


def _chapter(chapter_id: str, block_id: str, text: str) -> dict:
    return {
        "chapter_id": chapter_id,
        "title": chapter_id,
        "blocks": [
            {
                "block_id": block_id,
                "order_index": 0,
                "block_type": "paragraph",
                "clean_text": text,
            }
        ],
    }


def _source_hash(chapter: dict) -> str:
    rows = [
        {
            "block_id": row["block_id"],
            "order_index": row["order_index"],
            "text": row["clean_text"],
        }
        for row in chapter["blocks"]
    ]
    return canonical_hash({"chapter_id": chapter["chapter_id"], "blocks": rows})


def _registry(
    chapter: dict,
    *,
    entity_id: str,
    surface: str,
    provisional: bool = False,
) -> dict:
    block_id = chapter["blocks"][0]["block_id"]
    claim = {
        "field": "gender",
        "value": "masculine",
        "basis": "contextual_inference" if provisional else "explicit_textual",
        "semantic_status": "unreviewed",
        "effective": not provisional,
    }
    card = {
        "entity_id": entity_id,
        "canonical_surface": surface,
        "stable_surfaces": [surface],
        "record_class": (
            "unresolved_named_reference" if provisional else "named_entity_candidate"
        ),
        "record_state": "chapter_provisional" if provisional else "chapter_confirmed",
        "chapter_authority": not provisional,
        "identity_authority": False,
        "book_authority": False,
        "claims": [claim],
        "referent_kind": {
            "value": "person",
            "basis": "explicit_textual",
            "semantic_status": "unreviewed",
            "effective": True,
        },
        "identity_summary": {
            "text": f"{surface} is a locally observed person.",
            "semantic_status": "unreviewed",
            "authority_scope": "chapter_provisional",
        },
        "first_seen": {
            "chapter_id": chapter["chapter_id"],
            "block_id": block_id,
            "order_index": 0,
        },
        "presence_history": [
            {
                "chapter_id": chapter["chapter_id"],
                "presence_basis": "direct_presence",
                "semantic_status": "observed",
                "source_block_ids": [block_id],
            }
        ],
        "support_block_ids": [block_id],
        "source_refs": [f"scan:{entity_id}"],
        "aliases": [],
        "address_forms_used": [],
        "distinguishing_note": None,
    }
    projection = {
        "schema_version": "literary_b1_prior_cards_v1",
        "chapter_id": chapter["chapter_id"],
        "cards": _project_prior_cards([card]),
    }
    body = {
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "chapter_id": chapter["chapter_id"],
        "lineage": {"source_chapter_hash": _source_hash(chapter)},
        "cards": [card],
        "relation_edges": [],
        "prior_cards_projection": projection,
        "identity_authority_granted": False,
        "book_authority_granted": False,
        "database_mutation_performed": False,
        "production_publish_performed": False,
    }
    return {**body, "registry_hash": canonical_hash(body)}


def _reconciled_projection(
    registries: list[dict],
    *,
    book_id: str,
    card_id: str,
    evidence_block_id: str,
) -> dict:
    body = {
        "schema_version": "literary_b1_reconciled_projection_v1",
        "book_id": book_id,
        "source_registry_hashes": [row["registry_hash"] for row in registries],
        "ledger_hash": "l" * 64,
        "effective_entities": [],
        "resolved_distinct_cases": [],
        "pending_cases": [
            {
                "entry_id": "b1dec_pending",
                "component_id": "b1xhear_pending",
                "chapter_id": registries[-1]["chapter_id"],
                "question_type": "identity_linkage",
                "review_route": "identity_auditor",
                "card_ids": [card_id],
                "candidate_set": [card_id],
                "excluded_prior_card_ids": [],
                "state": "evidence_needed",
                "evidence_block_ids": [evidence_block_id],
                "reason": "More source evidence is required.",
                "resolution_condition": "A new source-grounded identity fact.",
            }
        ],
        "claim_adjudications": [],
        "observation_adjudications": [],
        "metrics": {
            "source_card_count": len(registries),
            "source_entity_id_count": 1,
            "effective_entity_count": 0,
            "merged_group_count": 0,
            "resolved_distinct_count": 0,
            "pending_case_count": 1,
            "claim_adjudication_count": 0,
        },
        "identity_authority_granted": False,
    }
    return {**body, "projection_hash": canonical_hash(body)}


def _rehash_projection(projection: dict) -> dict:
    body = deepcopy(projection)
    body.pop("projection_hash", None)
    return {**body, "projection_hash": canonical_hash(body)}


def test_adapter_preserves_confirmed_card_for_b2_without_book_authority(
    tmp_path: Path,
) -> None:
    chapter = _chapter("book_ch01", "book_ch01_b001", "Robin Vale arrived.")
    package = build_b2_registry_input_package_v1(
        document={"document_id": "book", "chapters": [chapter]},
        chapter_registries=[
            _registry(chapter, entity_id="ent_robin", surface="Robin Vale")
        ],
        current_git_head="head_a",
    )
    prefix = package["chapters"][0]["prefix_bundle"]
    card = prefix["b0_context_cards"][0]
    assert "chapter_snapshots" not in card
    assert card["effective_claims"]["gender"] == "masculine"
    assert card["effective_claims"]["referential_gender"] == "masculine"
    assert card["non_authoritative_context_claims"]["identity_summary"].startswith(
        "Robin Vale"
    )
    assert card["identity_authority"] is False
    assert card["book_authority"] is False

    root = tmp_path / "input"
    write_b2_registry_input_package_v1(output_root=root, package=package)
    loaded = load_b2_registry_input_package_v1(root, current_git_head="head_a")
    assert loaded["certification_eligible"] is True
    packet = build_candidate_packet_v1(
        chapter_id="book_ch01",
        active_blocks=chapter["blocks"],
        tail_blocks=[],
        prefix_bundle=loaded["chapters"][0]["prefix_bundle"],
        candidate_card_cap=8,
        profile=load_b2_phase_a_profile(PROFILE),
    )
    assert [row["candidate_card_id"] for row in packet["candidate_cards"]] == [
        "ent_robin"
    ]
    assert packet["candidate_cards"][0]["non_authoritative_context_claims"][
        "identity_summary"
    ].startswith("Robin Vale")


def test_provisional_claim_stays_candidate_and_is_not_effective() -> None:
    chapter = _chapter("book_ch01", "book_ch01_b001", "Robin Vale was engraved.")
    package = build_b2_registry_input_package_v1(
        document={"document_id": "book", "chapters": [chapter]},
        chapter_registries=[
            _registry(
                chapter,
                entity_id="ent_robin_inscription",
                surface="Robin Vale",
                provisional=True,
            )
        ],
        current_git_head="head_a",
    )
    prefix = package["chapters"][0]["prefix_bundle"]
    assert prefix["b0_context_cards"] == []
    card = prefix["candidate_only_context_cards"][0]
    assert "gender" not in card["effective_claims"]
    assert {row["disputed_field"] for row in card["disputed_claims"]} == {
        "gender",
        "identity_membership",
    }


def test_same_surface_across_chapters_stays_two_candidates_for_b2() -> None:
    chapter1 = _chapter("book_ch01", "book_ch01_b001", "Robin Vale was engraved.")
    chapter2 = _chapter("book_ch02", "book_ch02_b001", "Robin Vale entered.")
    package = build_b2_registry_input_package_v1(
        document={"document_id": "book", "chapters": [chapter1, chapter2]},
        chapter_registries=[
            _registry(chapter1, entity_id="ent_robin_old", surface="Robin Vale"),
            _registry(chapter2, entity_id="ent_robin_new", surface="Robin Vale"),
        ],
        current_git_head="head_a",
    )
    prefix = package["chapters"][1]["prefix_bundle"]
    assert prefix["b0_context_cards"] == []
    assert {row["prior_card_id"] for row in prefix["candidate_only_context_cards"]} == {
        "ent_robin_old",
        "ent_robin_new",
    }
    assert prefix["prefix_identity_uncertainties"][0]["prior_card_ids"] == [
        "ent_robin_new",
        "ent_robin_old",
    ]


def test_reused_entity_id_keeps_one_current_card_and_chapter_snapshots() -> None:
    chapter1 = _chapter("book_ch01", "book_ch01_b001", "Robin Vale arrived.")
    chapter2 = _chapter("book_ch02", "book_ch02_b001", "Mr. Vale returned.")
    registry1 = _registry(
        chapter1, entity_id="ent_robin", surface="Robin Vale"
    )
    registry2 = _registry(chapter2, entity_id="ent_robin", surface="Mr. Vale")

    package = build_b2_registry_input_package_v1(
        document={"document_id": "book", "chapters": [chapter1, chapter2]},
        chapter_registries=[registry1, registry2],
        current_git_head="head_a",
    )
    prefix = package["chapters"][1]["prefix_bundle"]
    cards = prefix["b0_context_cards"] + prefix["candidate_only_context_cards"]

    assert [row["prior_card_id"] for row in cards] == ["ent_robin"]
    card = cards[0]
    assert card["canonical_surface"] == "Mr. Vale"
    assert card["stable_surfaces"] == ["Robin Vale", "Mr. Vale"]
    assert card["first_supported_block_id"] == "book_ch01_b001"
    assert {row["chapter_id"] for row in card["provenance_refs"]} == {
        "book_ch01",
        "book_ch02",
    }
    assert [row["chapter_id"] for row in card["chapter_snapshots"]] == [
        "book_ch01",
        "book_ch02",
    ]
    assert [
        row["card"]["canonical_surface"] for row in card["chapter_snapshots"]
    ] == ["Robin Vale", "Mr. Vale"]
    packet = build_candidate_packet_v1(
        chapter_id="book_ch02",
        active_blocks=chapter2["blocks"],
        tail_blocks=[],
        prefix_bundle=prefix,
        candidate_card_cap=8,
        profile=load_b2_phase_a_profile(PROFILE),
    )
    assert [row["candidate_card_id"] for row in packet["candidate_cards"]] == [
        "ent_robin"
    ]
    assert verify_b2_registry_input_package_v1(package) == package


def test_reconciled_projection_is_bound_to_exact_registry_and_source_blocks() -> None:
    chapter = _chapter("book_ch01", "book_ch01_b001", "Robin Vale arrived.")
    registry = _registry(chapter, entity_id="ent_robin", surface="Robin Vale")
    projection = _reconciled_projection(
        [registry],
        book_id="book",
        card_id="ent_robin",
        evidence_block_id="book_ch01_b001",
    )

    package = build_b2_registry_input_package_v1(
        document={"document_id": "book", "chapters": [chapter]},
        chapter_registries=[registry],
        current_git_head="head_a",
        reconciled_projection=projection,
    )

    assert package["reconciled_projection"] == projection
    assert package["chapters"][0]["prefix_bundle"][
        "candidate_only_context_cards"
    ][0]["identity_resolution"]["state"] == "pending_evidence"


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("foreign_book", "different document"),
        ("foreign_registry", "source registry hashes differ"),
        ("foreign_block", "foreign evidence block"),
        ("foreign_card", "foreign registry card"),
    ],
)
def test_reconciled_projection_binding_rejects_foreign_lineage(
    mutation: str, message: str
) -> None:
    chapter = _chapter("book_ch01", "book_ch01_b001", "Robin Vale arrived.")
    registry = _registry(chapter, entity_id="ent_robin", surface="Robin Vale")
    projection = _reconciled_projection(
        [registry],
        book_id="book",
        card_id="ent_robin",
        evidence_block_id="book_ch01_b001",
    )
    if mutation == "foreign_book":
        projection["book_id"] = "other_book"
    elif mutation == "foreign_registry":
        projection["source_registry_hashes"] = ["f" * 64]
    elif mutation == "foreign_block":
        projection["pending_cases"][0]["evidence_block_ids"] = [
            "other_chapter_b999"
        ]
    else:
        projection["pending_cases"][0]["card_ids"] = ["ent_foreign"]
    projection = _rehash_projection(projection)

    with pytest.raises(B1RegistryToB2InputError, match=message):
        build_b2_registry_input_package_v1(
            document={"document_id": "book", "chapters": [chapter]},
            chapter_registries=[registry],
            current_git_head="head_a",
            reconciled_projection=projection,
        )


def test_package_verifier_rejects_rehashed_foreign_projection() -> None:
    chapter = _chapter("book_ch01", "book_ch01_b001", "Robin Vale arrived.")
    registry = _registry(chapter, entity_id="ent_robin", surface="Robin Vale")
    projection = _reconciled_projection(
        [registry],
        book_id="book",
        card_id="ent_robin",
        evidence_block_id="book_ch01_b001",
    )
    package = build_b2_registry_input_package_v1(
        document={"document_id": "book", "chapters": [chapter]},
        chapter_registries=[registry],
        current_git_head="head_a",
        reconciled_projection=projection,
    )
    tampered = deepcopy(package)
    tampered["reconciled_projection"]["pending_cases"][0][
        "evidence_block_ids"
    ] = ["other_chapter_b999"]
    tampered["reconciled_projection"] = _rehash_projection(
        tampered["reconciled_projection"]
    )
    body = deepcopy(tampered)
    body.pop("package_hash", None)
    tampered["package_hash"] = canonical_hash(body)

    with pytest.raises(B1RegistryToB2InputError, match="foreign evidence block"):
        verify_b2_registry_input_package_v1(tampered)


def test_duplicate_entity_id_inside_one_registry_still_fails_closed() -> None:
    chapter = _chapter("book_ch01", "book_ch01_b001", "Robin Vale arrived.")
    registry = _registry(chapter, entity_id="ent_robin", surface="Robin Vale")
    duplicate = deepcopy(registry)
    duplicate["cards"].append(deepcopy(duplicate["cards"][0]))
    body = {key: value for key, value in duplicate.items() if key != "registry_hash"}
    duplicate["registry_hash"] = canonical_hash(body)

    with pytest.raises(
        B1RegistryToB2InputError,
        match="within one chapter",
    ):
        build_b2_registry_input_package_v1(
            document={"document_id": "book", "chapters": [chapter]},
            chapter_registries=[duplicate],
            current_git_head="head_a",
        )


def test_package_tamper_fails_closed() -> None:
    chapter = _chapter("book_ch01", "book_ch01_b001", "Robin Vale arrived.")
    package = build_b2_registry_input_package_v1(
        document={"document_id": "book", "chapters": [chapter]},
        chapter_registries=[
            _registry(chapter, entity_id="ent_robin", surface="Robin Vale")
        ],
        current_git_head="head_a",
    )
    tampered = deepcopy(package)
    tampered["chapters"][0]["prefix_bundle"]["b0_context_cards"][0][
        "book_authority"
    ] = True
    with pytest.raises(B1RegistryToB2InputError, match="hash mismatch"):
        verify_b2_registry_input_package_v1(tampered)


def test_registry_source_hash_mismatch_fails_closed() -> None:
    chapter = _chapter("book_ch01", "book_ch01_b001", "Robin Vale arrived.")
    registry = _registry(chapter, entity_id="ent_robin", surface="Robin Vale")
    changed = deepcopy(chapter)
    changed["blocks"][0]["clean_text"] = "Different source."
    with pytest.raises(B1RegistryToB2InputError, match="source chapter differs"):
        build_b2_registry_input_package_v1(
            document={"document_id": "book", "chapters": [changed]},
            chapter_registries=[registry],
            current_git_head="head_a",
        )
