from __future__ import annotations

from copy import deepcopy
import json

import pytest

from pipeline.literary.b0_chapter_summary_v1 import (
    b2_speaker_recovery_candidate_scope_v1,
)
from pipeline.literary.b2_slim_speaker_recovery_v1 import (
    B2SlimSpeakerRecoveryError,
    apply_b2_slim_speaker_recovery_decision_v1,
    build_b2_effective_review_projection_v1,
    build_b2_slim_speaker_recovery_index_v1,
    make_b2_slim_speaker_recovery_validator_v1,
    render_b2_slim_speaker_recovery_request_v1,
    verify_b2_slim_speaker_recovery_artifact_v1,
)
from pipeline.literary.checkpoint import canonical_hash
from pipeline.scripts import run_literary_b2_speaker_recovery_modelapi_v1 as modelapi_runner


def _sealed(body: dict, field: str) -> dict:
    return {**body, field: canonical_hash(body)}


def _source() -> tuple[dict, list[dict]]:
    cards = [
        {
            "candidate_card_id": "ent_lockwood",
            "canonical_surface": "Mr. Lockwood",
        },
        {
            "candidate_card_id": "ent_heathcliff",
            "canonical_surface": "Mr. Heathcliff",
        },
    ]
    payload = {
        "request_kind": "b2_slim_interaction",
        "chapter_id": "wh_ch01",
        "window_id": "w1",
        "active_blocks": [
            {"block_id": "b024", "block_type": "dialogue", "text": "No, thank you."},
            {"block_id": "b025", "block_type": "dialogue", "text": "Not bitten, are you?"},
            {
                "block_id": "b026",
                "block_type": "dialogue",
                "text": "If I had been, I would have set my signet on the biter.",
            },
        ],
        "preceding_tail": [],
        "candidate_packets": {"candidate_cards": cards},
    }
    request_body = {
        "schema_version": "literary_b2_rendered_request_v1",
        "request_kind": "window_interaction",
        "prompt_id": "literary_b2_slim_interaction_window_v11",
        "prompt_sha256": "a" * 64,
        "chapter_id": "wh_ch01",
        "window_id": "w1",
        "messages": [
            {"role": "system", "content": "test"},
            {"role": "user", "content": json.dumps(payload)},
        ],
        "response_schema": {"type": "object"},
        "response_schema_hash": canonical_hash({"type": "object"}),
    }
    request = _sealed(request_body, "request_fingerprint")
    pending_turn = {
        "speaker_turn_id": "turn_pending",
        "block_id": "b025",
        "utterance_anchor": "Not bitten, are you?",
        "speaker": {
            "surface": None,
            "resolution_status": "unresolved",
            "candidate_card_ids": [],
        },
        "speaker_authority_status": "pending_review",
        "row_status": "review_required_speaker_attribution",
    }
    accepted_turn = {
        "speaker_turn_id": "turn_accepted",
        "block_id": "b026",
        "utterance_anchor": "If I had been",
        "speaker": {
            "surface": "Heathcliff",
            "resolution_status": "resolved_candidate",
            "candidate_card_ids": ["ent_heathcliff"],
        },
        "speaker_authority_status": "provisional_resolved",
        "row_status": "accepted_observation",
    }
    chapter_body = {
        "schema_version": "literary_b2_slim_chapter_artifact_v1",
        "chapter_id": "wh_ch01",
        "interaction_artifacts": [{"window_id": "w1", "artifact_hash": "b" * 64}],
        "speaker_turns": [pending_turn, accepted_turn],
        "salient_events": [],
        "review_requests": [
            {
                "review_id": "review_model",
                "review_kind": "speaker_attribution",
                "blocking_kind": "scene_ambiguity",
                "competing_card_ids": [],
                "origin_window_id": "w1",
                "source_block_ids": ["b025"],
                "candidate_card_ids": ["ent_heathcliff"],
                "reason": "Local sequence suggests one candidate.",
                "origin": "model",
                "status": "pending",
            },
            {
                "review_id": "review_code",
                "review_kind": "speaker_attribution",
                "blocking_kind": "scene_ambiguity",
                "competing_card_ids": [],
                "origin_window_id": "w1",
                "source_block_ids": ["b025"],
                "candidate_card_ids": [],
                "reason": "Speaker authority was withheld.",
                "origin": "code",
                "status": "pending",
            },
        ],
        "identity_or_claim_mutation_performed": False,
        "frame_segments": [
            {
                "frame_segment_id": "frame_1",
                "start_block_id": "b024",
                "end_block_id": "b026",
                "covered_block_ids": ["b024", "b025", "b026"],
            }
        ],
    }
    return _sealed(chapter_body, "artifact_hash"), [request]


