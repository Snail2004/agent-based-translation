from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path

import pytest

from pipeline.literary.b2_context_v1 import (
    B2_INTERACTION_MODEL_OMITTED_CANDIDATE_CARD_FIELDS_V1,
    B2_MODEL_OMITTED_CANDIDATE_CARD_FIELDS_V1,
    B2ContextError,
    build_b2_windows_v1,
    build_candidate_packet_v1,
    load_b2_phase_a_profile,
    project_b2_candidate_packet_for_model_v1,
    project_b2_interaction_candidate_packet_for_model_v1,
)
from pipeline.literary.b2_context_v3 import (
    render_b2_frame_request_v2,
    render_b2_interaction_request_v3,
)
from pipeline.literary.checkpoint import canonical_hash
from pipeline.literary.b2_contract_v1 import B2ContractError
from pipeline.literary.b2_contract_v3 import (
    normalize_b2_frame_response_v2,
    normalize_b2_interaction_response_v3,
)
from pipeline.literary.b2_live_canary_v1 import (
    B2LiveCanaryError,
    partition_b2_frame_structure_reviews_v1,
)
from pipeline.literary.b2_review_routing_v1 import (
    MODEL_REVIEW_QUARANTINE_REASONS,
    ReviewRoutingError,
    code_review_v1,
    registered_code_review_callsites_v1,
    route_review,
)
from pipeline.literary.b2_phase_a_v3 import build_b2_slim_phase_a_bundle_v1
from pipeline.literary.b2_prompts_v3 import (
    B2_FRAME_PROMPT_ID_V5,
    B2_FRAME_SYSTEM_PROMPT_V5,
    B2_SLIM_INTERACTION_PROMPT_ID_V11,
    B2_SLIM_INTERACTION_SYSTEM_PROMPT_V11,
    b2_frame_response_schema_v2,
    b2_interaction_response_schema_v3,
)


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
PROFILE_PATH = (
    RUNTIME_ROOT
    / "pipeline"
    / "configs"
    / "literary_b2_slim_phase_a_profile_v1.json"
)


def _profile():
    return load_b2_phase_a_profile(PROFILE_PATH)


def _chapter() -> dict:
    return {
        "chapter_id": "book_ch01",
        "blocks": [
            {
                "block_id": "book_ch01_h001",
                "order_index": 0,
                "block_type": "heading",
                "clean_text": "Chapter One",
            },
            {
                "block_id": "book_ch01_b001",
                "order_index": 1,
                "block_type": "paragraph",
                "clean_text": "Mr. Vale and Robin entered North House with the hound.",
            },
            {
                "block_id": "book_ch01_b002",
                "order_index": 2,
                "block_type": "dialogue",
                "clean_text": '"Robin, come here," said Mr. Vale. "No," Robin replied.',
            },
            {
                "block_id": "book_ch01_b003",
                "order_index": 3,
                "block_type": "paragraph",
                "clean_text": "Robin promised to marry Vale next spring.",
            },
            {
                "block_id": "book_ch01_b004",
                "order_index": 4,
                "block_type": "paragraph",
                "clean_text": "Years later, the hound was killed in the regional war.",
            },
        ],
    }


def _card(card_id: str, surface: str, *, kind: str = "person") -> dict:
    return {
        "prior_card_id": card_id,
        "canonical_surface": surface,
        "stable_surfaces": [surface],
        "authority_scope": "chapter_confirmed",
        "effective_claims": {
            "referent_kind": kind,
            "referential_gender": None,
            "identity_summary": "A source-grounded candidate.",
        },
        "disputed_claims": [],
        "first_supported_block_id": "book_ch01_b001",
        "provenance_refs": [
            {"chapter_id": "book_ch01", "block_id": "book_ch01_b001"}
        ],
    }


def _prefix() -> dict:
    return {
        "prefix_bundle_hash": "prefix_" + "a" * 57,
        "b0_context_cards": [
            _card("card_vale", "Mr. Vale"),
            _card("card_robin", "Robin"),
            _card("card_hound", "the hound", kind="animal"),
            _card("card_house", "North House", kind="place"),
        ],
        "candidate_only_context_cards": [],
        "prefix_identity_uncertainties": [],
    }


def _frame_request() -> dict:
    return render_b2_frame_request_v2(
        chapter=_chapter(), prefix_bundle=_prefix(), profile=_profile()
    )


def _window_request() -> tuple[dict, dict]:
    window = build_b2_windows_v1(_chapter(), profile=_profile())[0]
    request = render_b2_interaction_request_v3(
        window=window,
        prefix_bundle=_prefix(),
        profile=_profile(),
        frame_context={"frame_segments": []},
    )
    return window, request


def _endpoint(
    surface: str | None,
    status: str,
    candidate_ids: list[str],
) -> dict:
    return {
        "surface": surface,
        "resolution_status": status,
        "candidate_card_ids": candidate_ids,
    }


def _turn(
    *,
    anchor: str = '"Robin, come here,"',
    speaker: dict | None = None,
    addressee: dict | None = None,
) -> dict:
    return {
        "block_id": "book_ch01_b002",
        "utterance_anchor": anchor,
        "speaker": speaker or _endpoint("Mr. Vale", "resolved_candidate", ["card_vale"]),
        "addressee": addressee
        or _endpoint("Robin", "resolved_candidate", ["card_robin"]),
        "address_terms": ["Robin"],
        "register_cue": "neutral",
        "register_cue_raw": None,
        "delivery_tone": None,
    }


def _event(
    *,
    source_block_ids: list[str] | None = None,
    anchor_block_id: str = "book_ch01_b004",
    anchor: str = "the hound was killed in the regional war",
    event_status: str = "occurred",
    evidence_mode: str = "directly_narrated",
    review_status: str = "resolved",
) -> dict:
    return {
        "source_block_ids": source_block_ids or ["book_ch01_b004"],
        "anchor_block_id": anchor_block_id,
        "event_anchor": anchor,
        "event_kind": "world_state_change",
        "event_scope": "regional",
        "participants": [
            {
                "role": "affected",
                **_endpoint("the hound", "resolved_candidate", ["card_hound"]),
            },
            {
                "role": "location",
                **_endpoint("regional", "unresolved", []),
            },
        ],
        "summary": "The hound dies during a regional war.",
        "memory_role": "world_state_change",
        "event_status": event_status,
        "evidence_mode": evidence_mode,
        "review_status": review_status,
    }


