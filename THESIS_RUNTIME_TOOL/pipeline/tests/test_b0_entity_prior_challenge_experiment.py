from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from pipeline.literary.b0_entity_prior_challenge_experiment import (
    AUTHORITY_SCOPE,
    B0PriorChallengeError,
    MAX_GLOSSARY_CARDS,
    MAX_MODEL_REVIEW_CASE_SOURCE_BLOCK_IDS,
    MAX_MODEL_SURFACE_HIT_BLOCK_IDS,
    MAX_PRIOR_CARDS,
    PROMPT_ID,
    PROMPT_SHA256,
    PROMPT_UTF8_BYTES,
    build_candidate_only_packets,
    build_glossary_packets,
    build_hidden_corruption_manifest,
    build_prior_packets,
    evaluate_hidden_corruption,
    prior_challenge_response_schema,
    render_prior_challenge_request,
    validate_prior_cards,
    validate_prior_challenge_response,
)
from pipeline.literary.checkpoint import canonical_hash


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
DESIGN_DOC = RUNTIME_ROOT.parent / "design" / "LITERARY_PROMPT_DESIGN.md"


def _chapter() -> dict:
    return {
        "chapter_id": "bk_ch02",
        "blocks": [
            {
                "block_id": "bk_ch02_b001",
                "block_type": "paragraph",
                "order_index": 1,
                "clean_text": "Mr. Vale entered North House and called for Orr.",
            },
            {
                "block_id": "bk_ch02_b002",
                "block_type": "paragraph",
                "order_index": 2,
                "clean_text": "Orr, an old man, answered Mr. Vale at once.",
            },
            {
                "block_id": "bk_ch02_b003",
                "block_type": "paragraph",
                "order_index": 3,
                "clean_text": "Vale signed as V. Vale beside Brindle, an old female hound.",
            },
        ],
    }


def _prior_card(
    *,
    prior_card_id: str = "prior_vale",
    canonical_surface: str = "Mr. Vale",
    stable_surfaces: list[str] | None = None,
    referent_kind: str = "person",
    referential_gender: str | None = "masculine",
    identity_summary: str = "The named resident associated with North House.",
) -> dict:
    return {
        "prior_card_id": prior_card_id,
        "canonical_surface": canonical_surface,
        "stable_surfaces": stable_surfaces or ["Mr. Vale", "Vale"],
        "referent_kind": referent_kind,
        "referential_gender": referential_gender,
        "identity_summary": identity_summary,
        "authority_scope": AUTHORITY_SCOPE,
        "first_supported_block_id": "bk_ch01_b001",
        "provenance_refs": [{"chapter_id": "bk_ch01", "block_id": "bk_ch01_b001"}],
    }


def _orr_card() -> dict:
    return _prior_card(
        prior_card_id="prior_orr",
        canonical_surface="Orr",
        stable_surfaces=["Orr"],
        identity_summary="The named older household servant.",
    )


def _candidate_only_card(surface: str = "Brindle") -> dict:
    body = {
        "prior_card_id": "candidate_brindle",
        "canonical_surface": surface,
        "stable_surfaces": [surface],
        "effective_claims": {
            "referent_kind": "animal",
            "referential_gender": "feminine",
            "identity_summary": "An unresolved named hound from an earlier chapter.",
        },
        "disputed_claims": [
            {
                "disputed_field": "identity_membership",
                "status": "pending",
            }
        ],
        "authority_scope": "candidate_only",
        "first_supported_block_id": "bk_ch01_b009",
        "provenance_refs": [
            {"chapter_id": "bk_ch01", "block_id": "bk_ch01_b009"}
        ],
        "source_candidate_id": "local_brindle",
    }
    return {**body, "context_card_hash": canonical_hash(body)}


def _glossary_card(
    *,
    surface: str = "North House",
    lifecycle_state: str = "chapter_confirmed",
) -> dict:
    authority_scope = {
        "chapter_confirmed": "chapter_confirmed_prefix",
        "pending_evidence": "candidate_only",
        "rejected_dormant": "dormant",
    }[lifecycle_state]
    body = {
        "glossary_card_id": f"glossary_{surface.casefold().replace(' ', '_')}",
        "surface": surface,
        "stable_surfaces": [surface],
        "category_claim": "place_term",
        "local_sense": "A locally significant named residence.",
        "preferred_rendering_vi": None,
        "render_policy": (
            "advisory_meaning"
            if lifecycle_state == "chapter_confirmed"
            else "none"
        ),
        "lifecycle_state": lifecycle_state,
        "authority_scope": authority_scope,
        "first_supported_block_id": "bk_ch01_b001",
        "provenance_refs": [
            {"chapter_id": "bk_ch01", "block_id": "bk_ch01_b001"}
        ],
        "source_candidate_id": "source_glossary",
        "cross_chapter_dispositions": [],
        "hearing_count": 1,
        "same_evidence_reopen_forbidden": True,
    }
    return {**body, "glossary_card_hash": canonical_hash(body)}


def _empty_response() -> dict:
    return {
        "new_entity_candidates": [],
        "new_glossary_candidates": [],
        "unresolved_referents": [],
        "prior_enrichment_requests": [],
        "prior_card_dispositions": [],
        "candidate_only_observations": [],
        "review_case_observations": [],
        "prior_glossary_dispositions": [],
        "chapter_priority_order": [],
    }


def _compatible(prior_card_id: str, block_id: str) -> dict:
    return {
        "prior_card_id": prior_card_id,
        "verdict": "compatible",
        "referent_continuity": "same_referent",
        "issue_code": None,
        "disputed_field": None,
        "source_block_ids": [block_id],
        "reason": None,
    }


def _challenge(
    prior_card_id: str,
    *,
    issue_code: str,
    disputed_field: str,
    block_id: str,
) -> dict:
    return {
        "prior_card_id": prior_card_id,
        "verdict": "challenge",
        "referent_continuity": (
            "possible_collision"
            if issue_code in {
                "identity_collision",
                "alias_target_conflict",
                "alias_scope_conflict",
            }
            else "same_referent"
        ),
        "issue_code": issue_code,
        "disputed_field": disputed_field,
        "source_block_ids": [block_id],
        "reason": "The current source supplies a stable contradiction.",
    }


