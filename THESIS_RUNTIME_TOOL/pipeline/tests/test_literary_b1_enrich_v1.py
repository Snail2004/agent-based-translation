from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from pipeline.literary.b1_enrich_v1 import (
    B1EnrichError,
    LINK_RELATIONS,
    PROMPT_ID,
    _validate_links,
    build_b1_enrich_continuity_context_v1,
    render_b1_enrich_request_v1,
    select_b1_enrich_prior_cards_v1,
    validate_b1_enrich_response_v1,
    verify_b1_enrich_continuity_context_v1,
)


DESIGN = Path("../design/LITERARY_PROMPT_DESIGN.md")


def _chapter():
    return {
        "chapter_id": "bk_ch01",
        "blocks": [
            {
                "block_id": "bk_ch01_b001",
                "order_index": 1,
                "clean_text": "Mr. Vale entered North House and called, 'Mara!'",
            },
            {
                "block_id": "bk_ch01_b002",
                "order_index": 2,
                "clean_text": "A brindled dog stopped the quarrel. The old word wuthering was used.",
            },
        ],
    }


def _scan():
    return {
        "artifact_hash": "a" * 64,
        "chapter_id": "bk_ch01",
        "entity_observations": [
            {
                "observation_id": "b1obs_vale",
                "surface": "Mr. Vale",
                "source_block_ids": ["bk_ch01_b001"],
                "referent_kind_claim": "person",
                "record_class": "named_entity_candidate",
                "presence_basis": "direct_presence",
                "scan_note": "Named participant.",
            },
            {
                "observation_id": "b1obs_house",
                "surface": "North House",
                "source_block_ids": ["bk_ch01_b001"],
                "referent_kind_claim": "place",
                "record_class": "named_entity_candidate",
                "presence_basis": "direct_presence",
                "scan_note": "Named place.",
            },
        ],
        "glossary_observations": [
            {
                "term_observation_id": "b1term_wuthering",
                "surface": "wuthering",
                "source_block_ids": ["bk_ch01_b002"],
                "category_hint": "regional_term",
            }
        ],
    }


def _claim(field, *, value, status="supported", basis="explicit_textual", block="bk_ch01_b001"):
    return {
        "field": field,
        "status": status,
        "value": value,
        "basis": basis,
        "anchor_block_ids": [block] if status != "not_applicable" else [],
        "story_time_note": None,
    }


def _entity(scan_id, claims):
    return {
        "scan_observation_id": scan_id,
        "claims": claims,
        "kinship_links": [],
        "links": [],
        "address_forms_used": [],
        "aliases_observed": [],
        "identity_summary": "A source-grounded identity summary.",
        "distinguishing_note": None,
    }


def _response():
    return {
        "schema_id": "LiteraryB1EnrichOutputV1",
        "chapter_id": "bk_ch01",
        "entities": [
            _entity(
                "b1obs_vale",
                [
                    _claim("gender", value="masculine"),
                    _claim("life_stage", value=None, status="unclear", basis=None),
                ],
            ),
            _entity("b1obs_house", [_claim("place_type", value="dwelling")]),
        ],
        "additional_entities": [],
        "spurious_challenges": [],
        "same_referent_proposals": [],
        "conflict_findings": [],
        "presence_correction_findings": [],
        "glossary_items": [
            {
                "term_observation_id": "b1term_wuthering",
                "contextual_sense": "A regional adjective for turbulent weather.",
                "ambiguity_status": "clear",
                "source_block_ids": ["bk_ch01_b002"],
            }
        ],
    }


def _validate(response):
    return validate_b1_enrich_response_v1(
        response,
        chapter=_chapter(),
        scan_artifact=_scan(),
        request_fingerprint="b" * 64,
    )


def test_wrong_chapter_echo_is_normalized_without_dropping_dossiers() -> None:
    response = _response()
    response["chapter_id"] = "copied_example_chapter"

    artifact = _validate(response)

    assert artifact["chapter_id"] == "bk_ch01"
    assert len(artifact["entity_dossiers"]) == 2
    assert artifact["response_normalization_notes"][0]["field"] == "chapter_id"


def test_render_uses_scan_artifact_as_task_list() -> None:
    rendered = render_b1_enrich_request_v1(
        chapter=_chapter(), scan_artifact=_scan(), design_doc=DESIGN
    )
    assert rendered.prompt_id == PROMPT_ID
    assert len(rendered.sections["entity_tasks"]) == 2
    assert rendered.sections["entity_tasks"][0]["task_ref"] == "scan:b1obs_vale"
    assert rendered.parent_working_revision_hash == "a" * 64