def _decision_for_actions(
    index: dict,
    *,
    actions_by_ticket: dict[str, str],
    attach_target_candidate_card_id: str = "ent_heathcliff",
) -> dict:
    request = render_b2_slim_speaker_recovery_request_v1(index)
    assert request is not None
    tickets = {row["ticket_id"]: row for row in index["registry_gap_tickets"]}
    component_results = []
    for component in index["registry_components"]:
        ticket_actions = []
        for ticket_id in component["ticket_ids"]:
            action = actions_by_ticket[ticket_id]
            ticket = tickets[ticket_id]
            target = (
                attach_target_candidate_card_id
                if action == "attach_existing"
                else None
            )
            pending_reason = (
                "Evidence remains insufficient."
                if action == "keep_pending"
                else None
            )
            action_row = {
                "ticket_id": ticket_id,
                "action": action,
                "target_candidate_card_id": target,
                "provisional_group_key": (
                    f"local_{ticket_id}"
                    if action == "create_chapter_local"
                    else None
                ),
                "canonical_surface": (
                    "you"
                    if action == "create_chapter_local"
                    else None
                ),
                "referent_kind": (
                    "person" if action == "create_chapter_local" else None
                ),
                "identity_summary": (
                    "An unnamed chapter-local speaker in the supplied turn."
                    if action == "create_chapter_local"
                    else None
                ),
                "source_block_ids": list(ticket["source_block_ids"]),
                "pending_reason": pending_reason,
                "resolution_note": "Bounded local decision.",
            }
            if action == "keep_pending":
                action_row["narrowed_candidate_card_ids"] = list(
                    ticket.get("candidate_card_ids") or []
                )
            ticket_actions.append(action_row)
        component_results.append(
            {
                "component_id": component["component_id"],
                "result": {
                    "schema_version": "literary_b2_registry_recovery_response_v1",
                    "chapter_id": index["chapter_id"],
                    "component_id": component["component_id"],
                    "ticket_actions": ticket_actions,
                },
            }
        )
    raw = {
        "schema_version": "literary_b2_registry_recovery_batch_response_v1_1",
        "chapter_id": index["chapter_id"],
        "batch_id": request.component_id,
        "component_results": component_results,
    }
    validator = make_b2_slim_speaker_recovery_validator_v1(
        index=index, request=request
    )
    return dict(validator(raw))


def _decision(
    index: dict,
    *,
    action: str,
    attach_target_candidate_card_id: str = "ent_heathcliff",
) -> dict:
    return _decision_for_actions(
        index,
        actions_by_ticket={
            row["ticket_id"]: action for row in index["registry_gap_tickets"]
        },
        attach_target_candidate_card_id=attach_target_candidate_card_id,
    )


def _multi_turn_source() -> tuple[dict, list[dict]]:
    chapter, requests = _source()
    body = deepcopy(chapter)
    body.pop("artifact_hash")
    body["speaker_turns"].insert(
        0,
        {
            "speaker_turn_id": "turn_pending_sequence",
            "block_id": "b024",
            "utterance_anchor": "No, thank you.",
            "speaker": {
                "surface": None,
                "resolution_status": "unresolved",
                "candidate_card_ids": [],
            },
            "speaker_authority_status": "pending_review",
            "row_status": "review_required_speaker_attribution",
        },
    )
    body["review_requests"].append(
        {
            "review_id": "review_sequence",
            "review_kind": "speaker_attribution",
            "blocking_kind": "scene_ambiguity",
            "competing_card_ids": [],
            "origin_window_id": "w1",
            "source_block_ids": ["b024", "b025"],
            "candidate_card_ids": ["ent_heathcliff"],
            "reason": "The successive replies may share one speaker.",
            "origin": "model",
            "status": "pending",
        }
    )
    return _sealed(body, "artifact_hash"), requests


