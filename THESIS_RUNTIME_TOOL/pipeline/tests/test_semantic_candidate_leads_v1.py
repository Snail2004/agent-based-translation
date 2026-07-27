from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from pipeline.literary.book_source_lineage import (
    build_book_source_manifest,
    state_lineage_id_for_manifest,
)
from pipeline.literary.chapter_cycle_review_v1 import (
    REVIEW_LEDGER_SCHEMA_VERSION,
    REVIEW_LEDGER_VALIDATOR_VERSION,
    append_prefix_identity_uncertainties_v1,
)
from pipeline.literary.chapter_cycle_profile_v1 import load_chapter_cycle_profile
from pipeline.literary.chapter_prefix_prior_v1 import (
    CANDIDATE_ONLY_SCOPE,
    CHAPTER_CONFIRMED_SCOPE,
    PREFIX_PRIOR_SCHEMA_VERSION,
    PREFIX_PRIOR_VALIDATOR_VERSION,
    verify_chapter_prefix_prior_bundle_v1,
)
from pipeline.literary.checkpoint import canonical_hash
from pipeline.literary.incremental_identity_auditor_v1 import (
    build_incremental_identity_index_v1,
)
from pipeline.literary.semantic_candidate_leads_v1 import (
    SemanticCandidateLeadError,
    apply_semantic_candidate_leads_to_prefix_v1,
    build_semantic_candidate_lead_index_from_profile_v1,
    build_semantic_candidate_lead_index_v1,
    compatible_prior_card_ids_from_challenge_v1,
    materialize_waiting_identity_occurrences_v1,
    verify_semantic_identity_occurrence_bridge_v1,
    verify_semantic_candidate_lead_index_v1,
)


PROFILE_PATH = (
    Path(__file__).resolve().parents[1]
    / "configs"
    / "literary_chapter_cycle_profile_v1.json"
)


def _document(chapter_two_texts: list[str] | None = None) -> dict:
    texts = chapter_two_texts or ["North House opened again."]
    return {
        "document_id": "synthetic_t_lite_book",
        "chapters": [
            {
                "chapter_id": "syn_ch01",
                "blocks": [
                    {
                        "block_id": "syn_ch01_b001",
                        "order_index": 1,
                        "block_type": "paragraph",
                        "clean_text": "North House stood beyond the field.",
                    },
                    {
                        "block_id": "syn_ch01_b002",
                        "order_index": 2,
                        "block_type": "paragraph",
                        "clean_text": "An unnamed hound waited near the gate.",
                    },
                ],
            },
            {
                "chapter_id": "syn_ch02",
                "blocks": [
                    {
                        "block_id": f"syn_ch02_b{offset:03d}",
                        "order_index": offset,
                        "block_type": "paragraph",
                        "clean_text": text,
                    }
                    for offset, text in enumerate(texts, start=1)
                ],
            },
        ],
    }


def _card(
    card_id: str,
    *,
    surface: str,
    kind: str,
    chapter_id: str,
    block_id: str,
    authority: str = CHAPTER_CONFIRMED_SCOPE,
    gender: str | None = None,
) -> dict:
    body = {
        "prior_card_id": card_id,
        "canonical_surface": surface,
        "stable_surfaces": [surface],
        "effective_claims": {
            "referent_kind": kind,
            "referential_gender": gender,
            "identity_summary": f"A stable {kind} candidate used by the synthetic probe.",
        },
        "disputed_claims": [],
        "authority_scope": authority,
        "first_supported_block_id": block_id,
        "provenance_refs": [{"chapter_id": chapter_id, "block_id": block_id}],
        "source_candidate_id": f"source_{card_id}",
    }
    return {**body, "context_card_hash": canonical_hash(body)}