def _interaction_response(
    request: dict,
    *,
    turns: list[dict] | None = None,
    events: list[dict] | None = None,
    reviews: list[dict] | None = None,
) -> dict:
    return {
        "schema_version": "literary_b2_interaction_response_v3",
        "chapter_id": "book_ch01",
        "window_id": request["window_id"],
        "speaker_turns": list(turns or []),
        "salient_events": list(events or []),
        "review_requests": list(reviews or []),
    }


def test_current_prompts_are_book_neutral_and_name_review_identifier_space() -> None:
    prompt = B2_FRAME_SYSTEM_PROMPT_V5 + B2_SLIM_INTERACTION_SYSTEM_PROMPT_V11
    lowered = prompt.casefold()
    for forbidden in ("wuthering", "heathcliff", "lockwood", "gatsby"):
        assert forbidden not in lowered
    assert "do not write a chapter gist" in lowered
    assert "not an inventory of actions" in lowered
    assert "ordinary movement" in lowered
    assert "reaction clause" in lowered
    assert "speech attribution" in lowered
    assert "pre-existing state" in lowered
    assert "grammatical agent" in lowered
    assert "closing quotation mark" in lowered
    assert "not an explicit speech tag" in lowered
    assert "already-existing ownership" in lowered
    assert prompt.count(
        "the exact `candidate_card_id` string copied from the supplied"
    ) == 2
    assert prompt.count(
        "Use `frame_structure` when the blocker is where a scene boundary"
    ) == 2
    assert "never a surface name, never an abbreviation" in prompt
    assert "event_participant" in prompt
    assert "`blocking_kind` to `timeline_pending`" in prompt
    assert "chapter_orientation" not in b2_frame_response_schema_v2()["properties"]
    assert b2_frame_response_schema_v2()["additionalProperties"] is False
    assert b2_interaction_response_schema_v3()["additionalProperties"] is False


def test_v3_schema_keeps_only_three_memory_roles_and_no_old_event_list() -> None:
    schema = b2_interaction_response_schema_v3()
    assert "interaction_events" not in schema["properties"]
    event = schema["properties"]["salient_events"]["items"]["properties"]
    assert event["memory_role"]["enum"] == [
        "relationship_evidence",
        "durable_state_change",
        "world_state_change",
    ]
    assert "durable_effect_candidates" not in event


def test_interaction_candidate_arrays_cover_the_profile_upper_bound() -> None:
    schema = b2_interaction_response_schema_v3()
    properties = schema["properties"]
    turn = properties["speaker_turns"]["items"]["properties"]
    event = properties["salient_events"]["items"]["properties"]
    review = properties["review_requests"]["items"]["properties"]

    arrays = [
        turn["speaker"]["properties"]["candidate_card_ids"],
        turn["addressee"]["properties"]["candidate_card_ids"],
        event["participants"]["items"]["properties"]["candidate_card_ids"],
        review["candidate_card_ids"],
        review["competing_card_ids"],
    ]
    assert {row["maxItems"] for row in arrays} == {128}


def test_rendered_requests_bind_new_prompt_ids_and_candidate_cards_once() -> None:
    frame = _frame_request()
    _window, interaction = _window_request()
    assert frame["prompt_id"] == B2_FRAME_PROMPT_ID_V5
    assert interaction["prompt_id"] == B2_SLIM_INTERACTION_PROMPT_ID_V11
    assert frame["response_schema"] == b2_frame_response_schema_v2()
    assert interaction["response_schema"] == b2_interaction_response_schema_v3()
    user_payload = interaction["messages"][1]["content"]
    assert user_payload.count('"candidate_card_id":"card_vale"') == 1


def test_model_packet_projection_omits_audit_lifecycle_but_keeps_semantics() -> None:
    chapter = _chapter()
    profile = _profile()
    source_packet = build_candidate_packet_v1(
        chapter_id=chapter["chapter_id"],
        active_blocks=chapter["blocks"][1:],
        tail_blocks=[],
        prefix_bundle=_prefix(),
        candidate_card_cap=profile.frame_candidate_card_cap,
        profile=profile,
    )
    original = deepcopy(source_packet)
    projected = project_b2_candidate_packet_for_model_v1(source_packet)

    assert source_packet == original
    assert projected["packet_hash"] != source_packet["packet_hash"]
    assert len(projected["candidate_cards"]) == len(source_packet["candidate_cards"])
    for source_card, projected_card in zip(
        source_packet["candidate_cards"], projected["candidate_cards"], strict=True
    ):
        expected = deepcopy(source_card)
        for field in B2_MODEL_OMITTED_CANDIDATE_CARD_FIELDS_V1:
            expected.pop(field, None)
        raw_flags = expected.pop("uncertainty_flags")
        if raw_flags:
            expected["uncertainty_flags"] = [
                {
                    "disputed_field": flag["disputed_field"],
                    "status": flag["status"],
                    "pending_reason_codes": flag["pending_reason_codes"],
                }
                for flag in raw_flags
            ]
        assert projected_card == expected
        assert projected_card["canonical_surface"] == source_card["canonical_surface"]
        assert projected_card["stable_surfaces"] == source_card["stable_surfaces"]
        assert projected_card["authority_scope"] == source_card["authority_scope"]
        assert projected_card["effective_claims_as_of"] == (
            source_card["effective_claims_as_of"]
        )
        assert projected_card.get("non_authoritative_context_claims") == (
            source_card.get("non_authoritative_context_claims")
        )

    request = render_b2_frame_request_v2(
        chapter=chapter,
        prefix_bundle=_prefix(),
        profile=profile,
    )
    payload = json.loads(request["messages"][1]["content"])
    model_packet = payload["candidate_packets"]
    assert model_packet == projected
    assert request["context_hashes"] == {
        "candidate_packet_hash": projected["packet_hash"],
        "source_candidate_packet_hash": source_packet["packet_hash"],
    }


def test_model_packet_projection_rejects_tampered_source_packet() -> None:
    chapter = _chapter()
    profile = _profile()
    source_packet = build_candidate_packet_v1(
        chapter_id=chapter["chapter_id"],
        active_blocks=chapter["blocks"][1:],
        tail_blocks=[],
        prefix_bundle=_prefix(),
        candidate_card_cap=profile.frame_candidate_card_cap,
        profile=profile,
    )
    source_packet["candidate_cards"][0]["canonical_surface"] = "Tampered"
    with pytest.raises(B2ContextError, match="candidate packet hash mismatch"):
        project_b2_candidate_packet_for_model_v1(source_packet)