def test_prompt_is_pinned_book_neutral_and_delta_oriented() -> None:
    request = render_prior_challenge_request(
        chapter=_chapter(), prior_cards=[], design_doc=DESIGN_DOC
    )
    prompt = request.messages[0]["content"]
    assert request.prompt_id == PROMPT_ID
    assert request.prompt_sha256 == PROMPT_SHA256
    assert len(prompt.encode("utf-8")) == PROMPT_UTF8_BYTES
    lowered = prompt.casefold()
    for forbidden in ("heathcliff", "lockwood", "wuthering", "gatsby", "madam"):
        assert forbidden not in lowered
    assert "emit nothing" in lowered
    assert "prior_enrichment_requests" in prompt
    assert "prior_card_dispositions" in prompt


def test_empty_prior_control_is_valid_and_contains_no_hidden_gold() -> None:
    request = render_prior_challenge_request(
        chapter=_chapter(), prior_cards=[], design_doc=DESIGN_DOC
    )
    assert request.sections["supplied_prior_packets"] == []
    rendered = "\n".join(row["content"] for row in request.messages)
    for forbidden in (
        "mutation_id",
        "changed_card_fields",
        "expected_issue_code",
        "expected_disputed_field",
        "gold_answer",
    ):
        assert forbidden not in rendered


def test_model_view_compacts_repeated_hit_addresses_without_losing_code_presence() -> None:
    chapter = deepcopy(_chapter())
    chapter["blocks"] = [
        {
            "block_id": f"bk_ch02_b{index:03d}",
            "block_type": "paragraph",
            "order_index": index,
            "clean_text": "Vale remained in the room.",
        }
        for index in range(1, 13)
    ]
    full_packets, _ = build_prior_packets(
        chapter=chapter, prior_cards=[_prior_card()]
    )
    full_hit = next(
        row
        for row in full_packets[0]["current_surface_hits"]
        if row["surface"] == "Vale"
    )
    assert len(full_hit["current_block_ids"]) == 12

    request = render_prior_challenge_request(
        chapter=chapter, prior_cards=[_prior_card()], design_doc=DESIGN_DOC
    )
    model_hit = next(
        row
        for row in request.sections["supplied_prior_packets"][0][
            "current_surface_hits"
        ]
        if row["surface"] == "Vale"
    )
    assert len(model_hit["current_block_ids"]) == MAX_MODEL_SURFACE_HIT_BLOCK_IDS
    assert model_hit["current_block_ids"][:2] == ["bk_ch02_b001", "bk_ch02_b002"]
    assert model_hit["current_block_ids"][-2:] == ["bk_ch02_b011", "bk_ch02_b012"]
    assert model_hit["current_hit_count"] == 12
    assert model_hit["current_block_ids_truncated"] is True
    assert all(
        set(row) == {"block_id", "text"}
        for row in request.sections["source_blocks"]
    )
    rendered = "\n".join(row["content"] for row in request.messages)
    assert "prior_manifest_hash" not in rendered
    assert "candidate_only_manifest_hash" not in rendered
    assert "glossary_manifest_hash" not in rendered
    assert "review_case_manifest_hash" not in rendered
    assert request.sections["prior_manifest_hash"]


def test_prior_packets_keep_card_and_all_current_hits_adjacent() -> None:
    packets, manifest_hash = build_prior_packets(
        chapter=_chapter(), prior_cards=[_prior_card()]
    )
    assert manifest_hash
    assert len(packets) == 1
    packet = packets[0]
    assert packet["prior_card"]["prior_card_id"] == "prior_vale"
    assert packet["current_surface_hits"] == [
        {
            "surface": "Mr. Vale",
            "current_block_ids": ["bk_ch02_b001", "bk_ch02_b002"],
        },
        {
            "surface": "Vale",
            "current_block_ids": [
                "bk_ch02_b001",
                "bk_ch02_b002",
                "bk_ch02_b003",
            ],
        },
    ]


def test_glossary_packets_are_surface_filtered_and_keep_card_beside_hits() -> None:
    packets, manifest_hash = build_glossary_packets(
        chapter=_chapter(),
        glossary_cards=[
            _glossary_card(),
            _glossary_card(surface="Old Tower", lifecycle_state="pending_evidence"),
        ],
    )
    assert manifest_hash
    assert len(packets) == 1
    assert packets[0]["glossary_card"]["surface"] == "North House"
    assert packets[0]["current_surface_hits"] == [
        {"surface": "North House", "current_block_ids": ["bk_ch02_b001"]}
    ]

    history = [_glossary_card()] + [
        _glossary_card(surface=f"Absent Term {index}")
        for index in range(MAX_GLOSSARY_CARDS + 1)
    ]
    filtered, _ = build_glossary_packets(
        chapter=_chapter(),
        glossary_cards=history,
    )
    assert [row["glossary_card"]["surface"] for row in filtered] == ["North House"]


def test_glossary_disposition_exact_cover_and_priority_have_no_authority() -> None:
    card = _glossary_card()
    response = _empty_response()
    response["prior_glossary_dispositions"] = [
        {
            "glossary_card_id": card["glossary_card_id"],
            "verdict": "compatible",
            "source_block_ids": ["bk_ch02_b001"],
            "reason": None,
        }
    ]
    response["chapter_priority_order"] = [
        {
            "surface": "North House",
            "item_class": "prior_glossary",
            "source_block_id": "bk_ch02_b001",
        }
    ]
    artifact = validate_prior_challenge_response(
        response,
        chapter=_chapter(),
        prior_cards=[],
        glossary_cards=[card],
        request_fingerprint="req_glossary_prior",
    )
    assert artifact["validation_report"]["prior_glossary_compatible_count"] == 1
    assert artifact["code_derived_glossary_presence"][0]["glossary_card_id"] == card[
        "glossary_card_id"
    ]
    assert artifact["chapter_priority_order"][0]["authority_effect"] == "none"
    assert artifact["chapter_priority_order"][0]["resolved_refs"] == [
        card["glossary_card_id"]
    ]

    with pytest.raises(B0PriorChallengeError, match="exact-cover"):
        validate_prior_challenge_response(
            _empty_response(),
            chapter=_chapter(),
            prior_cards=[],
            glossary_cards=[card],
            request_fingerprint="req_glossary_missing",
        )


