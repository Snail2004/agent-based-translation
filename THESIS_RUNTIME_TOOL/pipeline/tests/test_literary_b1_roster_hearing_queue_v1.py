"""Tests for roster recognition proposals reaching a real hearing.

The channel exists for the referent a chapter names differently - a surname
standing alone, a shortened place name - which mechanical retrieval can never
reach.  What must hold: the hearing sees BOTH sides, one card gets ONE hearing,
and a proposal that cannot be queued stays countable instead of vanishing.
"""

from __future__ import annotations

from copy import deepcopy

from pipeline.literary.b1_chapter_registry_writer_v1 import (
    build_b1_cross_chapter_hearing_queue_v1,
    seal_b1_chapter_registry_v1,
)
from pipeline.literary.b1_cross_chapter_audit_bridge_v1 import (
    ALIAS_REFERRAL_VERDICTS,
    allowed_verdicts_for_component_v1,
)
from pipeline.literary.checkpoint import canonical_hash
from pipeline.tests.test_literary_b1_chapter_registry_writer_v1 import (
    _audit,
    _chapter,
    _enrich,
    _rehash_artifact,
    _scan,
)

PRIOR_HOUSE = "b0ent_prior_house"
PRIOR_MARA = "b0ent_prior_mara"


def _prior_card(card_id: str, surface: str, *, surfaces=None, chapter="bk_ch00") -> dict:
    return {
        "prior_card_id": card_id,
        "canonical_surface": surface,
        "stable_surfaces": surfaces or [surface],
        "referent_kind": "person",
        "record_class": "confirmed_entity",
        "presence_basis": "direct_presence",
        "claim_state": "confirmed",
        "identity_summary": f"{surface} was established in an earlier chapter.",
        "first_supported_block_id": f"{chapter}_b001",
        "provenance_refs": [{"chapter_id": chapter, "block_id": f"{chapter}_b001"}],
        "profile_claims": [],
    }


PRIOR_CARDS = [
    _prior_card(PRIOR_MARA, "Mara Vale"),
    _prior_card(PRIOR_HOUSE, "North House"),
]


def _roster_proposal(surface: str, prior_card_id: str, blocks=None) -> dict:
    return {
        "proposal_id": f"b1rrp_{surface.replace(' ', '_')}",
        "surface": surface,
        "prior_card_id": prior_card_id,
        "source_block_ids": blocks or ["bk_ch01_b001"],
        "reason": "The chapter names this referent in a shortened form.",
        "roster_card": {"prior_card_id": prior_card_id, "canonical_surface": "x"},
        "authority_scope": "proposal_only",
        "identity_authority_granted": False,
    }


def _build(
    *,
    proposals,
    prior_cards,
    continuity_hearing_on=None,
    continuity_observation_ids=None,
    reconciled_projection=None,
):
    chapter = _chapter()
    scan = _scan()
    scan["roster_recognition_proposals"] = proposals
    scan = _rehash_artifact(scan)
    enrich = _enrich(scan)
    if continuity_hearing_on:
        enrich["continuity_cases"] = [
            {
                "continuity_case_id": "b1cont_case",
                "chapter_id": "bk_ch01",
                "scan_artifact_hash": scan["artifact_hash"],
                "prior_card_id": continuity_hearing_on,
                "current_scan_observation_ids": (
                    ["b1obs_house"]
                    if continuity_observation_ids is None
                    else continuity_observation_ids
                ),
                "scan_verdict": "uncertain",
                "reason_code": "insufficient_evidence",
                "source_block_ids": ["bk_ch01_b001"],
                "reason": "Two readings of this reference remain plausible.",
                "packet_action": "withhold_prior_card",
                "hearing_required": True,
                "mechanical_risk_codes": [],
                "evidence_manifest_hash": "e" * 64,
                "prior_card_snapshot": _prior_card(continuity_hearing_on, "North House"),
                "identity_authority_granted": False,
            }
        ]
    enrich = _rehash_artifact(enrich)
    audit = _rehash_artifact(_audit(chapter, scan, enrich))
    registry = seal_b1_chapter_registry_v1(
        chapter=chapter, scan_artifact=scan, enrich_artifact=enrich, audit_artifact=audit
    )
    queue = build_b1_cross_chapter_hearing_queue_v1(
        chapter_registry=registry,
        scan_artifact=scan,
        enrich_artifact=enrich,
        audit_artifact=audit,
        prior_cards=prior_cards,
        reconciled_projection=reconciled_projection,
    )
    return queue


