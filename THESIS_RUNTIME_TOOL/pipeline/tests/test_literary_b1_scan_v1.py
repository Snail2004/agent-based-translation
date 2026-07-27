from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.literary.b1_scan_v1 import (
    B1ScanError,
    MAX_PRIOR_CARDS,
    MAX_PRIOR_PROFILE_CLAIMS,
    MAX_OBSERVATION_BLOCK_IDS,
    OUTPUT_SCHEMA_ID,
    PROMPT_ID,
    b1_scan_response_schema_v1,
    build_prior_candidate_packets_v1,
    render_b1_scan_request_v1,
    shared_b1_scan_request_v1,
    validate_b1_scan_response_v1,
)
from pipeline.literary.builder_pilot import load_system_prompt_from_design


REPO_ROOT = Path(__file__).resolve().parents[3]
DESIGN_DOC = REPO_ROOT / "design" / "LITERARY_PROMPT_DESIGN.md"


def _chapter() -> dict:
    return {
        "chapter_id": "bk_ch02",
        "blocks": [
            {
                "block_id": "bk_ch02_b001",
                "order_index": 1,
                "clean_text": "Ms. Vale entered North House with Brindle.",
            },
            {
                "block_id": "bk_ch02_b002",
                "order_index": 2,
                "clean_text": "A stone read 'Robin Vale 1672'; the regional word stormfast followed.",
            },
            {
                "block_id": "bk_ch02_b003",
                "order_index": 3,
                "clean_text": "Robin Vale answered from the doorway.",
            },
        ],
    }


def _prior(
    card_id: str = "pcard_robin_old",
    *,
    record_class: str = "unresolved_named_reference",
    claim_state: str = "provisional",
) -> dict:
    return {
        "prior_card_id": card_id,
        "canonical_surface": "Robin Vale",
        "stable_surfaces": ["Robin Vale"],
        "referent_kind": "unknown",
        "identity_summary": "A name found in an earlier dated inscription.",
        "record_class": record_class,
        "presence_basis": "inscription_or_document",
        "claim_state": claim_state,
        "first_supported_block_id": "bk_ch01_b010",
        "provenance_refs": [
            {"chapter_id": "bk_ch01", "block_id": "bk_ch01_b010"}
        ],
    }


def _response(*, continuity: list[dict] | None = None) -> dict:
    return {
        "schema_id": OUTPUT_SCHEMA_ID,
        "chapter_id": "bk_ch02",
        "entity_observations": [
            {
                "surface": "Ms. Vale",
                "source_block_ids": ["bk_ch02_b001"],
                "referent_kind_claim": "person",
                "record_class": "named_entity_candidate",
                "presence_basis": "direct_presence",
                "scan_note": "A named participant entering the house.",
            },
            {
                "surface": "Robin Vale",
                "source_block_ids": ["bk_ch02_b002"],
                "referent_kind_claim": "unknown",
                "record_class": "unresolved_named_reference",
                "presence_basis": "inscription_or_document",
                "scan_note": "A name in a dated inscription, not an established person here.",
            },
            {
                "surface": "Robin Vale",
                "source_block_ids": ["bk_ch02_b003"],
                "referent_kind_claim": "person",
                "record_class": "named_entity_candidate",
                "presence_basis": "direct_presence",
                "scan_note": "A named participant who answers.",
            },
        ],
        "glossary_observations": [
            {
                "surface": "stormfast",
                "source_block_ids": ["bk_ch02_b002"],
                "category_hint": "regional_term",
                "term_category_raw": None,
            }
        ],
        "prior_continuity_proposals": continuity or [],
    }


def test_wrong_chapter_echo_is_normalized_without_dropping_scan_rows() -> None:
    response = _response()
    response["chapter_id"] = "copied_example_chapter"

    artifact = validate_b1_scan_response_v1(
        response,
        chapter=_chapter(),
        prior_candidate_packets=[],
        request_fingerprint="a" * 64,
    )

    assert artifact["chapter_id"] == "bk_ch02"
    assert len(artifact["entity_observations"]) == 3
    assert artifact["response_normalization_notes"][0]["field"] == "chapter_id"