def test_pending_glossary_card_cannot_smuggle_rendering_guidance() -> None:
    card = _glossary_card(lifecycle_state="pending_evidence")
    body = dict(card)
    body.pop("glossary_card_hash")
    body["preferred_rendering_vi"] = "unsafe"
    body["render_policy"] = "advisory_meaning"
    unsafe = {**body, "glossary_card_hash": canonical_hash(body)}
    with pytest.raises(B0PriorChallengeError, match="cannot carry rendering guidance"):
        build_glossary_packets(chapter=_chapter(), glossary_cards=[unsafe])


def test_candidate_only_packet_is_surface_gated_and_non_authoritative() -> None:
    packets, manifest_hash = build_candidate_only_packets(
        chapter=_chapter(), candidate_only_cards=[_candidate_only_card()]
    )
    assert manifest_hash
    assert "authority_scope" not in packets[0]["candidate_only_card"]
    assert packets[0]["current_surface_hits"] == [
        {"surface": "Brindle", "current_block_ids": ["bk_ch02_b003"]}
    ]
    folded, _ = build_candidate_only_packets(
        chapter=_chapter(), candidate_only_cards=[_candidate_only_card("brindle")]
    )
    assert folded[0]["current_surface_hits"] == [
        {"surface": "brindle", "current_block_ids": ["bk_ch02_b003"]}
    ]
    absent, _ = build_candidate_only_packets(
        chapter=_chapter(),
        candidate_only_cards=[_candidate_only_card("Absent Hound")],
    )
    assert absent == []


def test_candidate_only_observation_is_optional_and_never_promotes() -> None:
    candidate = _candidate_only_card()
    request = render_prior_challenge_request(
        chapter=_chapter(),
        prior_cards=[],
        candidate_only_cards=[candidate],
        design_doc=DESIGN_DOC,
    )
    response = _empty_response()
    artifact = validate_prior_challenge_response(
        response,
        chapter=_chapter(),
        prior_cards=[],
        candidate_only_cards=[candidate],
        request_fingerprint=request.request_fingerprint,
    )
    assert artifact["candidate_only_observations"] == []


    response["candidate_only_observations"] = [
        {
            "prior_card_id": "candidate_brindle",
            "observation": "supports_continuity",
            "disputed_field": None,
            "source_block_ids": ["bk_ch02_b003"],
            "reason": "The current chapter uses the retrieved stable name for a hound.",
        }
    ]
    observed = validate_prior_challenge_response(
        response,
        chapter=_chapter(),
        prior_cards=[],
        candidate_only_cards=[candidate],
        request_fingerprint=request.request_fingerprint,
    )
    assert observed["candidate_only_observations"][0]["observation"] == (
        "supports_continuity"
    )
    assert "authority_scope" not in observed["candidate_only_observations"][0]

    response["candidate_only_observations"][0]["disputed_field"] = "identity_summary"
    normalized = validate_prior_challenge_response(
        response,
        chapter=_chapter(),
        prior_cards=[],
        candidate_only_cards=[candidate],
        request_fingerprint=request.request_fingerprint,
    )
    assert normalized["candidate_only_observations"][0]["disputed_field"] is None
    assert (
        normalized["validation_report"][
            "normalized_inapplicable_candidate_disputed_field_count"
        ]
        == 1
    )

    response["candidate_only_observations"][0] = {
        **response["candidate_only_observations"][0],
        "observation": "new_claim_evidence",
        "disputed_field": "identity_summary",
    }
    identity_only_downgrade = validate_prior_challenge_response(
        response,
        chapter=_chapter(),
        prior_cards=[],
        candidate_only_cards=[candidate],
        request_fingerprint=request.request_fingerprint,
    )
    assert identity_only_downgrade["candidate_only_observations"][0][
        "observation"
    ] == "supports_continuity"
    assert identity_only_downgrade["candidate_only_observations"][0][
        "disputed_field"
    ] is None

    no_dispute_body = {
        key: value
        for key, value in candidate.items()
        if key != "context_card_hash"
    }
    no_dispute_body["disputed_claims"] = []
    no_dispute = {
        **no_dispute_body,
        "context_card_hash": canonical_hash(no_dispute_body),
    }
    response["candidate_only_observations"][0] = {
        **response["candidate_only_observations"][0],
        "observation": "new_claim_evidence",
        "disputed_field": "identity_summary",
    }
    downgraded = validate_prior_challenge_response(
        response,
        chapter=_chapter(),
        prior_cards=[],
        candidate_only_cards=[no_dispute],
        request_fingerprint=request.request_fingerprint,
    )
    assert downgraded["candidate_only_observations"][0]["observation"] == (
        "supports_continuity"
    )
    assert downgraded["candidate_only_observations"][0]["disputed_field"] is None
    assert (
        downgraded["validation_report"][
            "downgraded_unowned_candidate_claim_evidence_count"
        ]
        == 1
    )


