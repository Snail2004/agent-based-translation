from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from pipeline.literary.b2_context_v1 import (
    build_b2_windows_v1,
    load_b2_phase_a_profile,
)
from pipeline.literary.b2_context_v2 import render_b2_interaction_request_v2
from pipeline.literary.b2_contract_v1 import B2ContractError
from pipeline.literary.b2_contract_v2 import normalize_b2_interaction_response_v2
from pipeline.literary.b2_prompts_v2 import (
    B2_INTERACTION_PROMPT_ID_V2_1,
    B2_INTERACTION_SYSTEM_PROMPT_V2_1,
    b2_interaction_response_schema_v2,
)


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
PROFILE_PATH = (
    RUNTIME_ROOT / "pipeline" / "configs" / "literary_b2_phase_a_profile_v1.json"
)


def _profile():
    return load_b2_phase_a_profile(PROFILE_PATH)


def _chapter() -> dict:
    return {
        "chapter_id": "book_ch01",
        "blocks": [
            {
                "block_id": "book_ch01_b001",
                "order_index": 1,
                "block_type": "paragraph",
                "clean_text": "Mr. Vale and Robin entered the room together.",
            },
            {
                "block_id": "book_ch01_b002",
                "order_index": 2,
                "block_type": "dialogue",
                "clean_text": '"Come here," said Mr. Vale to Robin, closing the door.',
            },
            {
                "block_id": "book_ch01_b003",
                "order_index": 3,
                "block_type": "paragraph",
                "clean_text": "Robin refused and closed the door.",
            },
        ],
    }


def _card(card_id: str, surface: str) -> dict:
    return {
        "prior_card_id": card_id,
        "canonical_surface": surface,
        "stable_surfaces": [surface],
        "authority_scope": "chapter_confirmed",
        "effective_claims": {
            "referent_kind": "person",
            "referential_gender": None,
            "identity_summary": "A named participant in the chapter.",
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
        ],
        "candidate_only_context_cards": [],
        "prefix_identity_uncertainties": [],
    }


def _request() -> dict:
    window = build_b2_windows_v1(_chapter(), profile=_profile())[0]
    return render_b2_interaction_request_v2(
        window=window,
        prefix_bundle=_prefix(),
        profile=_profile(),
        frame_context={"frame_segments": []},
    )


def _endpoint(
    surface: str | None,
    status: str,
    candidate_ids: list[str],
    *,
    reference_form: str = "proper_name",
    resolution_basis: str = "explicit_name",
) -> dict:
    return {
        "surface": surface,
        "reference_form": reference_form,
        "resolution_status": status,
        "candidate_card_ids": candidate_ids,
        "resolution_basis": resolution_basis,
    }


def _turn() -> dict:
    return {
        "block_id": "book_ch01_b002",
        "utterance_anchor": '"Come here,"',
        "speaker": _endpoint(
            "Mr. Vale", "resolved_candidate", ["card_vale"]
        ),
        "addressee": _endpoint(
            "Robin", "resolved_candidate", ["card_robin"]
        ),
        "speaker_support": {
            "source_block_id": "book_ch01_b002",
            "support_anchor": "said Mr. Vale",
            "support_kind": "explicit_reporting_clause",
        },
        "address_terms": [],
        "speech_function": "command",
        "register_cue": "neutral",
    }


def _event(*, anchor: str = "Mr. Vale and Robin entered the room together") -> dict:
    return {
        "block_id": "book_ch01_b001",
        "event_anchor": anchor,
        "actor": _endpoint(
            "Mr. Vale and Robin",
            "resolved_joint_candidates",
            ["card_vale", "card_robin"],
            reference_form="group",
            resolution_basis="group_expression",
        ),
        "target": _endpoint(
            None,
            "unresolved",
            [],
            reference_form="implicit",
            resolution_basis="unknown",
        ),
        "interaction_kind": "meeting_or_separation",
        "action_summary": "They enter together.",
        "observed_valence": "neutral",
    }


def _response(*, turns: list[dict] | None = None, events: list[dict] | None = None) -> dict:
    return {
        "schema_version": "literary_b2_interaction_response_v2",
        "chapter_id": "book_ch01",
        "window_id": "b2w1_book_ch01_01",
        "speaker_turns": list(turns or []),
        "interaction_events": list(events or []),
        "review_requests": [],
    }


def test_v2_prompt_is_book_neutral_and_speech_has_single_owner() -> None:
    lowered = B2_INTERACTION_SYSTEM_PROMPT_V2_1.casefold()
    for forbidden in ("wuthering", "heathcliff", "lockwood", "gatsby"):
        assert forbidden not in lowered
    assert "non-speech" in lowered
    assert "never put an enum label" in lowered
    assert "ordinary object handling" in lowered
    assert _request()["prompt_id"] == B2_INTERACTION_PROMPT_ID_V2_1
    schema = b2_interaction_response_schema_v2()
    event_kind = schema["properties"]["interaction_events"]["items"]["properties"][
        "interaction_kind"
    ]["enum"]
    assert "speech_act" not in event_kind