def test_other_glossary_category_preserves_raw_description() -> None:
    response = _response()
    response["glossary_observations"][0].update(
        {
            "category_hint": "other",
            "term_category_raw": "weather-dialect descriptor",
        }
    )

    artifact = validate_b1_scan_response_v1(
        response,
        chapter=_chapter(),
        prior_candidate_packets=[],
        request_fingerprint="a" * 64,
    )

    row = artifact["glossary_observations"][0]
    assert row["category_hint"] == "other"
    assert row["term_category_raw"] == "weather-dialect descriptor"
    assert row["term_category_status"] == "model_other"
    assert artifact["content_field_quarantines"] == []


def test_unknown_glossary_category_degrades_without_losing_row() -> None:
    response = _response()
    response["glossary_observations"][0].update(
        {
            "category_hint": "folk_weather_term",
            "term_category_raw": None,
        }
    )

    artifact = validate_b1_scan_response_v1(
        response,
        chapter=_chapter(),
        prior_candidate_packets=[],
        request_fingerprint="a" * 64,
    )

    row = artifact["glossary_observations"][0]
    assert row["category_hint"] == "other"
    assert row["term_category_raw"] == "folk_weather_term"
    assert row["term_category_status"] == "quarantined_invalid_enum"
    assert artifact["review_issues"] == []
    assert artifact["content_field_quarantines"][0]["raw_value"] == "folk_weather_term"


def test_prompt_is_narrow_book_neutral_and_does_not_claim_identity_authority() -> None:
    prompt = load_system_prompt_from_design(DESIGN_DOC, PROMPT_ID)
    lowered = prompt.casefold()
    for forbidden in ("heathcliff", "hareton", "wuthering heights", "lockwood"):
        assert forbidden not in lowered
    assert "lightweight scan" in lowered
    assert "not a dossier builder" in lowered
    assert "pronouns" in lowered
    assert "inscription_or_document" in prompt
    assert "do not merge or split identities" in lowered


def test_request_contains_grouped_prior_packet_and_no_full_registry() -> None:
    rendered = render_b1_scan_request_v1(
        chapter=_chapter(), design_doc=DESIGN_DOC, prior_cards=[_prior()]
    )
    packets = rendered.sections["prior_candidate_packets"]
    assert len(packets) == 1
    assert packets[0]["prior_card"]["prior_card_id"] == "pcard_robin_old"
    hits = packets[0]["current_surface_hits"]
    assert hits[0] == {
        "surface": "Robin Vale",
        "retrieval_surface": "Robin Vale",
        "match_basis": "exact",
        "current_block_ids": ["bk_ch02_b002", "bk_ch02_b003"],
        "current_hit_block_count": 2,
        "block_ids_truncated": False,
    }
    assert {row["retrieval_surface"] for row in hits} == {
        "Robin Vale",
        "Robin",
        "Vale",
    }
    assert "full_registry" not in rendered.sections
    shared = shared_b1_scan_request_v1(rendered)
    assert shared["response_schema"] == b1_scan_response_schema_v1()
    assert shared["request_fingerprint"] == rendered.request_fingerprint


def test_bounded_prior_identity_claims_travel_with_the_surface_hit() -> None:
    prior = _prior(record_class="confirmed_entity", claim_state="confirmed")
    prior["profile_claims"] = [
        {
            "field": "gender",
            "status": "supported",
            "value": "masculine",
            "basis": "explicit_textual",
            "effective": True,
            "anchor_block_ids": ["bk_ch01_b010"],
            "story_time_note": None,
            "validity": {"from_block": None, "to_block": None},
            "semantic_status": "unreviewed",
        }
    ]
    prior["distinguishing_note"] = "Earlier occurrence is inscriptional only."
    packets = build_prior_candidate_packets_v1(
        chapter=_chapter(), prior_cards=[prior]
    )
    card = packets[0]["prior_card"]
    assert card["profile_claims"] == [
        {
            "field": "gender",
            "status": "supported",
            "value": "masculine",
            "effective": True,
            "basis_values": ["explicit_textual"],
            "support_count": 1,
        }
    ]
    assert "provenance_refs" not in card
    assert card["distinguishing_note"] == (
        "Earlier occurrence is inscriptional only."
    )


