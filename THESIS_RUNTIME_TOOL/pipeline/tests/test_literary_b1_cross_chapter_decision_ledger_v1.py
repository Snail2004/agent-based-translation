"""Tests for the cross-chapter decision ledger and reconciled projection.

The cases that matter are the ones where a record could vanish quietly: a merge
that deletes a card, a settled question that gets re-opened every chapter, a
pending case that stops being visible, or a tampered entry that still passes.
Fixtures are book-neutral; no corpus name appears here.
"""

from __future__ import annotations

from copy import deepcopy

import pytest

from pipeline.literary.b1_cross_chapter_decision_ledger_v1 import (
    B1DecisionLedgerError,
    _retire_superseded_pending_cases_v1,
    append_cross_chapter_decisions_v1,
    build_projected_prior_cards_v1,
    reopen_admissibility_v1,
    empty_decision_ledger_v1,
    project_reconciled_b1_registry_v1,
    verify_decision_ledger_v1,
)
from pipeline.literary.checkpoint import canonical_hash

BOOK = "bk_test"
PRIOR_ID = "b0ent_prior_inscription"
CURRENT_ID = "b0ent_current_living"
CURRENT_ALT_ID = "b0ent_current_alternate"
OTHER_ID = "b0ent_other_person"


def _card(entity_id: str, surface: str, *, chapter: str, surfaces=None) -> dict:
    return {
        "entity_id": entity_id,
        "canonical_surface": surface,
        "stable_surfaces": surfaces or [surface],
        "aliases": [],
        "referent_kind": "person",
        "record_class": "named_entity_candidate",
        "source_refs": [{"chapter_id": chapter, "block_id": f"{chapter}_b001"}],
        "first_seen": {"chapter_id": chapter, "block_id": f"{chapter}_b001", "order_index": 1},
    }


def _registry(chapter: str, cards: list[dict]) -> dict:
    body = {"chapter_id": chapter, "cards": cards}
    return {**body, "registry_hash": canonical_hash(body)}


REG1 = _registry(
    "bk_ch01",
    [_card(PRIOR_ID, "Aldercote", chapter="bk_ch01"), _card(OTHER_ID, "Maren Tull", chapter="bk_ch01")],
)
REG2 = _registry(
    "bk_ch02",
    [_card(CURRENT_ID, "Rowan Aldercote", chapter="bk_ch02", surfaces=["Rowan Aldercote", "Rowan"])],
)
REG2_MULTI = _registry(
    "bk_ch02",
    [
        _card(
            CURRENT_ID,
            "Rowan Aldercote",
            chapter="bk_ch02",
            surfaces=["Rowan Aldercote", "Rowan"],
        ),
        _card(CURRENT_ALT_ID, "Aldercote", chapter="bk_ch02"),
    ],
)


def _component(component_id: str, **overrides) -> dict:
    row = {
        "component_id": component_id,
        "question_type": "identity_linkage",
        "review_route": "identity_auditor",
        "prior_card_id": PRIOR_ID,
        "current_entity_id": CURRENT_ID,
        "lifecycle_state": "ready_for_hearing",
    }
    row.update(overrides)
    return row


def _queue(components: list[dict], *, registry=REG2) -> dict:
    body = {
        "chapter_id": registry["chapter_id"],
        "registry_hash": registry["registry_hash"],
        "components": components,
    }
    return {**body, "queue_hash": canonical_hash(body)}


def _decision(component_id: str, verdict: str, **overrides) -> dict:
    row = {
        "component_id": component_id,
        "verdict": verdict,
        "evidence": [{"block_id": "bk_ch02_b001", "quote": "supplied verbatim text"}],
        "reason": "A source-grounded reason for this verdict.",
        "resolution_condition": (
            "A later source block must identify the referent."
            if verdict == "insufficient_evidence"
            else None
        ),
    }
    if verdict in {"merge_referents", "alias_confirmed"}:
        row["merge_target_prior_card_id"] = PRIOR_ID
    row.update(overrides)
    return row


def _apply(verdict: str, **overrides) -> dict:
    queue = _queue([_component("b1xhear_case1")])
    return append_cross_chapter_decisions_v1(
        ledger=empty_decision_ledger_v1(book_id=BOOK),
        decisions=[_decision("b1xhear_case1", verdict, **overrides)],
        queue=queue,
        registry=REG2,
    )


# ---------------------------------------------------------------------------
# ledger integrity
# ---------------------------------------------------------------------------


def test_empty_ledger_is_a_legal_state() -> None:
    ledger = empty_decision_ledger_v1(book_id=BOOK)
    assert verify_decision_ledger_v1(ledger) == ledger
    assert ledger["entries"] == []


