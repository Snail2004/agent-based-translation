"""Tests for the B1-Scan registry roster and recognition-proposal channel (N1).

The roster makes every known prior entity visible to the model even when no
stable surface of that entity appears verbatim in the current chapter - the
only path that can catch renames, married names, and titles.  Proposals stay
zero-authority; identity remains a hearing question.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from pipeline.literary.b1_scan_v1 import (
    MAX_OBSERVATION_BLOCK_IDS,
    MAX_ROSTER_PROPOSALS,
    PROMPT_ID,
    B1ScanError,
    b1_scan_response_schema_v1,
    build_b1_registry_roster_v1,
    make_b1_scan_semantic_validator_v1,
    render_b1_scan_request_v1,
    validate_b1_scan_response_v1,
)
from pipeline.literary.builder_pilot import load_system_prompt_from_design

DESIGN_DOC = Path(__file__).resolve().parents[3] / "design" / "LITERARY_PROMPT_DESIGN.md"


def _prior_card(card_id: str, canonical: str, *, surfaces: list[str] | None = None) -> dict:
    return {
        "prior_card_id": card_id,
        "canonical_surface": canonical,
        "stable_surfaces": surfaces or [canonical],
        "referent_kind": "person",
        "identity_summary": f"{canonical} is a known referent from an earlier chapter.",
        "record_class": "confirmed_entity",
        "presence_basis": "direct_presence",
        "claim_state": "confirmed",
        "first_supported_block_id": "bk_ch01_b001",
        "provenance_refs": [{"chapter_id": "bk_ch01", "block_id": "bk_ch01_b001"}],
    }


# The chapter text never contains the surface "Odalys Fenwick"; her card can
# only reach the model through the roster.
CHAPTER = {
    "chapter_id": "bk_ch02",
    "blocks": [
        {
            "block_id": "bk_ch02_b001",
            "order_index": 1,
            "text": "Tamsin Reed carried the lantern across the yard.",
        },
        {
            "block_id": "bk_ch02_b002",
            "order_index": 2,
            "text": "The mistress of the low farm counted the sacks herself.",
        },
        {
            "block_id": "bk_ch02_b003",
            "order_index": 3,
            "text": "Nobody argued with the mistress of the low farm about the ledger.",
        },
    ],
}

PRIOR_CARDS = [
    _prior_card("pcard_fenwick01", "Odalys Fenwick"),
    _prior_card("pcard_reed02", "Tamsin Reed"),
]


def _base_response(**overrides) -> dict:
    row = {
        "schema_id": "LiteraryB1ScanOutputV1",
        "chapter_id": "bk_ch02",
        "entity_observations": [
            {
                "surface": "Tamsin Reed",
                "source_block_ids": ["bk_ch02_b001"],
                "referent_kind_claim": "person",
                "record_class": "named_entity_candidate",
                "presence_basis": "direct_presence",
                "scan_note": "Named person acting directly in the chapter.",
            }
        ],
        "glossary_observations": [],
        "prior_continuity_proposals": [
            {
                "prior_card_id": "pcard_reed02",
                "verdict": "propose_continue",
                "reason_code": "consistent_current_reference",
                "source_block_ids": ["bk_ch02_b001"],
                "reason": "The same named referent acts in this chapter.",
            }
        ],
    }
    row.update(overrides)
    return row


def _roster_proposal(**overrides) -> dict:
    row = {
        "surface": "the mistress of the low farm",
        "prior_card_id": "pcard_fenwick01",
        "source_block_ids": ["bk_ch02_b002", "bk_ch02_b003"],
        "reason": "The chapter refers to this household head by role instead of name.",
    }
    row.update(overrides)
    return row


def _validate(response: dict) -> dict:
    rendered = render_b1_scan_request_v1(
        chapter=CHAPTER,
        design_doc=DESIGN_DOC,
        prior_cards=PRIOR_CARDS,
    )
    validate = make_b1_scan_semantic_validator_v1(chapter=CHAPTER, rendered=rendered)
    return dict(validate(response))


# ---------------------------------------------------------------------------
# roster rendering
# ---------------------------------------------------------------------------


def test_roster_lists_every_prior_card_including_unmatched_surfaces() -> None:
    rendered = render_b1_scan_request_v1(
        chapter=CHAPTER, design_doc=DESIGN_DOC, prior_cards=PRIOR_CARDS
    )
    roster = rendered.sections["registry_roster"]
    assert [row["prior_card_id"] for row in roster] == [
        "pcard_fenwick01",
        "pcard_reed02",
    ]
    # packets stay surface-filtered: only Tamsin Reed matches the chapter text
    packet_ids = {
        row["prior_card"]["prior_card_id"]
        for row in rendered.sections["prior_candidate_packets"]
    }
    assert packet_ids == {"pcard_reed02"}
    # roster rows are compact retrieval context, not full cards
    assert set(roster[0]) == {
        "prior_card_id",
        "canonical_surface",
        "stable_surfaces",
        "referent_kind",
        "record_class",
    }


def test_first_chapter_roster_is_empty_and_legal() -> None:
    rendered = render_b1_scan_request_v1(
        chapter=CHAPTER, design_doc=DESIGN_DOC, prior_cards=None
    )
    assert rendered.sections["registry_roster"] == []


def test_bound_prompt_documents_the_channel_and_the_evidence_rule() -> None:
    assert PROMPT_ID == "literary_b1_scan_v1_5"
    prompt = load_system_prompt_from_design(DESIGN_DOC, PROMPT_ID)
    assert "REGISTRY_ROSTER" in prompt
    assert "roster_recognition_proposals" in prompt
    # the prompt must not re-impose the verbatim rule the validator dropped
    assert "ONLY blocks that contain that exact surface" not in prompt
    assert "up to eight" in prompt
    schema = b1_scan_response_schema_v1()
    assert "roster_recognition_proposals" in schema["properties"]
    # optional: historical responses without the channel stay valid
    assert "roster_recognition_proposals" not in schema["required"]


# ---------------------------------------------------------------------------
# proposal validation
# ---------------------------------------------------------------------------


def test_valid_recognition_proposal_is_accepted_with_zero_authority() -> None:
    artifact = _validate(
        _base_response(roster_recognition_proposals=[_roster_proposal()])
    )
    rows = artifact["roster_recognition_proposals"]
    assert len(rows) == 1
    row = rows[0]
    assert row["prior_card_id"] == "pcard_fenwick01"
    assert row["authority_scope"] == "proposal_only"
    assert row["identity_authority_granted"] is False
    assert row["roster_card"]["canonical_surface"] == "Odalys Fenwick"
    assert row["proposal_id"].startswith("b1rrp_")
    assert artifact["metrics"]["roster_recognition_count"] == 1
    assert artifact["metrics"]["review_issue_count"] == 0


def test_absent_channel_stays_backward_compatible() -> None:
    artifact = _validate(_base_response())
    assert artifact["roster_recognition_proposals"] == []
    assert artifact["metrics"]["roster_recognition_count"] == 0


def test_unknown_roster_target_is_quarantined_not_fatal() -> None:
    artifact = _validate(
        _base_response(
            roster_recognition_proposals=[
                _roster_proposal(prior_card_id="pcard_unknown99")
            ]
        )
    )
    assert artifact["roster_recognition_proposals"] == []
    reasons = [row["reason"] for row in artifact["review_issues"]]
    assert any("outside the supplied roster" in reason for reason in reasons)


def test_surface_need_not_appear_verbatim_because_code_does_not_read_language() -> None:
    # A block can carry a referent through a pronoun, an epithet, or dialogue
    # with no name in it.  Deciding whether it does is the model's judgment;
    # code checks only that the cited block exists in this chapter.
    artifact = _validate(
        _base_response(
            roster_recognition_proposals=[
                _roster_proposal(surface="the mistress of the high farm")
            ]
        )
    )
    assert len(artifact["roster_recognition_proposals"]) == 1
    assert artifact["metrics"]["review_issue_count"] == 0
    assert artifact["roster_recognition_proposals"][0]["identity_authority_granted"] is False


def test_foreign_block_is_still_rejected() -> None:
    # The mechanical guard that survives: a cited block must exist here.
    artifact = _validate(
        _base_response(
            roster_recognition_proposals=[
                _roster_proposal(source_block_ids=["bk_ch99_b001"])
            ]
        )
    )
    assert artifact["roster_recognition_proposals"] == []
    reasons = [row["reason"] for row in artifact["review_issues"]]
    assert any("foreign block" in reason for reason in reasons)


def test_over_cited_proposal_is_trimmed_and_kept_not_dropped() -> None:
    # Regression: a real run proposed "Mrs. Heathcliff" -> a prior card with
    # five supporting blocks and the row was dropped whole, losing the only
    # channel that can catch a renamed referent.  Over-citing bounds evidence;
    # it never invalidates the judgment.
    chapter = {
        "chapter_id": "bk_ch02",
        "blocks": [
            {
                "block_id": f"bk_ch02_c{index:03d}",
                "order_index": index,
                "text": f"The mistress of the low farm settled matter {index}.",
            }
            for index in range(1, 12)
        ],
    }
    rendered = render_b1_scan_request_v1(
        chapter=chapter, design_doc=DESIGN_DOC, prior_cards=PRIOR_CARDS
    )
    validate = make_b1_scan_semantic_validator_v1(chapter=chapter, rendered=rendered)
    over_cited = [f"bk_ch02_c{index:03d}" for index in range(1, 12)]
    response = _base_response(
        entity_observations=[],
        prior_continuity_proposals=[],
        roster_recognition_proposals=[
            _roster_proposal(source_block_ids=over_cited)
        ],
    )
    artifact = dict(validate(response))
    rows = artifact["roster_recognition_proposals"]
    assert len(rows) == 1
    assert len(rows[0]["source_block_ids"]) == MAX_OBSERVATION_BLOCK_IDS
    assert rows[0]["source_block_ids"] == over_cited[:MAX_OBSERVATION_BLOCK_IDS]
    overflow = [
        row
        for row in artifact["review_issues"]
        if row["row_type"] == "roster_recognition_proposal_support_overflow"
    ]
    assert len(overflow) == 1
    assert overflow[0]["omitted_source_block_count"] == 11 - MAX_OBSERVATION_BLOCK_IDS


def test_known_stable_surface_is_rejected_as_channel_overlap() -> None:
    artifact = _validate(
        _base_response(
            roster_recognition_proposals=[
                _roster_proposal(
                    surface="Tamsin Reed",
                    prior_card_id="pcard_reed02",
                    source_block_ids=["bk_ch02_b001"],
                )
            ]
        )
    )
    assert artifact["roster_recognition_proposals"] == []
    reasons = [row["reason"] for row in artifact["review_issues"]]
    assert any("continuity channel owns same-surface" in reason for reason in reasons)


def test_duplicate_proposals_and_cap_overflow_are_quarantined() -> None:
    duplicate = [_roster_proposal(), _roster_proposal()]
    artifact = _validate(_base_response(roster_recognition_proposals=duplicate))
    assert len(artifact["roster_recognition_proposals"]) == 1
    reasons = [row["reason"] for row in artifact["review_issues"]]
    assert any("duplicate roster proposal" in reason for reason in reasons)

    surfaces = [
        "Tamsin Reed carried the lantern",
        "the lantern across the yard",
        "counted the sacks herself",
        "argued with the mistress",
        "the low farm",
        "carried the lantern",
        "across the yard",
        "the ledger",
        "the yard",
        "the sacks",
    ]
    many = [
        _roster_proposal(
            surface=surface,
            source_block_ids=[
                "bk_ch02_b001"
                if surface
                in {
                    "Tamsin Reed carried the lantern",
                    "the lantern across the yard",
                    "carried the lantern",
                    "across the yard",
                    "the yard",
                }
                else ("bk_ch02_b002" if surface in {"counted the sacks herself", "the low farm", "the sacks"} else "bk_ch02_b003")
            ],
        )
        for surface in surfaces
    ]
    artifact = _validate(_base_response(roster_recognition_proposals=many))
    assert len(artifact["roster_recognition_proposals"]) == MAX_ROSTER_PROPOSALS
    reasons = [row["reason"] for row in artifact["review_issues"]]
    assert any("bounded cap" in reason for reason in reasons)


def test_direct_validate_without_roster_quarantines_all_proposals() -> None:
    # a caller that supplies no roster cannot accept recognition rows
    artifact = validate_b1_scan_response_v1(
        _base_response(roster_recognition_proposals=[_roster_proposal()]),
        chapter=CHAPTER,
        prior_candidate_packets=[],
        request_fingerprint="a" * 64,
        registry_roster=None,
    )
    assert artifact["roster_recognition_proposals"] == []


def test_roster_proposal_never_grants_identity_authority_flags() -> None:
    artifact = _validate(
        _base_response(roster_recognition_proposals=[_roster_proposal()])
    )
    assert artifact["identity_authority_granted"] is False
    assert artifact["registry_mutation_performed"] is False


def test_inputs_are_not_mutated_and_validation_is_deterministic() -> None:
    response = _base_response(roster_recognition_proposals=[_roster_proposal()])
    response_before = deepcopy(response)
    first = _validate(response)
    second = _validate(response)
    assert first == second
    assert response == response_before