def test_interaction_candidates_are_selected_from_active_blocks_not_tail() -> None:
    chapter = _chapter()
    profile = replace(
        _profile(),
        preceding_tail_blocks=1,
        max_active_blocks=1,
    )
    window = build_b2_windows_v1(chapter, profile=profile)[1]
    prefix = _prefix()
    prefix["b0_context_cards"].append(
        _card("card_tail_only", "Tail Only")
    )
    prefix["b0_context_cards"][-1]["stable_surfaces"] = ["North House"]
    request = render_b2_interaction_request_v3(
        window=window,
        prefix_bundle=prefix,
        profile=profile,
        frame_context={"frame_segments": []},
    )
    payload = json.loads(request["messages"][1]["content"])
    packet = payload["candidate_packets"]
    supplied_ids = {
        row["candidate_card_id"] for row in packet["candidate_cards"]
    }

    assert window["preceding_tail"]
    assert "North House" in window["preceding_tail"][0]["clean_text"]
    assert "North House" not in " ".join(
        row["clean_text"] for row in window["active_blocks"]
    )
    assert "card_tail_only" not in supplied_ids
    assert packet["preceding_tail_block_ids"] == []
    assert payload["preceding_tail"]


def test_interaction_projection_keeps_semantics_and_drops_audit_lifecycle() -> None:
    chapter = _chapter()
    profile = _profile()
    source_packet = build_candidate_packet_v1(
        chapter_id=chapter["chapter_id"],
        active_blocks=chapter["blocks"][1:],
        tail_blocks=[],
        prefix_bundle=_prefix(),
        candidate_card_cap=profile.interaction_candidate_card_cap,
        profile=profile,
    )
    source_card = source_packet["candidate_cards"][0]
    source_card["uncertainty_flags"] = [
        {
            "disputed_field": "life_stage",
            "status": "pending",
            "pending_reason_codes": ["chapter_provisional_claim"],
            "next_review_trigger": "new_source_evidence_or_identity_review",
            "hearing_count": 7,
        }
    ]
    source_body = deepcopy(source_packet)
    source_body.pop("packet_hash")
    source_packet["packet_hash"] = canonical_hash(source_body)

    projected = project_b2_interaction_candidate_packet_for_model_v1(
        source_packet
    )
    card = projected["candidate_cards"][0]

    for field in B2_INTERACTION_MODEL_OMITTED_CANDIDATE_CARD_FIELDS_V1:
        assert field not in card
    assert card["canonical_surface"] == source_card["canonical_surface"]
    assert card["stable_surfaces"] == source_card["stable_surfaces"]
    assert card["authority_scope"] == source_card["authority_scope"]
    assert card["effective_claims_as_of"] == source_card["effective_claims_as_of"]
    assert card["uncertainty_flags"] == [
        {
            "disputed_field": "life_stage",
            "status": "pending",
            "pending_reason_codes": ["chapter_provisional_claim"],
        }
    ]


def test_interaction_projection_rejects_tail_retrieval_packet() -> None:
    chapter = _chapter()
    profile = _profile()
    source_packet = build_candidate_packet_v1(
        chapter_id=chapter["chapter_id"],
        active_blocks=chapter["blocks"][2:],
        tail_blocks=chapter["blocks"][1:2],
        prefix_bundle=_prefix(),
        candidate_card_cap=profile.interaction_candidate_card_cap,
        profile=profile,
    )
    with pytest.raises(
        B2ContextError,
        match="preceding-tail retrieval",
    ):
        project_b2_interaction_candidate_packet_for_model_v1(source_packet)


def test_runtime_ids_do_not_change_b2_provider_schema() -> None:
    chapter = deepcopy(_chapter())
    chapter["chapter_id"] = "book_ch99"
    second_frame = render_b2_frame_request_v2(
        chapter=chapter,
        prefix_bundle=_prefix(),
        profile=_profile(),
    )
    window, first_interaction = _window_request()
    second_window = deepcopy(window)
    second_window["window_id"] = "book_ch01_window_other"
    second_interaction = render_b2_interaction_request_v3(
        window=second_window,
        prefix_bundle=_prefix(),
        profile=_profile(),
        frame_context={"frame_segments": []},
    )

    assert _frame_request()["response_schema"] == second_frame["response_schema"]
    assert (
        first_interaction["response_schema"]
        == second_interaction["response_schema"]
    )


def test_same_surface_retrieves_two_candidates_without_code_selecting_one() -> None:
    prefix = _prefix()
    prefix["b0_context_cards"].append(
        {
            **_card("card_mrs_vale", "Mrs. Vale"),
            "stable_surfaces": ["Mrs. Vale", "Vale"],
        }
    )
    prefix["b0_context_cards"][0]["stable_surfaces"] = ["Mr. Vale", "Vale"]
    window = build_b2_windows_v1(_chapter(), profile=_profile())[0]
    request = render_b2_interaction_request_v3(
        window=window,
        prefix_bundle=prefix,
        profile=_profile(),
        frame_context={"frame_segments": []},
    )
    payload = json.loads(request["messages"][1]["content"])
    groups = payload["candidate_packets"]["surface_groups"]
    collision = next(row for row in groups if row["source_surface"] == "Vale")
    assert set(collision["candidate_card_ids"]) == {"card_mrs_vale", "card_vale"}


def test_frame_start_points_are_code_expanded_to_exact_cover() -> None:
    request = _frame_request()
    response = {
        "schema_version": "literary_b2_frame_response_v2",
        "chapter_id": "book_ch01",
        "frame_starts": [
            {
                "start_block_id": "book_ch01_b001",
                "narrator_surface": "Mr. Vale",
                "narrator_status": "resolved_candidate",
                "candidate_card_ids": ["card_vale"],
                "narrative_mode": "direct_current",
                "boundary_cue_anchor": "Mr. Vale",
            },
            {
                "start_block_id": "book_ch01_b004",
                "narrator_surface": None,
                "narrator_status": "external_or_authorial",
                "candidate_card_ids": [],
                "narrative_mode": "recollected",
                "boundary_cue_anchor": "Years later",
            },
        ],
        "review_requests": [],
    }
    artifact = normalize_b2_frame_response_v2(request=request, response=response)
    assert [
        block_id
        for segment in artifact["frame_segments"]
        for block_id in segment["covered_block_ids"]
    ] == [
        "book_ch01_b001",
        "book_ch01_b002",
        "book_ch01_b003",
        "book_ch01_b004",
    ]
    assert artifact["frame_segments"][0]["end_block_id"] == "book_ch01_b003"