def test_appending_never_rewrites_an_existing_entry() -> None:
    first = _apply("confirmed_distinct")
    queue = _queue([_component("b1xhear_case1"), _component("b1xhear_case2", prior_card_id=OTHER_ID)])
    second = append_cross_chapter_decisions_v1(
        ledger=first,
        decisions=[_decision("b1xhear_case2", "confirmed_distinct")],
        queue=queue,
        registry=REG2,
    )
    assert len(second["entries"]) == 2
    assert second["entries"][0] == first["entries"][0]
    assert second["entries"][1]["sequence_index"] == 1
    verify_decision_ledger_v1(second)


def test_one_component_cannot_be_answered_twice() -> None:
    ledger = _apply("confirmed_distinct")
    with pytest.raises(B1DecisionLedgerError, match="already has a decision"):
        append_cross_chapter_decisions_v1(
            ledger=ledger,
            decisions=[_decision("b1xhear_case1", "merge_referents")],
            queue=_queue([_component("b1xhear_case1")]),
            registry=REG2,
        )


def test_tampered_entry_fails_verification() -> None:
    ledger = deepcopy(_apply("confirmed_distinct"))
    ledger["entries"][0]["verdict"] = "merge_referents"
    with pytest.raises(B1DecisionLedgerError, match="does not match its id"):
        verify_decision_ledger_v1(ledger)


def test_reordered_entries_fail_verification() -> None:
    first = _apply("confirmed_distinct")
    queue = _queue([_component("b1xhear_case1"), _component("b1xhear_case2", prior_card_id=OTHER_ID)])
    ledger = deepcopy(
        append_cross_chapter_decisions_v1(
            ledger=first,
            decisions=[_decision("b1xhear_case2", "confirmed_distinct")],
            queue=queue,
            registry=REG2,
        )
    )
    ledger["entries"].reverse()
    with pytest.raises(B1DecisionLedgerError, match="order was rewritten"):
        verify_decision_ledger_v1(ledger)


def test_decision_for_unknown_component_is_refused() -> None:
    with pytest.raises(B1DecisionLedgerError, match="absent from the supplied queue"):
        append_cross_chapter_decisions_v1(
            ledger=empty_decision_ledger_v1(book_id=BOOK),
            decisions=[_decision("b1xhear_foreign", "confirmed_distinct")],
            queue=_queue([_component("b1xhear_case1")]),
            registry=REG2,
        )


def test_queue_from_another_registry_is_refused() -> None:
    stale = _queue([_component("b1xhear_case1")], registry=REG1)
    with pytest.raises(B1DecisionLedgerError, match="does not belong to this registry"):
        append_cross_chapter_decisions_v1(
            ledger=empty_decision_ledger_v1(book_id=BOOK),
            decisions=[_decision("b1xhear_case1", "confirmed_distinct")],
            queue=stale,
            registry=REG2,
        )


def test_merge_must_echo_the_supplied_prior_card() -> None:
    with pytest.raises(B1DecisionLedgerError, match="must echo the prior card"):
        _apply("merge_referents", merge_target_prior_card_id=OTHER_ID)


def test_closing_verdict_requires_evidence_but_pending_does_not() -> None:
    with pytest.raises(B1DecisionLedgerError, match="must cite at least one evidence"):
        _apply("confirmed_distinct", evidence=[])
    ledger = _apply("insufficient_evidence", evidence=[])
    assert ledger["entries"][0]["evidence"] == []


def test_plural_current_entity_reference_is_bound_into_the_ledger() -> None:
    component = _component("b1xhear_case1")
    component.pop("current_entity_id")
    component["current_entity_ids"] = [CURRENT_ID]
    queue = _queue([component])
    ledger = append_cross_chapter_decisions_v1(
        ledger=empty_decision_ledger_v1(book_id=BOOK),
        decisions=[_decision("b1xhear_case1", "insufficient_evidence")],
        queue=queue,
        registry=REG2,
    )

    assert ledger["entries"][0]["current_entity_id"] == CURRENT_ID
    assert ledger["entries"][0]["current_entity_ids"] == [CURRENT_ID]
    projection = project_reconciled_b1_registry_v1(
        registries=[REG1, REG2], ledger=ledger
    )
    assert projection["pending_cases"][0]["card_ids"] == sorted(
        [PRIOR_ID, CURRENT_ID]
    )


def _apply_multi_current(verdict: str) -> tuple[dict, dict]:
    component = _component("b1xhear_case1")
    component.pop("current_entity_id")
    component["current_entity_ids"] = [CURRENT_ALT_ID, CURRENT_ID]
    queue = _queue([component], registry=REG2_MULTI)
    ledger = append_cross_chapter_decisions_v1(
        ledger=empty_decision_ledger_v1(book_id=BOOK),
        decisions=[_decision("b1xhear_case1", verdict)],
        queue=queue,
        registry=REG2_MULTI,
    )
    return ledger, project_reconciled_b1_registry_v1(
        registries=[REG1, REG2_MULTI], ledger=ledger
    )


