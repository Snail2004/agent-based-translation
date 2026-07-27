"""What B2 and B3 see once the cross-chapter Auditor has ruled.

Four card states must be distinguishable downstream, because each needs a
different response: rely on it, rely on it but know it was merged, do not rely
on it and know why, or do not rely on it because nobody has looked yet.  The
case that must never happen is a settled question arriving as an open one -
that both wastes a hearing every chapter and invites two chapters to answer it
differently.
"""

from __future__ import annotations

from pipeline.literary.b1_cross_chapter_decision_ledger_v1 import (
    append_cross_chapter_decisions_v1,
    empty_decision_ledger_v1,
    project_reconciled_b1_registry_v1,
)
from pipeline.literary.b1_registry_to_b2_input_v1 import (
    build_b2_registry_input_package_v1,
)
from pipeline.literary.checkpoint import canonical_hash
from pipeline.tests.test_literary_b1_registry_to_b2_input_v1 import _chapter, _registry

OLD = "ent_robin_old"
NEW = "ent_robin_new"
CH1 = _chapter("book_ch01", "book_ch01_b001", "Robin Vale was engraved.")
CH2 = _chapter("book_ch02", "book_ch02_b001", "Robin Vale entered.")
REG1 = _registry(CH1, entity_id=OLD, surface="Robin Vale")
REG2 = _registry(CH2, entity_id=NEW, surface="Robin Vale")
DOC = {"document_id": "book", "chapters": [CH1, CH2]}
EVIDENCE = [
    {"block_id": "book_ch01_b001", "quote": "Robin Vale was engraved."},
    {"block_id": "book_ch02_b001", "quote": "Robin Vale entered."},
]


def _ledger_with(verdict: str, *, evidence=None):
    component = {
        "component_id": "b1xhear_robin",
        "question_type": "identity_linkage",
        "review_route": "identity_auditor",
        "prior_card_id": OLD,
        "current_entity_id": NEW,
    }
    body = {
        "chapter_id": "book_ch02",
        "registry_hash": REG2["registry_hash"],
        "components": [component],
    }
    queue = {**body, "queue_hash": canonical_hash(body)}
    decision = {
        "component_id": "b1xhear_robin",
        "verdict": verdict,
        "evidence": EVIDENCE if evidence is None else evidence,
        "reason": "A source-grounded reason for this verdict.",
    }
    if verdict == "merge_referents":
        decision["merge_target_prior_card_id"] = OLD
    return append_cross_chapter_decisions_v1(
        ledger=empty_decision_ledger_v1(book_id="book"),
        decisions=[decision],
        queue=queue,
        registry=REG2,
    )


def _prefix(projection=None):
    package = build_b2_registry_input_package_v1(
        document=DOC,
        chapter_registries=[REG1, REG2],
        current_git_head="head_a",
        reconciled_projection=projection,
    )
    return package["chapters"][1]["prefix_bundle"], package


def _projection(verdict: str, *, evidence=None):
    return project_reconciled_b1_registry_v1(
        registries=[REG1, REG2], ledger=_ledger_with(verdict, evidence=evidence)
    )


# ---------------------------------------------------------------------------
# state 4: nobody has looked yet - unchanged behaviour
# ---------------------------------------------------------------------------


def test_without_any_decision_the_adapter_behaves_exactly_as_before() -> None:
    prefix, _ = _prefix(None)
    assert prefix["b0_context_cards"] == []
    assert len(prefix["candidate_only_context_cards"]) == 2
    assert len(prefix["prefix_identity_uncertainties"]) == 1
    for card in prefix["candidate_only_context_cards"]:
        assert "identity_resolution" not in card


# ---------------------------------------------------------------------------
# state 1: settled as one referent
# ---------------------------------------------------------------------------


def test_merged_case_reaches_b2_confirmed_and_unflagged() -> None:
    prefix, _ = _prefix(_projection("merge_referents"))
    assert prefix["prefix_identity_uncertainties"] == []
    assert prefix["candidate_only_context_cards"] == []
    ids = {row["prior_card_id"] for row in prefix["b0_context_cards"]}
    assert ids == {OLD, NEW}
    for card in prefix["b0_context_cards"]:
        res = card["identity_resolution"]
        assert res["state"] == "settled_merged"
        assert sorted(res["member_card_ids"]) == sorted([OLD, NEW])
        # the pointer back to the verdict, not a copy of the hearing
        assert res["ledger_entry_ids"][0].startswith("b1dec_")
        # flat ids only: this is what a later builder must reach outside of
        assert res["evidence_block_ids"] == ["book_ch01_b001", "book_ch02_b001"]
        assert all(isinstance(row, str) for row in res["evidence_block_ids"])
        assert res["reopen_rule"]


# ---------------------------------------------------------------------------
# state 2: settled as two referents
# ---------------------------------------------------------------------------