def test_frame_structure_review_cannot_enter_speaker_recovery() -> None:
    chapter, requests = _source()
    body = deepcopy(chapter)
    body.pop("artifact_hash")
    body["review_requests"].append(
        {
            "review_id": "frame_hold",
            "review_kind": "narrator_contract",
            "blocking_kind": "frame_structure",
            "competing_card_ids": [],
            "origin_window_id": "w1",
            "source_block_ids": ["b025"],
            "candidate_card_ids": [],
            "reason": "Frame structure is held outside endpoint recovery.",
            "origin": "code",
            "status": "pending",
        }
    )
    with pytest.raises(
        B2SlimSpeakerRecoveryError,
        match="frame-structure review reached Speaker Recovery",
    ):
        build_b2_slim_speaker_recovery_index_v1(
            chapter_artifact=_sealed(body, "artifact_hash"),
            interaction_requests=requests,
        )


def test_two_review_signals_become_one_ticket_without_reopening_accepted_turn() -> None:
    chapter, requests = _source()
    index = build_b2_slim_speaker_recovery_index_v1(
        chapter_artifact=chapter, interaction_requests=requests
    )
    assert index["counts"]["registry_gap_tickets"] == 1
    ticket = index["registry_gap_tickets"][0]
    assert ticket["source_row_id"] == "turn_pending"
    assert [row["review_id"] for row in ticket["source_review_signals"]] == [
        "review_code",
        "review_model",
    ]
    assert "turn_accepted" not in {
        row["source_row_id"] for row in index["registry_gap_tickets"]
    }
    assert index["slim_speaker_policy"]["accepted_turn_reinspection"] is False


def test_unserviceable_route_a_review_is_held_without_losing_valid_endpoints() -> None:
    chapter, requests = _source()
    body = deepcopy(chapter)
    body.pop("artifact_hash")
    held_review = {
        "review_id": "review_mismatched_endpoint",
        "review_kind": "speaker_identity",
        "blocking_kind": "scene_ambiguity",
        "competing_card_ids": [],
        "origin_window_id": "w1",
        "source_block_ids": ["b025"],
        "candidate_card_ids": [],
        "reason": "The typed review does not name a serviceable endpoint.",
        "origin": "model",
        "status": "pending",
    }
    body["review_requests"].append(held_review)
    chapter = _sealed(body, "artifact_hash")

    index = build_b2_slim_speaker_recovery_index_v1(
        chapter_artifact=chapter,
        interaction_requests=requests,
    )
    assert index["counts"]["registry_gap_tickets"] == 1
    assert index["counts"]["held_route_a_reviews"] == 1
    assert index["held_route_a_reviews"][0]["review_id"] == (
        "review_mismatched_endpoint"
    )
    assert index["held_route_a_reviews"][0]["hold_reason"] == (
        "unsupported_route_a_review_kind"
    )
    assert index["held_route_a_reviews"][0]["source_review"] == held_review

    artifact = apply_b2_slim_speaker_recovery_decision_v1(
        chapter_artifact=chapter,
        index=index,
        batch_decision=_decision(index, action="attach_existing"),
    )
    assert artifact["held_route_a_reviews"] == index["held_route_a_reviews"]
    assert {
        row["review_id"] for row in artifact["review_dispositions"]
    } == {"review_code", "review_model"}

    projection = build_b2_effective_review_projection_v1(
        chapter_artifact=chapter,
        recovery_artifact=artifact,
        allowed_candidate_card_ids={"ent_lockwood", "ent_heathcliff"},
    )
    assert projection["held_route_a_review_ids"] == [
        "review_mismatched_endpoint"
    ]
    assert [
        row["review_id"] for row in projection["effective_review_requests"]
    ] == ["review_mismatched_endpoint"]
    assert projection["resolved_review_ids"] == ["review_code", "review_model"]

    tampered = deepcopy(artifact)
    tampered["held_route_a_reviews"][0]["source_review"]["reason"] = "forged"
    tampered.pop("artifact_hash")
    tampered["artifact_hash"] = canonical_hash(tampered)
    with pytest.raises(
        B2SlimSpeakerRecoveryError,
        match="held route-A review set differs",
    ):
        build_b2_effective_review_projection_v1(
            chapter_artifact=chapter,
            recovery_artifact=tampered,
            allowed_candidate_card_ids={"ent_lockwood", "ent_heathcliff"},
        )