def test_multi_current_pending_verdict_preserves_every_pair() -> None:
    ledger, projection = _apply_multi_current("insufficient_evidence")

    entry = ledger["entries"][0]
    assert entry["current_entity_id"] is None
    assert entry["current_entity_ids"] == sorted([CURRENT_ID, CURRENT_ALT_ID])
    assert {tuple(row["card_ids"]) for row in projection["pending_cases"]} == {
        tuple(sorted([PRIOR_ID, CURRENT_ID])),
        tuple(sorted([PRIOR_ID, CURRENT_ALT_ID])),
    }
    assert all(
        row["current_candidate_set"] == sorted([CURRENT_ID, CURRENT_ALT_ID])
        for row in projection["pending_cases"]
    )


def test_multi_current_distinct_verdict_settles_every_pair() -> None:
    _ledger, projection = _apply_multi_current("confirmed_distinct")

    assert projection["pending_cases"] == []
    assert {tuple(row["card_ids"]) for row in projection["resolved_distinct_cases"]} == {
        tuple(sorted([PRIOR_ID, CURRENT_ID])),
        tuple(sorted([PRIOR_ID, CURRENT_ALT_ID])),
    }


def test_multi_current_merge_verdict_unions_the_complete_hearing_set() -> None:
    ledger, projection = _apply_multi_current("merge_referents")

    merged = next(
        row
        for row in projection["effective_entities"]
        if PRIOR_ID in row["member_card_ids"]
    )
    assert set(merged["member_card_ids"]) == {
        PRIOR_ID,
        CURRENT_ID,
        CURRENT_ALT_ID,
    }
    assert merged["decision_refs"] == [ledger["entries"][0]["entry_id"]]
    assert projection["pending_cases"] == []


def test_multi_current_merge_is_atomic_against_distinct_constraints() -> None:
    distinct_component = _component(
        "b1xhear_distinct_alt", current_entity_id=CURRENT_ALT_ID
    )
    distinct_queue = _queue([distinct_component], registry=REG2_MULTI)
    ledger = append_cross_chapter_decisions_v1(
        ledger=empty_decision_ledger_v1(book_id=BOOK),
        decisions=[_decision("b1xhear_distinct_alt", "confirmed_distinct")],
        queue=distinct_queue,
        registry=REG2_MULTI,
    )
    merge_component = _component("b1xhear_merge_multi")
    merge_component.pop("current_entity_id")
    merge_component["current_entity_ids"] = [CURRENT_ID, CURRENT_ALT_ID]
    merge_queue = _queue([merge_component], registry=REG2_MULTI)
    ledger = append_cross_chapter_decisions_v1(
        ledger=ledger,
        decisions=[_decision("b1xhear_merge_multi", "merge_referents")],
        queue=merge_queue,
        registry=REG2_MULTI,
    )

    projection = project_reconciled_b1_registry_v1(
        registries=[REG1, REG2_MULTI], ledger=ledger
    )
    groups = [set(row["member_card_ids"]) for row in projection["effective_entities"]]
    assert not any(
        PRIOR_ID in members
        and ({CURRENT_ID, CURRENT_ALT_ID} & members)
        for members in groups
    )
    conflict = next(
        row
        for row in projection["pending_cases"]
        if row["state"] == "decision_conflict_unapplied"
    )
    assert conflict["card_ids"] == sorted(
        [PRIOR_ID, CURRENT_ID, CURRENT_ALT_ID]
    )


def test_multi_current_component_rejects_duplicate_ids() -> None:
    component = _component("b1xhear_case1")
    component.pop("current_entity_id")
    component["current_entity_ids"] = [CURRENT_ID, CURRENT_ID]
    with pytest.raises(B1DecisionLedgerError, match="contains a duplicate"):
        append_cross_chapter_decisions_v1(
            ledger=empty_decision_ledger_v1(book_id=BOOK),
            decisions=[_decision("b1xhear_case1", "insufficient_evidence")],
            queue=_queue([component]),
            registry=REG2,
        )