def _prefix(
    document: dict,
    *,
    prior_cards: list[dict],
    current_cards: list[dict] | None = None,
    unresolved_ids: set[str] | None = None,
) -> dict:
    current_cards = current_cards or []
    unresolved_ids = unresolved_ids or set()
    all_cards = [*prior_cards, *current_cards]
    active = [
        row for row in all_cards if row["authority_scope"] == CHAPTER_CONFIRMED_SCOPE
    ]
    candidate = [
        row for row in all_cards if row["authority_scope"] == CANDIDATE_ONLY_SCOPE
    ]
    manifest = build_book_source_manifest(document)
    body = {
        "schema_version": PREFIX_PRIOR_SCHEMA_VERSION,
        "validator_version": PREFIX_PRIOR_VALIDATOR_VERSION,
        "state_lineage_id": state_lineage_id_for_manifest(manifest),
        "book_source_manifest_hash": manifest["manifest_hash"],
        "coverage_through_chapter_id": "syn_ch02",
        "covered_chapter_ids": ["syn_ch01", "syn_ch02"],
        "audited_inventory_provenance": [],
        "claim_cards": [
            {"prior_card_id": row["prior_card_id"]} for row in active
        ],
        "b0_context_cards": sorted(active, key=lambda row: row["prior_card_id"]),
        "candidate_only_context_cards": sorted(
            candidate, key=lambda row: row["prior_card_id"]
        ),
        "source_entity_manifest": [
            {
                "prior_card_id": row["prior_card_id"],
                "local_state": (
                    "unresolved"
                    if row["prior_card_id"] in unresolved_ids
                    else (
                        "confirmed"
                        if row["authority_scope"] == CHAPTER_CONFIRMED_SCOPE
                        else "pending"
                    )
                ),
            }
            for row in all_cards
        ],
        "glossary_context_cards": [],
        "source_glossary_manifest": [],
        "claim_projection_hashes": [],
        "glossary_projection_hashes": [],
        "prefix_identity_uncertainties": [],
        "production_publish_performed": False,
    }
    result = {**body, "prefix_bundle_hash": canonical_hash(body)}
    return verify_chapter_prefix_prior_bundle_v1(result, document=document)


def _empty_review(prefix: dict) -> dict:
    body = {
        "schema_version": REVIEW_LEDGER_SCHEMA_VERSION,
        "validator_version": REVIEW_LEDGER_VALIDATOR_VERSION,
        "state_lineage_id": prefix["state_lineage_id"],
        "coverage_through_chapter_id": "syn_ch02",
        "observed_queue_hashes": ["a" * 64],
        "review_items": [],
        "production_publish_performed": False,
    }
    return {**body, "review_ledger_hash": canonical_hash(body)}