def test_prior_identity_claim_bound_preserves_long_run_history() -> None:
    prior = _prior(record_class="confirmed_entity", claim_state="confirmed")
    claim = {
        "field": "gender",
        "status": "supported",
        "value": "masculine",
        "basis": "explicit_textual",
        "effective": True,
        "anchor_block_ids": ["bk_ch01_b010"],
        "story_time_note": None,
        "validity": {"from_block": None, "to_block": None},
        "semantic_status": "unreviewed",
    }
    prior["profile_claims"] = [dict(claim) for _ in range(MAX_PRIOR_PROFILE_CLAIMS)]
    prior["distinguishing_note"] = None
    packets = build_prior_candidate_packets_v1(
        chapter=_chapter(), prior_cards=[prior]
    )
    assert packets[0]["prior_card"]["profile_claims"] == [
        {
            "field": "gender",
            "status": "supported",
            "value": "masculine",
            "effective": True,
            "basis_values": ["explicit_textual"],
            "support_count": MAX_PRIOR_PROFILE_CLAIMS,
        }
    ]

    prior["profile_claims"].append(dict(claim))
    with pytest.raises(B1ScanError, match="profile_claims must be a bounded list"):
        build_prior_candidate_packets_v1(chapter=_chapter(), prior_cards=[prior])


def test_prior_retrieval_ignores_only_outer_punctuation() -> None:
    prior = _prior()
    prior["canonical_surface"] = "“Robin Vale.”"
    prior["stable_surfaces"] = ["“Robin Vale.”"]
    packets = build_prior_candidate_packets_v1(
        chapter=_chapter(), prior_cards=[prior]
    )
    hits = packets[0]["current_surface_hits"]
    assert hits[0] == {
        "surface": "“Robin Vale.”",
        "retrieval_surface": "Robin Vale",
        "match_basis": "outer_punctuation_normalized",
        "current_block_ids": ["bk_ch02_b002", "bk_ch02_b003"],
        "current_hit_block_count": 2,
        "block_ids_truncated": False,
    }
    assert {row["retrieval_surface"] for row in hits} == {
        "Robin Vale",
        "Robin",
        "Vale",
    }


def test_prior_retrieval_mechanically_widens_names_without_description_tokens() -> None:
    chapter = {
        "chapter_id": "bk_ch02",
        "blocks": [
            {
                "block_id": "bk_ch02_b001",
                "order_index": 1,
                "clean_text": "Heathcliff returned to the Grange; her mother answered.",
            }
        ],
    }
    heathcliff = _prior(record_class="confirmed_entity", claim_state="confirmed")
    heathcliff.update(
        canonical_surface="Mr. Heathcliff",
        stable_surfaces=["Mr. Heathcliff"],
    )
    grange = _prior(
        "pcard_grange", record_class="confirmed_entity", claim_state="confirmed"
    )
    grange.update(
        canonical_surface="Thrushcross Grange",
        stable_surfaces=["Thrushcross Grange"],
    )
    canine = _prior(
        "pcard_canine",
        record_class="important_unnamed_referent",
        claim_state="confirmed",
    )
    canine.update(
        canonical_surface="the canine mother",
        stable_surfaces=["the canine mother"],
    )

    packets = build_prior_candidate_packets_v1(
        chapter=chapter,
        prior_cards=[heathcliff, grange, canine],
    )
    by_id = {row["prior_card"]["prior_card_id"]: row for row in packets}

    assert set(by_id) == {"pcard_robin_old", "pcard_grange"}
    assert by_id["pcard_robin_old"]["current_surface_hits"][0][
        "match_basis"
    ] == "leading_wrapper_omitted"
    assert by_id["pcard_grange"]["current_surface_hits"][0] == {
        "surface": "Thrushcross Grange",
        "retrieval_surface": "Grange",
        "match_basis": "name_component",
        "current_block_ids": ["bk_ch02_b001"],
        "current_hit_block_count": 1,
        "block_ids_truncated": False,
    }


def test_prior_retrieval_unions_exact_and_shortened_name_hits() -> None:
    chapter = {
        "chapter_id": "bk_ch02",
        "blocks": [
            {
                "block_id": "bk_ch02_b001",
                "order_index": 1,
                "clean_text": "Mr. Heathcliff opened the door.",
            },
            {
                "block_id": "bk_ch02_b002",
                "order_index": 2,
                "clean_text": "Heathcliff answered from the passage.",
            },
        ],
    }
    prior = _prior(record_class="confirmed_entity", claim_state="confirmed")
    prior.update(
        canonical_surface="Mr. Heathcliff",
        stable_surfaces=["Mr. Heathcliff"],
    )

    packets = build_prior_candidate_packets_v1(chapter=chapter, prior_cards=[prior])

    assert [row["retrieval_surface"] for row in packets[0]["current_surface_hits"]] == [
        "Mr. Heathcliff",
        "Heathcliff",
    ]
    assert {
        block_id
        for row in packets[0]["current_surface_hits"]
        for block_id in row["current_block_ids"]
    } == {"bk_ch02_b001", "bk_ch02_b002"}