def test_verifier_rejects_resealed_duplicate_current_ids() -> None:
    ledger = deepcopy(_apply("insufficient_evidence"))
    entry = ledger["entries"][0]
    entry["current_entity_id"] = None
    entry["current_entity_ids"] = [CURRENT_ID, CURRENT_ID]
    entry_body = {
        key: value
        for key, value in entry.items()
        if key not in {"entry_id", "sequence_index"}
    }
    entry["entry_id"] = "b1dec_" + canonical_hash(entry_body)[:20]
    ledger_body = {
        "schema_version": ledger["schema_version"],
        "book_id": ledger["book_id"],
        "entries": ledger["entries"],
    }
    ledger["ledger_hash"] = canonical_hash(ledger_body)

    with pytest.raises(B1DecisionLedgerError, match="contains a duplicate"):
        verify_decision_ledger_v1(ledger)


# ---------------------------------------------------------------------------
# projection
# ---------------------------------------------------------------------------


def test_merge_keeps_both_cards_and_unions_their_surfaces() -> None:
    projection = project_reconciled_b1_registry_v1(
        registries=[REG1, REG2], ledger=_apply("merge_referents")
    )
    merged = [e for e in projection["effective_entities"] if len(e["member_card_ids"]) > 1]
    assert len(merged) == 1
    row = merged[0]
    # neither card is deleted
    assert sorted(row["member_card_ids"]) == sorted([PRIOR_ID, CURRENT_ID])
    assert "Aldercote" in row["stable_surfaces"]
    assert "Rowan Aldercote" in row["stable_surfaces"]
    # provenance from both chapters survives
    assert row["member_chapters"] == ["bk_ch01", "bk_ch02"]
    assert len(row["source_refs"]) == 2
    # the merge can be explained later
    assert row["decision_refs"] and row["decision_refs"][0].startswith("b1dec_")
    assert row["identity_authority_granted"] is False
    # the unrelated card is untouched
    assert any(e["member_card_ids"] == [OTHER_ID] for e in projection["effective_entities"])


def test_distinct_keeps_two_entities_and_is_remembered() -> None:
    projection = project_reconciled_b1_registry_v1(
        registries=[REG1, REG2], ledger=_apply("confirmed_distinct")
    )
    assert projection["metrics"]["effective_entity_count"] == 3
    assert projection["metrics"]["merged_group_count"] == 0
    assert projection["metrics"]["resolved_distinct_count"] == 1
    # the settled question is not re-opened in later chapters
    # re-asking over the same evidence is refused
    stale = reopen_admissibility_v1(
        projection, card_ids=[PRIOR_ID, CURRENT_ID], cited_block_ids=["bk_ch02_b001"]
    )
    assert stale["already_decided"] is True and stale["admissible"] is False
    # a later chapter necessarily cites new blocks, so a real finding gets heard
    fresh = reopen_admissibility_v1(
        projection, card_ids=[PRIOR_ID, CURRENT_ID], cited_block_ids=["bk_ch09_b044"]
    )
    assert fresh["admissible"] is True and fresh["new_block_ids"] == ["bk_ch09_b044"]
    # an untouched pair was never decided, so nothing blocks it
    untouched = reopen_admissibility_v1(
        projection, card_ids=[PRIOR_ID, OTHER_ID], cited_block_ids=["bk_ch02_b001"]
    )
    assert untouched["already_decided"] is False and untouched["admissible"] is True


def test_pending_case_stays_visible_with_its_reason() -> None:
    projection = project_reconciled_b1_registry_v1(
        registries=[REG1, REG2], ledger=_apply("insufficient_evidence")
    )
    assert projection["metrics"]["pending_case_count"] == 1
    row = projection["pending_cases"][0]
    assert row["state"] == "evidence_needed"
    assert row["card_ids"] == sorted([PRIOR_ID, CURRENT_ID])
    assert row["reason"]
    assert row["resolution_condition"]
    # unresolved means still separate, never quietly merged
    assert projection["metrics"]["merged_group_count"] == 0


def _pending_then_distinct_projection() -> tuple[dict, dict]:
    ledger = _apply("insufficient_evidence")
    reg3 = _registry(
        "bk_ch03",
        [_card(CURRENT_ID, "Rowan Aldercote", chapter="bk_ch03")],
    )
    queue = _queue(
        [
            _component(
                "b1xhear_case_ch03",
                prior_card_id=PRIOR_ID,
                current_entity_id=CURRENT_ID,
            )
        ],
        registry=reg3,
    )
    ledger = append_cross_chapter_decisions_v1(
        ledger=ledger,
        decisions=[
            _decision(
                "b1xhear_case_ch03",
                "confirmed_distinct",
                evidence=[
                    {
                        "block_id": "bk_ch03_b001",
                        "quote": "later supplied verbatim text",
                    }
                ],
            )
        ],
        queue=queue,
        registry=reg3,
    )
    return ledger, project_reconciled_b1_registry_v1(
        registries=[REG1, REG2, reg3],
        ledger=ledger,
    )