def test_proposal_opens_a_hearing_carrying_both_sides() -> None:
    # Mirrors the real chapter-2 shape: the differently-named surface is also a
    # scan observation of its own, so this chapter has built a dossier for it.
    queue = _build(
        proposals=[_roster_proposal("Mara Vale", PRIOR_MARA)], prior_cards=PRIOR_CARDS
    )
    rows = [c for c in queue["components"] if c["question_type"] == "roster_recognition"]
    assert len(rows) == 1
    component = rows[0]
    assert component["review_route"] == "identity_auditor"
    assert component["prior_card_ids"] == [PRIOR_MARA]
    # the prior side is the full dossier, not the compact roster row
    prior = component["prior_candidate_snapshots"][0]
    assert prior["identity_summary"]
    assert prior["provenance_refs"] == [{"chapter_id": "bk_ch00", "block_id": "bk_ch00_b001"}]
    # the earlier chapter's evidence is cited so the Auditor can read both sides
    assert "bk_ch00_b001" in component["source_block_ids"]
    assert "bk_ch01_b001" in component["source_block_ids"]
    # whatever this chapter built for that surface travels with it
    assert component["current_dossier_snapshots"]
    assert component["current_card_snapshots"]
    assert component["identity_authority_granted"] is False
    # rejecting is an ordinary outcome of this question
    assert allowed_verdicts_for_component_v1(component) == ALIAS_REFERRAL_VERDICTS
    assert "alias_rejected_distinct" in ALIAS_REFERRAL_VERDICTS


def test_one_card_gets_one_hearing_not_two() -> None:
    # A card already facing a continuity hearing must not also get a rival
    # roster hearing: two hearings on one card can answer each other's question
    # differently and neither would know.
    queue = _build(
        proposals=[_roster_proposal("North", PRIOR_HOUSE)],
        prior_cards=PRIOR_CARDS,
        continuity_hearing_on=PRIOR_HOUSE,
    )
    on_card = [
        c
        for c in queue["components"]
        if PRIOR_HOUSE in (c.get("prior_card_ids") or [])
    ]
    assert len(on_card) == 1
    component = on_card[0]
    assert component["question_type"] == "identity_linkage"
    attached = component["roster_recognition_proposals"]
    assert [row["surface"] for row in attached] == ["North"]
    assert "bk_ch01_b001" in component["source_block_ids"]
    assert queue["metrics"]["roster_proposals_attached_to_existing_case"] == 1
    assert queue["metrics"]["roster_proposal_component_count"] == 0


def test_roster_evidence_promotes_a_waiting_continuity_case_before_readiness() -> None:
    queue = _build(
        proposals=[_roster_proposal("North House", PRIOR_HOUSE)],
        prior_cards=PRIOR_CARDS,
        continuity_hearing_on=PRIOR_HOUSE,
        continuity_observation_ids=[],
    )
    component = next(
        row
        for row in queue["components"]
        if PRIOR_HOUSE in (row.get("prior_card_ids") or [])
    )
    assert component["current_scan_observation_ids"] == ["b1obs_house"]
    assert component["current_card_snapshots"]
    assert component["current_dossier_snapshots"]
    assert component["lifecycle_state"] == "ready_for_hearing"


def test_several_proposals_on_one_card_stay_in_a_single_hearing() -> None:
    queue = _build(
        proposals=[
            _roster_proposal("Mara", PRIOR_MARA),
            _roster_proposal("Vale", PRIOR_MARA, blocks=["bk_ch01_b002"]),
        ],
        prior_cards=PRIOR_CARDS,
    )
    rows = [c for c in queue["components"] if c["question_type"] == "roster_recognition"]
    assert len(rows) == 1
    assert [row["surface"] for row in rows[0]["roster_recognition_proposals"]] == [
        "Mara",
        "Vale",
    ]


def test_surface_with_no_observation_still_opens_a_hearing_on_the_prior_side() -> None:
    # A proposal about a surface this chapter did not record separately still
    # deserves a hearing: the prior dossier and the cited blocks are enough to
    # ask the question. Missing the current dossier must not silence the case.
    queue = _build(
        proposals=[_roster_proposal("Mara", PRIOR_MARA)], prior_cards=PRIOR_CARDS
    )
    rows = [c for c in queue["components"] if c["question_type"] == "roster_recognition"]
    assert len(rows) == 1
    assert rows[0]["current_dossier_snapshots"] == []
    assert rows[0]["prior_card_snapshot"]["identity_summary"]
    assert "bk_ch00_b001" in rows[0]["source_block_ids"]
    assert rows[0]["lifecycle_state"] == "ready_for_hearing"


