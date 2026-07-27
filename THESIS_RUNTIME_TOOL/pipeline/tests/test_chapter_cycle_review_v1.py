from __future__ import annotations

from copy import deepcopy

import pytest

from pipeline.literary.chapter_cycle_review_v1 import (
    ChapterCycleReviewError,
    build_chapter_cycle_review_ledger_v1,
    finalize_chapter_cycle_review_ledger_v1,
    verify_chapter_cycle_review_ledger_v1,
)
from pipeline.literary.checkpoint import canonical_hash


LINEAGE = "1" * 64
EVIDENCE = "2" * 64


def _pending(*, hearing_count: int = 1, prior_evidence: list[str] | None = None) -> dict:
    return {
        "prior_card_id": "card_claim",
        "disputed_field": "referential_gender",
        "revision_ids": ["revision_one"],
        "status": "pending",
        "pending_reason_codes": ["conflicting_evidence"],
        "evidence_manifest_hashes": prior_evidence or ["3" * 64],
        "hearing_count": hearing_count,
        "automatic_hearing_limit": 2,
        "same_evidence_reopen_forbidden": True,
        "next_review_trigger": "new_evidence_or_book_end",
    }


def _queue(*, pending: dict | None = None) -> dict:
    body = {
        "schema_version": "two_chapter_candidate_review_queue_v1",
        "ticket_index_hash": "4" * 64,
        "identity_referrals": [],
        "pending_identity_reviews": [],
        "prefix_identity_uncertainties": [],
        "candidate_identity_observations": [],
        "candidate_claim_evidence_queue": [
            {
                "prior_card_id": "card_claim",
                "observation": "new_claim_evidence",
                "disputed_field": "referential_gender",
                "source_block_ids": ["bk_ch02_b001"],
                "reason": "The current chapter supplies additional evidence.",
                "evidence_manifest_hash": EVIDENCE,
                "pending_claim_snapshot": pending or _pending(),
            }
        ],
        "production_publish_performed": False,
    }
    return {**body, "queue_hash": canonical_hash(body)}


def test_new_pending_claim_evidence_queues_one_automatic_hearing() -> None:
    ledger = build_chapter_cycle_review_ledger_v1(
        state_lineage_id=LINEAGE,
        chapter_id="bk_ch02",
        candidate_review_queue=_queue(),
    )
    verified = verify_chapter_cycle_review_ledger_v1(ledger)
    assert len(verified["review_items"]) == 1
    row = verified["review_items"][0]
    assert row["route"] == "stable_claim_rehearing"
    assert row["lifecycle_state"] == "queued"
    assert row["authority_effect"] == "none"
    assert row["reopen_classification"]["route"] == "automatic_hearing"


def test_same_evidence_is_suppressed_and_hearing_limit_defers_book_end() -> None:
    duplicate = build_chapter_cycle_review_ledger_v1(
        state_lineage_id=LINEAGE,
        chapter_id="bk_ch02",
        candidate_review_queue=_queue(
            pending=_pending(prior_evidence=[EVIDENCE])
        ),
    )
    assert duplicate["review_items"][0]["lifecycle_state"] == (
        "duplicate_suppressed"
    )
    capped = build_chapter_cycle_review_ledger_v1(
        state_lineage_id=LINEAGE,
        chapter_id="bk_ch02",
        candidate_review_queue=_queue(pending=_pending(hearing_count=2)),
    )
    assert capped["review_items"][0]["lifecycle_state"] == "book_end_pending"
    assert capped["review_items"][0]["reopen_classification"]["route"] == (
        "defer_book_end_or_human"
    )


def test_identity_and_continuity_observations_never_grant_authority() -> None:
    queue = _queue()
    queue["candidate_claim_evidence_queue"] = []
    queue["candidate_identity_observations"] = [
        {
            "prior_card_id": "card_identity",
            "observation": "possible_collision",
            "disputed_field": None,
            "source_block_ids": ["bk_ch02_b002"],
            "reason": "The surface may name a distinct referent.",
            "evidence_manifest_hash": "5" * 64,
            "pending_claim_snapshot": None,
        },
        {
            "prior_card_id": "card_continuity",
            "observation": "supports_continuity",
            "disputed_field": None,
            "source_block_ids": ["bk_ch02_b003"],
            "reason": "The surface remains compatible with the prior card.",
            "evidence_manifest_hash": "6" * 64,
            "pending_claim_snapshot": None,
        },
    ]
    queue_body = dict(queue)
    queue_body.pop("queue_hash")
    queue = {**queue_body, "queue_hash": canonical_hash(queue_body)}
    ledger = build_chapter_cycle_review_ledger_v1(
        state_lineage_id=LINEAGE,
        chapter_id="bk_ch02",
        candidate_review_queue=queue,
    )
    by_route = {row["route"]: row for row in ledger["review_items"]}
    assert by_route["book_identity_auditor"]["lifecycle_state"] == "queued"
    assert by_route["continuity_evidence"]["lifecycle_state"] == "evidence_only"
    assert {row["authority_effect"] for row in ledger["review_items"]} == {"none"}


def test_ledger_replay_is_idempotent_and_book_end_is_finite() -> None:
    first = build_chapter_cycle_review_ledger_v1(
        state_lineage_id=LINEAGE,
        chapter_id="bk_ch02",
        candidate_review_queue=_queue(),
    )
    replay = build_chapter_cycle_review_ledger_v1(
        state_lineage_id=LINEAGE,
        chapter_id="bk_ch02",
        candidate_review_queue=_queue(),
        previous_ledger=first,
    )
    assert replay["review_items"] == first["review_items"]
    assert replay["review_ledger_hash"] == first["review_ledger_hash"]
    finalized = finalize_chapter_cycle_review_ledger_v1(replay)
    assert finalized["review_items"][0]["lifecycle_state"] == "book_end_pending"
    assert finalized["book_end_finalized"] is True


def test_tampered_queue_and_foreign_book_end_id_fail_closed() -> None:
    queue = _queue()
    queue["candidate_claim_evidence_queue"][0]["source_block_ids"] = [
        "bk_ch02_b999"
    ]
    with pytest.raises(ChapterCycleReviewError, match="hash mismatch"):
        build_chapter_cycle_review_ledger_v1(
            state_lineage_id=LINEAGE,
            chapter_id="bk_ch02",
            candidate_review_queue=queue,
        )
    ledger = build_chapter_cycle_review_ledger_v1(
        state_lineage_id=LINEAGE,
        chapter_id="bk_ch02",
        candidate_review_queue=_queue(),
    )
    with pytest.raises(ChapterCycleReviewError, match="foreign review item"):
        finalize_chapter_cycle_review_ledger_v1(
            deepcopy(ledger), closed_review_item_ids=["cycrev1_foreign"]
        )