def test_missing_initial_frame_is_pending_not_fatal() -> None:
    request = _frame_request()
    response = {
        "schema_version": "literary_b2_frame_response_v2",
        "chapter_id": "book_ch01",
        "frame_starts": [],
        "review_requests": [],
    }
    artifact = normalize_b2_frame_response_v2(request=request, response=response)
    first = artifact["frame_segments"][0]
    assert first["narrator_status"] == "unknown"
    assert first["covered_block_ids"] == [
        "book_ch01_b001",
        "book_ch01_b002",
        "book_ch01_b003",
        "book_ch01_b004",
    ]
    assert any(
        review["review_kind"] == "missing_initial_frame"
        for review in artifact["review_requests"]
    )
    review = next(
        row
        for row in artifact["review_requests"]
        if row["review_kind"] == "missing_initial_frame"
    )
    assert review["blocking_kind"] == "frame_structure"
    assert route_review(review) == "E"
    assert review["competing_card_ids"] == []


def test_turn_order_is_derived_from_exact_source_location() -> None:
    _window, request = _window_request()
    later = _turn(
        anchor='"No,"',
        speaker=_endpoint("Robin", "resolved_candidate", ["card_robin"]),
        addressee=_endpoint("Mr. Vale", "resolved_candidate", ["card_vale"]),
    )
    response = _interaction_response(request, turns=[later, _turn()])
    artifact = normalize_b2_interaction_response_v3(
        request=request, response=response
    )
    assert [row["utterance_anchor"] for row in artifact["speaker_turns"]] == [
        '"Robin, come here,"',
        '"No,"',
    ]
    assert [row["turn_index_in_window"] for row in artifact["speaker_turns"]] == [1, 2]


def test_overlong_utterance_anchor_is_bounded_without_losing_the_turn() -> None:
    chapter = _chapter()
    long_anchor = "A" * 620
    chapter["blocks"][2]["clean_text"] = long_anchor
    window = build_b2_windows_v1(chapter, profile=_profile())[0]
    request = render_b2_interaction_request_v3(
        window=window,
        prefix_bundle=_prefix(),
        profile=_profile(),
        frame_context={"frame_segments": []},
    )

    artifact = normalize_b2_interaction_response_v3(
        request=request,
        response=_interaction_response(
            request,
            turns=[_turn(anchor=long_anchor)],
        ),
    )

    assert len(artifact["speaker_turns"]) == 1
    assert artifact["speaker_turns"][0]["utterance_anchor"] == long_anchor[:500]
    assert artifact["speaker_turns"][0]["grounding_status"] == "grounded"
    assert artifact["normalization_counts"]["truncated_utterance_anchors"] == 1
    note = artifact["utterance_anchor_normalizations"][0]
    assert note["row_index"] == 0
    assert note["block_id"] == "book_ch01_b002"
    assert note["raw_length"] == 620
    assert note["normalized_length"] == 500
    assert len(note["raw_anchor_sha256"]) == 64


@pytest.mark.parametrize(
    ("field", "foreign_value"),
    [
        ("chapter_id", "copied_example_chapter"),
        ("window_id", "copied_example_window"),
    ],
)
def test_wrong_window_identity_echoes_are_rejected(
    field: str, foreign_value: str
) -> None:
    _window, request = _window_request()
    response = _interaction_response(request, turns=[_turn()])
    response[field] = foreign_value

    with pytest.raises(B2ContractError, match=rf"response {field} differs"):
        normalize_b2_interaction_response_v3(request=request, response=response)


def test_unlocatable_turn_is_retained_without_speaker_authority() -> None:
    _window, request = _window_request()
    response = _interaction_response(
        request, turns=[_turn(anchor="not present in source")]
    )
    artifact = normalize_b2_interaction_response_v3(
        request=request, response=response
    )
    row = artifact["speaker_turns"][0]
    assert row["grounding_status"] == "review_required_unlocatable"
    assert row["speaker_authority_status"] == "pending_review"
    assert any(
        review["review_kind"] == "source_anchor"
        for review in artifact["review_requests"]
    )
    review = next(
        row
        for row in artifact["review_requests"]
        if row["review_kind"] == "source_anchor"
    )
    assert review["blocking_kind"] == "anchor_defect"
    assert review["competing_card_ids"] == []


def test_endpoint_cardinality_conflict_is_pending_instead_of_dropped() -> None:
    _window, request = _window_request()
    bad = _turn(
        speaker=_endpoint("Mr. Vale", "resolved_candidate", ["card_vale", "card_robin"])
    )
    artifact = normalize_b2_interaction_response_v3(
        request=request,
        response=_interaction_response(request, turns=[bad]),
    )
    row = artifact["speaker_turns"][0]
    assert row["speaker"]["resolution_status"] == "pending_contract_conflict"
    assert row["speaker_authority_status"] == "pending_review"


def test_foreign_candidate_id_fails_closed() -> None:
    _window, request = _window_request()
    response = _interaction_response(
        request,
        turns=[
            _turn(
                speaker=_endpoint("stranger", "resolved_candidate", ["foreign_card"])
            )
        ],
    )
    with pytest.raises(B2ContractError):
        normalize_b2_interaction_response_v3(request=request, response=response)


def test_tail_block_cannot_own_an_output_row() -> None:
    profile = replace(
        _profile(),
        target_active_source_tokens=8,
        max_active_blocks=1,
        preceding_tail_blocks=1,
    )
    windows = build_b2_windows_v1(_chapter(), profile=profile)
    window = windows[1]
    request = render_b2_interaction_request_v3(
        window=window,
        prefix_bundle=_prefix(),
        profile=profile,
        frame_context={"frame_segments": []},
    )
    tail_id = window["preceding_tail_block_ids"][0]
    turn = _turn()
    turn["block_id"] = tail_id
    with pytest.raises(B2ContractError):
        normalize_b2_interaction_response_v3(
            request=request,
            response=_interaction_response(request, turns=[turn]),
        )


def test_nonhuman_world_event_is_accepted_as_provisional_observation() -> None:
    _window, request = _window_request()
    artifact = normalize_b2_interaction_response_v3(
        request=request,
        response=_interaction_response(request, events=[_event()]),
    )
    row = artifact["salient_events"][0]
    assert row["participants"][0]["candidate_card_ids"] == ["card_hound"]
    assert row["participant_authority_status"] == "partial"
    assert row["memory_role"] == "world_state_change"
    assert row["event_authority_status"] == "provisional_occurred_observation"