def test_later_distinct_verdict_retires_same_pair_pending_case() -> None:
    ledger, projection = _pending_then_distinct_projection()

    assert projection["pending_cases"] == []
    assert len(projection["superseded_pending_cases"]) == 1
    retired = projection["superseded_pending_cases"][0]
    settled = projection["resolved_distinct_cases"][0]
    assert retired["card_ids"] == sorted([PRIOR_ID, CURRENT_ID])
    assert retired["superseded_by_entry_id"] == settled["entry_id"]
    assert retired["superseded_by_component"] == "b1xhear_case_ch03"
    assert retired["superseded_in_chapter"] == "bk_ch03"
    assert retired["superseded_reason"] == (
        "later verdict settled the same card set"
    )
    assert projection["metrics"]["superseded_pending_case_count"] == 1
    assert projection["review_issues"] == []
    assert len(ledger["entries"]) == 2


def test_earlier_distinct_verdict_does_not_retire_later_pending_case() -> None:
    distinct = _apply("confirmed_distinct")
    reg3 = _registry(
        "bk_ch03",
        [_card(CURRENT_ID, "Rowan Aldercote", chapter="bk_ch03")],
    )
    queue = _queue(
        [_component("b1xhear_pending_ch03")],
        registry=reg3,
    )
    ledger = append_cross_chapter_decisions_v1(
        ledger=distinct,
        decisions=[
            _decision(
                "b1xhear_pending_ch03",
                "insufficient_evidence",
                evidence=[
                    {
                        "block_id": "bk_ch03_b001",
                        "quote": "later unresolved source text",
                    }
                ],
            )
        ],
        queue=queue,
        registry=reg3,
    )
    projection = project_reconciled_b1_registry_v1(
        registries=[REG1, REG2, reg3],
        ledger=ledger,
    )

    assert len(projection["pending_cases"]) == 1
    assert projection["superseded_pending_cases"] == []
    assert projection["review_issues"][0]["issue_code"] == (
        "pending_resolved_order_conflict"
    )


def test_overlapping_superset_does_not_supersede_pending_pair() -> None:
    pending = [
        {
            "entry_id": "pending",
            "component_id": "pending_component",
            "chapter_id": "bk_ch02",
            "card_ids": ["card_a", "card_b"],
        }
    ]
    resolved = [
        {
            "entry_id": "resolved",
            "component_id": "resolved_component",
            "chapter_id": "bk_ch03",
            "card_ids": ["card_a", "card_b", "card_c"],
            "verdict": "confirmed_distinct",
        }
    ]
    entries = [
        {"chapter_id": "bk_ch02", "sequence_index": 0},
        {"chapter_id": "bk_ch03", "sequence_index": 1},
    ]

    effective, superseded, issues = _retire_superseded_pending_cases_v1(
        pending_cases=pending,
        resolved_distinct_cases=resolved,
        ledger_entries=entries,
    )

    assert effective == pending
    assert superseded == []
    assert issues == []


def test_retirement_pass_removes_the_raw_pending_resolved_contradiction() -> None:
    _ledger, projection = _pending_then_distinct_projection()
    raw_pending = [
        {
            "entry_id": "pending",
            "component_id": "pending_component",
            "chapter_id": "bk_ch02",
            "card_ids": sorted([PRIOR_ID, CURRENT_ID]),
        }
    ]
    raw_resolved = projection["resolved_distinct_cases"]
    assert {
        frozenset(row["card_ids"]) for row in raw_pending
    } & {
        frozenset(row["card_ids"]) for row in raw_resolved
    }
    assert not (
        {frozenset(row["card_ids"]) for row in projection["pending_cases"]}
        & {frozenset(row["card_ids"]) for row in raw_resolved}
    )


def test_supersession_does_not_change_reopen_admissibility() -> None:
    _ledger, projection = _pending_then_distinct_projection()

    fresh = reopen_admissibility_v1(
        projection,
        card_ids=[PRIOR_ID, CURRENT_ID],
        cited_block_ids=["bk_ch05_b009"],
    )

    assert fresh["already_decided"] is True
    assert fresh["admissible"] is True
    assert fresh["new_block_ids"] == ["bk_ch05_b009"]


def test_no_decision_at_all_leaves_every_card_standing() -> None:
    projection = project_reconciled_b1_registry_v1(
        registries=[REG1, REG2], ledger=empty_decision_ledger_v1(book_id=BOOK)
    )
    assert projection["metrics"]["effective_entity_count"] == 3
    assert projection["metrics"]["merged_group_count"] == 0