def test_review_case_packet_is_bounded_and_observation_grants_no_authority() -> None:
    packet_body = {
        "review_case_id": "litcase1_scope",
        "case_type": "alias_scope",
        "surface": "Vale",
        "status": "collecting_evidence",
        "authority_effect": "retrieval_only",
        "disputed_field": "alias_scope",
        "evidence_needed": ["scope_disambiguation"],
        "hearing_count": 0,
        "automatic_hearing_limit": 2,
        "current_surface_hit_block_ids": ["bk_ch02_b001", "bk_ch02_b002"],
        "subject_cards": [],
    }
    packet = {**packet_body, "packet_hash": canonical_hash(packet_body)}
    manifest_body = {
        "schema_version": "literary_relevant_review_case_packet_v1",
        "chapter_id": "bk_ch02",
        "review_case_ledger_hash": "a" * 64,
        "packets": [packet],
        "overflow_count": 0,
    }
    manifest = {
        **manifest_body,
        "review_case_manifest_hash": canonical_hash(manifest_body),
    }
    request = render_prior_challenge_request(
        chapter=_chapter(),
        prior_cards=[],
        relevant_review_cases=manifest,
        design_doc=DESIGN_DOC,
    )
    model_manifest = request.sections["relevant_review_cases"]
    assert "review_case_manifest_hash" not in model_manifest
    assert "packet_hash" not in model_manifest["packets"][0]
    assert model_manifest["packets"][0]["current_surface_hit_block_ids"] == [
        "bk_ch02_b001",
        "bk_ch02_b002",
    ]
    assert "current_surface_hit_count" not in model_manifest["packets"][0]
    assert (
        "current_surface_hit_block_ids_truncated"
        not in model_manifest["packets"][0]
    )
    assert request.sections["review_case_manifest_hash"] == manifest[
        "review_case_manifest_hash"
    ]
    response = _empty_response()
    response["review_case_observations"] = [
        {
            "review_case_id": "litcase1_scope",
            "observation": "ambiguous",
            "source_block_ids": ["bk_ch02_b001"],
            "reason": "The recurring form still has no uniquely attributable owner.",
        }
    ]
    artifact = validate_prior_challenge_response(
        response,
        chapter=_chapter(),
        prior_cards=[],
        relevant_review_cases=manifest,
        request_fingerprint=request.request_fingerprint,
    )
    assert artifact["review_case_observations"][0]["observation"] == "ambiguous"
    assert "authority_effect" not in artifact["review_case_observations"][0]


def test_review_case_oversupplied_valid_blocks_are_bounded_mechanically() -> None:
    chapter = deepcopy(_chapter())
    chapter["blocks"].extend(
        [
            {
                "block_id": "bk_ch02_b004",
                "block_type": "paragraph",
                "order_index": 4,
                "clean_text": "Vale appeared again in the northern room.",
            },
            {
                "block_id": "bk_ch02_b005",
                "block_type": "paragraph",
                "order_index": 5,
                "clean_text": "The room fell silent.",
            },
        ]
    )
    packet_body = {
        "review_case_id": "litcase1_scope",
        "case_type": "alias_scope",
        "surface": "Vale",
        "status": "collecting_evidence",
        "authority_effect": "retrieval_only",
        "disputed_field": "alias_scope",
        "evidence_needed": ["scope_disambiguation"],
        "hearing_count": 0,
        "automatic_hearing_limit": 2,
        "current_surface_hit_block_ids": [
            "bk_ch02_b001",
            "bk_ch02_b002",
            "bk_ch02_b003",
            "bk_ch02_b004",
        ],
        "subject_cards": [],
    }
    packet = {**packet_body, "packet_hash": canonical_hash(packet_body)}
    manifest_body = {
        "schema_version": "literary_relevant_review_case_packet_v1",
        "chapter_id": "bk_ch02",
        "review_case_ledger_hash": "a" * 64,
        "packets": [packet],
        "overflow_count": 0,
    }
    manifest = {
        **manifest_body,
        "review_case_manifest_hash": canonical_hash(manifest_body),
    }
    response = _empty_response()
    response["review_case_observations"] = [
        {
            "review_case_id": "litcase1_scope",
            "observation": "supports",
            "source_block_ids": [
                "bk_ch02_b004",
                "bk_ch02_b002",
                "bk_ch02_b001",
                "bk_ch02_b003",
            ],
            "reason": "Several current blocks add bounded evidence for the open case.",
        }
    ]
    artifact = validate_prior_challenge_response(
        response,
        chapter=chapter,
        prior_cards=[],
        relevant_review_cases=manifest,
        request_fingerprint="req_review_oversupply",
    )
    assert artifact["review_case_observations"][0]["source_block_ids"] == [
        "bk_ch02_b001",
        "bk_ch02_b003",
        "bk_ch02_b004",
    ]
    assert artifact["validation_report"][
        "normalized_review_case_observation_count"
    ] == 1
    assert artifact["validation_report"][
        "omitted_review_case_source_block_count"
    ] == 1
    assert prior_challenge_response_schema()["properties"][
        "review_case_observations"
    ]["items"]["properties"]["source_block_ids"]["maxItems"] == (
        MAX_MODEL_REVIEW_CASE_SOURCE_BLOCK_IDS
    )


def test_review_case_support_normalization_keeps_foreign_blocks_fatal() -> None:
    packet_body = {
        "review_case_id": "litcase1_scope",
        "case_type": "alias_scope",
        "surface": "Vale",
        "status": "collecting_evidence",
        "authority_effect": "retrieval_only",
        "disputed_field": "alias_scope",
        "evidence_needed": ["scope_disambiguation"],
        "hearing_count": 0,
        "automatic_hearing_limit": 2,
        "current_surface_hit_block_ids": ["bk_ch02_b001"],
        "subject_cards": [],
    }
    packet = {**packet_body, "packet_hash": canonical_hash(packet_body)}
    manifest_body = {
        "schema_version": "literary_relevant_review_case_packet_v1",
        "chapter_id": "bk_ch02",
        "review_case_ledger_hash": "a" * 64,
        "packets": [packet],
        "overflow_count": 0,
    }
    manifest = {
        **manifest_body,
        "review_case_manifest_hash": canonical_hash(manifest_body),
    }
    response = _empty_response()
    response["review_case_observations"] = [
        {
            "review_case_id": "litcase1_scope",
            "observation": "supports",
            "source_block_ids": ["bk_ch02_b001", "foreign_b001"],
            "reason": "The current source adds evidence for the open case.",
        }
    ]
    with pytest.raises(B0PriorChallengeError, match="foreign blocks"):
        validate_prior_challenge_response(
            response,
            chapter=_chapter(),
            prior_cards=[],
            relevant_review_cases=manifest,
            request_fingerprint="req_review_foreign",
        )