def test_name_component_widening_uses_only_edge_components() -> None:
    chapter = {
        "chapter_id": "bk_ch02",
        "blocks": [
            {
                "block_id": "bk_ch02_b001",
                "order_index": 1,
                "clean_text": "The word and appears here.",
            }
        ],
    }
    prior = _prior(record_class="confirmed_entity", claim_state="confirmed")
    prior.update(
        canonical_surface="Seventy Times Seven and First",
        stable_surfaces=["Seventy Times Seven and First"],
        referent_kind="named_text",
    )

    assert build_prior_candidate_packets_v1(
        chapter=chapter, prior_cards=[prior]
    ) == []


def test_shared_name_component_is_explicitly_weak_retrieval() -> None:
    chapter = {
        "chapter_id": "bk_ch02",
        "blocks": [
            {
                "block_id": "bk_ch02_b001",
                "order_index": 1,
                "clean_text": "Earnshaw answered from the doorway.",
            }
        ],
    }
    hareton = _prior(
        "pcard_hareton", record_class="confirmed_entity", claim_state="confirmed"
    )
    hareton.update(
        canonical_surface="Hareton Earnshaw",
        stable_surfaces=["Hareton Earnshaw", "Earnshaw"],
        referent_kind="person",
    )
    catherine = _prior(
        "pcard_catherine",
        record_class="confirmed_entity",
        claim_state="confirmed",
    )
    catherine.update(
        canonical_surface="Catherine Earnshaw",
        stable_surfaces=["Catherine Earnshaw"],
        referent_kind="person",
    )

    packets = build_prior_candidate_packets_v1(
        chapter=chapter, prior_cards=[hareton, catherine]
    )
    by_id = {row["prior_card"]["prior_card_id"]: row for row in packets}

    assert {
        row["match_basis"]
        for row in by_id["pcard_hareton"]["current_surface_hits"]
        if row["retrieval_surface"] == "Earnshaw"
    } == {"shared_name_component", "shared_name_component_exact"}
    assert by_id["pcard_catherine"]["current_surface_hits"][0][
        "match_basis"
    ] == "shared_name_component"


def test_summary_context_is_forwarded_without_becoming_identity_authority() -> None:
    rendered = render_b1_scan_request_v1(
        chapter=_chapter(),
        design_doc=DESIGN_DOC,
        prior_cards=[_prior()],
        previous_chapter_summary="Robin Vale appeared only in an inscription.",
        global_summary="The story has reached North House.",
    )
    assert rendered.sections["summary_context"] == {
        "previous_chapter_summary": "Robin Vale appeared only in an inscription.",
        "global_summary": "The story has reached North House.",
    }


def test_scan_preserves_same_surface_as_two_distinct_observations() -> None:
    packets = build_prior_candidate_packets_v1(
        chapter=_chapter(), prior_cards=[_prior()]
    )
    response = _response(
        continuity=[
            {
                "prior_card_id": "pcard_robin_old",
                "verdict": "uncertain",
                "reason_code": "prior_reference_not_established_entity",
                "source_block_ids": [
                    "bk_ch02_b001",
                    "bk_ch02_b002",
                    "bk_ch02_b003",
                ],
                "reason": "The prior inscription does not establish that the active speaker is the same referent.",
            }
        ]
    )
    artifact = validate_b1_scan_response_v1(
        response,
        chapter=_chapter(),
        prior_candidate_packets=packets,
        request_fingerprint="a" * 64,
    )
    robin_rows = [
        row for row in artifact["entity_observations"] if row["surface"] == "Robin Vale"
    ]
    assert len(robin_rows) == 2
    assert {row["presence_basis"] for row in robin_rows} == {
        "direct_presence",
        "inscription_or_document",
    }
    route = artifact["continuity_routes"][0]
    assert route["packet_action"] == "withhold_prior_card"
    assert route["hearing_required"] is True
    assert route["identity_authority_granted"] is False
    assert artifact["registry_mutation_performed"] is False