def test_first_recurrence_gets_a_review_only_occurrence_pair() -> None:
    document = _document()
    prior = _card(
        "pcard_north",
        surface="North House",
        kind="place",
        chapter_id="syn_ch01",
        block_id="syn_ch01_b001",
    )
    prefix = _prefix(document, prior_cards=[prior])
    index = build_semantic_candidate_lead_index_v1(
        document=document,
        prefix_bundle=prefix,
        current_chapter_id="syn_ch02",
    )
    assert index["counts"]["waiting_for_pair_count"] == 1
    lead = index["leads"][0]
    assert lead["trigger_kind"] == "first_recurrence_weak_card"
    assert lead["current_candidate_card_ids"] == []
    assert lead["authority_effect"] == "none"

    projected = apply_semantic_candidate_leads_to_prefix_v1(
        prefix_bundle=prefix,
        lead_index=index,
    )
    projected, bridge = materialize_waiting_identity_occurrences_v1(
        document=document,
        prefix_bundle=projected,
        lead_index=index,
    )
    assert verify_semantic_identity_occurrence_bridge_v1(bridge) == bridge
    assert bridge["counts"] == {
        "waiting_input_count": 1,
        "paired_count": 1,
        "unlocatable_count": 0,
        "component_cap_deferred_count": 0,
        "occurrence_cap_deferred_count": 0,
        "occurrence_card_count": 1,
        "unresolved_count": 0,
    }
    assert projected["b0_context_cards"] == []
    occurrence_ids = bridge["rows"][0]["occurrence_candidate_card_ids"]
    assert len(occurrence_ids) == 1
    by_id = {
        row["prior_card_id"]: row
        for row in projected["candidate_only_context_cards"]
    }
    assert set(by_id) == {"pcard_north", *occurrence_ids}
    occurrence = by_id[occurrence_ids[0]]
    assert occurrence["canonical_surface"] == "North House"
    assert occurrence["effective_claims"] == {
        "referent_kind": None,
        "referential_gender": None,
        "identity_summary": None,
    }
    assert occurrence["authority_scope"] == CANDIDATE_ONLY_SCOPE
    uncertainty = projected["prefix_identity_uncertainties"][0]
    assert set(uncertainty["prior_card_ids"]) == {"pcard_north", *occurrence_ids}

    review = append_prefix_identity_uncertainties_v1(
        ledger=_empty_review(projected),
        prefix_bundle=projected,
        chapter_id="syn_ch02",
    )
    identity = build_incremental_identity_index_v1(
        document=document,
        prefix_bundle=projected,
        review_ledger=review,
    )
    assert len(identity["components"]) == 1
    assert identity["singleton_review_item_ids"] == []
    assert set(identity["components"][0]["candidate_prior_card_ids"]) == {
        "pcard_north",
        *occurrence_ids,
    }
    replayed, _ = materialize_waiting_identity_occurrences_v1(
        document=document,
        prefix_bundle=projected,
        lead_index=index,
    )
    assert replayed == projected

    tampered_body = deepcopy(bridge)
    tampered_body.pop("bridge_hash")
    tampered_body["max_occurrence_cards_per_lead"] = 0
    tampered = {**tampered_body, "bridge_hash": canonical_hash(tampered_body)}
    with pytest.raises(SemanticCandidateLeadError, match="cap"):
        verify_semantic_identity_occurrence_bridge_v1(tampered)


def test_explicit_same_referent_confirmation_cannot_override_weak_singleton() -> None:
    document = _document()
    prior = _card(
        "pcard_north",
        surface="North House",
        kind="place",
        chapter_id="syn_ch01",
        block_id="syn_ch01_b001",
    )
    prefix = _prefix(document, prior_cards=[prior])
    index = build_semantic_candidate_lead_index_v1(
        document=document,
        prefix_bundle=prefix,
        current_chapter_id="syn_ch02",
    )
    challenge_body = {
        "schema_version": "b0_entity_prior_challenge_exp_v6",
        "prior_card_dispositions": [
            {
                "prior_card_id": "pcard_north",
                "verdict": "compatible",
                "referent_continuity": "same_referent",
            }
        ],
    }
    challenge = {
        **challenge_body,
        "prior_challenge_artifact_hash": canonical_hash(challenge_body),
    }
    confirmed = compatible_prior_card_ids_from_challenge_v1(challenge)
    assert confirmed == ["pcard_north"]

    projected = apply_semantic_candidate_leads_to_prefix_v1(
        prefix_bundle=prefix,
        lead_index=index,
        continuity_confirmed_prior_card_ids=confirmed,
    )
    assert projected["b0_context_cards"] == []
    assert [row["prior_card_id"] for row in projected["candidate_only_context_cards"]] == [
        "pcard_north"
    ]
    assert len(projected["prefix_identity_uncertainties"]) == 1
    assert projected["prefix_identity_uncertainties"][0]["reason_code"] == (
        "semantic_candidate_first_recurrence_weak_card"
    )
    bridged, bridge = materialize_waiting_identity_occurrences_v1(
        document=document,
        prefix_bundle=projected,
        lead_index=index,
    )
    assert bridge["counts"]["paired_count"] == 1
    assert len(bridged["candidate_only_context_cards"]) == 2

    tampered = deepcopy(challenge)
    tampered["prior_card_dispositions"][0]["verdict"] = "uncertain"
    with pytest.raises(SemanticCandidateLeadError):
        compatible_prior_card_ids_from_challenge_v1(tampered)