def test_cross_frame_route_a_review_is_held_without_losing_valid_endpoints() -> None:
    chapter, requests = _source()
    request_body = deepcopy(requests[0])
    request_body.pop("request_fingerprint")
    payload = json.loads(request_body["messages"][1]["content"])
    payload["active_blocks"].append(
        {"block_id": "b027", "block_type": "dialogue", "text": "Who spoke?"}
    )
    request_body["messages"][1]["content"] = json.dumps(payload)
    requests = [_sealed(request_body, "request_fingerprint")]

    body = deepcopy(chapter)
    body.pop("artifact_hash")
    body["frame_segments"][0]["end_block_id"] = "b026"
    body["frame_segments"].append(
        {
            "frame_segment_id": "frame_2",
            "start_block_id": "b027",
            "end_block_id": "b027",
            "covered_block_ids": ["b027"],
        }
    )
    held_review = {
        "review_id": "review_cross_frame",
        "review_kind": "addressee_identity",
        "blocking_kind": "scene_ambiguity",
        "competing_card_ids": [],
        "origin_window_id": "w1",
        "source_block_ids": ["b025", "b027"],
        "candidate_card_ids": [],
        "reason": "Two separate utterances have no secure listener.",
        "origin": "model",
        "status": "pending",
    }
    body["review_requests"].append(held_review)
    chapter = _sealed(body, "artifact_hash")

    index = build_b2_slim_speaker_recovery_index_v1(
        chapter_artifact=chapter,
        interaction_requests=requests,
    )
    assert index["counts"]["registry_gap_tickets"] == 1
    assert index["counts"]["held_route_a_reviews"] == 1
    assert index["held_route_a_reviews"][0]["review_id"] == "review_cross_frame"
    assert index["held_route_a_reviews"][0]["hold_reason"] == (
        "route_a_review_spans_multiple_frame_segments"
    )
    assert index["held_route_a_reviews"][0]["source_review"] == held_review

    artifact = apply_b2_slim_speaker_recovery_decision_v1(
        chapter_artifact=chapter,
        index=index,
        batch_decision=_decision(index, action="attach_existing"),
    )
    assert artifact["held_route_a_reviews"] == index["held_route_a_reviews"]
    assert {
        row["review_id"] for row in artifact["review_dispositions"]
    } == {"review_code", "review_model"}

    projection = build_b2_effective_review_projection_v1(
        chapter_artifact=chapter,
        recovery_artifact=artifact,
        allowed_candidate_card_ids={"ent_lockwood", "ent_heathcliff"},
    )
    assert projection["held_route_a_review_ids"] == ["review_cross_frame"]
    assert [
        row["review_id"] for row in projection["effective_review_requests"]
    ] == ["review_cross_frame"]


def test_overlapping_review_signals_coalesce_to_one_ticket_per_turn() -> None:
    chapter, requests = _multi_turn_source()
    index = build_b2_slim_speaker_recovery_index_v1(
        chapter_artifact=chapter, interaction_requests=requests
    )
    assert index["counts"]["registry_gap_tickets"] == 2
    assert len(
        {row["source_row_id"] for row in index["registry_gap_tickets"]}
    ) == 2
    tickets = {
        row["source_row_id"]: row for row in index["registry_gap_tickets"]
    }
    assert [
        row["review_id"]
        for row in tickets["turn_pending"]["source_review_signals"]
    ] == ["review_code", "review_model", "review_sequence"]
    assert [
        row["review_id"]
        for row in tickets["turn_pending_sequence"]["source_review_signals"]
    ] == ["review_sequence"]