def test_schema_invalid_event_is_quarantined_without_losing_window_rows() -> None:
    _window, request = _window_request()
    valid_event = _event()
    invalid_event = deepcopy(valid_event)
    invalid_event["participants"][0]["role"] = "addressee"

    artifact = normalize_b2_interaction_response_v3(
        request=request,
        response=_interaction_response(
            request,
            turns=[_turn()],
            events=[valid_event, invalid_event],
        ),
    )

    assert len(artifact["speaker_turns"]) == 1
    assert len(artifact["salient_events"]) == 1
    assert artifact["normalization_counts"]["raw_salient_events"] == 2
    assert artifact["normalization_counts"]["normalized_salient_events"] == 1
    assert artifact["normalization_counts"]["quarantined_salient_events"] == 1
    quarantine = artifact["quarantined_salient_events"]
    assert len(quarantine) == 1
    assert quarantine[0]["quarantine_reason"] == "event_response_schema_violation"
    assert quarantine[0]["row_index"] == 1
    assert quarantine[0]["schema_path"] == "participants.0.role"
    assert quarantine[0]["raw_event"]["participants"][0]["role"] == "addressee"
    assert len(quarantine[0]["raw_event_sha256"]) == 64


def test_planned_reported_event_remains_non_authoritative() -> None:
    _window, request = _window_request()
    event = _event(
        source_block_ids=["book_ch01_b003"],
        anchor_block_id="book_ch01_b003",
        anchor="promised to marry Vale next spring",
        event_status="planned",
        evidence_mode="reported_by_character",
    )
    event["event_kind"] = "commitment_or_separation"
    event["event_scope"] = "interpersonal"
    event["memory_role"] = "relationship_evidence"
    event["participants"] = [
        {"role": "initiator", **_endpoint("Robin", "resolved_candidate", ["card_robin"])},
        {"role": "counterpart", **_endpoint("Vale", "resolved_candidate", ["card_vale"])},
    ]
    event["summary"] = "Robin promises a future marriage to Vale."
    artifact = normalize_b2_interaction_response_v3(
        request=request,
        response=_interaction_response(request, events=[event]),
    )
    assert artifact["salient_events"][0]["event_authority_status"] == (
        "non_authoritative_report_or_proposal"
    )


def test_exact_duplicate_collapses_but_conflicting_event_survives_for_review() -> None:
    _window, request = _window_request()
    base = _event()
    conflicting = deepcopy(base)
    conflicting["summary"] = "A war is reported but the hound's fate is unclear."
    conflicting["event_status"] = "uncertain"
    conflicting["review_status"] = "pending_review"
    artifact = normalize_b2_interaction_response_v3(
        request=request,
        response=_interaction_response(request, events=[base, deepcopy(base), conflicting]),
    )
    assert len(artifact["salient_events"]) == 2
    assert all(
        row["row_status"] == "review_required_conflicting_rows"
        for row in artifact["salient_events"]
    )
    event_reviews = [
        row
        for row in artifact["review_requests"]
        if row["review_kind"] in {"event_significance", "event_actuality"}
    ]
    assert event_reviews
    assert {row["blocking_kind"] for row in event_reviews} == {
        "timeline_pending"
    }
    assert all(row["competing_card_ids"] == [] for row in event_reviews)


def test_model_review_requires_typed_blocker_and_closed_competing_cards() -> None:
    _window, request = _window_request()
    review = {
        "review_kind": "addressee_identity",
        "blocking_kind": "unresolved_entity",
        "source_block_ids": ["book_ch01_b002"],
        "candidate_card_ids": ["card_vale", "card_robin"],
        "competing_card_ids": ["card_vale", "card_robin"],
        "reason": "The model reports a typed identity ambiguity.",
    }
    artifact = normalize_b2_interaction_response_v3(
        request=request,
        response=_interaction_response(request, reviews=[review]),
    )
    normalized = artifact["review_requests"][0]
    assert normalized["blocking_kind"] == "unresolved_entity"
    assert set(normalized["competing_card_ids"]) == {"card_vale", "card_robin"}

    invalid = deepcopy(review)
    invalid["blocking_kind"] = "scene_ambiguity"
    artifact = normalize_b2_interaction_response_v3(
        request=request,
        response=_interaction_response(request, reviews=[invalid]),
    )
    assert artifact["normalization_counts"]["model_review_requests"] == 0
    assert artifact["quarantined_review_requests"][0]["quarantine_reason"] == (
        "competing_on_non_entity"
    )


def test_unknown_review_card_ids_quarantine_only_the_review_row() -> None:
    _window, request = _window_request()
    review = {
        "review_kind": "addressee_identity",
        "blocking_kind": "unresolved_entity",
        "source_block_ids": ["book_ch01_b002"],
        "candidate_card_ids": ["card_vale", "card_robin"],
        "competing_card_ids": ["E2", "E11"],
        "reason": "The supplied participants remain ambiguous.",
    }
    artifact = normalize_b2_interaction_response_v3(
        request=request,
        response=_interaction_response(
            request,
            turns=[_turn()],
            events=[_event()],
            reviews=[review],
        ),
    )

    assert len(artifact["speaker_turns"]) == 1
    assert len(artifact["salient_events"]) == 1
    assert artifact["normalization_counts"]["model_review_requests"] == 0
    assert artifact["normalization_counts"]["quarantined_review_requests"] == 1
    assert artifact["quarantined_review_requests"] == [
        {
            "quarantine_reason": "unknown_candidate_id",
            "offending_values": ["E2", "E11"],
            "review_kind": "addressee_identity",
            "blocking_kind": "unresolved_entity",
            "source_block_ids": ["book_ch01_b002"],
            "reason": "The supplied participants remain ambiguous.",
        }
    ]


def test_delivery_tone_is_independent_from_social_register() -> None:
    _window, request = _window_request()
    turn = _turn()
    turn["register_cue"] = "formal"
    turn["delivery_tone"] = "mournful"

    artifact = normalize_b2_interaction_response_v3(
        request=request,
        response=_interaction_response(request, turns=[turn]),
    )

    assert len(artifact["speaker_turns"]) == 1
    normalized_turn = artifact["speaker_turns"][0]
    assert normalized_turn["register_cue"] == "formal"
    assert normalized_turn["register_cue_raw"] is None
    assert normalized_turn["register_cue_status"] == "in_vocabulary"
    assert normalized_turn["delivery_tone"] == "mournful"
    assert artifact["quarantined_register_cues"] == []