def test_occurrences_in_multiple_blocks_remain_separate_candidates() -> None:
    document = _document(
        [
            "North House opened again.",
            "A messenger named North House in the same chapter.",
        ]
    )
    prior = _card(
        "pcard_north",
        surface="North House",
        kind="place",
        chapter_id="syn_ch01",
        block_id="syn_ch01_b001",
    )
    prefix = _prefix(document, prior_cards=[prior])
    index = build_semantic_candidate_lead_index_v1(
        document=document,
        prefix_bundle=prefix,
        current_chapter_id="syn_ch02",
    )
    projected = apply_semantic_candidate_leads_to_prefix_v1(
        prefix_bundle=prefix,
        lead_index=index,
    )

    bridged, report = materialize_waiting_identity_occurrences_v1(
        document=document,
        prefix_bundle=projected,
        lead_index=index,
    )

    occurrence_ids = report["rows"][0]["occurrence_candidate_card_ids"]
    assert len(occurrence_ids) == 2
    by_id = {
        row["prior_card_id"]: row
        for row in bridged["candidate_only_context_cards"]
    }
    assert {
        by_id[card_id]["first_supported_block_id"] for card_id in occurrence_ids
    } == {"syn_ch02_b001", "syn_ch02_b002"}
    assert len(bridged["prefix_identity_uncertainties"]) == 1
    assert set(bridged["prefix_identity_uncertainties"][0]["prior_card_ids"]) == {
        "pcard_north",
        *occurrence_ids,
    }


def test_unlocatable_exact_surface_stays_visible_without_a_card() -> None:
    document = _document(["North\u2014House opened again."])
    prior = _card(
        "pcard_north",
        surface="North House",
        kind="place",
        chapter_id="syn_ch01",
        block_id="syn_ch01_b001",
    )
    prefix = _prefix(document, prior_cards=[prior])
    index = build_semantic_candidate_lead_index_v1(
        document=document,
        prefix_bundle=prefix,
        current_chapter_id="syn_ch02",
    )
    assert index["counts"]["waiting_for_pair_count"] == 1
    projected = apply_semantic_candidate_leads_to_prefix_v1(
        prefix_bundle=prefix,
        lead_index=index,
    )

    bridged, report = materialize_waiting_identity_occurrences_v1(
        document=document,
        prefix_bundle=projected,
        lead_index=index,
    )

    assert report["rows"][0]["lifecycle_state"] == "unlocatable_surface"
    assert report["counts"]["unresolved_count"] == 1
    assert [
        row["prior_card_id"] for row in bridged["candidate_only_context_cards"]
    ] == ["pcard_north"]


def test_occurrence_component_cap_defers_without_minting_a_card() -> None:
    document = _document(["North House opened again."])
    prior = _card(
        "pcard_north",
        surface="North House",
        kind="place",
        chapter_id="syn_ch01",
        block_id="syn_ch01_b001",
    )
    prefix = _prefix(document, prior_cards=[prior])
    index = build_semantic_candidate_lead_index_v1(
        document=document,
        prefix_bundle=prefix,
        current_chapter_id="syn_ch02",
    )
    tampered_body = deepcopy(index)
    tampered_body.pop("lead_index_hash")
    tampered_body["bounds"]["max_identity_components_per_chapter"] = 0
    tampered = {
        **tampered_body,
        "lead_index_hash": canonical_hash(tampered_body),
    }
    projected = apply_semantic_candidate_leads_to_prefix_v1(
        prefix_bundle=prefix,
        lead_index=tampered,
    )

    bridged, report = materialize_waiting_identity_occurrences_v1(
        document=document,
        prefix_bundle=projected,
        lead_index=tampered,
    )

    assert report["rows"][0]["lifecycle_state"] == "component_cap_deferred"
    assert report["counts"]["unresolved_count"] == 1
    assert len(bridged["candidate_only_context_cards"]) == 1