def test_render_forwards_adjacent_and_global_summary_context() -> None:
    rendered = render_b1_enrich_request_v1(
        chapter=_chapter(),
        scan_artifact=_scan(),
        design_doc=DESIGN,
        previous_chapter_summary="The adjacent chapter summary.",
        global_summary="The append-only story capsule.",
    )
    assert rendered.sections["summary_context"] == {
        "previous_chapter_summary": "The adjacent chapter summary.",
        "global_summary": "The append-only story capsule.",
    }


def test_prior_projection_includes_only_scan_approved_cards() -> None:
    scan = _scan()
    scan["continuity_routes"] = [
        {
            "prior_card_id": "prior_vale",
            "packet_action": "include_prior_card",
            "hearing_required": False,
        },
        {
            "prior_card_id": "prior_inscription",
            "packet_action": "withhold_prior_card",
            "hearing_required": True,
        },
    ]
    cards = [
        {"prior_card_id": "prior_vale", "canonical_surface": "Mr. Vale"},
        {
            "prior_card_id": "prior_inscription",
            "canonical_surface": "Hareton Earnshaw",
        },
        {"prior_card_id": "prior_unmatched", "canonical_surface": "North House"},
    ]
    assert select_b1_enrich_prior_cards_v1(
        scan_artifact=scan, prior_cards=cards
    ) == [cards[0]]


def test_continuity_context_prejoins_only_approved_card_and_withholds_pending() -> None:
    scan = _scan()
    scan["continuity_routes"] = [
        {
            "prior_card_id": "prior_vale",
            "verdict": "propose_continue",
            "reason_code": "consistent_current_reference",
            "source_block_ids": ["bk_ch01_b001"],
            "reason": "The chapter supports continuity for this candidate.",
            "packet_action": "include_prior_card",
            "hearing_required": False,
            "mechanical_risk_codes": [],
            "identity_authority_granted": False,
        },
        {
            "prior_card_id": "prior_house",
            "verdict": "uncertain",
            "reason_code": "prior_reference_not_established_entity",
            "source_block_ids": ["bk_ch01_b001"],
            "reason": "The prior occurrence is not established strongly enough.",
            "packet_action": "withhold_prior_card",
            "hearing_required": True,
            "mechanical_risk_codes": ["prior_claim_state_is_not_confirmed"],
            "identity_authority_granted": False,
        },
    ]
    cards = [
        {
            "prior_card_id": "prior_vale",
            "canonical_surface": "Mr. Vale",
            "stable_surfaces": ["Mr. Vale"],
        },
        {
            "prior_card_id": "prior_house",
            "canonical_surface": "North House",
            "stable_surfaces": ["North House"],
        },
    ]
    context = build_b1_enrich_continuity_context_v1(
        scan_artifact=scan, prior_cards=cards
    )
    verified = verify_b1_enrich_continuity_context_v1(context)
    by_id = {
        row["scan_observation_id"]: row for row in verified["entity_task_packets"]
    }
    assert by_id["b1obs_vale"]["continuity"] == {
        "state": "continue_prior",
        "continuity_case_ids": [verified["continuity_cases"][1]["continuity_case_id"]],
        "continued_prior_card_id": "prior_vale",
        "prior_card": cards[0],
        "withheld_prior_card_ids": [],
        "marker": None,
    }
    pending = by_id["b1obs_house"]["continuity"]
    assert pending["state"] == "linkage_pending"
    assert pending["prior_card"] is None
    assert pending["withheld_prior_card_ids"] == ["prior_house"]
    assert verified["selected_prior_cards"] == [cards[0]]