def test_unknown_register_cue_quarantines_only_cue_and_preserves_turn() -> None:
    _window, request = _window_request()
    turn = _turn()
    turn["register_cue"] = "mournful"
    review = {
        "review_kind": "event_participant",
        "blocking_kind": "timeline_pending",
        "source_block_ids": ["book_ch01_b002"],
        "candidate_card_ids": ["card_vale", "card_robin"],
        "competing_card_ids": [],
        "reason": "The event participant remains temporally unresolved.",
    }

    artifact = normalize_b2_interaction_response_v3(
        request=request,
        response=_interaction_response(
            request,
            turns=[turn],
            events=[_event()],
            reviews=[review],
        ),
    )

    assert len(artifact["speaker_turns"]) == 1
    normalized_turn = artifact["speaker_turns"][0]
    assert normalized_turn["register_cue"] is None
    assert normalized_turn["register_cue_raw"] == "mournful"
    assert (
        normalized_turn["register_cue_status"]
        == "quarantined_invalid_enum"
    )
    assert normalized_turn["delivery_tone"] is None
    assert len(artifact["salient_events"]) == 1
    assert len(artifact["review_requests"]) == 1
    assert artifact["normalization_counts"]["raw_speaker_turns"] == 1
    assert artifact["normalization_counts"]["normalized_speaker_turns"] == 1
    assert artifact["normalization_counts"]["quarantined_register_cues"] == 1
    quarantine = artifact["quarantined_register_cues"]
    assert len(quarantine) == 1
    assert quarantine[0]["quarantine_reason"] == "unsupported_register_cue"
    assert quarantine[0]["raw_value"] == "mournful"
    assert quarantine[0]["block_id"] == "book_ch01_b002"
    assert quarantine[0]["utterance_anchor"] == '"Robin, come here,"'
    assert len(quarantine[0]["raw_turn_sha256"]) == 64


def test_non_string_register_cue_still_rejects_interaction_response() -> None:
    _window, request = _window_request()
    turn = _turn()
    turn["register_cue"] = {"invented": "mournful"}

    with pytest.raises(B2ContractError):
        normalize_b2_interaction_response_v3(
            request=request,
            response=_interaction_response(request, turns=[turn]),
        )


def test_other_register_cue_requires_and_preserves_raw_value() -> None:
    _window, request = _window_request()
    turn = _turn()
    turn["register_cue"] = "other"
    turn["register_cue_raw"] = "ritually submissive"

    artifact = normalize_b2_interaction_response_v3(
        request=request,
        response=_interaction_response(request, turns=[turn]),
    )

    normalized_turn = artifact["speaker_turns"][0]
    assert normalized_turn["register_cue"] == "other"
    assert normalized_turn["register_cue_raw"] == "ritually submissive"
    assert normalized_turn["register_cue_status"] == "model_other"
    assert artifact["quarantined_register_cues"] == []


def test_event_review_wrong_route_is_quarantined_without_losing_payload() -> None:
    _window, request = _window_request()
    review = {
        "review_kind": "event_participant",
        "blocking_kind": "scene_ambiguity",
        "source_block_ids": ["book_ch01_b002"],
        "candidate_card_ids": ["card_vale", "card_robin"],
        "competing_card_ids": [],
        "reason": "The event participant remains unclear within the scene.",
    }
    artifact = normalize_b2_interaction_response_v3(
        request=request,
        response=_interaction_response(
            request,
            turns=[_turn()],
            events=[_event()],
            reviews=[review],
        ),
    )

    assert len(artifact["speaker_turns"]) == 1
    assert len(artifact["salient_events"]) == 1
    assert artifact["normalization_counts"]["model_review_requests"] == 0
    assert artifact["quarantined_review_requests"] == [
        {
            "quarantine_reason": "event_review_requires_timeline_pending",
            "offending_values": ["scene_ambiguity"],
            "review_kind": "event_participant",
            "blocking_kind": "scene_ambiguity",
            "source_block_ids": ["book_ch01_b002"],
            "reason": "The event participant remains unclear within the scene.",
        }
    ]


def test_event_review_timeline_pending_is_accepted_and_routes_to_d() -> None:
    _window, request = _window_request()
    review = {
        "review_kind": "event_participant",
        "blocking_kind": "timeline_pending",
        "source_block_ids": ["book_ch01_b002"],
        "candidate_card_ids": ["card_vale", "card_robin"],
        "competing_card_ids": [],
        "reason": "The event participant remains temporally unresolved.",
    }
    artifact = normalize_b2_interaction_response_v3(
        request=request,
        response=_interaction_response(request, reviews=[review]),
    )

    assert artifact["quarantined_review_requests"] == []
    assert len(artifact["review_requests"]) == 1
    assert route_review(artifact["review_requests"][0]) == "D"


@pytest.mark.parametrize(
    ("row", "expected_reason", "expected_offending"),
    [
        (
            {
                "review_kind": "addressee_identity",
                "blocking_kind": "not_registered",
                "source_block_ids": ["foreign_block"],
                "candidate_card_ids": ["card_vale"],
                "competing_card_ids": ["foreign_card"],
                "reason": "Two faults exercise first-match ordering.",
            },
            "unknown_candidate_id",
            ["foreign_card"],
        ),
        (
            {
                "review_kind": "addressee_identity",
                "blocking_kind": None,
                "source_block_ids": ["book_ch01_b002"],
                "candidate_card_ids": ["card_vale"],
                "competing_card_ids": [],
                "reason": "The blocking kind is absent.",
            },
            "invalid_blocking_kind",
            [None],
        ),
        (
            {
                "review_kind": "event_participant",
                "blocking_kind": "scene_ambiguity",
                "source_block_ids": ["book_ch01_b002"],
                "candidate_card_ids": ["card_vale"],
                "competing_card_ids": [],
                "reason": "An event review selected a non-temporal route.",
            },
            "event_review_requires_timeline_pending",
            ["scene_ambiguity"],
        ),
        (
            {
                "review_kind": "addressee_identity",
                "blocking_kind": "scene_ambiguity",
                "source_block_ids": ["foreign_block"],
                "candidate_card_ids": ["card_vale", "card_robin"],
                "competing_card_ids": ["card_vale", "card_robin"],
                "reason": "Competing cards are invalid on this kind.",
            },
            "competing_on_non_entity",
            ["card_vale", "card_robin"],
        ),
        (
            {
                "review_kind": "addressee_identity",
                "blocking_kind": "unresolved_entity",
                "source_block_ids": ["foreign_block"],
                "candidate_card_ids": ["card_vale"],
                "competing_card_ids": ["card_vale"],
                "reason": "Only one candidate was supplied.",
            },
            "insufficient_competing",
            ["card_vale"],
        ),
        (
            {
                "review_kind": "addressee_identity",
                "blocking_kind": "unresolved_entity",
                "source_block_ids": ["foreign_block"],
                "candidate_card_ids": ["card_vale", "card_robin"],
                "competing_card_ids": ["card_vale", "card_robin"],
                "reason": "The source block is outside the active window.",
            },
            "foreign_source_block",
            ["foreign_block"],
        ),
        (
            {
                "review_kind": "addressee_identity",
                "blocking_kind": "scene_ambiguity",
                "source_block_ids": [],
                "candidate_card_ids": ["card_vale"],
                "competing_card_ids": [],
                "reason": "No source block was supplied.",
            },
            "malformed_review_row",
            [],
        ),
    ],
)
def test_model_review_quarantine_reasons_are_ordered_and_typed(
    row: dict, expected_reason: str, expected_offending: list
) -> None:
    _window, request = _window_request()
    artifact = normalize_b2_interaction_response_v3(
        request=request,
        response=_interaction_response(request, reviews=[row]),
    )

    quarantine = artifact["quarantined_review_requests"]
    assert len(quarantine) == 1
    assert quarantine[0]["quarantine_reason"] == expected_reason
    assert quarantine[0]["offending_values"] == expected_offending
    assert quarantine[0]["reason"] == row["reason"]