def test_review_spanning_two_turns_is_resolved_only_when_both_attach() -> None:
    chapter, requests = _multi_turn_source()
    index = build_b2_slim_speaker_recovery_index_v1(
        chapter_artifact=chapter, interaction_requests=requests
    )
    ticket_by_turn = {
        row["source_row_id"]: row["ticket_id"]
        for row in index["registry_gap_tickets"]
    }
    artifact = apply_b2_slim_speaker_recovery_decision_v1(
        chapter_artifact=chapter,
        index=index,
        batch_decision=_decision_for_actions(
            index,
            actions_by_ticket={
                ticket_by_turn["turn_pending"]: "attach_existing",
                ticket_by_turn["turn_pending_sequence"]: "keep_pending",
            },
        ),
    )
    assert len(artifact["speaker_overlays"]) == 2
    dispositions = {
        row["review_id"]: row for row in artifact["review_dispositions"]
    }
    assert dispositions["review_code"]["status"] == "resolved"
    assert dispositions["review_model"]["status"] == "resolved"
    assert dispositions["review_sequence"]["status"] == "unresolved_ambiguous"
    assert dispositions["review_sequence"]["decision_action"] == "mixed"
    assert set(dispositions["review_sequence"]["ticket_ids"]) == set(
        ticket_by_turn.values()
    )


def test_duplicate_ticket_action_is_quarantined_without_losing_other_turn() -> None:
    chapter, requests = _multi_turn_source()
    index = build_b2_slim_speaker_recovery_index_v1(
        chapter_artifact=chapter, interaction_requests=requests
    )
    decision = _decision(index, action="attach_existing")
    duplicate_ticket_id = index["registry_gap_tickets"][0]["ticket_id"]
    tampered = deepcopy(decision)
    duplicate_action = next(
        deepcopy(action)
        for component in tampered["component_decisions"]
        for action in component["ticket_actions"]
        if action["ticket_id"] == duplicate_ticket_id
    )
    target_component = next(
        component
        for component in tampered["component_decisions"]
        if duplicate_ticket_id
        in {row["ticket_id"] for row in component["ticket_actions"]}
    )
    target_component["ticket_actions"].append(duplicate_action)
    tampered.pop("batch_decision_hash")
    tampered = _sealed(tampered, "batch_decision_hash")

    artifact = apply_b2_slim_speaker_recovery_decision_v1(
        chapter_artifact=chapter,
        index=index,
        batch_decision=tampered,
    )
    assert len(artifact["speaker_overlays"]) == 1
    assert len(artifact["quarantined_ticket_actions"]) == 1
    quarantine = artifact["quarantined_ticket_actions"][0]
    assert quarantine["ticket_id"] == duplicate_ticket_id
    assert quarantine["state"] == "unreviewed"
    assert quarantine["action_count"] == 2
    assert all(
        row["status"] == "unresolved_ambiguous"
        for row in artifact["review_dispositions"]
        if duplicate_ticket_id in row["ticket_ids"]
    )


def test_component_uses_the_full_contiguous_frame_context() -> None:
    chapter, requests = _source()
    index = build_b2_slim_speaker_recovery_index_v1(
        chapter_artifact=chapter, interaction_requests=requests
    )
    assert index["registry_components"][0]["source_block_ids"] == [
        "b024",
        "b025",
        "b026",
    ]
    request = render_b2_slim_speaker_recovery_request_v1(index)
    assert request is not None
    assert len(request.semantic_payload["components"]) == 1


def test_attach_creates_overlay_only_for_ticketed_turn() -> None:
    chapter, requests = _source()
    index = build_b2_slim_speaker_recovery_index_v1(
        chapter_artifact=chapter, interaction_requests=requests
    )
    decision = _decision(index, action="attach_existing")
    artifact = apply_b2_slim_speaker_recovery_decision_v1(
        chapter_artifact=chapter, index=index, batch_decision=decision
    )
    assert artifact["ticketed_speaker_turn_ids"] == ["turn_pending"]
    assert artifact["speaker_overlays"][0]["effective_speaker"][
        "candidate_card_ids"
    ] == ["ent_heathcliff"]
    assert artifact["accepted_turn_reinspection_performed"] is False
    assert artifact["unticketed_turn_mutation_performed"] is False
    assert chapter["speaker_turns"][1]["speaker"]["candidate_card_ids"] == [
        "ent_heathcliff"
    ]