def test_candidate_claim_evidence_names_a_supplied_pending_field_without_promoting() -> None:
    candidate = _candidate_only_card()
    body = {key: value for key, value in candidate.items() if key != "context_card_hash"}
    body["disputed_claims"] = [
        {"disputed_field": "referential_gender", "status": "pending"}
    ]
    candidate = {**body, "context_card_hash": canonical_hash(body)}
    request = render_prior_challenge_request(
        chapter=_chapter(),
        prior_cards=[],
        candidate_only_cards=[candidate],
        design_doc=DESIGN_DOC,
    )
    response = _empty_response()
    response["candidate_only_observations"] = [
        {
            "prior_card_id": "candidate_brindle",
            "observation": "new_claim_evidence",
            "disputed_field": "referential_gender",
            "source_block_ids": ["bk_ch02_b003"],
            "reason": "The current source adds evidence relevant to the pending field.",
        }
    ]
    artifact = validate_prior_challenge_response(
        response,
        chapter=_chapter(),
        prior_cards=[],
        candidate_only_cards=[candidate],
        request_fingerprint=request.request_fingerprint,
    )
    observation = artifact["candidate_only_observations"][0]
    assert observation["disputed_field"] == "referential_gender"
    assert "revised_value" not in observation

    response["candidate_only_observations"][0]["disputed_field"] = (
        "identity_summary"
    )
    with pytest.raises(
        B0PriorChallengeError,
        match="targets no supplied disputed field",
    ):
        validate_prior_challenge_response(
            response,
            chapter=_chapter(),
            prior_cards=[],
            candidate_only_cards=[candidate],
            request_fingerprint=request.request_fingerprint,
        )
    response["candidate_only_observations"][0]["disputed_field"] = (
        "referential_gender"
    )

    response["candidate_only_observations"][0]["source_block_ids"] = ["bk_ch02_b001"]
    closed = validate_prior_challenge_response(
        response,
        chapter=_chapter(),
        prior_cards=[],
        candidate_only_cards=[candidate],
        request_fingerprint=request.request_fingerprint,
    )
    assert closed["candidate_only_observations"][0]["source_block_ids"] == [
        "bk_ch02_b001",
        "bk_ch02_b003",
    ]
    assert (
        closed["validation_report"][
            "normalized_candidate_observation_missing_hit_count"
        ]
        == 1
    )
    assert (
        closed["validation_report"][
            "added_candidate_observation_hit_block_count"
        ]
        == 1
    )

    response["candidate_only_observations"][0]["source_block_ids"] = ["bk_ch02_b003"]
    response["candidate_only_observations"][0]["disputed_field"] = "referent_kind"
    with pytest.raises(B0PriorChallengeError, match="no supplied disputed field"):
        validate_prior_challenge_response(
            response,
            chapter=_chapter(),
            prior_cards=[],
            candidate_only_cards=[candidate],
            request_fingerprint=request.request_fingerprint,
        )


def test_prior_card_cap_duplicate_ids_and_foreign_shape_are_fatal() -> None:
    oversized = [
        _prior_card(
            prior_card_id=f"prior_{index}",
            canonical_surface="Vale",
            stable_surfaces=["Vale"],
        )
        for index in range(MAX_PRIOR_CARDS + 1)
    ]
    with pytest.raises(B0PriorChallengeError, match="bounded cap"):
        validate_prior_cards(oversized)
    assert len(validate_prior_cards(oversized, maximum=None)) == MAX_PRIOR_CARDS + 1
    with pytest.raises(B0PriorChallengeError, match="duplicate ids"):
        validate_prior_cards([_prior_card(), _prior_card()])
    malformed = _prior_card()
    malformed["foreign"] = True
    with pytest.raises(B0PriorChallengeError, match="field set differs"):
        validate_prior_cards([malformed])


def test_every_prior_card_needs_a_current_exact_surface_hit() -> None:
    card = _prior_card(
        canonical_surface="Absent Name", stable_surfaces=["Absent Name"]
    )
    with pytest.raises(B0PriorChallengeError, match="no exact current surface hit"):
        build_prior_packets(chapter=_chapter(), prior_cards=[card])


def test_surface_packet_does_not_match_inside_a_longer_word() -> None:
    chapter = deepcopy(_chapter())
    chapter["blocks"] = [
        {
            "block_id": "bk_ch02_b001",
            "block_type": "paragraph",
            "order_index": 1,
            "clean_text": "Valentine entered alone.",
        }
    ]
    card = _prior_card(canonical_surface="Vale", stable_surfaces=["Vale"])
    with pytest.raises(B0PriorChallengeError, match="no exact current surface hit"):
        build_prior_packets(chapter=chapter, prior_cards=[card])


def test_first_supported_block_must_belong_to_prior_provenance() -> None:
    card = _prior_card()
    card["first_supported_block_id"] = "foreign_b001"
    with pytest.raises(B0PriorChallengeError, match="absent from provenance"):
        validate_prior_cards([card])


def test_response_schema_is_closed_and_has_only_delta_channels() -> None:
    schema = prior_challenge_response_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {
        "new_entity_candidates",
        "new_glossary_candidates",
        "unresolved_referents",
        "prior_enrichment_requests",
        "prior_card_dispositions",
        "candidate_only_observations",
        "review_case_observations",
        "prior_glossary_dispositions",
        "chapter_priority_order",
    }
    assert "confirmed_prior_card_ids" not in schema["properties"]
    disposition = schema["properties"]["prior_card_dispositions"]["items"]
    assert "replacement_value" not in disposition["properties"]


