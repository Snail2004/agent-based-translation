from __future__ import annotations

from copy import deepcopy

import pytest

from pipeline.literary.b1_cross_chapter_decision_ledger_v1 import (
    append_cross_chapter_decisions_v1,
    empty_decision_ledger_v1,
    project_reconciled_b1_registry_v1,
    verify_decision_ledger_v1,
)
from pipeline.literary.checkpoint import canonical_hash
from pipeline.literary.identity_decision_correction_v1 import (
    LiteraryIdentityDecisionCorrectionError,
    apply_identity_decision_correction_v1,
    verify_identity_decision_correction_receipt_v1,
)


def test_mistaken_merge_becomes_distinct_and_retires_older_pending() -> None:
    source_ledger = _ledger()
    source_copy = deepcopy(source_ledger)
    target = source_ledger["entries"][1]

    corrected, normalized, receipt = apply_identity_decision_correction_v1(
        decision_ledger=source_ledger,
        source_document=_document(),
        overlay=_overlay(source_ledger),
    )

    assert source_ledger == source_copy
    verify_decision_ledger_v1(corrected)
    verify_identity_decision_correction_receipt_v1(receipt)
    replacement = corrected["entries"][1]
    assert replacement["entry_id"] != target["entry_id"]
    assert replacement["component_id"] == target["component_id"]
    assert replacement["verdict"] == "confirmed_distinct"
    assert replacement["merge_target_prior_card_id"] is None
    assert normalized["human_semantic_correction_performed"] is True
    assert receipt["provider_calls"] == 0

    projection = project_reconciled_b1_registry_v1(
        registries=[_registry()],
        ledger=corrected,
    )
    assert projection["metrics"]["effective_entity_count"] == 2
    assert projection["metrics"]["merged_group_count"] == 0
    assert projection["metrics"]["pending_case_count"] == 0
    assert projection["metrics"]["superseded_pending_case_count"] == 1
    assert projection["metrics"]["resolved_distinct_count"] == 1


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda value: value.update(source_ledger_hash="0" * 64),
            "source ledger hash differs",
        ),
        (
            lambda value: value.update(target_component_id="b1xhear_foreign"),
            "target component differs",
        ),
        (
            lambda value: value["replacement"]["evidence"][0].update(
                quote="not in the source"
            ),
            "quote is not verbatim",
        ),
        (
            lambda value: value["replacement"].update(verdict="merge_referents"),
            "replacement verdict is unsupported",
        ),
        (
            lambda value: value["replacement"].update(
                merge_target_prior_card_id="b0ent_old"
            ),
            "cannot retain a merge target",
        ),
    ],
)
def test_identity_decision_correction_fails_closed(mutate, message: str) -> None:
    ledger = _ledger()
    overlay = _overlay(ledger)
    mutate(overlay)

    with pytest.raises(
        LiteraryIdentityDecisionCorrectionError,
        match=message,
    ):
        apply_identity_decision_correction_v1(
            decision_ledger=ledger,
            source_document=_document(),
            overlay=overlay,
        )


def test_receipt_rejects_tampering() -> None:
    ledger = _ledger()
    _corrected, _normalized, receipt = apply_identity_decision_correction_v1(
        decision_ledger=ledger,
        source_document=_document(),
        overlay=_overlay(ledger),
    )
    receipt["new_verdict"] = "insufficient_evidence"

    with pytest.raises(
        LiteraryIdentityDecisionCorrectionError,
        match="receipt seal is invalid",
    ):
        verify_identity_decision_correction_receipt_v1(receipt)


def _ledger() -> dict:
    registry = _registry()
    ledger = empty_decision_ledger_v1(book_id="book")
    pending_queue = _queue(
        registry,
        component_id="b1xhear_pending",
        chapter_id="book_ch02",
    )
    ledger = append_cross_chapter_decisions_v1(
        ledger=ledger,
        decisions=[
            {
                "component_id": "b1xhear_pending",
                "verdict": "insufficient_evidence",
                "merge_target_prior_card_id": None,
                "excluded_prior_card_ids": [],
                "evidence": [
                    {
                        "block_id": "book_ch01_b001",
                        "quote": "the old inscription",
                    }
                ],
                "reason": "The supplied rows do not settle the identity.",
                "resolution_condition": "A dated living appearance would settle it.",
                "field_adjudications": [],
            }
        ],
        queue=pending_queue,
        registry=registry,
    )
    merge_queue = _queue(
        registry,
        component_id="b1xhear_merge",
        chapter_id="book_ch04",
    )
    return append_cross_chapter_decisions_v1(
        ledger=ledger,
        decisions=[
            {
                "component_id": "b1xhear_merge",
                "verdict": "merge_referents",
                "merge_target_prior_card_id": "b0ent_old",
                "excluded_prior_card_ids": [],
                "evidence": [
                    {
                        "block_id": "book_ch04_b001",
                        "quote": "the living bearer",
                    }
                ],
                "reason": "The model merged the records.",
                "resolution_condition": None,
                "field_adjudications": [],
            }
        ],
        queue=merge_queue,
        registry=registry,
    )


def _queue(
    registry: dict,
    *,
    component_id: str,
    chapter_id: str,
) -> dict:
    body = {
        "chapter_id": chapter_id,
        "registry_hash": registry["registry_hash"],
        "components": [
            {
                "component_id": component_id,
                "question_type": "identity_linkage",
                "review_route": "identity_auditor",
                "prior_card_id": "b0ent_old",
                "prior_card_ids": ["b0ent_old"],
                "current_entity_id": "b0ent_living",
                "current_entity_ids": ["b0ent_living"],
            }
        ],
    }
    return {**body, "queue_hash": canonical_hash(body)}


def _registry() -> dict:
    return {
        "chapter_id": "book_ch04",
        "registry_hash": "a" * 64,
        "cards": [
            {"entity_id": "b0ent_old"},
            {"entity_id": "b0ent_living"},
        ],
    }


def _document() -> dict:
    return {
        "doc_id": "book",
        "chapters": [
            {
                "chapter_id": "book_ch01",
                "blocks": [
                    {
                        "block_id": "book_ch01_b001",
                        "clean_text": "the old inscription",
                        "source_text": "the old inscription",
                    }
                ],
            },
            {
                "chapter_id": "book_ch04",
                "blocks": [
                    {
                        "block_id": "book_ch04_b001",
                        "clean_text": "the living bearer appears in 1801",
                        "source_text": "the living bearer appears in 1801",
                    }
                ],
            },
        ],
    }


def _overlay(ledger: dict) -> dict:
    target = ledger["entries"][1]
    return {
        "schema_version": "literary_identity_decision_correction_overlay_v1",
        "source_ledger_hash": ledger["ledger_hash"],
        "correction_id": "identity-correction-001",
        "target_entry_id": target["entry_id"],
        "target_component_id": target["component_id"],
        "expected_verdict": "merge_referents",
        "expected_merge_target_prior_card_id": "b0ent_old",
        "replacement": {
            "verdict": "confirmed_distinct",
            "merge_target_prior_card_id": None,
            "excluded_prior_card_ids": [],
            "evidence": [
                {
                    "block_id": "book_ch01_b001",
                    "quote": "the old inscription",
                    "supports_excluded_prior_card_ids": [],
                },
                {
                    "block_id": "book_ch04_b001",
                    "quote": "the living bearer appears in 1801",
                    "supports_excluded_prior_card_ids": [],
                },
            ],
            "reason": "The dated records establish distinct referents.",
            "resolution_condition": None,
        },
    }