def test_missing_exact_cover_is_safely_withheld_without_killing_scan() -> None:
    packets = build_prior_candidate_packets_v1(
        chapter=_chapter(), prior_cards=[_prior()]
    )
    artifact = validate_b1_scan_response_v1(
        _response(),
        chapter=_chapter(),
        prior_candidate_packets=packets,
        request_fingerprint="b" * 64,
    )
    assert artifact["continuity_routes"] == [
        {
            "prior_card_id": "pcard_robin_old",
            "verdict": "uncertain",
            "reason_code": "insufficient_evidence",
            "source_block_ids": [
                "bk_ch02_b001",
                "bk_ch02_b002",
                "bk_ch02_b003",
            ],
            "reason": "The model did not provide one valid exact-cover proposal.",
            "packet_action": "withhold_prior_card",
            "hearing_required": True,
            "mechanical_risk_codes": [
                "prior_record_is_not_confirmed_entity",
                "prior_claim_state_is_not_confirmed",
            ],
            "identity_authority_granted": False,
        }
    ]
    assert artifact["metrics"]["review_issue_count"] == 1


def test_confirmed_compatible_card_can_flow_to_enrich() -> None:
    prior = _prior(record_class="confirmed_entity", claim_state="confirmed")
    packets = build_prior_candidate_packets_v1(chapter=_chapter(), prior_cards=[prior])
    response = _response(
        continuity=[
            {
                "prior_card_id": "pcard_robin_old",
                "verdict": "propose_continue",
                "reason_code": "consistent_current_reference",
                "source_block_ids": ["bk_ch02_b003"],
                "reason": "The current named participant is compatible with the supplied identity.",
            }
        ]
    )
    artifact = validate_b1_scan_response_v1(
        response,
        chapter=_chapter(),
        prior_candidate_packets=packets,
        request_fingerprint="c" * 64,
    )
    route = artifact["continuity_routes"][0]
    assert route["packet_action"] == "include_prior_card"
    assert route["hearing_required"] is False


def test_provisional_card_risk_is_diagnostic_not_a_verdict_override() -> None:
    packets = build_prior_candidate_packets_v1(
        chapter=_chapter(), prior_cards=[_prior()]
    )
    artifact = validate_b1_scan_response_v1(
        _response(
            continuity=[
                {
                    "prior_card_id": "pcard_robin_old",
                    "verdict": "propose_continue",
                    "reason_code": "consistent_current_reference",
                    "source_block_ids": ["bk_ch02_b003"],
                    "reason": "The current evidence supports continuation.",
                }
            ]
        ),
        chapter=_chapter(),
        prior_candidate_packets=packets,
        request_fingerprint="d" * 64,
    )
    route = artifact["continuity_routes"][0]
    assert route["mechanical_risk_codes"] == [
        "prior_record_is_not_confirmed_entity",
        "prior_claim_state_is_not_confirmed",
    ]
    assert route["packet_action"] == "include_prior_card"
    assert route["hearing_required"] is False


def test_same_surface_multiple_cards_is_diagnostic_not_a_verdict_override() -> None:
    second = _prior(
        "pcard_robin_new", record_class="confirmed_entity", claim_state="confirmed"
    )
    first = _prior(record_class="confirmed_entity", claim_state="confirmed")
    packets = build_prior_candidate_packets_v1(
        chapter=_chapter(), prior_cards=[first, second]
    )
    response = _response(
        continuity=[
            {
                "prior_card_id": card_id,
                "verdict": "propose_continue",
                "reason_code": "consistent_current_reference",
                "source_block_ids": ["bk_ch02_b003"],
                "reason": "This is one plausible supplied continuity candidate.",
            }
            for card_id in ("pcard_robin_old", "pcard_robin_new")
        ]
    )
    artifact = validate_b1_scan_response_v1(
        response,
        chapter=_chapter(),
        prior_candidate_packets=packets,
        request_fingerprint="d" * 64,
    )
    assert all(
        row["packet_action"] == "include_prior_card"
        for row in artifact["continuity_routes"]
    )
    assert all(
        row["hearing_required"] is False
        for row in artifact["continuity_routes"]
    )
    assert all(
        "same_surface_matches_multiple_prior_cards" in row["mechanical_risk_codes"]
        for row in artifact["continuity_routes"]
    )