def test_compatible_prior_is_exact_covered_and_code_records_presence() -> None:
    request = render_prior_challenge_request(
        chapter=_chapter(), prior_cards=[_prior_card()], design_doc=DESIGN_DOC
    )
    response = _empty_response()
    response["prior_card_dispositions"] = [
        _compatible("prior_vale", "bk_ch02_b001")
    ]
    artifact = validate_prior_challenge_response(
        response,
        chapter=_chapter(),
        prior_cards=[_prior_card()],
        request_fingerprint=request.request_fingerprint,
    )
    assert artifact["delta_inventory"]["entity_candidates"] == []
    assert artifact["validation_report"] == {
        "prior_packet_count": 1,
        "compatible_count": 1,
        "challenge_count": 0,
        "uncertain_count": 0,
        "enrichment_count": 0,
            "candidate_only_packet_count": 0,
            "candidate_only_observation_count": 0,
            "supplied_review_case_count": 0,
            "review_case_observation_count": 0,
            "normalized_review_case_observation_count": 0,
            "omitted_review_case_source_block_count": 0,
            "normalized_inapplicable_candidate_disputed_field_count": 0,
            "normalized_candidate_observation_missing_hit_count": 0,
            "added_candidate_observation_hit_block_count": 0,
            "omitted_candidate_observation_context_block_count": 0,
            "downgraded_unowned_candidate_claim_evidence_count": 0,
            "prior_glossary_packet_count": 0,
            "prior_glossary_compatible_count": 0,
            "prior_glossary_challenge_count": 0,
            "prior_glossary_uncertain_count": 0,
            "normalized_compatible_entity_disposition_count": 0,
            "omitted_compatible_entity_source_block_count": 0,
            "normalized_missing_entity_disposition_reason_count": 0,
            "normalized_compatible_glossary_disposition_count": 0,
            "omitted_compatible_glossary_source_block_count": 0,
            "normalized_missing_glossary_disposition_reason_count": 0,
            "accepted_priority_count": 0,
            "priority_issue_count": 0,
            "priority_issues": [],
        }
    assert artifact["code_derived_prior_presence"][0]["current_surface_hits"]


def test_compatible_prior_oversupplied_hits_are_normalized_mechanically() -> None:
    chapter = deepcopy(_chapter())
    chapter["blocks"][1]["clean_text"] += " Vale remained in view."
    response = _empty_response()
    response["prior_card_dispositions"] = [
        {
            **_compatible("prior_vale", "bk_ch02_b001"),
            "source_block_ids": [
                "bk_ch02_b003",
                "bk_ch02_b001",
                "bk_ch02_b002",
            ],
        }
    ]

    artifact = validate_prior_challenge_response(
        response,
        chapter=chapter,
        prior_cards=[_prior_card()],
        request_fingerprint="req_compatible_oversupply",
    )

    assert artifact["prior_card_dispositions"][0]["source_block_ids"] == [
        "bk_ch02_b001",
        "bk_ch02_b002",
    ]
    assert (
        artifact["validation_report"][
            "normalized_compatible_entity_disposition_count"
        ]
        == 1
    )
    assert (
        artifact["validation_report"][
            "omitted_compatible_entity_source_block_count"
        ]
        == 1
    )


def test_compatible_prior_nonhit_coordinate_uses_nearest_packet_hit() -> None:
    chapter = deepcopy(_chapter())
    chapter["blocks"].append(
        {
            "block_id": "bk_ch02_b004",
            "block_type": "paragraph",
            "order_index": 4,
            "clean_text": "The corridor fell silent after the exchange.",
        }
    )
    response = _empty_response()
    response["prior_card_dispositions"] = [
        _compatible("prior_vale", "bk_ch02_b004")
    ]

    artifact = validate_prior_challenge_response(
        response,
        chapter=chapter,
        prior_cards=[_prior_card()],
        request_fingerprint="req_compatible_nearest_hit",
    )

    assert artifact["prior_card_dispositions"][0]["source_block_ids"] == [
        "bk_ch02_b003"
    ]
    assert (
        artifact["validation_report"][
            "normalized_compatible_entity_disposition_count"
        ]
        == 1
    )
    assert (
        artifact["validation_report"][
            "omitted_compatible_entity_source_block_count"
        ]
        == 1
    )


def test_missing_noncompatible_reasons_remain_visible_without_halting() -> None:
    response = _empty_response()
    response["prior_card_dispositions"] = [
        {
            **_challenge(
                "prior_vale",
                issue_code="identity_collision",
                disputed_field="identity_summary",
                block_id="bk_ch02_b001",
            ),
            "reason": None,
        }
    ]
    response["prior_glossary_dispositions"] = [
        {
            "glossary_card_id": "glossary_north_house",
            "verdict": "uncertain",
            "source_block_ids": ["bk_ch02_b001"],
            "reason": None,
        }
    ]

    artifact = validate_prior_challenge_response(
        response,
        chapter=_chapter(),
        prior_cards=[_prior_card()],
        glossary_cards=[_glossary_card()],
        request_fingerprint="req_missing_noncompatible_reasons",
    )

    assert artifact["prior_card_dispositions"][0]["reason"] == (
        "model_omitted_challenge_reason"
    )
    assert artifact["prior_glossary_dispositions"][0]["reason"] == (
        "model_omitted_glossary_disposition_reason"
    )
    assert (
        artifact["validation_report"][
            "normalized_missing_entity_disposition_reason_count"
        ]
        == 1
    )
    assert (
        artifact["validation_report"][
            "normalized_missing_glossary_disposition_reason_count"
        ]
        == 1
    )


def test_noncompatible_prior_oversupply_still_fails_closed() -> None:
    chapter = deepcopy(_chapter())
    chapter["blocks"].append(
        {
            "block_id": "bk_ch02_b004",
            "block_type": "paragraph",
            "order_index": 4,
            "clean_text": "Vale remained nearby.",
        }
    )
    response = _empty_response()
    response["prior_card_dispositions"] = [
        {
            **_challenge(
                "prior_vale",
                issue_code="unsupported_stable_claim",
                disputed_field="identity_summary",
                block_id="bk_ch02_b001",
            ),
            "source_block_ids": [
                "bk_ch02_b001",
                "bk_ch02_b002",
                "bk_ch02_b003",
                "bk_ch02_b004",
            ],
        }
    ]

    with pytest.raises(B0PriorChallengeError, match="cardinality"):
        validate_prior_challenge_response(
            response,
            chapter=chapter,
            prior_cards=[_prior_card()],
            request_fingerprint="req_challenge_oversupply",
        )