def test_repeated_same_evidence_is_duplicate_suppressed() -> None:
    document = _document()
    prefix = _prefix(
        document,
        prior_cards=[
            _card(
                "pcard_north",
                surface="North House",
                kind="place",
                chapter_id="syn_ch01",
                block_id="syn_ch01_b001",
            )
        ],
    )
    first = build_semantic_candidate_lead_index_v1(
        document=document,
        prefix_bundle=prefix,
        current_chapter_id="syn_ch02",
    )
    replay = build_semantic_candidate_lead_index_v1(
        document=document,
        prefix_bundle=prefix,
        current_chapter_id="syn_ch02",
        previous_lead_index=first,
    )
    assert replay["leads"][0]["lead_id"] == first["leads"][0]["lead_id"]
    assert replay["leads"][0]["lifecycle_state"] == "duplicate_suppressed"
    assert replay["counts"]["duplicate_suppressed_count"] == 1


def test_surface_core_overlap_routes_one_bounded_identity_component() -> None:
    document = _document(["The North House received another visitor."])
    prefix = _prefix(
        document,
        prior_cards=[
            _card(
                "pcard_north",
                surface="North House",
                kind="place",
                chapter_id="syn_ch01",
                block_id="syn_ch01_b001",
            )
        ],
        current_cards=[
            _card(
                "pcard_the_north",
                surface="The North House",
                kind="place",
                chapter_id="syn_ch02",
                block_id="syn_ch02_b001",
            )
        ],
    )
    leads = build_semantic_candidate_lead_index_v1(
        document=document,
        prefix_bundle=prefix,
        current_chapter_id="syn_ch02",
    )
    assert [row["trigger_kind"] for row in leads["leads"]] == [
        "surface_core_overlap"
    ]
    assert leads["leads"][0]["lifecycle_state"] == "queued_pairable"
    projected = apply_semantic_candidate_leads_to_prefix_v1(
        prefix_bundle=prefix,
        lead_index=leads,
        continuity_confirmed_prior_card_ids=["pcard_north"],
    )
    assert projected["b0_context_cards"] == []
    review = append_prefix_identity_uncertainties_v1(
        ledger=_empty_review(projected),
        prefix_bundle=projected,
        chapter_id="syn_ch02",
    )
    identity = build_incremental_identity_index_v1(
        document=document,
        prefix_bundle=projected,
        review_ledger=review,
    )
    assert len(identity["components"]) == 1
    assert set(identity["components"][0]["candidate_prior_card_ids"]) == {
        "pcard_north",
        "pcard_the_north",
    }


def test_person_terminal_token_overlap_routes_review_without_deciding_identity() -> None:
    document = _document(["Mrs. Vale arrived before dawn."])
    prefix = _prefix(
        document,
        prior_cards=[
            _card(
                "pcard_avery_vale",
                surface="Avery Vale",
                kind="person",
                chapter_id="syn_ch01",
                block_id="syn_ch01_b001",
                gender="feminine",
            )
        ],
        current_cards=[
            _card(
                "pcard_mrs_vale",
                surface="Mrs. Vale",
                kind="person",
                chapter_id="syn_ch02",
                block_id="syn_ch02_b001",
                gender="feminine",
            )
        ],
    )

    leads = build_semantic_candidate_lead_index_v1(
        document=document,
        prefix_bundle=prefix,
        current_chapter_id="syn_ch02",
    )

    assert [row["trigger_kind"] for row in leads["leads"]] == [
        "person_terminal_token_overlap"
    ]
    assert leads["leads"][0]["surface_keys"] == ["vale"]
    projected = apply_semantic_candidate_leads_to_prefix_v1(
        prefix_bundle=prefix,
        lead_index=leads,
    )
    assert projected["b0_context_cards"] == []
    assert {
        row["prior_card_id"] for row in projected["candidate_only_context_cards"]
    } == {"pcard_avery_vale", "pcard_mrs_vale"}
    review = append_prefix_identity_uncertainties_v1(
        ledger=_empty_review(projected),
        prefix_bundle=projected,
        chapter_id="syn_ch02",
    )
    identity = build_incremental_identity_index_v1(
        document=document,
        prefix_bundle=projected,
        review_ledger=review,
    )
    assert len(identity["components"]) == 1
    assert set(identity["components"][0]["candidate_prior_card_ids"]) == {
        "pcard_avery_vale",
        "pcard_mrs_vale",
    }