def test_attach_accepts_empty_but_rejects_nonempty_narrowed_candidates() -> None:
    chapter, requests = _source()
    index = build_b2_slim_speaker_recovery_index_v1(
        chapter_artifact=chapter, interaction_requests=requests
    )

    def with_narrowed(value: list[str]) -> dict:
        decision = deepcopy(_decision(index, action="attach_existing"))
        decision.pop("batch_decision_hash")
        for component in decision["component_decisions"]:
            component.pop("decision_hash")
            for action in component["ticket_actions"]:
                action["narrowed_candidate_card_ids"] = list(value)
            component["decision_hash"] = canonical_hash(component)
        decision["batch_decision_hash"] = canonical_hash(decision)
        return decision

    accepted = apply_b2_slim_speaker_recovery_decision_v1(
        chapter_artifact=chapter,
        index=index,
        batch_decision=with_narrowed([]),
    )
    assert len(accepted["speaker_overlays"]) == 1
    assert accepted["quarantined_ticket_actions"] == []

    rejected = apply_b2_slim_speaker_recovery_decision_v1(
        chapter_artifact=chapter,
        index=index,
        batch_decision=with_narrowed(["ent_lockwood"]),
    )
    assert rejected["speaker_overlays"] == []
    assert rejected["quarantined_ticket_actions"][0]["reason"] == (
        "resolved route-A action carries narrowed candidates"
    )


def test_keep_pending_preserves_zero_speaker_authority() -> None:
    chapter, requests = _source()
    index = build_b2_slim_speaker_recovery_index_v1(
        chapter_artifact=chapter, interaction_requests=requests
    )
    artifact = apply_b2_slim_speaker_recovery_decision_v1(
        chapter_artifact=chapter,
        index=index,
        batch_decision=_decision(index, action="keep_pending"),
    )
    assert artifact["speaker_overlays"][0]["effective_speaker"] is None
    assert artifact["review_dispositions"][0]["status"] == "unresolved_ambiguous"


def test_create_chapter_local_builds_a_non_global_card_and_resolved_overlay() -> None:
    chapter, requests = _source()
    index = build_b2_slim_speaker_recovery_index_v1(
        chapter_artifact=chapter,
        interaction_requests=requests,
    )
    artifact = apply_b2_slim_speaker_recovery_decision_v1(
        chapter_artifact=chapter,
        index=index,
        batch_decision=_decision(index, action="create_chapter_local"),
    )

    ledger = artifact["registry_recovery_ledger"]
    assert len(ledger["local_candidate_cards"]) == 1
    card = ledger["local_candidate_cards"][0]
    assert card["authority_scope"] == "chapter_local_recovery"
    assert card["stable_surfaces"] == []
    assert card["uncertainty_flags"] == [
        "chapter_local_only",
        "no_global_alias_authority",
    ]
    overlay = artifact["speaker_overlays"][0]
    assert overlay["action"] == "create_chapter_local"
    assert overlay["effective_speaker"]["candidate_card_ids"] == [
        card["candidate_card_id"]
    ]
    assert artifact["quarantined_ticket_actions"] == []

    scope = b2_speaker_recovery_candidate_scope_v1(
        b2_artifact=chapter,
        recovery_artifact=artifact,
        recovery_index=index,
    )
    assert scope == {"ent_lockwood", "ent_heathcliff"}
    assert card["candidate_card_id"] not in scope
    projection = build_b2_effective_review_projection_v1(
        chapter_artifact=chapter,
        recovery_artifact=artifact,
        allowed_candidate_card_ids=scope,
        recovery_index=index,
    )
    assert projection["resolved_review_ids"] == ["review_code", "review_model"]
    with pytest.raises(
        B2SlimSpeakerRecoveryError,
        match="lacks its recovery index",
    ):
        verify_b2_slim_speaker_recovery_artifact_v1(
            chapter_artifact=chapter,
            recovery_artifact=artifact,
            allowed_candidate_card_ids={"ent_lockwood", "ent_heathcliff"},
        )