def test_joint_actor_is_distinct_from_ambiguous_candidates() -> None:
    artifact = normalize_b2_interaction_response_v2(
        request=_request(), response=_response(events=[_event()])
    )
    actor = artifact["interaction_events"][0]["actor"]
    assert actor["resolution_status"] == "resolved_joint_candidates"
    assert actor["candidate_card_ids"] == ["card_robin", "card_vale"]

    ambiguous = _event()
    ambiguous["actor"]["resolution_status"] = "ambiguous_candidates"
    second = normalize_b2_interaction_response_v2(
        request=_request(), response=_response(events=[ambiguous])
    )
    assert second["interaction_events"][0]["actor"]["resolution_status"] == (
        "ambiguous_candidates"
    )


def test_joint_actor_with_one_card_stays_pending_instead_of_halting() -> None:
    event = _event()
    event["actor"]["candidate_card_ids"] = ["card_vale"]
    artifact = normalize_b2_interaction_response_v2(
        request=_request(), response=_response(events=[event])
    )
    row = artifact["interaction_events"][0]
    assert row["actor"]["resolution_status"] == "pending_contract_conflict"
    assert row["row_status"] == "review_required_endpoint_contract"


def test_speaker_support_is_located_but_remains_provisional() -> None:
    artifact = normalize_b2_interaction_response_v2(
        request=_request(), response=_response(turns=[_turn()])
    )
    row = artifact["speaker_turns"][0]
    assert row["speaker_support"]["grounding_status"] == "grounded"
    assert row["speaker_authority_status"] == "provisional_explicit"
    assert row["speech_function"] == "command"


def test_unlocatable_speaker_support_is_retained_for_review() -> None:
    turn = _turn()
    turn["speaker_support"]["support_anchor"] = "a reporting clause not in source"
    artifact = normalize_b2_interaction_response_v2(
        request=_request(), response=_response(turns=[turn])
    )
    row = artifact["speaker_turns"][0]
    assert row["speaker_authority_status"] == "pending_review"
    assert row["row_status"] == "review_required_speaker_attribution"
    assert any(
        review["review_kind"] == "speaker_attribution"
        for review in artifact["review_requests"]
    )


def test_turn_event_source_overlap_is_visible_and_not_dropped() -> None:
    event = _event(anchor='"Come here,"')
    event["block_id"] = "book_ch01_b002"
    artifact = normalize_b2_interaction_response_v2(
        request=_request(), response=_response(turns=[_turn()], events=[event])
    )
    assert len(artifact["speaker_turns"]) == 1
    assert len(artifact["interaction_events"]) == 1
    assert artifact["interaction_events"][0]["row_status"] == (
        "review_required_turn_event_overlap"
    )
    assert artifact["normalization_counts"]["turn_event_overlap_events"] == 1


def test_nonspeech_event_outside_quoted_turn_is_not_marked_as_duplicate() -> None:
    turn = _turn()
    turn["utterance_anchor"] = '"Come here," said Mr. Vale to Robin, closing the door.'
    event = _event(anchor="closing the door")
    event["block_id"] = "book_ch01_b002"
    artifact = normalize_b2_interaction_response_v2(
        request=_request(), response=_response(turns=[turn], events=[event])
    )
    assert artifact["interaction_events"][0]["row_status"] == "accepted_observation"
    assert artifact["normalization_counts"]["turn_event_overlap_events"] == 0


def test_foreign_candidate_still_fails_closed() -> None:
    event = _event()
    event["actor"]["candidate_card_ids"] = ["foreign_card", "card_vale"]
    with pytest.raises(B2ContractError, match="violates response schema"):
        normalize_b2_interaction_response_v2(
            request=_request(), response=_response(events=[event])
        )


def test_v2_request_is_deterministic_and_v1_shape_is_not_accepted() -> None:
    request = _request()
    assert request == _request()
    properties = request["response_schema"]["properties"]
    assert properties["chapter_id"]["enum"] == ["book_ch01"]
    assert properties["window_id"]["enum"] == ["b2w1_book_ch01_01"]
    assert properties["speaker_turns"]["items"]["properties"]["speaker"][
        "properties"
    ]["candidate_card_ids"]["items"]["enum"] == ["card_robin", "card_vale"]
    response = _response(turns=[_turn()])
    legacy = deepcopy(response)
    legacy["schema_version"] = "literary_b2_interaction_response_v1"
    with pytest.raises(B2ContractError, match="violates response schema"):
        normalize_b2_interaction_response_v2(request=_request(), response=legacy)