def test_render_compacts_only_semantically_identical_prior_claim_history() -> None:
    scan = _scan()
    scan["continuity_routes"] = [
        {
            "prior_card_id": "prior_vale",
            "verdict": "propose_continue",
            "reason_code": "consistent_current_reference",
            "source_block_ids": ["bk_ch01_b001"],
            "reason": "The current participant continues the supplied card.",
            "packet_action": "include_prior_card",
            "hearing_required": False,
            "mechanical_risk_codes": [],
            "identity_authority_granted": False,
        }
    ]
    semantic_claim = {
        "field": "gender",
        "status": "supported",
        "value": "masculine",
        "basis": "explicit_textual",
        "effective": True,
        "story_time_note": None,
        "validity": {"from_block": None, "to_block": None},
        "semantic_status": "auditor_reviewed",
    }
    claims = [
        {**semantic_claim, "anchor_block_ids": ["bk_ch00_b003"]},
        {**semantic_claim, "anchor_block_ids": ["bk_ch01_b001"]},
        {
            **semantic_claim,
            "effective": False,
            "anchor_block_ids": ["bk_ch00_b004"],
        },
        {
            **semantic_claim,
            "basis": "contextual_inference",
            "anchor_block_ids": ["bk_ch00_b005"],
        },
        {
            **semantic_claim,
            "story_time_note": "During the winter of 1801.",
            "anchor_block_ids": ["bk_ch00_b006"],
        },
        {
            **semantic_claim,
            "validity": {
                "from_block": "bk_ch00_b006",
                "to_block": "bk_ch01_b001",
            },
            "anchor_block_ids": ["bk_ch00_b006", "bk_ch01_b001"],
        },
        {
            **semantic_claim,
            "semantic_status": "carried_prior_context",
            "anchor_block_ids": ["bk_ch00_b007"],
        },
    ]
    card = {
        "prior_card_id": "prior_vale",
        "canonical_surface": "Mr. Vale",
        "stable_surfaces": ["Mr. Vale", "Vale"],
        "referent_kind": "person",
        "identity_summary": "Mr. Vale is the named traveler at North House.",
        "record_class": "confirmed_entity",
        "presence_basis": "direct_presence",
        "claim_state": "confirmed",
        "first_supported_block_id": "bk_ch00_b003",
        "profile_claims": claims,
        "provenance_refs": [
            {"chapter_id": "bk_ch00", "block_id": f"bk_ch00_b{index:03d}"}
            for index in range(1, 7)
        ]
        + [
            {"chapter_id": "bk_ch01", "block_id": "bk_ch01_b001"},
            {"chapter_id": "bk_ch01", "block_id": "bk_ch01_b002"},
            {
                "chapter_id": "bk_ch00",
                "block_id": "bk_ch00_b007",
                "source_role": "legacy_extension",
            },
        ],
        "distinguishing_note": "Not the younger Mr. Vale.",
    }
    context = build_b1_enrich_continuity_context_v1(
        scan_artifact=scan, prior_cards=[card]
    )
    context_before_render = deepcopy(context)

    rendered = render_b1_enrich_request_v1(
        chapter=_chapter(),
        scan_artifact=scan,
        design_doc=DESIGN,
        continuity_context=context,
    )

    assert context == context_before_render
    assert verify_b1_enrich_continuity_context_v1(context) == context
    full_card = next(
        row["continuity"]["prior_card"]
        for row in context["entity_task_packets"]
        if row["scan_observation_id"] == "b1obs_vale"
    )
    assert full_card == card

    model_card = next(
        row["continuity"]["prior_card"]
        for row in rendered.sections["entity_tasks"]
        if row["scan_observation_id"] == "b1obs_vale"
    )
    assert {
        key: model_card[key]
        for key in card
        if key not in {"profile_claims", "provenance_refs"}
    } == {
        key: card[key]
        for key in card
        if key not in {"profile_claims", "provenance_refs"}
    }
    assert "provenance_refs" not in model_card
    assert model_card["provenance_summary"] == {
        "source_ref_count": 8,
        "support_chapter_ids": ["bk_ch00", "bk_ch01"],
        "first_block_id": "bk_ch00_b001",
        "last_block_id": "bk_ch01_b002",
        "nonstandard_refs": [card["provenance_refs"][8]],
    }

    compact_claims = model_card["profile_claims"]
    assert len(compact_claims) == 6
    grouped = next(
        row
        for row in compact_claims
        if row["effective"] is True
        and row["basis"] == "explicit_textual"
        and row["story_time_note"] is None
        and row["validity"] == {"from_block": None, "to_block": None}
        and row["semantic_status"] == "auditor_reviewed"
    )
    assert grouped == {
        **semantic_claim,
        "anchor_block_ids": ["bk_ch00_b003", "bk_ch01_b001"],
        "support_record_count": 2,
    }
    assert {
        (
            row["basis"],
            row["effective"],
            row["story_time_note"],
            tuple(sorted(row["validity"].items())),
            row["semantic_status"],
        )
        for row in compact_claims
    } == {
        (
            row["basis"],
            row["effective"],
            row["story_time_note"],
            tuple(sorted(row["validity"].items())),
            row["semantic_status"],
        )
        for row in claims
    }


