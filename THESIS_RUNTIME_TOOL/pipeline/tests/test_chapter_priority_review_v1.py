from __future__ import annotations

from copy import deepcopy

import pytest

from pipeline.literary.book_source_lineage import (
    build_book_source_manifest,
    state_lineage_id_for_manifest,
)
from pipeline.literary.chapter_prefix_prior_v1 import (
    CANDIDATE_ONLY_SCOPE,
    CHAPTER_CONFIRMED_SCOPE,
    PREFIX_PRIOR_SCHEMA_VERSION,
    PREFIX_PRIOR_VALIDATOR_VERSION,
    verify_chapter_prefix_prior_bundle_v1,
)
from pipeline.literary.chapter_priority_review_v1 import (
    ChapterPriorityReviewError,
    build_chapter_priority_review_index_v1,
    verify_chapter_priority_review_index_v1,
)
from pipeline.literary.checkpoint import canonical_hash


def _document() -> dict:
    return {
        "document_id": "priority_probe_book",
        "chapters": [
            {
                "chapter_id": "syn_ch01",
                "blocks": [
                    {
                        "block_id": "syn_ch01_b001",
                        "order_index": 1,
                        "block_type": "paragraph",
                        "clean_text": "Catherine entered North House.",
                    },
                    {
                        "block_id": "syn_ch01_b002",
                        "order_index": 2,
                        "block_type": "paragraph",
                        "clean_text": "Vale waited outside.",
                    },
                ],
            },
            {
                "chapter_id": "syn_ch02",
                "blocks": [
                    {
                        "block_id": "syn_ch02_b001",
                        "order_index": 1,
                        "block_type": "paragraph",
                        "clean_text": "Catherine returned to North House.",
                    },
                    {
                        "block_id": "syn_ch02_b002",
                        "order_index": 2,
                        "block_type": "paragraph",
                        "clean_text": "Vale answered.",
                    },
                ],
            },
        ],
    }


def _card(
    card_id: str,
    *,
    source_id: str,
    surface: str,
    chapter_id: str,
    block_id: str,
    authority: str,
) -> dict:
    body = {
        "prior_card_id": card_id,
        "canonical_surface": surface,
        "stable_surfaces": [surface],
        "effective_claims": {
            "referent_kind": "person",
            "referential_gender": None,
            "identity_summary": "A stable person candidate for a synthetic probe.",
        },
        "disputed_claims": [],
        "authority_scope": authority,
        "first_supported_block_id": block_id,
        "provenance_refs": [{"chapter_id": chapter_id, "block_id": block_id}],
        "source_candidate_id": source_id,
    }
    return {**body, "context_card_hash": canonical_hash(body)}