def test_effective_review_projection_keeps_only_pending_reviews() -> None:
    chapter, requests = _source()
    index = build_b2_slim_speaker_recovery_index_v1(
        chapter_artifact=chapter, interaction_requests=requests
    )
    pending_artifact = apply_b2_slim_speaker_recovery_decision_v1(
        chapter_artifact=chapter,
        index=index,
        batch_decision=_decision(index, action="keep_pending"),
    )
    pending = build_b2_effective_review_projection_v1(
        chapter_artifact=chapter,
        recovery_artifact=pending_artifact,
        allowed_candidate_card_ids={"ent_lockwood", "ent_heathcliff"},
    )
    assert pending["effective_review_requests"] == []
    assert pending["resolved_review_ids"] == []
    assert pending["unresolved_ambiguous_review_ids"] == [
        "review_code",
        "review_model",
    ]

    resolved_artifact = apply_b2_slim_speaker_recovery_decision_v1(
        chapter_artifact=chapter,
        index=index,
        batch_decision=_decision(index, action="attach_existing"),
    )
    resolved = build_b2_effective_review_projection_v1(
        chapter_artifact=chapter,
        recovery_artifact=resolved_artifact,
        allowed_candidate_card_ids={"ent_lockwood", "ent_heathcliff"},
    )
    assert resolved["effective_review_requests"] == []
    assert resolved["resolved_review_ids"] == ["review_code", "review_model"]


def test_b0_rechecks_speaker_recovery_with_the_sealed_cross_chapter_scope() -> None:
    chapter, requests = _source()
    index = build_b2_slim_speaker_recovery_index_v1(
        chapter_artifact=chapter, interaction_requests=requests
    )
    recovery = apply_b2_slim_speaker_recovery_decision_v1(
        chapter_artifact=chapter,
        index=index,
        batch_decision=_decision(
            index,
            action="attach_existing",
            attach_target_candidate_card_id="ent_lockwood",
        ),
    )

    local_chapter_registry_ids = {"ent_heathcliff"}
    with pytest.raises(B2SlimSpeakerRecoveryError, match="malformed"):
        build_b2_effective_review_projection_v1(
            chapter_artifact=chapter,
            recovery_artifact=recovery,
            allowed_candidate_card_ids=local_chapter_registry_ids,
        )

    sealed_scope = b2_speaker_recovery_candidate_scope_v1(
        b2_artifact=chapter,
        recovery_artifact=recovery,
        recovery_index=index,
    )
    assert sealed_scope == {"ent_heathcliff", "ent_lockwood"}
    assert "ent_lockwood" not in local_chapter_registry_ids
    projection = build_b2_effective_review_projection_v1(
        chapter_artifact=chapter,
        recovery_artifact=recovery,
        allowed_candidate_card_ids=sealed_scope,
    )
    assert projection["resolved_review_ids"] == ["review_code", "review_model"]


def test_b0_speaker_recovery_scope_still_rejects_an_unknown_candidate() -> None:
    chapter, requests = _source()
    index = build_b2_slim_speaker_recovery_index_v1(
        chapter_artifact=chapter, interaction_requests=requests
    )
    recovery = apply_b2_slim_speaker_recovery_decision_v1(
        chapter_artifact=chapter,
        index=index,
        batch_decision=_decision(index, action="attach_existing"),
    )
    forged = deepcopy(recovery)
    overlay = forged["speaker_overlays"][0]
    overlay_body = deepcopy(overlay)
    overlay_body.pop("overlay_id")
    overlay_body["effective_endpoint"]["candidate_card_ids"] = ["ent_foreign"]
    overlay_body["effective_speaker"]["candidate_card_ids"] = ["ent_foreign"]
    forged["speaker_overlays"][0] = {
        **overlay_body,
        "overlay_id": f"b2endov1_{canonical_hash(overlay_body)[:20]}",
    }
    forged_body = deepcopy(forged)
    forged_body.pop("artifact_hash")
    forged["artifact_hash"] = canonical_hash(forged_body)

    sealed_scope = b2_speaker_recovery_candidate_scope_v1(
        b2_artifact=chapter,
        recovery_artifact=forged,
        recovery_index=index,
    )
    with pytest.raises(B2SlimSpeakerRecoveryError, match="malformed"):
        build_b2_effective_review_projection_v1(
            chapter_artifact=chapter,
            recovery_artifact=forged,
            allowed_candidate_card_ids=sealed_scope,
        )