def test_continuity_join_uses_the_scan_outer_punctuation_normalization() -> None:
    scan = _scan()
    scan["continuity_routes"] = [
        {
            "prior_card_id": "prior_vale",
            "verdict": "propose_continue",
            "reason_code": "consistent_current_reference",
            "source_block_ids": ["bk_ch01_b001"],
            "reason": "The current occurrence supports continuity.",
            "packet_action": "include_prior_card",
            "hearing_required": False,
            "mechanical_risk_codes": [],
            "identity_authority_granted": False,
        }
    ]
    context = build_b1_enrich_continuity_context_v1(
        scan_artifact=scan,
        prior_cards=[
            {
                "prior_card_id": "prior_vale",
                "canonical_surface": "\u201cMr. Vale.\u201d",
                "stable_surfaces": ["\u201cMr. Vale.\u201d"],
            }
        ],
    )
    task = next(
        row
        for row in context["entity_task_packets"]
        if row["scan_observation_id"] == "b1obs_vale"
    )
    assert task["continuity"]["state"] == "continue_prior"


def test_continuity_join_recovers_current_side_from_roster_proposal() -> None:
    scan = _scan()
    scan["entity_observations"][0]["surface"] = "Vale"
    scan["continuity_routes"] = [
        {
            "prior_card_id": "prior_vale",
            "verdict": "propose_continue",
            "reason_code": "consistent_current_reference",
            "source_block_ids": ["bk_ch01_b001"],
            "reason": "The current participant continues the supplied card.",
            "packet_action": "include_prior_card",
            "hearing_required": False,
            "mechanical_risk_codes": [],
            "identity_authority_granted": False,
        }
    ]
    scan["roster_recognition_proposals"] = [
        {
            "proposal_id": "b1rrp_vale",
            "surface": "Vale",
            "prior_card_id": "prior_vale",
            "source_block_ids": ["bk_ch01_b001"],
            "reason": "The shortened surface names the supplied participant.",
        }
    ]
    card = {
        "prior_card_id": "prior_vale",
        "canonical_surface": "Mr. Vale",
        "stable_surfaces": ["Mr. Vale"],
    }

    context = build_b1_enrich_continuity_context_v1(
        scan_artifact=scan, prior_cards=[card]
    )

    case = context["continuity_cases"][0]
    assert case["current_scan_observation_ids"] == ["b1obs_vale"]
    assert case["mechanical_risk_codes"] == [
        "no_exact_current_scan_observation"
    ]
    assert case["hearing_required"] is False
    assert case["packet_action"] == "include_prior_card"
    task = next(
        row
        for row in context["entity_task_packets"]
        if row["scan_observation_id"] == "b1obs_vale"
    )
    assert task["continuity"]["state"] == "continue_prior"
    assert task["continuity"]["continued_prior_card_id"] == "prior_vale"


def test_continuity_carries_referenced_prior_without_inventing_observation() -> None:
    scan = _scan()
    scan["continuity_routes"] = [
        {
            "prior_card_id": "prior_grange",
            "verdict": "propose_continue",
            "reason_code": "consistent_current_reference",
            "source_block_ids": ["bk_ch01_b001"],
            "reason": "The chapter references the same supplied place.",
            "packet_action": "include_prior_card",
            "hearing_required": False,
            "mechanical_risk_codes": [],
            "identity_authority_granted": False,
        }
    ]
    card = {
        "prior_card_id": "prior_grange",
        "canonical_surface": "South Grange",
        "stable_surfaces": ["South Grange"],
    }

    context = build_b1_enrich_continuity_context_v1(
        scan_artifact=scan, prior_cards=[card]
    )

    case = context["continuity_cases"][0]
    assert case["current_scan_observation_ids"] == []
    assert case["packet_action"] == "carry_referenced_prior_card"
    assert case["hearing_required"] is False
    assert "referenced_prior_without_current_observation" in case[
        "mechanical_risk_codes"
    ]
    assert context["selected_prior_cards"] == [card]
    assert all(
        row["continuity"]["continued_prior_card_id"] is None
        for row in context["entity_task_packets"]
    )
    assert verify_b1_enrich_continuity_context_v1(context) == context