def test_compatible_glossary_oversupplied_hits_are_normalized() -> None:
    chapter = deepcopy(_chapter())
    chapter["blocks"][1]["clean_text"] += " North House remained visible."
    chapter["blocks"][2]["clean_text"] += " North House stood nearby."
    card = _glossary_card()
    response = _empty_response()
    response["prior_glossary_dispositions"] = [
        {
            "glossary_card_id": card["glossary_card_id"],
            "verdict": "compatible",
            "source_block_ids": [
                "bk_ch02_b003",
                "bk_ch02_b001",
                "bk_ch02_b002",
            ],
            "reason": None,
        }
    ]

    artifact = validate_prior_challenge_response(
        response,
        chapter=chapter,
        prior_cards=[],
        glossary_cards=[card],
        request_fingerprint="req_glossary_oversupply",
    )

    assert artifact["prior_glossary_dispositions"][0]["source_block_ids"] == [
        "bk_ch02_b001",
        "bk_ch02_b002",
    ]
    assert (
        artifact["validation_report"][
            "normalized_compatible_glossary_disposition_count"
        ]
        == 1
    )


def test_missing_prior_disposition_is_fatal() -> None:
    with pytest.raises(B0PriorChallengeError, match="exact-cover"):
        validate_prior_challenge_response(
            _empty_response(),
            chapter=_chapter(),
            prior_cards=[_prior_card()],
            request_fingerprint="req_missing_disposition",
        )


def test_uncertain_prior_is_not_projected_as_conflict() -> None:
    response = _empty_response()
    response["prior_card_dispositions"] = [
        {
            "prior_card_id": "prior_vale",
            "verdict": "uncertain",
            "referent_continuity": "uncertain",
            "issue_code": None,
            "disputed_field": None,
            "source_block_ids": ["bk_ch02_b001"],
            "reason": "The shared surface is not enough to resolve identity safely.",
        }
    ]
    artifact = validate_prior_challenge_response(
        response,
        chapter=_chapter(),
        prior_cards=[_prior_card()],
        request_fingerprint="req_uncertain",
    )
    assert artifact["prior_conflict_tickets"] == []
    assert artifact["validation_report"]["uncertain_count"] == 1


def test_referent_continuity_is_independent_from_disputed_field() -> None:
    response = _empty_response()
    response["prior_card_dispositions"] = [
        {
            "prior_card_id": "prior_vale",
            "verdict": "challenge",
            "referent_continuity": "possible_collision",
            "issue_code": "identity_collision",
            "disputed_field": "referent_kind",
            "source_block_ids": ["bk_ch02_b001"],
            "reason": "The same surface may identify another referent of a different kind.",
        }
    ]

    artifact = validate_prior_challenge_response(
        response,
        chapter=_chapter(),
        prior_cards=[_prior_card()],
        request_fingerprint="req_collision_kind",
    )

    assert artifact["prior_conflict_tickets"] == [
        {
            "prior_card_id": "prior_vale",
            "issue_code": "identity_collision",
            "disputed_field": "referent_kind",
            "referent_continuity": "possible_collision",
            "source_block_ids": ["bk_ch02_b001"],
            "reason": "The same surface may identify another referent of a different kind.",
        }
    ]


@pytest.mark.parametrize(
    ("verdict", "continuity"),
    [("compatible", "uncertain"), ("uncertain", "same_referent")],
)
def test_illegal_verdict_continuity_pairs_fail_closed(
    verdict: str, continuity: str
) -> None:
    response = _empty_response()
    response["prior_card_dispositions"] = [
        {
            "prior_card_id": "prior_vale",
            "verdict": verdict,
            "referent_continuity": continuity,
            "issue_code": None,
            "disputed_field": None,
            "source_block_ids": ["bk_ch02_b001"],
            "reason": None if verdict == "compatible" else "Identity remains unresolved.",
        }
    ]

    with pytest.raises(B0PriorChallengeError, match="continuity"):
        validate_prior_challenge_response(
            response,
            chapter=_chapter(),
            prior_cards=[_prior_card()],
            request_fingerprint="req_bad_continuity",
        )


def test_normal_b0_validator_is_reused_for_new_delta_rows() -> None:
    response = _empty_response()
    response["prior_card_dispositions"] = [
        _compatible("prior_vale", "bk_ch02_b001")
    ]
    response["new_entity_candidates"] = [
        {
            "canonical_surface": "Brindle",
            "canonical_surface_support_block_ids": ["bk_ch02_b003"],
            "canonical_name_class": "proper_name",
            "alternative_names": [],
            "referent_kind_claim": {
                "value": "animal",
                "basis": "explicit",
                "support_block_ids": ["bk_ch02_b003"],
            },
            "referential_gender_claim": {
                "value": "feminine",
                "basis": "explicit",
                "support_block_ids": ["bk_ch02_b003"],
            },
            "identity_summary_draft": "An individualized named hound.",
        }
    ]
    artifact = validate_prior_challenge_response(
        response,
        chapter=_chapter(),
        prior_cards=[_prior_card()],
        request_fingerprint="req_delta",
    )
    row = artifact["delta_inventory"]["entity_candidates"][0]
    assert row["canonical_surface"] == "Brindle"
    assert row["referent_kind_claim"]["value"] == "animal"


def test_stable_name_enrichment_is_located_and_never_applied_directly() -> None:
    response = _empty_response()
    response["prior_card_dispositions"] = [
        _compatible("prior_vale", "bk_ch02_b003")
    ]
    response["prior_enrichment_requests"] = [
        {
            "prior_card_id": "prior_vale",
            "surface": "V. Vale",
            "name_class": "stable_nickname",
            "source_block_ids": ["bk_ch02_b003"],
            "reason": "The current chapter uses this stable signed form.",
        }
    ]
    artifact = validate_prior_challenge_response(
        response,
        chapter=_chapter(),
        prior_cards=[_prior_card()],
        request_fingerprint="req_enrich",
    )
    assert artifact["prior_enrichment_requests"][0]["surface"] == "V. Vale"
    assert "applied" not in artifact["prior_enrichment_requests"][0]


def test_repeated_prior_surface_is_not_an_enrichment() -> None:
    response = _empty_response()
    response["prior_card_dispositions"] = [
        _compatible("prior_vale", "bk_ch02_b001")
    ]
    response["prior_enrichment_requests"] = [
        {
            "prior_card_id": "prior_vale",
            "surface": "Mr. Vale",
            "name_class": "title_plus_name",
            "source_block_ids": ["bk_ch02_b001"],
            "reason": "Repeated supplied surface.",
        }
    ]
    with pytest.raises(B0PriorChallengeError, match="repeats an existing"):
        validate_prior_challenge_response(
            response,
            chapter=_chapter(),
            prior_cards=[_prior_card()],
            request_fingerprint="req_repeat",
        )