def test_person_terminal_overlap_uses_known_gender_to_bound_family_components() -> None:
    document = _document(
        [
            "Mr. Vale arrived before Mrs. Vale joined Rowan Vale.",
            "Avery Vale remained at the gate.",
        ]
    )
    prefix = _prefix(
        document,
        prior_cards=[
            _card(
                "pcard_avery_vale",
                surface="Avery Vale",
                kind="person",
                chapter_id="syn_ch01",
                block_id="syn_ch01_b001",
                gender="feminine",
            ),
            _card(
                "pcard_rowan_vale",
                surface="Rowan Vale",
                kind="person",
                chapter_id="syn_ch01",
                block_id="syn_ch01_b002",
                gender="masculine",
            ),
        ],
        current_cards=[
            _card(
                "pcard_mrs_vale",
                surface="Mrs. Vale",
                kind="person",
                chapter_id="syn_ch02",
                block_id="syn_ch02_b001",
                gender="feminine",
            ),
            _card(
                "pcard_mr_vale",
                surface="Mr. Vale",
                kind="person",
                chapter_id="syn_ch02",
                block_id="syn_ch02_b001",
                gender="masculine",
            ),
        ],
    )

    leads = build_semantic_candidate_lead_index_v1(
        document=document,
        prefix_bundle=prefix,
        current_chapter_id="syn_ch02",
    )

    terminal = [
        row
        for row in leads["leads"]
        if row["trigger_kind"] == "person_terminal_token_overlap"
    ]
    assert len(terminal) == 2
    assert leads["counts"]["queued_component_count"] == 2
    assert {
        tuple([*row["prior_card_ids"], *row["current_candidate_card_ids"]])
        for row in terminal
    } == {
        ("pcard_avery_vale", "pcard_mrs_vale"),
        ("pcard_rowan_vale", "pcard_mr_vale"),
    }


def test_person_terminal_overlap_without_known_gender_stays_out_of_code_pairing() -> None:
    document = _document(["Mrs. Vale arrived before dawn."])
    prefix = _prefix(
        document,
        prior_cards=[
            _card(
                "pcard_avery_vale",
                surface="Avery Vale",
                kind="person",
                chapter_id="syn_ch01",
                block_id="syn_ch01_b001",
            )
        ],
        current_cards=[
            _card(
                "pcard_mrs_vale",
                surface="Mrs. Vale",
                kind="person",
                chapter_id="syn_ch02",
                block_id="syn_ch02_b001",
                gender="feminine",
            )
        ],
    )

    leads = build_semantic_candidate_lead_index_v1(
        document=document,
        prefix_bundle=prefix,
        current_chapter_id="syn_ch02",
    )

    assert leads["leads"] == []


def test_shared_terminal_token_does_not_route_non_person_cards() -> None:
    document = _document(["South House opened before dawn."])
    prefix = _prefix(
        document,
        prior_cards=[
            _card(
                "pcard_north_house",
                surface="North House",
                kind="place",
                chapter_id="syn_ch01",
                block_id="syn_ch01_b001",
            )
        ],
        current_cards=[
            _card(
                "pcard_south_house",
                surface="South House",
                kind="place",
                chapter_id="syn_ch02",
                block_id="syn_ch02_b001",
            )
        ],
    )

    result = build_semantic_candidate_lead_index_v1(
        document=document,
        prefix_bundle=prefix,
        current_chapter_id="syn_ch02",
    )

    assert result["leads"] == []