def test_continuity_context_non_unique_join_does_not_override_scan_verdict() -> None:
    scan = _scan()
    scan["continuity_routes"] = [
        {
            "prior_card_id": "prior_vale_a",
            "verdict": "propose_continue",
            "reason_code": "consistent_current_reference",
            "source_block_ids": ["bk_ch01_b001"],
            "reason": "One supplied candidate may continue.",
            "packet_action": "include_prior_card",
            "hearing_required": False,
            "mechanical_risk_codes": [],
            "identity_authority_granted": False,
        },
        {
            "prior_card_id": "prior_vale_b",
            "verdict": "propose_continue",
            "reason_code": "consistent_current_reference",
            "source_block_ids": ["bk_ch01_b001"],
            "reason": "Another supplied candidate may continue.",
            "packet_action": "include_prior_card",
            "hearing_required": False,
            "mechanical_risk_codes": [],
            "identity_authority_granted": False,
        },
    ]
    cards = [
        {
            "prior_card_id": card_id,
            "canonical_surface": "Mr. Vale",
            "stable_surfaces": ["Mr. Vale"],
        }
        for card_id in ("prior_vale_a", "prior_vale_b")
    ]
    context = build_b1_enrich_continuity_context_v1(
        scan_artifact=scan, prior_cards=cards
    )
    task = next(
        row
        for row in context["entity_task_packets"]
        if row["scan_observation_id"] == "b1obs_vale"
    )
    assert task["continuity"]["state"] == "linkage_pending"
    assert task["continuity"]["prior_card"] is None
    assert all(
        row["hearing_required"] is False
        for row in context["continuity_cases"]
    )
    assert {row["prior_card_id"] for row in context["selected_prior_cards"]} == {
        "prior_vale_a",
        "prior_vale_b",
    }
    assert all(
        "current_observation_matches_multiple_prior_cards"
        in row["mechanical_risk_codes"]
        for row in context["continuity_cases"]
    )


def test_continuity_context_hash_tamper_fails_closed() -> None:
    context = build_b1_enrich_continuity_context_v1(
        scan_artifact=_scan(), prior_cards=[]
    )
    context["entity_task_packets"][0]["surface"] = "Foreign surface"
    with pytest.raises(B1EnrichError, match="hash mismatch"):
        verify_b1_enrich_continuity_context_v1(context)


@pytest.mark.parametrize("failure", ["foreign", "duplicate", "contradiction"])
def test_prior_projection_rejects_invalid_routes(failure: str) -> None:
    scan = _scan()
    route = {
        "prior_card_id": "prior_vale",
        "packet_action": "include_prior_card",
        "hearing_required": False,
    }
    scan["continuity_routes"] = [route]
    cards = [{"prior_card_id": "prior_vale", "canonical_surface": "Mr. Vale"}]
    if failure == "foreign":
        route["prior_card_id"] = "prior_foreign"
    elif failure == "duplicate":
        scan["continuity_routes"].append(deepcopy(route))
    else:
        route["hearing_required"] = True
    with pytest.raises(B1EnrichError):
        select_b1_enrich_prior_cards_v1(
            scan_artifact=scan, prior_cards=cards
        )


def test_valid_response_builds_provisional_dossiers() -> None:
    artifact = _validate(_response())
    assert artifact["metrics"] == {
        "enriched_entity_count": 2,
        "additional_entity_count": 0,
        "spurious_challenge_count": 0,
        "same_referent_proposal_count": 0,
        "glossary_item_count": 1,
        "finding_count": 0,
        "quarantined_task_count": 0,
        "review_issue_count": 0,
        "content_field_quarantine_count": 0,
    }