def test_prior_candidate_packet_cap_accepts_256_and_rejects_257() -> None:
    cards = [_prior(f"pcard_robin_{index:02d}") for index in range(MAX_PRIOR_CARDS)]
    packets = build_prior_candidate_packets_v1(chapter=_chapter(), prior_cards=cards)
    assert len(packets) == MAX_PRIOR_CARDS

    with pytest.raises(B1ScanError, match=r"257 > 256"):
        build_prior_candidate_packets_v1(
            chapter=_chapter(),
            prior_cards=[*cards, _prior("pcard_robin_256")],
        )


def test_bad_row_becomes_review_issue_instead_of_aborting_chapter() -> None:
    response = _response()
    response["entity_observations"].append(
        {
            "surface": "Invented Person",
            "source_block_ids": ["bk_ch99_b001"],
            "referent_kind_claim": "person",
            "record_class": "named_entity_candidate",
            "presence_basis": "direct_presence",
            "scan_note": "Row citing a block outside this chapter.",
        }
    )
    artifact = validate_b1_scan_response_v1(
        response,
        chapter=_chapter(),
        prior_candidate_packets=[],
        request_fingerprint="e" * 64,
    )
    assert len(artifact["entity_observations"]) == 3
    assert artifact["metrics"]["review_issue_count"] == 1
    assert "foreign block" in artifact["review_issues"][0]["reason"]


def test_surface_absent_from_cited_block_is_accepted_not_quarantined() -> None:
    # Code cannot read language: a block may carry the referent through a
    # pronoun, an epithet, or unattributed dialogue.  Whether it does is the
    # model's judgment, so only block existence is enforced here.
    response = _response()
    response["entity_observations"].append(
        {
            "surface": "The tenant of the north farm",
            "source_block_ids": ["bk_ch02_b001"],
            "referent_kind_claim": "person",
            "record_class": "important_unnamed_referent",
            "presence_basis": "direct_presence",
            "scan_note": "Referent present without being named in the block.",
        }
    )
    artifact = validate_b1_scan_response_v1(
        response,
        chapter=_chapter(),
        prior_candidate_packets=[],
        request_fingerprint="e" * 64,
    )
    assert len(artifact["entity_observations"]) == 4
    assert artifact["metrics"]["review_issue_count"] == 0


def test_valid_support_overflow_keeps_entity_with_bounded_earliest_evidence() -> None:
    block_count = MAX_OBSERVATION_BLOCK_IDS + 3
    ordered_ids = [f"bk_ch02_b{index:03d}" for index in range(1, block_count + 1)]
    chapter = {
        "chapter_id": "bk_ch02",
        "blocks": [
            {
                "block_id": block_id,
                "order_index": index,
                "clean_text": "Robin Vale answered.",
            }
            for index, block_id in enumerate(ordered_ids, start=1)
        ],
    }
    response = {
        "schema_id": OUTPUT_SCHEMA_ID,
        "chapter_id": "bk_ch02",
        "entity_observations": [
            {
                "surface": "Robin Vale",
                "source_block_ids": list(reversed(ordered_ids)),
                "referent_kind_claim": "person",
                "record_class": "named_entity_candidate",
                "presence_basis": "direct_presence",
                "scan_note": "A directly present named participant.",
            }
        ],
        "glossary_observations": [],
        "prior_continuity_proposals": [],
    }
    artifact = validate_b1_scan_response_v1(
        response,
        chapter=chapter,
        prior_candidate_packets=[],
        request_fingerprint="f" * 64,
    )
    assert (
        artifact["entity_observations"][0]["source_block_ids"]
        == ordered_ids[:MAX_OBSERVATION_BLOCK_IDS]
    )
    assert artifact["entity_observations"][0]["all_source_block_ids"] == ordered_ids
    assert artifact["entity_observations"][0]["source_block_count"] == block_count
    assert artifact["review_issues"][0]["row_type"] == (
        "entity_observation_support_overflow"
    )
    assert artifact["review_issues"][0]["omitted_source_block_count"] == (
        block_count - MAX_OBSERVATION_BLOCK_IDS
    )