def test_exact_surface_collision_is_pairable_and_content_addressed() -> None:
    document = _document(["Vale returned at dawn."])
    prefix = _prefix(
        document,
        prior_cards=[
            _card(
                "pcard_vale_old",
                surface="Vale",
                kind="person",
                chapter_id="syn_ch01",
                block_id="syn_ch01_b001",
            )
        ],
        current_cards=[
            _card(
                "pcard_vale_new",
                surface="Vale",
                kind="person",
                chapter_id="syn_ch02",
                block_id="syn_ch02_b001",
            )
        ],
    )
    first = build_semantic_candidate_lead_index_v1(
        document=document,
        prefix_bundle=prefix,
        current_chapter_id="syn_ch02",
    )
    second = build_semantic_candidate_lead_index_v1(
        document=document,
        prefix_bundle=prefix,
        current_chapter_id="syn_ch02",
    )
    assert first == second
    assert first["leads"][0]["trigger_kind"] == "exact_surface_collision"
    assert first["leads"][0]["lead_id"].startswith("semlead1_")


def test_incompatible_referent_kinds_do_not_create_a_lead() -> None:
    document = _document(["The North House appeared in the account."])
    prefix = _prefix(
        document,
        prior_cards=[
            _card(
                "pcard_place",
                surface="North House",
                kind="place",
                chapter_id="syn_ch01",
                block_id="syn_ch01_b001",
            )
        ],
        current_cards=[
            _card(
                "pcard_person",
                surface="The North House",
                kind="person",
                chapter_id="syn_ch02",
                block_id="syn_ch02_b001",
            )
        ],
    )
    result = build_semantic_candidate_lead_index_v1(
        document=document,
        prefix_bundle=prefix,
        current_chapter_id="syn_ch02",
    )
    assert result["leads"] == []


@pytest.mark.parametrize("surface", ["he", "the master"])
def test_unresolved_generic_surface_never_becomes_a_global_identity_lead(
    surface: str,
) -> None:
    document = _document([f"{surface} appeared again."])
    prior = _card(
        "pcard_generic_old",
        surface=surface,
        kind="person",
        chapter_id="syn_ch01",
        block_id="syn_ch01_b001",
        authority=CANDIDATE_ONLY_SCOPE,
    )
    current = _card(
        "pcard_generic_new",
        surface=surface,
        kind="person",
        chapter_id="syn_ch02",
        block_id="syn_ch02_b001",
        authority=CANDIDATE_ONLY_SCOPE,
    )
    prefix = _prefix(
        document,
        prior_cards=[prior],
        current_cards=[current],
        unresolved_ids={"pcard_generic_old", "pcard_generic_new"},
    )
    result = build_semantic_candidate_lead_index_v1(
        document=document,
        prefix_bundle=prefix,
        current_chapter_id="syn_ch02",
    )
    assert result["leads"] == []


def test_overflow_defers_without_halt_or_authority() -> None:
    names = ["Alder Vale", "Birch Vale", "Cedar Vale"]
    document = _document(["Alder Vale, Birch Vale, and Cedar Vale returned."])
    prior = [
        _card(
            f"pcard_old_{offset}",
            surface=name,
            kind="person",
            chapter_id="syn_ch01",
            block_id="syn_ch01_b001",
        )
        for offset, name in enumerate(names)
    ]
    current = [
        _card(
            f"pcard_new_{offset}",
            surface=name,
            kind="person",
            chapter_id="syn_ch02",
            block_id="syn_ch02_b001",
        )
        for offset, name in enumerate(names)
    ]
    prefix = _prefix(document, prior_cards=prior, current_cards=current)
    leads = build_semantic_candidate_lead_index_v1(
        document=document,
        prefix_bundle=prefix,
        current_chapter_id="syn_ch02",
        max_leads_per_chapter=3,
        max_identity_components_per_chapter=1,
    )
    assert leads["counts"]["queued_pairable_count"] == 1
    assert leads["counts"]["chapter_cap_deferred_count"] == 2
    assert all(row["authority_effect"] == "none" for row in leads["leads"])
    projected = apply_semantic_candidate_leads_to_prefix_v1(
        prefix_bundle=prefix,
        lead_index=leads,
    )
    assert projected["b0_context_cards"] == []
    review = append_prefix_identity_uncertainties_v1(
        ledger=_empty_review(projected),
        prefix_bundle=projected,
        chapter_id="syn_ch02",
    )
    assert sum(row["lifecycle_state"] == "queued" for row in review["review_items"]) == 1
    assert sum(row["lifecycle_state"] == "evidence_only" for row in review["review_items"]) == 2

    tampered = deepcopy(leads)
    deferred = next(
        row for row in tampered["leads"] if row["lifecycle_state"] == "chapter_cap_deferred"
    )
    deferred["lifecycle_state"] = "queued_pairable"
    tampered["counts"]["queued_pairable_count"] += 1
    tampered["counts"]["chapter_cap_deferred_count"] -= 1
    tampered["counts"]["queued_component_count"] += 1
    body = dict(tampered)
    body.pop("lead_index_hash")
    tampered["lead_index_hash"] = canonical_hash(body)
    with pytest.raises(SemanticCandidateLeadError, match="component count exceeds"):
        verify_semantic_candidate_lead_index_v1(tampered)

    lead_capped = build_semantic_candidate_lead_index_v1(
        document=document,
        prefix_bundle=prefix,
        current_chapter_id="syn_ch02",
        max_leads_per_chapter=1,
        max_identity_components_per_chapter=3,
    )
    assert lead_capped["counts"]["chapter_cap_deferred_count"] == 2