def test_non_object_model_review_is_quarantined_as_malformed() -> None:
    _window, request = _window_request()
    response = _interaction_response(request)
    response["review_requests"] = ["not-an-object"]
    artifact = normalize_b2_interaction_response_v3(
        request=request, response=response
    )

    assert artifact["quarantined_review_requests"][0] == {
        "quarantine_reason": "malformed_review_row",
        "offending_values": ["not-an-object"],
        "review_kind": None,
        "blocking_kind": None,
        "source_block_ids": [],
        "reason": None,
    }


def test_long_window_evidence_span_preserves_turns_events_and_reviews() -> None:
    chapter = deepcopy(_chapter())
    for index in range(5, 15):
        chapter["blocks"].append(
            {
                "block_id": f"book_ch01_b{index:03d}",
                "order_index": index,
                "block_type": "paragraph",
                "clean_text": f"Additional source block {index}.",
            }
        )
    window = build_b2_windows_v1(chapter, profile=_profile())[0]
    request = render_b2_interaction_request_v3(
        window=window,
        prefix_bundle=_prefix(),
        profile=_profile(),
        frame_context={"frame_segments": []},
    )
    source_ids = [f"book_ch01_b{index:03d}" for index in range(1, 14)]
    review = {
        "review_kind": "addressee_identity",
        "blocking_kind": "scene_ambiguity",
        "source_block_ids": source_ids,
        "candidate_card_ids": [],
        "competing_card_ids": [],
        "reason": "The addressee remains unresolved across the supplied span.",
    }

    artifact = normalize_b2_interaction_response_v3(
        request=request,
        response=_interaction_response(
            request,
            turns=[_turn()],
            events=[_event(source_block_ids=source_ids)],
            reviews=[review],
        ),
    )

    assert len(artifact["speaker_turns"]) == 1
    assert len(artifact["salient_events"]) == 1
    assert len(artifact["review_requests"]) == 1
    assert artifact["review_requests"][0]["source_block_ids"] == source_ids
    assert artifact["quarantined_review_requests"] == []


def test_frame_artifact_records_quarantined_model_review() -> None:
    request = _frame_request()
    response = {
        "schema_version": "literary_b2_frame_response_v2",
        "chapter_id": "book_ch01",
        "frame_starts": [],
        "review_requests": [
            {
                "review_kind": "narrator_identity",
                "blocking_kind": "unresolved_entity",
                "source_block_ids": ["book_ch01_b001"],
                "candidate_card_ids": ["card_vale"],
                "competing_card_ids": ["E2", "E11"],
                "reason": "The narrator candidates remain ambiguous.",
            }
        ],
    }
    artifact = normalize_b2_frame_response_v2(request=request, response=response)

    assert artifact["normalization_counts"]["quarantined_review_requests"] == 1
    assert artifact["quarantined_review_requests"][0]["offending_values"] == [
        "E2",
        "E11",
    ]


def test_unlisted_row_schema_faults_still_reject_the_response() -> None:
    _window, request = _window_request()
    review = {
        "review_kind": "not_a_review_kind",
        "blocking_kind": "scene_ambiguity",
        "source_block_ids": ["book_ch01_b002"],
        "candidate_card_ids": ["card_vale"],
        "competing_card_ids": [],
        "reason": "An invalid review kind is not a quarantine reason.",
    }
    with pytest.raises(B2ContractError):
        normalize_b2_interaction_response_v3(
            request=request,
            response=_interaction_response(request, reviews=[review]),
        )

    malformed_container = _interaction_response(request)
    malformed_container["review_requests"] = {}
    with pytest.raises(B2ContractError):
        normalize_b2_interaction_response_v3(
            request=request, response=malformed_container
        )


def test_code_callsite_and_downstream_route_remain_fail_closed() -> None:
    with pytest.raises(ReviewRoutingError, match="unregistered"):
        code_review_v1(
            callsite="not_registered",
            review_kind="speaker_identity",
            source_block_ids=["book_ch01_b002"],
            candidate_card_ids=[],
            reason="A synthetic unregistered callsite.",
        )
    with pytest.raises(ReviewRoutingError, match="unroutable"):
        route_review({"blocking_kind": "not_registered"})
    assert MODEL_REVIEW_QUARANTINE_REASONS == (
        "unknown_candidate_id",
        "invalid_blocking_kind",
        "event_review_requires_timeline_pending",
        "competing_on_non_entity",
        "insufficient_competing",
        "foreign_source_block",
        "malformed_review_row",
    )


def test_frame_structure_is_a_closed_fifth_kind_routed_to_e() -> None:
    review_schema = b2_frame_response_schema_v2()["properties"][
        "review_requests"
    ]["items"]
    assert review_schema["properties"]["blocking_kind"]["enum"] == [
        "scene_ambiguity",
        "unresolved_entity",
        "anchor_defect",
        "timeline_pending",
        "frame_structure",
    ]
    for callsite in (
        "frame_missing_initial",
        "frame_narrator_contract",
        "frame_row_conflict",
    ):
        review = code_review_v1(
            callsite=callsite,
            review_kind="frame_structure",
            source_block_ids=["book_ch01_b002"],
            candidate_card_ids=[],
            reason="Synthetic frame structure hold.",
        )
        assert review["blocking_kind"] == "frame_structure"
        assert route_review(review) == "E"