def test_effective_review_projection_requires_recovery_for_speaker_reviews() -> None:
    chapter, _requests = _source()
    with pytest.raises(B2SlimSpeakerRecoveryError, match="require"):
        build_b2_effective_review_projection_v1(
            chapter_artifact=chapter,
            recovery_artifact=None,
            allowed_candidate_card_ids={"ent_lockwood", "ent_heathcliff"},
        )


def test_no_pending_review_means_no_ticket_and_no_call() -> None:
    chapter, requests = _source()
    body = deepcopy(chapter)
    body.pop("artifact_hash")
    body["review_requests"] = []
    body["speaker_turns"][0]["speaker_authority_status"] = "provisional_resolved"
    body["speaker_turns"][0]["row_status"] = "accepted_observation"
    body["speaker_turns"][0]["speaker"] = {
        "surface": "Mr. Heathcliff",
        "resolution_status": "resolved_candidate",
        "candidate_card_ids": ["ent_heathcliff"],
    }
    index = build_b2_slim_speaker_recovery_index_v1(
        chapter_artifact=_sealed(body, "artifact_hash"), interaction_requests=requests
    )
    assert index["registry_components"] == []
    assert render_b2_slim_speaker_recovery_request_v1(index) is None


def test_registry_component_still_rejects_257_candidate_cards(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    chapter, requests = _source()
    candidate_ids = [f"ent_{index:03d}" for index in range(257)]
    candidate_cards = [
        {
            "candidate_card_id": candidate_id,
            "canonical_surface": f"Candidate {index}",
        }
        for index, candidate_id in enumerate(candidate_ids)
    ]
    request_body = deepcopy(requests[0])
    request_body.pop("request_fingerprint")
    payload = json.loads(request_body["messages"][1]["content"])
    payload["candidate_packets"]["candidate_cards"] = candidate_cards
    request_body["messages"][1]["content"] = json.dumps(payload)
    requests = [_sealed(request_body, "request_fingerprint")]
    chapter_body = deepcopy(chapter)
    chapter_body.pop("artifact_hash")
    chapter_body["review_requests"][0]["candidate_card_ids"] = candidate_ids
    chapter_body["speaker_turns"][0]["speaker"]["candidate_card_ids"] = candidate_ids

    index = build_b2_slim_speaker_recovery_index_v1(
        chapter_artifact=_sealed(chapter_body, "artifact_hash"),
        interaction_requests=requests,
    )
    assert index["registry_components"][0]["overflow"] is True
    assert index["registry_components"][0]["overflow_reasons"] == [
        "candidate_card_cap_exceeded"
    ]
    monkeypatch.setattr(modelapi_runner, "_validate_frozen_db", lambda _path: None)
    monkeypatch.setattr(
        modelapi_runner,
        "load_b2_slim_speaker_source_v1",
        lambda _root: ({}, []),
    )
    monkeypatch.setattr(
        modelapi_runner,
        "build_b2_slim_speaker_recovery_index_v1",
        lambda **_kwargs: index,
    )

    with pytest.raises(SystemExit):
        modelapi_runner._run_canary(
            b2_root=tmp_path,
            output_root=tmp_path,
            capability_root=tmp_path,
            run_id="cap_257",
            attempt_run_id="cap_257_attempt",
            frozen_db=tmp_path / "memory.sqlite3",
            replay_semantic_rejections=[],
            replay_batch_decision_files=[],
            secret="unused",
            commitment="unused",
            scheduler_root=tmp_path,
            current_head="0" * 40,
        )


def test_tampered_source_artifact_fails_closed() -> None:
    chapter, requests = _source()
    chapter["review_requests"][0]["reason"] = "tampered"
    with pytest.raises(B2SlimSpeakerRecoveryError, match="hash mismatch"):
        build_b2_slim_speaker_recovery_index_v1(
            chapter_artifact=chapter, interaction_requests=requests
        )