def _prefix(document: dict, cards: list[dict]) -> dict:
    manifest = build_book_source_manifest(document)
    active = [
        row for row in cards if row["authority_scope"] == CHAPTER_CONFIRMED_SCOPE
    ]
    candidate = [
        row for row in cards if row["authority_scope"] == CANDIDATE_ONLY_SCOPE
    ]
    body = {
        "schema_version": PREFIX_PRIOR_SCHEMA_VERSION,
        "validator_version": PREFIX_PRIOR_VALIDATOR_VERSION,
        "state_lineage_id": state_lineage_id_for_manifest(manifest),
        "book_source_manifest_hash": manifest["manifest_hash"],
        "coverage_through_chapter_id": "syn_ch02",
        "covered_chapter_ids": ["syn_ch01", "syn_ch02"],
        "audited_inventory_provenance": [],
        "claim_cards": [{"prior_card_id": row["prior_card_id"]} for row in active],
        "b0_context_cards": sorted(active, key=lambda row: row["prior_card_id"]),
        "candidate_only_context_cards": sorted(
            candidate, key=lambda row: row["prior_card_id"]
        ),
        "source_entity_manifest": [
            {
                "prior_card_id": row["prior_card_id"],
                "source_candidate_id": row["source_candidate_id"],
                "local_state": (
                    "confirmed"
                    if row["authority_scope"] == CHAPTER_CONFIRMED_SCOPE
                    else "pending"
                ),
            }
            for row in cards
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


def _priority(
    *,
    chapter_id: str,
    rows: list[dict] | None = None,
    issues: list[dict] | None = None,
) -> dict:
    return {
        "chapter_id": chapter_id,
        "chapter_priority_order": rows or [],
        "validation_report": {"priority_issues": issues or []},
    }


def _row(
    *, rank: int, surface: str, item_class: str, block_id: str, refs: list[str]
) -> dict:
    return {
        "rank": rank,
        "surface": surface,
        "item_class": item_class,
        "source_block_id": block_id,
        "resolved_refs": refs,
        "authority_effect": "none",
    }


def test_same_surface_same_active_card_is_recurrence_not_identity_conflict() -> None:
    document = _document()
    card = _card(
        "pcard_catherine",
        source_id="source_catherine",
        surface="Catherine",
        chapter_id="syn_ch01",
        block_id="syn_ch01_b001",
        authority=CHAPTER_CONFIRMED_SCOPE,
    )
    result = build_chapter_priority_review_index_v1(
        document=document,
        priority_artifacts={
            "syn_ch01": _priority(
                chapter_id="syn_ch01",
                rows=[
                    _row(
                        rank=1,
                        surface="Catherine",
                        item_class="new_entity",
                        block_id="syn_ch01_b001",
                        refs=["source_catherine"],
                    )
                ],
            ),
            "syn_ch02": _priority(
                chapter_id="syn_ch02",
                rows=[
                    _row(
                        rank=2,
                        surface="Catherine",
                        item_class="prior_entity",
                        block_id="syn_ch02_b001",
                        refs=["pcard_catherine"],
                    )
                ],
            ),
        },
        final_prefix_bundle=_prefix(document, [card]),
    )
    group = result["surface_groups"][0]
    assert group["ranked_chapter_count"] == 2
    assert group["subject_prior_card_ids"] == ["pcard_catherine"]
    assert group["signal_kinds"] == []
    assert result["review_leads"] == []


def test_same_surface_distinct_cards_routes_identity_review_without_authority() -> None:
    document = _document()
    cards = [
        _card(
            "pcard_catherine_old",
            source_id="source_catherine_old",
            surface="Catherine",
            chapter_id="syn_ch01",
            block_id="syn_ch01_b001",
            authority=CHAPTER_CONFIRMED_SCOPE,
        ),
        _card(
            "pcard_catherine_new",
            source_id="source_catherine_new",
            surface="Catherine",
            chapter_id="syn_ch02",
            block_id="syn_ch02_b001",
            authority=CANDIDATE_ONLY_SCOPE,
        ),
    ]
    result = build_chapter_priority_review_index_v1(
        document=document,
        priority_artifacts={
            "syn_ch01": _priority(
                chapter_id="syn_ch01",
                rows=[
                    _row(
                        rank=1,
                        surface="Catherine",
                        item_class="new_entity",
                        block_id="syn_ch01_b001",
                        refs=["source_catherine_old"],
                    )
                ],
            ),
            "syn_ch02": _priority(
                chapter_id="syn_ch02",
                rows=[
                    _row(
                        rank=1,
                        surface="Catherine",
                        item_class="new_entity",
                        block_id="syn_ch02_b001",
                        refs=["source_catherine_new"],
                    )
                ],
            ),
        },
        final_prefix_bundle=_prefix(document, cards),
    )
    assert result["counts"]["duplicate_priority_surface_group_count"] == 1
    lead = result["review_leads"][0]
    assert lead["route"] == "book_identity_auditor"
    assert lead["trigger_kinds"] == ["duplicate_priority_surface"]
    assert lead["subject_prior_card_ids"] == [
        "pcard_catherine_new",
        "pcard_catherine_old",
    ]
    assert lead["authority_effect"] == "none"


def test_priority_orphan_is_preserved_as_recall_review_lead() -> None:
    document = _document()
    result = build_chapter_priority_review_index_v1(
        document=document,
        priority_artifacts={
            "syn_ch01": _priority(
                chapter_id="syn_ch01",
                issues=[
                    {
                        "raw_rank": 2,
                        "reason": (
                            "priority row does not reference an emitted or supplied item"
                        ),
                        "raw_row": {
                            "surface": "North House",
                            "item_class": "new_entity",
                            "source_block_id": "syn_ch01_b001",
                        },
                    }
                ],
            ),
            "syn_ch02": _priority(chapter_id="syn_ch02"),
        },
        final_prefix_bundle=_prefix(document, []),
    )
    assert result["counts"]["priority_orphan_occurrence_count"] == 1
    lead = result["review_leads"][0]
    assert lead["route"] == "book_end_recall_review"
    assert lead["trigger_kinds"] == ["priority_orphan"]
    assert lead["subject_prior_card_ids"] == []


def test_recurring_candidate_only_priority_routes_book_identity_review() -> None:
    document = _document()
    card = _card(
        "pcard_vale",
        source_id="source_vale",
        surface="Vale",
        chapter_id="syn_ch01",
        block_id="syn_ch01_b002",
        authority=CANDIDATE_ONLY_SCOPE,
    )
    result = build_chapter_priority_review_index_v1(
        document=document,
        priority_artifacts={
            "syn_ch01": _priority(
                chapter_id="syn_ch01",
                rows=[
                    _row(
                        rank=3,
                        surface="Vale",
                        item_class="candidate_only_entity",
                        block_id="syn_ch01_b002",
                        refs=["source_vale"],
                    )
                ],
            ),
            "syn_ch02": _priority(
                chapter_id="syn_ch02",
                rows=[
                    _row(
                        rank=4,
                        surface="Vale",
                        item_class="candidate_only_entity",
                        block_id="syn_ch02_b002",
                        refs=["pcard_vale"],
                    )
                ],
            ),
        },
        final_prefix_bundle=_prefix(document, [card]),
    )
    group = result["surface_groups"][0]
    assert group["signal_kinds"] == [
        "recurring_priority_without_active_authority"
    ]
    assert result["review_leads"][0]["route"] == "book_identity_auditor"


def test_review_cap_defers_without_dropping_or_granting_authority() -> None:
    document = _document()
    result = build_chapter_priority_review_index_v1(
        document=document,
        priority_artifacts={
            "syn_ch01": _priority(
                chapter_id="syn_ch01",
                issues=[
                    {
                        "raw_rank": 1,
                        "reason": (
                            "priority row does not reference an emitted or supplied item"
                        ),
                        "raw_row": {
                            "surface": "Catherine",
                            "item_class": "new_entity",
                            "source_block_id": "syn_ch01_b001",
                        },
                    },
                    {
                        "raw_rank": 2,
                        "reason": (
                            "priority row does not reference an emitted or supplied item"
                        ),
                        "raw_row": {
                            "surface": "Vale",
                            "item_class": "new_entity",
                            "source_block_id": "syn_ch01_b002",
                        },
                    },
                ],
            ),
            "syn_ch02": _priority(chapter_id="syn_ch02"),
        },
        final_prefix_bundle=_prefix(document, []),
        max_review_leads=1,
    )
    assert len(result["review_leads"]) == 2
    assert result["counts"]["deferred_lead_count"] == 1
    assert all(row["authority_effect"] == "none" for row in result["review_leads"])


def test_tampered_priority_lead_cannot_gain_authority() -> None:
    document = _document()
    result = build_chapter_priority_review_index_v1(
        document=document,
        priority_artifacts={
            "syn_ch01": _priority(
                chapter_id="syn_ch01",
                issues=[
                    {
                        "raw_rank": 1,
                        "reason": (
                            "priority row does not reference an emitted or supplied item"
                        ),
                        "raw_row": {
                            "surface": "Catherine",
                            "item_class": "new_entity",
                            "source_block_id": "syn_ch01_b001",
                        },
                    }
                ],
            ),
            "syn_ch02": _priority(chapter_id="syn_ch02"),
        },
        final_prefix_bundle=_prefix(document, []),
    )
    tampered = deepcopy(result)
    tampered["review_leads"][0]["authority_effect"] = "promote"
    body = dict(tampered)
    body.pop("priority_review_index_hash")
    tampered["priority_review_index_hash"] = canonical_hash(body)
    with pytest.raises(ChapterPriorityReviewError, match="malformed"):
        verify_chapter_priority_review_index_v1(tampered, document=document)