def test_merge_is_transitive_across_chapters() -> None:
    third = _registry("bk_ch03", [_card("b0ent_third", "Rowan A.", chapter="bk_ch03")])
    ledger = _apply("merge_referents")
    queue = _queue(
        [_component("b1xhear_case3", prior_card_id=CURRENT_ID, current_entity_id="b0ent_third")],
        registry=third,
    )
    ledger = append_cross_chapter_decisions_v1(
        ledger=ledger,
        decisions=[_decision("b1xhear_case3", "merge_referents", merge_target_prior_card_id=CURRENT_ID)],
        queue=queue,
        registry=third,
    )
    projection = project_reconciled_b1_registry_v1(registries=[REG1, REG2, third], ledger=ledger)
    merged = [e for e in projection["effective_entities"] if len(e["member_card_ids"]) > 1]
    assert len(merged) == 1
    assert sorted(merged[0]["member_card_ids"]) == sorted([PRIOR_ID, CURRENT_ID, "b0ent_third"])
    assert merged[0]["member_chapters"] == ["bk_ch01", "bk_ch02", "bk_ch03"]


def test_merge_naming_an_absent_card_is_surfaced_not_dropped() -> None:
    ledger = _apply("merge_referents")
    projection = project_reconciled_b1_registry_v1(registries=[REG1], ledger=ledger)
    assert projection["metrics"]["merged_group_count"] == 0
    assert projection["pending_cases"][0]["state"] == "decision_not_applicable_here"


def test_stable_claim_verdict_is_recorded_without_touching_identity() -> None:
    ledger = _apply(
        "correction",
        field_adjudications=[{"field": "residence", "accepted_value": "the low farm"}],
    )
    projection = project_reconciled_b1_registry_v1(registries=[REG1, REG2], ledger=ledger)
    assert projection["metrics"]["claim_adjudication_count"] == 1
    assert projection["claim_adjudications"][0]["verdict"] == "correction"
    assert projection["metrics"]["merged_group_count"] == 0
    assert projection["metrics"]["effective_entity_count"] == 3


def test_projection_is_deterministic_and_does_not_mutate_inputs() -> None:
    ledger = _apply("merge_referents")
    before = deepcopy((REG1, REG2, ledger))
    first = project_reconciled_b1_registry_v1(registries=[REG1, REG2], ledger=ledger)
    second = project_reconciled_b1_registry_v1(registries=[REG2, REG1], ledger=ledger)
    assert first["effective_entities"] == second["effective_entities"]
    assert (REG1, REG2, ledger) == before


def test_projection_refuses_merge_that_would_join_a_distinct_pair() -> None:
    distinct_queue = _queue(
        [
            _component(
                "b1xhear_distinct_ab",
                prior_card_id=PRIOR_ID,
                current_entity_id=OTHER_ID,
            )
        ],
        registry=REG1,
    )
    ledger = append_cross_chapter_decisions_v1(
        ledger=empty_decision_ledger_v1(book_id=BOOK),
        decisions=[_decision("b1xhear_distinct_ab", "confirmed_distinct")],
        queue=distinct_queue,
        registry=REG1,
    )
    merge_a_queue = _queue([_component("b1xhear_merge_ca")])
    ledger = append_cross_chapter_decisions_v1(
        ledger=ledger,
        decisions=[_decision("b1xhear_merge_ca", "merge_referents")],
        queue=merge_a_queue,
        registry=REG2,
    )
    merge_b_queue = _queue(
        [_component("b1xhear_merge_cb", prior_card_id=OTHER_ID)]
    )
    ledger = append_cross_chapter_decisions_v1(
        ledger=ledger,
        decisions=[
            _decision(
                "b1xhear_merge_cb",
                "merge_referents",
                merge_target_prior_card_id=OTHER_ID,
            )
        ],
        queue=merge_b_queue,
        registry=REG2,
    )

    projection = project_reconciled_b1_registry_v1(
        registries=[REG1, REG2], ledger=ledger
    )
    groups = [set(row["member_card_ids"]) for row in projection["effective_entities"]]
    assert not any({PRIOR_ID, OTHER_ID} <= members for members in groups)
    conflict = next(
        row
        for row in projection["pending_cases"]
        if row["state"] == "decision_conflict_unapplied"
    )
    assert conflict["conflicting_entry_ids"]