def test_proposal_without_supplied_prior_cards_is_counted_not_dropped() -> None:
    queue = _build(proposals=[_roster_proposal("Mara", PRIOR_MARA)], prior_cards=None)
    assert queue["metrics"]["roster_proposal_component_count"] == 0
    assert queue["metrics"]["roster_proposals_unqueued_count"] == 1
    unqueued = queue["unqueued_roster_proposals"][0]
    assert unqueued["prior_card_id"] == PRIOR_MARA
    assert unqueued["reason"]


def test_proposal_naming_an_unsupplied_card_is_counted_not_dropped() -> None:
    queue = _build(
        proposals=[_roster_proposal("Mara", "b0ent_never_supplied")],
        prior_cards=PRIOR_CARDS,
    )
    assert queue["metrics"]["roster_proposals_unqueued_count"] == 1
    assert queue["metrics"]["roster_proposal_component_count"] == 0


def test_queue_without_proposals_binds_the_available_registry_roster() -> None:
    # Queue v2 binds the supplied roster because prior-side evidence expansion
    # uses those surfaces. Components remain unchanged when no proposal exists.
    without = _build(proposals=[], prior_cards=PRIOR_CARDS)
    ignored = _build(proposals=[], prior_cards=None)
    for queue in (without, ignored):
        assert queue["metrics"]["roster_proposal_component_count"] == 0
        assert queue["metrics"]["roster_proposals_unqueued_count"] == 0
        for component in queue["components"]:
            assert "roster_recognition_proposals" not in component
    assert without["components"] == ignored["components"]
    assert without["registry_roster_surfaces"]
    assert ignored["registry_roster_surfaces"] == []
    assert without["queue_hash"] != ignored["queue_hash"]


def test_queue_is_deterministic_and_does_not_mutate_prior_cards() -> None:
    before = deepcopy(PRIOR_CARDS)
    first = _build(proposals=[_roster_proposal("Mara", PRIOR_MARA)], prior_cards=PRIOR_CARDS)
    second = _build(proposals=[_roster_proposal("Mara", PRIOR_MARA)], prior_cards=PRIOR_CARDS)
    assert first["queue_hash"] == second["queue_hash"]
    assert PRIOR_CARDS == before


def test_two_prior_candidates_for_one_current_referent_share_one_hearing() -> None:
    queue = _build(
        proposals=[
            _roster_proposal("Mara Vale", PRIOR_MARA),
            _roster_proposal("Mara Vale", PRIOR_HOUSE),
        ],
        prior_cards=PRIOR_CARDS,
    )

    hearings = [
        row
        for row in queue["components"]
        if row["review_route"] == "identity_auditor"
    ]
    assert len(hearings) == 1
    assert hearings[0]["prior_card_ids"] == sorted([PRIOR_HOUSE, PRIOR_MARA])
    assert len(hearings[0]["prior_candidate_snapshots"]) == 2
    assert queue["metrics"]["identity_candidate_cluster_count"] == 1


def test_production_queue_suppresses_settled_pair_on_same_evidence() -> None:
    first = _build(
        proposals=[_roster_proposal("Mara Vale", PRIOR_MARA)],
        prior_cards=PRIOR_CARDS,
    )
    component = first["components"][0]
    current_id = component["current_entity_ids"][0]
    evidence = sorted(
        {
            *component["source_block_ids"],
            *(
                ref["block_id"]
                for ref in component["prior_candidate_snapshots"][0][
                    "provenance_refs"
                ]
            ),
        }
    )
    projection_body = {
        "schema_version": "literary_b1_reconciled_projection_v1",
        "effective_entities": [],
        "resolved_distinct_cases": [
            {
                "entry_id": "b1dec_settled",
                "card_ids": sorted([PRIOR_MARA, current_id]),
                "evidence_block_ids": evidence,
            }
        ],
        "pending_cases": [],
    }
    projection = {
        **projection_body,
        "projection_hash": canonical_hash(projection_body),
    }

    gated = _build(
        proposals=[_roster_proposal("Mara Vale", PRIOR_MARA)],
        prior_cards=PRIOR_CARDS,
        reconciled_projection=projection,
    )
    assert gated["components"] == []
    assert gated["metrics"]["suppressed_reopen_case_count"] == 1
    assert gated["suppressed_reopen_cases"][0]["prior_state"] == (
        "settled_distinct"
    )