def test_unknown_entity_link_relation_degrades_without_losing_dossier() -> None:
    response = _response()
    raw_link = {
        "relation": "guardian_of",
        "relation_note": "acts as guardian of",
        "target_ref": "scan:b1obs_house",
        "basis": "explicit_textual",
        "anchor_block_ids": ["bk_ch01_b001"],
    }
    response["entities"][0]["links"] = [deepcopy(raw_link)]

    artifact = _validate(response)

    dossier = next(
        row
        for row in artifact["entity_dossiers"]
        if row["scan_observation_id"] == "b1obs_vale"
    )
    assert dossier["links"] == [
        {
            "relation": "other_link",
            "target_ref": "scan:b1obs_house",
            "basis": "explicit_textual",
            "anchor_block_ids": ["bk_ch01_b001"],
            "relation_note": "acts as guardian of",
            "relation_raw": "guardian_of",
            "relation_status": "quarantined_invalid_enum",
            "validity_scope": "as_of_chapter",
            "semantic_status": "unreviewed",
        }
    ]
    assert artifact["review_issues"] == []
    assert artifact["content_field_quarantines"][0]["raw_value"] == "guardian_of"

    closed_issues = []
    assert (
        _validate_links(
            [raw_link],
            LINK_RELATIONS - {"other_link"},
            {"bk_ch01_b001": _chapter()["blocks"][0]},
            note_field=True,
            open_relation=None,
            issues=closed_issues,
            content_field_quarantines=[],
            issue_prefix="entity_link",
        )
        == []
    )
    assert "unsupported value 'guardian_of'" in closed_issues[0]["reason"]


def test_enrich_can_propose_unnamed_observation_to_named_observation() -> None:
    chapter = _chapter()
    scan = _scan()
    scan["entity_observations"].append(
        {
            "observation_id": "b1obs_caller",
            "surface": "the caller",
            "source_block_ids": ["bk_ch01_b001"],
            "referent_kind_claim": "person",
            "record_class": "important_unnamed_referent",
            "presence_basis": "direct_presence",
            "scan_note": "An individualized unnamed participant.",
        }
    )
    response = _response()
    response["entities"].append(
        _entity(
            "b1obs_caller",
            [
                _claim("gender", value=None, status="unclear", basis=None),
                _claim("life_stage", value=None, status="unclear", basis=None),
            ],
        )
    )
    response["same_referent_proposals"] = [
        {
            "subject_ref": "scan:b1obs_caller",
            "target_ref": "scan:b1obs_vale",
            "proposal_basis": "chapter_context_description",
            "source_block_ids": ["bk_ch01_b001"],
            "reason": "The chapter presents the unnamed caller as Mr. Vale.",
        }
    ]

    artifact = validate_b1_enrich_response_v1(
        response,
        chapter=chapter,
        scan_artifact=scan,
        request_fingerprint="b" * 64,
    )

    assert artifact["same_referent_proposals"] == [
        {
            "subject_ref": "scan:b1obs_caller",
            "target_ref": "scan:b1obs_vale",
            "proposal_basis": "chapter_context_description",
            "source_block_ids": ["bk_ch01_b001"],
            "reason": "The chapter presents the unnamed caller as Mr. Vale.",
            "retrieval_surface_policy": "subject_evidence_only",
            "requires_local_auditor": True,
            "identity_authority_granted": False,
        }
    ]
    assert artifact["metrics"]["same_referent_proposal_count"] == 1
    assert artifact["identity_authority_granted"] is False
    assert artifact["registry_mutation_performed"] is False


def test_same_referent_proposal_is_quarantined_when_target_dossier_is_invalid() -> None:
    chapter = _chapter()
    scan = _scan()
    scan["entity_observations"].append(
        {
            "observation_id": "b1obs_caller",
            "surface": "the caller",
            "source_block_ids": ["bk_ch01_b001"],
            "referent_kind_claim": "person",
            "record_class": "important_unnamed_referent",
            "presence_basis": "direct_presence",
            "scan_note": "An individualized unnamed participant.",
        }
    )
    response = _response()
    response["entities"].append(
        _entity(
            "b1obs_caller",
            [
                _claim("gender", value=None, status="unclear", basis=None),
                _claim("life_stage", value=None, status="unclear", basis=None),
            ],
        )
    )
    response["entities"][0]["claims"] = [_claim("gender", value="masculine")]
    response["same_referent_proposals"] = [
        {
            "subject_ref": "scan:b1obs_caller",
            "target_ref": "scan:b1obs_vale",
            "proposal_basis": "chapter_context_description",
            "source_block_ids": ["bk_ch01_b001"],
            "reason": "The chapter presents the unnamed caller as Mr. Vale.",
        }
    ]

    artifact = validate_b1_enrich_response_v1(
        response,
        chapter=chapter,
        scan_artifact=scan,
        request_fingerprint="b" * 64,
    )

    assert artifact["same_referent_proposals"] == []
    assert artifact["metrics"]["same_referent_proposal_count"] == 0
    assert {"task_id": "b1obs_vale", "reason": "missing_valid_disposition"} in artifact[
        "quarantined_tasks"
    ]
    issue = next(
        row
        for row in artifact["review_issues"]
        if row["row_type"] == "same_referent_proposal"
    )
    assert "requires accepted subject and target dossiers" in issue["reason"]