def test_partial_exclusion_settles_one_pair_and_keeps_only_the_other_pending() -> None:
    component = _component("b1xhear_partial")
    component.pop("prior_card_id")
    component["prior_card_ids"] = [PRIOR_ID, OTHER_ID]
    queue = _queue([component])
    decision = _decision(
        "b1xhear_partial",
        "insufficient_evidence",
        excluded_prior_card_ids=[PRIOR_ID],
        evidence=[
            {
                "block_id": "bk_ch02_b001",
                "quote": "supplied verbatim text",
                "supports_excluded_prior_card_ids": [PRIOR_ID],
            }
        ],
    )
    ledger = append_cross_chapter_decisions_v1(
        ledger=empty_decision_ledger_v1(book_id=BOOK),
        decisions=[decision],
        queue=queue,
        registry=REG2,
    )
    projection = project_reconciled_b1_registry_v1(
        registries=[REG1, REG2], ledger=ledger
    )

    assert any(
        row["card_ids"] == sorted([PRIOR_ID, CURRENT_ID])
        and row["finding"] == "partial_exclusion"
        for row in projection["resolved_distinct_cases"]
    )
    assert [row["card_ids"] for row in projection["pending_cases"]] == [
        sorted([OTHER_ID, CURRENT_ID])
    ]


# ---------------------------------------------------------------------------
# prior cards the next chapter actually reads
# ---------------------------------------------------------------------------


def _registry_with_projection(chapter: str, cards: list[dict], *, claims=None) -> dict:
    full = []
    projected = []
    for card in cards:
        entity_id = card["entity_id"]
        full.append(
            {
                **card,
                "claims": claims.get(entity_id, []) if claims else [],
                "distinguishing_note": f"note for {entity_id}",
            }
        )
        projected.append(
            {
                "prior_card_id": entity_id,
                "canonical_surface": card["canonical_surface"],
                "stable_surfaces": list(card["stable_surfaces"]),
                "referent_kind": card["referent_kind"],
                "record_class": card["record_class"],
                "presence_basis": "direct_presence",
                "claim_state": "confirmed",
                "identity_summary": f"summary for {entity_id}",
                "first_supported_block_id": f"{chapter}_b001",
                "provenance_refs": [{"chapter_id": chapter, "block_id": f"{chapter}_b001"}],
            }
        )
    body = {"chapter_id": chapter, "cards": full, "prior_cards_projection": {"cards": projected}}
    return {**body, "registry_hash": canonical_hash(body)}


def _claim(field: str, value: str, block: str) -> dict:
    return {
        "field": field,
        "status": "supported",
        "value": value,
        "basis": "explicit_textual",
        "effective": True,
        "anchor_block_ids": [block],
        "story_time_note": None,
        "validity": {"from_block": None, "to_block": None},
        "semantic_status": "unreviewed",
    }


PROJ_REG1 = _registry_with_projection(
    "bk_ch01",
    [
        {
            "entity_id": PRIOR_ID,
            "canonical_surface": "Aldercote",
            "stable_surfaces": ["Aldercote"],
            "referent_kind": "person",
            "record_class": "unresolved_named_reference",
        }
    ],
    claims={PRIOR_ID: [_claim("carved_on", "the lintel", "bk_ch01_b001")]},
)
PROJ_REG2 = _registry_with_projection(
    "bk_ch02",
    [
        {
            "entity_id": CURRENT_ID,
            "canonical_surface": "Rowan Aldercote",
            "stable_surfaces": ["Rowan Aldercote", "Rowan"],
            "referent_kind": "person",
            "record_class": "confirmed_entity",
        }
    ],
    claims={CURRENT_ID: [_claim("occupation", "farm hand", "bk_ch02_b004")]},
)


def _proj_ledger(verdict: str) -> dict:
    queue = _queue([_component("b1xhear_case1")], registry=PROJ_REG2)
    return append_cross_chapter_decisions_v1(
        ledger=empty_decision_ledger_v1(book_id=BOOK),
        decisions=[_decision("b1xhear_case1", verdict)],
        queue=queue,
        registry=PROJ_REG2,
    )


def test_merged_referent_reaches_next_chapter_as_one_card_under_all_names() -> None:
    ledger = _proj_ledger("merge_referents")
    projection = project_reconciled_b1_registry_v1(
        registries=[PROJ_REG1, PROJ_REG2], ledger=ledger
    )
    cards = build_projected_prior_cards_v1(
        registries=[PROJ_REG1, PROJ_REG2], projection=projection
    )
    assert len(cards) == 1
    card = cards[0]
    # every name the referent is known by, so retrieval can find it next chapter
    assert sorted(card["stable_surfaces"]) == ["Aldercote", "Rowan", "Rowan Aldercote"]
    # evidence from both chapters
    assert len(card["provenance_refs"]) == 2
    assert {c["field"] for c in card["profile_claims"]} == {"carved_on", "occupation"}
    assert all("member_card_id" not in claim for claim in card["profile_claims"])
    # the merged record is no longer an unresolved inscription: keeping the
    # weakest state would re-open the same hearing every chapter
    assert card["record_class"] == "confirmed_entity"
    assert "member_card_ids" not in card
    merged = projection["effective_entities"][0]
    assert sorted(merged["member_card_ids"]) == sorted([PRIOR_ID, CURRENT_ID])