def test_route_e_is_held_without_mutating_frames_or_reaching_downstream() -> None:
    frames = [
        {
            "frame_segment_id": "frame_1",
            "covered_block_ids": ["book_ch01_b001", "book_ch01_b002"],
        }
    ]
    original_frames = deepcopy(frames)
    ordinary = {
        "review_kind": "speaker_identity",
        "blocking_kind": "scene_ambiguity",
        "source_block_ids": ["book_ch01_b002"],
        "candidate_card_ids": [],
        "competing_card_ids": [],
        "origin": "model",
        "reason": "The endpoint remains unclear.",
    }
    frame_hold = {
        "review_kind": "narrator_contract",
        "blocking_kind": "frame_structure",
        "source_block_ids": ["book_ch01_b001", "book_ch01_b002"],
        "candidate_card_ids": [],
        "competing_card_ids": [],
        "origin": "code",
        "reason": "The frame boundary requires later structural handling.",
    }

    downstream, held = partition_b2_frame_structure_reviews_v1(
        reviews=[ordinary, frame_hold],
        frame_segments=frames,
    )
    assert downstream == [ordinary]
    assert held == [
        {
            "review_kind": "narrator_contract",
            "blocking_kind": "frame_structure",
            "source_block_ids": ["book_ch01_b001", "book_ch01_b002"],
            "frame_segment_ids": ["frame_1"],
            "origin": "code",
            "reason": "The frame boundary requires later structural handling.",
        }
    ]
    assert frames == original_frames

    with pytest.raises(B2LiveCanaryError, match="outside the chapter frames"):
        partition_b2_frame_structure_reviews_v1(
            reviews=[
                {
                    **frame_hold,
                    "source_block_ids": ["book_ch99_b999"],
                }
            ],
            frame_segments=frames,
        )


def test_code_review_callsite_table_is_closed_and_complete() -> None:
    assert set(registered_code_review_callsites_v1()) == {
        "event_actuality_uncertain",
        "event_conflicting_rows",
        "event_participant_contract",
        "event_participant_pending",
        "event_significance_pending",
        "event_source_anchor",
        "frame_missing_initial",
        "frame_narrator_contract",
        "frame_row_conflict",
        "frame_source_anchor",
        "turn_addressee_identity",
        "turn_conflicting_rows",
        "turn_endpoint_contract",
        "turn_source_anchor",
        "turn_speaker_pending",
    }


def test_zero_api_bundle_exact_covers_chapter_and_uses_lower_output_caps() -> None:
    real_input = {
        "input_hash": "input_hash",
        "source_run_root": "synthetic",
        "source_plan_hash": "plan_hash",
        "source_summary_hash": "summary_hash",
        "source_document_sha256": "document_hash",
        "source_run_git_head": "old_head",
        "current_git_head": "current_head",
        "certification_eligible": False,
        "certification_blockers": ["historical_source_head"],
        "ordered_chapter_ids": ["book_ch01"],
        "chapters": [
            {
                "chapter_id": "book_ch01",
                "chapter_ordinal": 1,
                "chapter": _chapter(),
                "prefix_bundle": _prefix(),
                "prefix_bundle_hash": _prefix()["prefix_bundle_hash"],
            }
        ],
    }
    bundle = build_b2_slim_phase_a_bundle_v1(
        real_input=real_input, profile=_profile()
    )
    plan = bundle["plan"]
    assert plan["totals"]["api_calls_performed"] == 0
    assert plan["chapters"][0]["planned_call_count"] >= 2
    assert plan["chapters"][0]["frame_request"]["token_reserve"][
        "output_token_cap"
    ] == 2500
    assert all(
        row["token_reserve"]["output_token_cap"] == 6000
        for row in plan["chapters"][0]["interaction_requests"]
    )


def test_addressee_can_say_there_is_no_listener_but_a_speaker_cannot() -> None:
    """A soliloquy has no addressee, and that is not the same as not knowing.

    Chapter 1 produced exactly this: Joseph "soliloquised", the source saying
    outright that the remark was not aimed at the person present, and the only
    available status was unresolved - which asserts a listener exists and was
    not identified. That sends a settled case to a recovery pass that can never
    close it, and it hides the distinction Vietnamese needs, where an honorific
    is owed to a listener and not owed to an empty room.
    """

    schema = b2_interaction_response_schema_v3()
    turn = schema["properties"]["speaker_turns"]["items"]["properties"]
    speaker = turn["speaker"]["properties"]["resolution_status"]["enum"]
    addressee = turn["addressee"]["properties"]["resolution_status"]["enum"]

    assert "no_addressee" in addressee
    assert "addressee_outside_scene" in addressee
    # The transport contract admits one canonical endpoint shape, so the states
    # ride on the shared enum and the restriction to addressee is carried by the
    # prompt. Pinning that here keeps the reason visible if someone later tries
    # to split the schema again and finds the probe refusing it.
    assert speaker == addressee

    prompt = B2_SLIM_INTERACTION_SYSTEM_PROMPT_V11
    assert "literary_b2_slim_interaction_window_v11" in prompt
    # the prompt must say when each applies, or the enum is decoration
    assert "no_addressee" in prompt and "addressee_outside_scene" in prompt
    assert "soliloquy" in prompt


def test_no_addressee_is_accepted_rather_than_downgraded() -> None:
    """A stated absence of a listener must survive normalization.

    The first live run after the enum was added showed the model using
    no_addressee correctly twice, and the contract turning both into
    pending_contract_conflict because the consistency rule listed only the
    older candidate-free statuses. That both discarded a correct reading and
    opened an addressee_identity review for an ambiguity that did not exist.
    """

    from pipeline.literary.b2_contract_v3 import _normalized_endpoint_v3

    for status in ("no_addressee", "addressee_outside_scene"):
        endpoint, consistent = _normalized_endpoint_v3(
            {"surface": None, "resolution_status": status, "candidate_card_ids": []},
            allowed_candidate_ids={"b0ent_x"},
            label="addressee",
        )
        assert consistent is True, status
        assert endpoint["resolution_status"] == status
        assert "model_resolution_status" not in endpoint

    # naming a candidate alongside them stays a conflict: no listener and a
    # listener cannot both be true
    endpoint, consistent = _normalized_endpoint_v3(
        {
            "surface": None,
            "resolution_status": "no_addressee",
            "candidate_card_ids": ["b0ent_x"],
        },
        allowed_candidate_ids={"b0ent_x"},
        label="addressee",
    )
    assert consistent is False
    assert endpoint["resolution_status"] == "pending_contract_conflict"