def test_missing_required_person_claim_quarantines_only_that_task() -> None:
    response = _response()
    response["entities"][0]["claims"] = [_claim("life_stage", value="adult")]
    artifact = _validate(response)
    assert len(artifact["entity_dossiers"]) == 1
    assert artifact["quarantined_tasks"] == [
        {"task_id": "b1obs_vale", "reason": "missing_valid_disposition"}
    ]
    assert artifact["review_issues"][0]["row_type"] == "entity"


def test_inapplicable_gender_cannot_be_supported_for_place() -> None:
    response = _response()
    response["entities"][1]["claims"].append(_claim("gender", value="neutral"))
    artifact = _validate(response)
    assert {row["task_id"] for row in artifact["quarantined_tasks"]} == {"b1obs_house"}
    assert "inapplicable" in artifact["review_issues"][0]["reason"]


def test_other_kind_specific_claims_cannot_cross_referent_kinds() -> None:
    response = _response()
    response["entities"][1]["claims"].append(_claim("species", value="house"))
    artifact = _validate(response)
    assert {row["task_id"] for row in artifact["quarantined_tasks"]} == {
        "b1obs_house"
    }
    assert "species is inapplicable" in artifact["review_issues"][0]["reason"]


def test_additional_entity_and_spurious_challenge_are_first_class() -> None:
    response = _response()
    response["entities"] = [response["entities"][1]]
    response["spurious_challenges"] = [
        {
            "scan_observation_id": "b1obs_vale",
            "reason": "The supplied row is not a usable referent in this fixture.",
            "source_block_ids": ["bk_ch01_b001"],
        }
    ]
    additional = _entity(
        "unused",
        [_claim("species", value="dog", block="bk_ch01_b002")],
    )
    additional.pop("scan_observation_id")
    additional.update(
        {
            "surface": "A brindled dog",
            "source_block_ids": ["bk_ch01_b002"],
            "referent_kind_claim": "animal",
        }
    )
    response["additional_entities"] = [additional]
    artifact = _validate(response)
    assert artifact["metrics"]["additional_entity_count"] == 1
    assert artifact["metrics"]["spurious_challenge_count"] == 1


def test_duplicate_enrich_and_challenge_is_quarantined() -> None:
    response = deepcopy(_response())
    response["spurious_challenges"] = [
        {
            "scan_observation_id": "b1obs_vale",
            "reason": "Conflicting model disposition.",
            "source_block_ids": ["bk_ch01_b001"],
        }
    ]
    artifact = _validate(response)
    assert {row["task_id"] for row in artifact["quarantined_tasks"]} == {"b1obs_vale"}


def test_nonverbatim_optional_anchor_is_removed_without_losing_dossier() -> None:
    response = _response()
    response["entities"][0]["address_forms_used"] = [
        {
            "counterpart_ref": "scan:b1obs_house",
            "mode": "about",
            "form": "Mr. Vale",
            "anchor_block_ids": ["bk_ch01_b001", "bk_ch01_b002"],
        }
    ]
    artifact = _validate(response)
    vale = next(
        row for row in artifact["entity_dossiers"] if row["surface"] == "Mr. Vale"
    )
    assert vale["address_forms_used"][0]["anchor_block_ids"] == ["bk_ch01_b001"]
    assert artifact["quarantined_tasks"] == []
    assert "removed non-verbatim anchor" in artifact["review_issues"][0]["reason"]


def test_additional_entity_keeps_exact_surface_anchor_and_flags_extra_block() -> None:
    response = _response()
    additional = _entity(
        "unused",
        [_claim("species", value="dog", block="bk_ch01_b002")],
    )
    additional.pop("scan_observation_id")
    additional.update(
        {
            "surface": "A brindled dog",
            "source_block_ids": ["bk_ch01_b001", "bk_ch01_b002"],
            "referent_kind_claim": "animal",
        }
    )
    response["additional_entities"] = [additional]
    artifact = _validate(response)
    assert artifact["additional_entity_dossiers"][0]["source_block_ids"] == [
        "bk_ch01_b002"
    ]
    assert artifact["metrics"]["additional_entity_count"] == 1
    assert "removed non-verbatim anchor" in artifact["review_issues"][0]["reason"]