def test_untouched_cards_pass_through_with_the_shape_scan_already_expects() -> None:
    projection = project_reconciled_b1_registry_v1(
        registries=[PROJ_REG1, PROJ_REG2], ledger=empty_decision_ledger_v1(book_id=BOOK)
    )
    cards = build_projected_prior_cards_v1(
        registries=[PROJ_REG1, PROJ_REG2], projection=projection
    )
    assert len(cards) == 2
    for card in cards:
        assert {
            "prior_card_id",
            "canonical_surface",
            "stable_surfaces",
            "referent_kind",
            "record_class",
            "presence_basis",
            "claim_state",
            "identity_summary",
            "first_supported_block_id",
            "provenance_refs",
            "profile_claims",
        } <= set(card)
        assert "member_card_ids" not in card
        assert all("member_card_id" not in claim for claim in card["profile_claims"])
    # a card nobody ruled on keeps its own weaker class
    inscription = [c for c in cards if c["prior_card_id"] == PRIOR_ID][0]
    assert inscription["record_class"] == "unresolved_named_reference"


def test_distinct_verdict_leaves_two_separate_prior_cards() -> None:
    ledger = _proj_ledger("confirmed_distinct")
    projection = project_reconciled_b1_registry_v1(
        registries=[PROJ_REG1, PROJ_REG2], ledger=ledger
    )
    cards = build_projected_prior_cards_v1(
        registries=[PROJ_REG1, PROJ_REG2], projection=projection
    )
    assert len(cards) == 2
    assert reopen_admissibility_v1(
        projection, card_ids=[PRIOR_ID, CURRENT_ID], cited_block_ids=["bk_ch01_b001"]
    )["already_decided"] is True


def test_repeated_entity_id_is_projected_as_cross_chapter_snapshots() -> None:
    later = _registry(
        "bk_ch03",
        [
            _card(
                PRIOR_ID,
                "Aldercote",
                chapter="bk_ch03",
                surfaces=["Aldercote", "Master Aldercote"],
            )
        ],
    )
    projection = project_reconciled_b1_registry_v1(
        registries=[later, REG1],
        ledger=empty_decision_ledger_v1(book_id=BOOK),
    )

    assert projection["metrics"]["source_card_count"] == 3
    assert projection["metrics"]["source_entity_id_count"] == 2
    assert projection["metrics"]["effective_entity_count"] == 2
    row = next(
        entity
        for entity in projection["effective_entities"]
        if entity["effective_entity_id"] == PRIOR_ID
    )
    assert row["member_card_ids"] == [PRIOR_ID]
    assert row["member_chapters"] == ["bk_ch01", "bk_ch03"]
    assert row["first_seen"]["chapter_id"] == "bk_ch01"
    assert row["stable_surfaces"] == ["Aldercote", "Master Aldercote"]
    assert {ref["chapter_id"] for ref in row["source_refs"]} == {
        "bk_ch01",
        "bk_ch03",
    }


def test_repeated_entity_id_preserves_prior_card_history() -> None:
    later = _registry_with_projection(
        "bk_ch02",
        [
            {
                "entity_id": PRIOR_ID,
                "canonical_surface": "Aldercote",
                "stable_surfaces": ["Aldercote", "Master Aldercote"],
                "referent_kind": "person",
                "record_class": "confirmed_entity",
            }
        ],
        claims={PRIOR_ID: [_claim("occupation", "estate keeper", "bk_ch02_b004")]},
    )
    ledger = empty_decision_ledger_v1(book_id=BOOK)
    projection = project_reconciled_b1_registry_v1(
        registries=[PROJ_REG1, later], ledger=ledger
    )
    cards = build_projected_prior_cards_v1(
        registries=[PROJ_REG1, later], projection=projection
    )
    reversed_projection = project_reconciled_b1_registry_v1(
        registries=[later, PROJ_REG1], ledger=ledger
    )
    reversed_cards = build_projected_prior_cards_v1(
        registries=[later, PROJ_REG1], projection=reversed_projection
    )

    assert cards == reversed_cards
    assert len(cards) == 1
    card = cards[0]
    assert card["prior_card_id"] == PRIOR_ID
    assert "member_card_ids" not in card
    assert card["stable_surfaces"] == ["Aldercote", "Master Aldercote"]
    assert {ref["chapter_id"] for ref in card["provenance_refs"]} == {
        "bk_ch01",
        "bk_ch02",
    }
    assert {claim["field"] for claim in card["profile_claims"]} == {
        "carved_on",
        "occupation",
    }
    assert card["record_class"] == "confirmed_entity"