def test_distinct_case_keeps_two_cards_and_stops_being_questioned() -> None:
    prefix, _ = _prefix(_projection("confirmed_distinct"))
    # the settled question is no longer raised with B2
    assert prefix["prefix_identity_uncertainties"] == []
    ids = {row["prior_card_id"] for row in prefix["b0_context_cards"]}
    assert ids == {OLD, NEW}
    for card in prefix["b0_context_cards"]:
        res = card["identity_resolution"]
        assert res["state"] == "settled_distinct"
        other = {OLD, NEW} - {card["prior_card_id"]}
        assert set(res["distinct_from"]) == other
        assert res["evidence_block_ids"]


# ---------------------------------------------------------------------------
# state 3: heard, but not answered
# ---------------------------------------------------------------------------


def test_pending_case_is_demoted_and_says_what_is_missing() -> None:
    prefix, _ = _prefix(_projection("insufficient_evidence", evidence=[]))
    assert prefix["b0_context_cards"] == []
    cards = prefix["candidate_only_context_cards"]
    assert {row["prior_card_id"] for row in cards} == {OLD, NEW}
    for card in cards:
        res = card["identity_resolution"]
        assert res["state"] == "pending_evidence"
        assert res["missing"]
        assert any(
            row.get("disputed_field") == "identity_membership"
            for row in card["disputed_claims"]
        )
    # an unfinished hearing is still an open question for B2
    assert len(prefix["prefix_identity_uncertainties"]) == 1


# ---------------------------------------------------------------------------
# the package must stay verifiable, and settled must not leak authority
# ---------------------------------------------------------------------------


def test_package_with_decisions_still_verifies_and_grants_no_authority() -> None:
    prefix, package = _prefix(_projection("merge_referents"))
    # build_* verifies internally; an unstored projection would break that
    assert package["reconciled_projection"] is not None
    assert package["adapter_policy"]["identity_merge_performed"] is False
    assert prefix["identity_merge_performed"] is False
    assert prefix["book_authority_granted"] is False
    for card in prefix["b0_context_cards"]:
        assert card["identity_authority"] is False
        assert card["book_authority"] is False


def test_pending_case_is_demoted_even_without_a_surface_collision() -> None:
    # The collision rule cannot cover this: two cards can be the same referent
    # under different names, so nothing about their surfaces raises a flag.
    # Only the unfinished verdict itself can demote them.
    ch2 = _chapter("book_ch02", "book_ch02_b001", "The mistress entered.")
    reg2 = _registry(ch2, entity_id=NEW, surface="the mistress")
    component = {
        "component_id": "b1xhear_alias",
        "question_type": "roster_recognition",
        "review_route": "identity_auditor",
        "prior_card_id": OLD,
        "current_entity_id": NEW,
    }
    body = {
        "chapter_id": "book_ch02",
        "registry_hash": reg2["registry_hash"],
        "components": [component],
    }
    queue = {**body, "queue_hash": canonical_hash(body)}
    ledger = append_cross_chapter_decisions_v1(
        ledger=empty_decision_ledger_v1(book_id="book"),
        decisions=[
            {
                "component_id": "b1xhear_alias",
                "verdict": "insufficient_evidence",
                "evidence": [],
                "reason": "No passage yet ties this role to that name.",
            }
        ],
        queue=queue,
        registry=reg2,
    )
    projection = project_reconciled_b1_registry_v1(
        registries=[REG1, reg2], ledger=ledger
    )
    package = build_b2_registry_input_package_v1(
        document={"document_id": "book", "chapters": [CH1, ch2]},
        chapter_registries=[REG1, reg2],
        current_git_head="head_a",
        reconciled_projection=projection,
    )
    prefix = package["chapters"][1]["prefix_bundle"]
    # surfaces differ, so the collision rule stays silent
    assert prefix["prefix_identity_uncertainties"] == []
    # but neither card may be relied on while the hearing is unfinished
    assert prefix["b0_context_cards"] == []
    ids = {row["prior_card_id"] for row in prefix["candidate_only_context_cards"]}
    assert ids == {OLD, NEW}
    for card in prefix["candidate_only_context_cards"]:
        assert card["identity_resolution"]["state"] == "pending_evidence"


def test_a_settled_card_beside_an_unseen_one_is_still_questioned() -> None:
    # Partial resolution must not silence the rest: only cards ruled on
    # together stop being flagged.
    third = _chapter("book_ch03", "book_ch03_b001", "Robin Vale spoke.")
    reg3 = _registry(third, entity_id="ent_robin_third", surface="Robin Vale")
    package = build_b2_registry_input_package_v1(
        document={"document_id": "book", "chapters": [CH1, CH2, third]},
        chapter_registries=[REG1, REG2, reg3],
        current_git_head="head_a",
        reconciled_projection=project_reconciled_b1_registry_v1(
            registries=[REG1, REG2, reg3],
            ledger=_ledger_with("confirmed_distinct"),
        ),
    )
    prefix = package["chapters"][2]["prefix_bundle"]
    assert len(prefix["prefix_identity_uncertainties"]) == 1
    assert "ent_robin_third" in prefix["prefix_identity_uncertainties"][0]["prior_card_ids"]