def test_foreign_prior_id_and_foreign_block_are_fatal() -> None:
    response = _empty_response()
    response["prior_card_dispositions"] = [
        _challenge(
            "prior_foreign",
            issue_code="kind_conflict",
            disputed_field="referent_kind",
            block_id="bk_ch02_b001",
        )
    ]
    with pytest.raises(B0PriorChallengeError, match="foreign prior card"):
        validate_prior_challenge_response(
            response,
            chapter=_chapter(),
            prior_cards=[_prior_card()],
            request_fingerprint="req_foreign",
        )
    response["prior_card_dispositions"][0]["prior_card_id"] = "prior_vale"
    response["prior_card_dispositions"][0]["source_block_ids"] = ["foreign_b001"]
    with pytest.raises(B0PriorChallengeError, match="foreign blocks"):
        validate_prior_challenge_response(
            response,
            chapter=_chapter(),
            prior_cards=[_prior_card()],
            request_fingerprint="req_foreign_block",
        )


def test_ticket_issue_field_pair_and_uniqueness_are_enforced() -> None:
    disposition = _challenge(
        "prior_orr",
        issue_code="gender_conflict",
        disputed_field="referent_kind",
        block_id="bk_ch02_b002",
    )
    response = _empty_response()
    response["prior_card_dispositions"] = [disposition]
    with pytest.raises(B0PriorChallengeError, match="disagree"):
        validate_prior_challenge_response(
            response,
            chapter=_chapter(),
            prior_cards=[_orr_card()],
            request_fingerprint="req_pair",
        )
    disposition["disputed_field"] = "referential_gender"
    response["prior_card_dispositions"] = [disposition, deepcopy(disposition)]
    with pytest.raises(B0PriorChallengeError, match="duplicate"):
        validate_prior_challenge_response(
            response,
            chapter=_chapter(),
            prior_cards=[_orr_card()],
            request_fingerprint="req_duplicate",
        )


def test_foreign_response_field_is_fatal() -> None:
    response = _empty_response()
    response["confirmed_prior_card_ids"] = ["prior_vale"]
    with pytest.raises(B0PriorChallengeError, match="field set differs"):
        validate_prior_challenge_response(
            response,
            chapter=_chapter(),
            prior_cards=[_prior_card()],
            request_fingerprint="req_foreign_output",
        )


def test_hidden_corruption_manifest_changes_exactly_one_card() -> None:
    correct = [_prior_card(), _orr_card()]
    supplied = deepcopy(correct)
    supplied[1]["referential_gender"] = "feminine"
    manifest = build_hidden_corruption_manifest(
        mutation_id="gender_arm_01",
        correct_prior_cards=correct,
        supplied_prior_cards=supplied,
        expected_issue_code="gender_conflict",
    )
    assert manifest["changed_prior_card_id"] == "prior_orr"
    assert manifest["changed_card_fields"] == ["referential_gender"]
    assert manifest["hidden_from_model"] is True
    two_changes = deepcopy(supplied)
    two_changes[0]["referent_kind"] = "place"
    with pytest.raises(B0PriorChallengeError, match="exactly one"):
        build_hidden_corruption_manifest(
            mutation_id="bad_arm",
            correct_prior_cards=correct,
            supplied_prior_cards=two_changes,
            expected_issue_code="gender_conflict",
        )


def test_corruption_manifest_cannot_change_protected_provenance() -> None:
    correct = [_prior_card()]
    supplied = deepcopy(correct)
    supplied[0]["first_supported_block_id"] = "rewritten_b001"
    supplied[0]["provenance_refs"] = [
        {"chapter_id": "bk_ch01", "block_id": "rewritten_b001"}
    ]
    with pytest.raises(B0PriorChallengeError, match="protected"):
        build_hidden_corruption_manifest(
            mutation_id="protected_arm",
            correct_prior_cards=correct,
            supplied_prior_cards=supplied,
            expected_issue_code="kind_conflict",
        )


def test_hidden_evaluation_detects_expected_and_counts_unrelated_tickets() -> None:
    correct = [_prior_card(), _orr_card()]
    supplied = deepcopy(correct)
    supplied[1]["referential_gender"] = "feminine"
    manifest = build_hidden_corruption_manifest(
        mutation_id="gender_arm_01",
        correct_prior_cards=correct,
        supplied_prior_cards=supplied,
        expected_issue_code="gender_conflict",
    )
    response = _empty_response()
    response["prior_card_dispositions"] = [
        _challenge(
            "prior_orr",
            issue_code="gender_conflict",
            disputed_field="referential_gender",
            block_id="bk_ch02_b002",
        ),
        _challenge(
            "prior_vale",
            issue_code="unsupported_stable_claim",
            disputed_field="identity_summary",
            block_id="bk_ch02_b001",
        ),
    ]
    artifact = validate_prior_challenge_response(
        response,
        chapter=_chapter(),
        prior_cards=supplied,
        request_fingerprint="req_eval",
    )
    evaluation = evaluate_hidden_corruption(artifact, manifest)
    assert evaluation["expected_ticket_detected"] is True
    assert evaluation["unrelated_ticket_count"] == 1
    tampered = deepcopy(artifact)
    tampered["validation_report"]["challenge_count"] = 0
    with pytest.raises(B0PriorChallengeError, match="artifact hash mismatch"):
        evaluate_hidden_corruption(tampered, manifest)


def test_prompt_byte_drift_fails_closed(tmp_path: Path) -> None:
    changed = tmp_path / "design.md"
    changed.write_text(
        DESIGN_DOC.read_text(encoding="utf-8").replace(
            "RELEVANT_REVIEW_CASES",
            "RELEVANT_OPEN_CASES",
            1,
        ),
        encoding="utf-8",
    )
    with pytest.raises(B0PriorChallengeError, match="prompt bytes differ"):
        render_prior_challenge_request(
            chapter=_chapter(), prior_cards=[], design_doc=changed
        )