def test_nonlexical_unnamed_animal_stays_explicitly_unsupported() -> None:
    document = _document(["A kennel dog scratched at the door."])
    prior = _card(
        "pcard_hound",
        surface="the hound",
        kind="animal",
        chapter_id="syn_ch01",
        block_id="syn_ch01_b002",
        authority=CANDIDATE_ONLY_SCOPE,
    )
    current = _card(
        "pcard_kennel_dog",
        surface="a kennel dog",
        kind="animal",
        chapter_id="syn_ch02",
        block_id="syn_ch02_b001",
        authority=CANDIDATE_ONLY_SCOPE,
    )
    prefix = _prefix(
        document,
        prior_cards=[prior],
        current_cards=[current],
        unresolved_ids={"pcard_hound", "pcard_kennel_dog"},
    )
    result = build_semantic_candidate_lead_index_v1(
        document=document,
        prefix_bundle=prefix,
        current_chapter_id="syn_ch02",
    )
    assert result["leads"] == []
    assert result["counts"]["unsupported_watch_count"] == 1
    assert result["unsupported_watch_items"][0]["resolution_state"] == (
        "unsupported_by_t_lite"
    )


def test_tampered_lead_cannot_gain_authority() -> None:
    document = _document()
    prefix = _prefix(
        document,
        prior_cards=[
            _card(
                "pcard_north",
                surface="North House",
                kind="place",
                chapter_id="syn_ch01",
                block_id="syn_ch01_b001",
            )
        ],
    )
    index = build_semantic_candidate_lead_index_v1(
        document=document,
        prefix_bundle=prefix,
        current_chapter_id="syn_ch02",
    )
    tampered = deepcopy(index)
    tampered["leads"][0]["authority_effect"] = "merge"
    body = dict(tampered)
    body.pop("lead_index_hash")
    tampered["lead_index_hash"] = canonical_hash(body)
    with pytest.raises(SemanticCandidateLeadError, match="grants authority"):
        verify_semantic_candidate_lead_index_v1(tampered)


def test_console_profile_is_the_runtime_source_for_t_lite_caps() -> None:
    document = _document()
    prefix = _prefix(
        document,
        prior_cards=[
            _card(
                "pcard_north",
                surface="North House",
                kind="place",
                chapter_id="syn_ch01",
                block_id="syn_ch01_b001",
            )
        ],
    )
    profile = load_chapter_cycle_profile(PROFILE_PATH)
    result = build_semantic_candidate_lead_index_from_profile_v1(
        document=document,
        prefix_bundle=prefix,
        current_chapter_id="syn_ch02",
        chapter_cycle_profile=profile,
    )
    assert result["bounds"] == {
        "max_leads_per_chapter": 8,
        "max_identity_components_per_chapter": 4,
    }
