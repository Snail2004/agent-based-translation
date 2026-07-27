from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from pipeline.literary.b1_registry_to_b2_input_v1 import (
    build_b2_registry_input_package_v1,
    write_b2_registry_input_package_v1,
)
from pipeline.literary.b3_temporal_context_v1 import (
    B3TemporalContextError,
    _compact_turn,
    _enrich_candidate_cards,
    build_b3_temporal_components_v1,
    build_b3_temporal_phase_a_bundle_v1,
    load_b2_temporal_input_v1,
    load_b3_temporal_profile_v1,
)
from pipeline.literary.b3_temporal_chapter_runner_v1 import B3_MODEL_REF_FIELDS_V1
from pipeline.literary.b3_temporal_contract_v1 import (
    B3TemporalContractError,
    normalize_b3_temporal_response_v1,
)
from pipeline.literary.b3_temporal_phase_a_v1 import (
    dry_render_b3_temporal_phase_a_v1,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json
from pipeline.literary.model_ref_v1 import project_model_request_v1
from pipeline.tests.test_literary_b1_registry_to_b2_input_v1 import (
    _chapter as _registry_chapter,
    _registry as _chapter_registry,
)


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
PROFILE_PATH = (
    RUNTIME_ROOT
    / "pipeline"
    / "configs"
    / "literary_b3_temporal_phase_a_v1.json"
)


def _profile():
    return load_b3_temporal_profile_v1(PROFILE_PATH)


def test_b3_transport_projects_consolidated_corroborating_state_ids() -> None:
    request = {
        "messages": [
            {"role": "system", "content": "test"},
            {
                "role": "user",
                "content": canonical_json(
                    {
                        "prior_state_packets": [
                            {
                                "state": {
                                    "state_id": "b3state1_primary",
                                    "corroborating_state_ids": [
                                        "b3state1_corroborating"
                                    ],
                                }
                            }
                        ]
                    }
                ),
            },
        ],
        "response_schema": {"type": "object", "properties": {}},
        "request_kind": "test",
    }

    projected, ref_map = project_model_request_v1(
        request,
        field_names_by_namespace=B3_MODEL_REF_FIELDS_V1,
    )
    payload = json.loads(projected["messages"][1]["content"])
    state = payload["prior_state_packets"][0]["state"]

    state_labels = {
        row["persistent_id"]: row["local_ref"]
        for row in ref_map["entries"]
        if row["namespace"] == "state"
    }
    assert state["state_id"] == state_labels["b3state1_primary"]
    assert state["corroborating_state_ids"] == [
        state_labels["b3state1_corroborating"]
    ]
    assert set(state_labels) == {"b3state1_primary", "b3state1_corroborating"}


def _card(card_id: str, ref: str, surface: str, *, scope: str = "chapter_confirmed_prefix") -> dict:
    return {
        "candidate_card_id": card_id,
        "referent_ref": ref,
        "referent_ref_kind": "candidate_lineage_ref",
        "identity_scope": scope,
        "source_candidate_id": f"source_{ref}",
        "origin_chapter_id": "book_ch01",
        "canonical_surface": surface,
        "stable_surfaces": [surface],
        "authority_scope": scope,
        "effective_claims_as_of": {
            "referent_kind": "person",
            "referential_gender": None,
            "identity_summary": f"The referent called {surface}.",
        },
        "relevant_claim_transitions": [],
        "uncertainty_flags": [],
        "first_supported_block_id": "book_ch01_b001",
        "provenance_refs": [{"chapter_id": "book_ch01", "block_id": "book_ch01_b001"}],
    }


def _endpoint(surface: str | None, card_ids: list[str], status: str) -> dict:
    return {
        "surface": surface,
        "resolution_status": status,
        "candidate_card_ids": card_ids,
    }


def test_compact_turn_carries_delivery_tone_separately_from_register() -> None:
    turn = {
        "speaker_turn_id": "turn_1",
        "block_id": "book_ch01_b001",
        "utterance_anchor": '"Come in," she mourned.',
        "speaker": _endpoint("Alex Vale", ["card_a"], "resolved_candidate"),
        "addressee": _endpoint("Robin Vale", ["card_b"], "resolved_candidate"),
        "address_terms": [],
        "register_cue": "formal",
        "delivery_tone": "mournful",
        "grounding_status": "grounded",
        "row_status": "accepted_observation",
    }

    compact = _compact_turn(
        turn,
        {"card_a": "ref_a", "card_b": "ref_b"},
    )

    assert compact["register_cue"] == "formal"
    assert compact["delivery_tone"] == "mournful"


def _temporal_input(*, dream: bool = False, pending_turn: bool = False) -> dict:
    cards = [
        _card("card_a", "ref_a", "Alex Vale"),
        _card("card_b", "ref_b", "Robin Vale"),
        _card("card_c", "ref_c", "North House"),
        _card("card_unused", "ref_unused", "Unused Person"),
    ]
    turn_status = "review_required_speaker_attribution" if pending_turn else "accepted_observation"
    grounding = "review_required_unlocatable" if pending_turn else "grounded"
    body = {
        "schema_version": "literary_b3_temporal_input_v1_1",
        "source_b2_run_root": "synthetic",
        "source_b2_seal_hash": "seal",
        "source_b2_artifact_hash": "artifact",
        "source_b2_request_fingerprints": ["request"],
        "source_prefix_bundle_hash": "prefix",
        "source_document_sha256": "doc",
        "chapter_id": "book_ch01",
        "frame_segments": [
            {
                "frame_segment_id": "frame_1",
                "narrator_surface": "Alex Vale",
                "narrator_status": "resolved_candidate",
                "candidate_card_ids": ["card_a"],
                "narrative_mode": "dream_or_vision" if dream else "direct_current",
                "normalization_status": "accepted",
                "covered_block_ids": ["book_ch01_b001", "book_ch01_b002"],
            }
        ],
        "speaker_turns": [
            {
                "speaker_turn_id": "turn_ab",
                "block_id": "book_ch01_b001",
                "utterance_anchor": "I will remain beside you.",
                "speaker": _endpoint("I", ["card_a"], "resolved_candidate"),
                "addressee": _endpoint("you", ["card_b"], "resolved_candidate"),
                "address_terms": [],
                "register_cue": "intimate",
                "grounding_status": grounding,
                "speaker_authority_status": (
                    "pending_review" if pending_turn else "provisional_resolved"
                ),
                "addressee_authority_status": "provisional_resolved",
                "row_status": turn_status,
            }
        ],
        "salient_events": [
            {
                "salient_event_id": "event_plan",
                "source_block_ids": ["book_ch01_b002"],
                "anchor_block_id": "book_ch01_b002",
                "event_anchor": "They planned to marry next spring.",
                "event_kind": "commitment_or_separation",
                "event_scope": "interpersonal",
                "participants": [
                    {
                        "role": "participant",
                        **_endpoint("Alex", ["card_a"], "resolved_candidate"),
                    },
                    {
                        "role": "counterpart",
                        **_endpoint("Robin", ["card_b"], "resolved_candidate"),
                    },
                ],
                "summary": "Alex and Robin plan to marry.",
                "memory_role": "durable_state_change",
                "event_status": "planned",
                "evidence_mode": "directly_narrated",
                "review_status": "resolved",
                "participant_authority_status": "resolved",
                "grounding_status": "grounded",
                "event_authority_status": "non_authoritative_report_or_proposal",
                "row_status": "accepted_observation",
            }
        ],
        "review_requests": [],
        "speaker_recovery_binding": None,
        "source_blocks": [
            {"block_id": "book_ch01_b001", "text": 'Alex said, "I will remain beside you."'},
            {"block_id": "book_ch01_b002", "text": "They planned to marry next spring."},
        ],
        "candidate_cards": cards,
        "candidate_card_to_referent_ref": {
            "card_a": "ref_a",
            "card_b": "ref_b",
            "card_c": "ref_c",
            "card_unused": "ref_unused",
        },
        "identity_lineage_id": "lineage_1",
        "gold_or_oracle_loaded": False,
        "production_publish_performed": False,
    }
    return {**body, "input_hash": canonical_hash(body)}


def _bundle(*, dream: bool = False, pending_turn: bool = False, prior_states=()):
    data = _temporal_input(dream=dream, pending_turn=pending_turn)
    return data, build_b3_temporal_phase_a_bundle_v1(
        temporal_input=data,
        profile=_profile(),
        prior_states=prior_states,
    )


def _two_component_bundle():
    data = deepcopy(_temporal_input())
    data["frame_segments"][0]["covered_block_ids"].append("book_ch01_b003")
    data["speaker_turns"].append(
        {
            "speaker_turn_id": "turn_c",
            "block_id": "book_ch01_b003",
            "utterance_anchor": "The house endures.",
            "speaker": _endpoint(
                "North House", ["card_c"], "resolved_candidate"
            ),
            "addressee": _endpoint(None, [], "no_addressee"),
            "address_terms": [],
            "register_cue": "neutral",
            "grounding_status": "grounded",
            "speaker_authority_status": "provisional_resolved",
            "addressee_authority_status": "not_applicable",
            "row_status": "accepted_observation",
        }
    )
    data["source_blocks"].append(
        {
            "block_id": "book_ch01_b003",
            "text": 'North House said, "The house endures."',
        }
    )
    unsigned = dict(data)
    unsigned.pop("input_hash", None)
    data["input_hash"] = canonical_hash(unsigned)
    return data, build_b3_temporal_phase_a_bundle_v1(
        temporal_input=data,
        profile=_profile(),
        prior_states=[],
    )


def test_b3_intake_keeps_only_timeline_route_reviews() -> None:
    data = deepcopy(_temporal_input())
    data["review_requests"] = [
        {
            "review_id": "route_a",
            "review_kind": "speaker_attribution",
            "blocking_kind": "scene_ambiguity",
            "source_block_ids": ["book_ch01_b001"],
            "candidate_card_ids": ["card_a"],
            "competing_card_ids": [],
            "reason": "Human-facing text is not used for routing.",
            "origin": "model",
            "status": "pending",
        },
        {
            "review_id": "route_d",
            "review_kind": "event_significance",
            "blocking_kind": "timeline_pending",
            "source_block_ids": ["book_ch01_b002"],
            "candidate_card_ids": ["card_a", "card_b"],
            "competing_card_ids": [],
            "reason": "Human-facing text is not used for routing.",
            "origin": "code",
            "status": "pending",
        },
    ]
    components = build_b3_temporal_components_v1(
        temporal_input=data,
        profile=_profile(),
    )
    routed = [
        review
        for component in components
        for review in component["b2_review_requests"]
    ]
    assert routed
    assert {row["review_id"] for row in routed} == {"route_d"}
    assert {row["blocking_kind"] for row in routed} == {"timeline_pending"}


def test_b3_intake_rejects_review_without_typed_route() -> None:
    data = deepcopy(_temporal_input())
    data["review_requests"] = [
        {
            "review_id": "untyped",
            "review_kind": "event_significance",
            "source_block_ids": ["book_ch01_b002"],
            "candidate_card_ids": ["card_a", "card_b"],
            "reason": "This prose must not determine a route.",
        }
    ]
    with pytest.raises(B3TemporalContextError, match="valid typed route"):
        build_b3_temporal_components_v1(
            temporal_input=data,
            profile=_profile(),
        )


def test_b3_intake_rejects_frame_structure_hold() -> None:
    data = deepcopy(_temporal_input())
    data["review_requests"] = [
        {
            "review_id": "route_e",
            "review_kind": "narrator_contract",
            "blocking_kind": "frame_structure",
            "source_block_ids": ["book_ch01_b001"],
            "candidate_card_ids": [],
            "competing_card_ids": [],
            "reason": "Frame structure remains parked.",
            "origin": "code",
            "status": "pending",
        }
    ]
    with pytest.raises(
        B3TemporalContextError,
        match="frame-structure review reached B3 temporal intake",
    ):
        build_b3_temporal_components_v1(
            temporal_input=data,
            profile=_profile(),
        )


def _base_response(request: dict) -> dict:
    return {
        "schema_version": "literary_b3_temporal_response_v1",
        "chapter_id": request["chapter_id"],
        "batch_id": request["batch_id"],
        "component_results": [
            {
                "component_id": component_id,
                "disposition": "no_durable_change",
                "state_actions": [],
                "pending_route": "none",
                "pending_reason": None,
            }
            for component_id in request["component_ids"]
        ],
    }


def _relationship_component(request: dict) -> dict:
    payload = json.loads(request["messages"][1]["content"])
    return next(
        row
        for row in payload["components"]
        if set(row["referent_refs"]) == {"ref_a", "ref_b"}
    )


def _action(
    request: dict,
    *,
    operation: str = "reveal_only",
    domain: str = "relationship",
    value: str = "trusted companions",
    event_status: str = "occurred",
    temporal_position: str = "current_progression",
    use_event: bool = False,
) -> tuple[str, dict]:
    component = _relationship_component(request)
    block_id = "book_ch01_b002" if use_event else "book_ch01_b001"
    return component["component_id"], {
        "operation": operation,
        "state_domain": domain,
        "subject_referent_refs": ["ref_a"],
        "counterpart_referent_refs": ["ref_b"],
        "state_value": value,
        "event_status": event_status,
        "temporal_position": temporal_position,
        "source_event_ids": ["event_plan"] if use_event else [],
        "source_turn_ids": [] if use_event else ["turn_ab"],
        "source_block_ids": [block_id],
        "frame_segment_ids": ["frame_1"],
        "reason": "The supplied evidence supports this temporal interpretation.",
    }


def _response_with_action(request: dict, action: dict, component_id: str) -> dict:
    response = _base_response(request)
    target = next(
        row
        for row in response["component_results"]
        if row["component_id"] == component_id
    )
    target.update(
        {
            "disposition": "state_actions_proposed",
            "state_actions": [action],
        }
    )
    return response


def _semantic_key(domain: str = "relationship") -> str:
    return "b3skey1_" + canonical_hash(
        {
            "state_domain": domain,
            "subject_referent_refs": ["ref_a"],
            "counterpart_referent_refs": ["ref_b"],
        }
    )[:20]


def _prior_state(state_id: str = "state_prior", *, value: str = "trusted companions") -> dict:
    return {
        "state_id": state_id,
        "semantic_key": _semantic_key(),
        "state_domain": "relationship",
        "subject_referent_refs": ["ref_a"],
        "counterpart_referent_refs": ["ref_b"],
        "state_value": value,
        "lifecycle_status": "open",
        "authority_status": "effective",
        "observed_at_block_id": "book_ch00_b001",
        "valid_from_block_id": None,
        "valid_to_block_id": None,
    }


def test_profile_is_chapter_batch_and_zero_api() -> None:
    profile = _profile()
    assert profile.prompt_tokens_per_request == 20_000
    assert profile.output_tokens_per_request == 8_000
    assert profile.safety["phase"] == "phase_a_zero_api"


def test_components_follow_evidence_edges_not_all_entity_pairs() -> None:
    data = _temporal_input()
    components = build_b3_temporal_components_v1(
        temporal_input=data, profile=_profile()
    )
    assert len(components) == 1
    assert components[0]["referent_refs"] == ["ref_a", "ref_b"]
    assert "ref_c" not in components[0]["referent_refs"]
    assert "ref_unused" not in components[0]["referent_refs"]


def test_many_logical_components_share_one_request_when_they_fit() -> None:
    _data, bundle = _bundle()
    assert bundle["plan"]["component_count"] == 1
    assert bundle["plan"]["request_count"] == 1
    assert bundle["requests"][0]["api_eligible"] is False


def test_dialogue_reaches_b3_without_a_salient_event() -> None:
    data = _temporal_input()
    data["salient_events"] = []
    components = build_b3_temporal_components_v1(
        temporal_input=data, profile=_profile()
    )
    assert len(components) == 1
    assert [row["speaker_turn_id"] for row in components[0]["speaker_turns"]] == [
        "turn_ab"
    ]


def test_reveal_only_never_invents_valid_from() -> None:
    _data, bundle = _bundle()
    request = bundle["requests"][0]
    component_id, action = _action(request)
    artifact = normalize_b3_temporal_response_v1(
        request=request,
        response=_response_with_action(request, action, component_id),
    )
    assert len(artifact["new_state_rows"]) == 1
    assert artifact["new_state_rows"][0]["valid_from_block_id"] is None
    assert artifact["new_state_rows"][0]["observed_at_block_id"] == "book_ch01_b001"


def test_planned_event_is_visible_but_not_effective() -> None:
    _data, bundle = _bundle()
    request = bundle["requests"][0]
    component_id, action = _action(
        request,
        operation="open_state",
        value="plan to marry",
        event_status="planned",
        temporal_position="prospective",
        use_event=True,
    )
    artifact = normalize_b3_temporal_response_v1(
        request=request,
        response=_response_with_action(request, action, component_id),
    )
    assert len(artifact["non_effective_observations"]) == 1
    assert artifact["effective_state_projection"] == []


def test_dream_cannot_mutate_current_state() -> None:
    _data, bundle = _bundle(dream=True)
    request = bundle["requests"][0]
    component_id, action = _action(request, operation="open_state")
    artifact = normalize_b3_temporal_response_v1(
        request=request,
        response=_response_with_action(request, action, component_id),
    )
    assert artifact["new_state_rows"] == []
    assert artifact["pending_cases"][0]["reason_codes"] == [
        "dream_or_vision_cannot_mutate_current_state"
    ]


def test_pending_b2_evidence_cannot_create_effective_state() -> None:
    _data, bundle = _bundle(pending_turn=True)
    request = bundle["requests"][0]
    component_id, action = _action(request, operation="open_state")
    artifact = normalize_b3_temporal_response_v1(
        request=request,
        response=_response_with_action(request, action, component_id),
    )
    assert artifact["effective_state_projection"] == []
    assert artifact["pending_cases"][0]["reason_codes"] == [
        "no_authoritative_b2_evidence"
    ]


def test_prior_states_are_filtered_to_component_referents() -> None:
    unrelated = {
        **_prior_state("unrelated"),
        "subject_referent_refs": ["ref_c"],
        "counterpart_referent_refs": [],
    }
    data = _temporal_input()
    components = build_b3_temporal_components_v1(
        temporal_input=data,
        profile=_profile(),
        prior_states=[_prior_state(), unrelated],
    )
    assert [row["state_id"] for row in components[0]["prior_open_states"]] == [
        "state_prior"
    ]


def test_referent_ref_persists_when_the_same_candidate_reappears_later() -> None:
    candidate_cards = {
        "card_mara": {
            "candidate_card_id": "card_mara",
            "canonical_surface": "Mara",
        }
    }

    def prefix(chapter_id: str) -> dict:
        return {
            "state_lineage_id": "lineage_book",
            "b0_context_cards": [],
            "active_context_cards": [
                {
                    "prior_card_id": "card_mara",
                    "source_candidate_id": "entity_mara",
                    "authority_scope": "book",
                    "provenance_refs": [{"chapter_id": chapter_id}],
                }
            ],
            "candidate_only_context_cards": [],
        }

    first = _enrich_candidate_cards(
        candidate_cards=candidate_cards,
        prefix_bundle=prefix("book_ch01"),
        chapter_id="book_ch01",
    )
    second = _enrich_candidate_cards(
        candidate_cards=candidate_cards,
        prefix_bundle=prefix("book_ch02"),
        chapter_id="book_ch02",
    )
    assert first["card_mara"]["referent_ref"] == second["card_mara"]["referent_ref"]
    assert first["card_mara"]["origin_chapter_id"] == "book_ch01"
    assert second["card_mara"]["origin_chapter_id"] == "book_ch02"


def test_change_links_the_unique_predecessor_in_code() -> None:
    _data, bundle = _bundle(prior_states=[_prior_state()])
    request = bundle["requests"][0]
    component_id, action = _action(
        request, operation="change_state", value="estranged companions"
    )
    artifact = normalize_b3_temporal_response_v1(
        request=request,
        response=_response_with_action(request, action, component_id),
    )
    assert artifact["transition_rows"][0]["predecessor_state_id"] == "state_prior"
    assert artifact["transition_rows"][0]["successor_state_id"].startswith("b3state1_")
    assert artifact["closed_prior_state_ids"] == ["state_prior"]


@pytest.mark.parametrize(
    "prior_states,reason",
    [
        ([], "missing_open_predecessor"),
        (
            [_prior_state("prior_1"), _prior_state("prior_2")],
            "multiple_open_predecessors",
        ),
    ],
)
def test_ambiguous_predecessor_becomes_pending(prior_states, reason) -> None:
    _data, bundle = _bundle(prior_states=prior_states)
    request = bundle["requests"][0]
    component_id, action = _action(
        request, operation="change_state", value="estranged companions"
    )
    artifact = normalize_b3_temporal_response_v1(
        request=request,
        response=_response_with_action(request, action, component_id),
    )
    assert artifact["transition_rows"] == []
    assert artifact["pending_cases"][0]["reason_codes"] == [reason]


def test_role_change_routes_to_stable_claim_review() -> None:
    _data, bundle = _bundle()
    request = bundle["requests"][0]
    component_id, action = _action(
        request,
        operation="open_state",
        domain="role",
        value="household steward",
    )
    artifact = normalize_b3_temporal_response_v1(
        request=request,
        response=_response_with_action(request, action, component_id),
    )
    assert artifact["pending_cases"][0]["review_route"] == "stable_claim_review"


def test_same_referent_on_both_sides_is_not_mechanically_rejected() -> None:
    _data, bundle = _bundle()
    request = bundle["requests"][0]
    component_id, action = _action(request, operation="open_state")
    action["counterpart_referent_refs"] = ["ref_a"]
    artifact = normalize_b3_temporal_response_v1(
        request=request,
        response=_response_with_action(request, action, component_id),
    )
    assert artifact["new_state_rows"][0]["counterpart_referent_refs"] == ["ref_a"]


def test_component_exact_cover_is_mandatory() -> None:
    _data, bundle = _bundle()
    request = bundle["requests"][0]
    response = _base_response(request)
    response["component_results"] = []
    with pytest.raises(B3TemporalContractError, match="schema failure|exact-cover"):
        normalize_b3_temporal_response_v1(request=request, response=response)


def test_wrong_chapter_and_batch_echoes_keep_temporal_component_results() -> None:
    _data, bundle = _bundle()
    request = bundle["requests"][0]
    response = _base_response(request)
    response["chapter_id"] = "copied_example_chapter"
    response["batch_id"] = "copied_example_batch"

    artifact = normalize_b3_temporal_response_v1(
        request=request, response=response
    )

    assert artifact["chapter_id"] == request["chapter_id"]
    assert artifact["batch_id"] == request["batch_id"]
    assert len(artifact["component_results"]) == len(request["component_ids"])
    assert [row["field"] for row in artifact["response_normalization_notes"]] == [
        "batch_id",
        "chapter_id",
    ]


def test_foreign_referent_and_block_fail_closed() -> None:
    _data, bundle = _bundle()
    request = bundle["requests"][0]
    component_id, action = _action(request)
    for field, value in (
        ("subject_referent_refs", ["foreign_ref"]),
        ("source_block_ids", ["foreign_block"]),
    ):
        bad = deepcopy(action)
        bad[field] = value
        with pytest.raises(B3TemporalContractError, match="schema failure"):
            normalize_b3_temporal_response_v1(
                request=request,
                response=_response_with_action(request, bad, component_id),
            )


def test_one_component_foreign_referent_quarantines_only_that_action() -> None:
    _data, bundle = _two_component_bundle()
    request = bundle["requests"][0]
    component_id, valid_action = _action(request)
    invalid_action = deepcopy(valid_action)
    invalid_action["counterpart_referent_refs"] = ["ref_c"]
    payload = json.loads(request["messages"][1]["content"])
    second_component = next(
        row for row in payload["components"] if row["referent_refs"] == ["ref_c"]
    )
    second_valid_action = {
        "operation": "reveal_only",
        "state_domain": "world_state",
        "subject_referent_refs": ["ref_c"],
        "counterpart_referent_refs": [],
        "state_value": "continues to endure",
        "event_status": "occurred",
        "temporal_position": "current_progression",
        "source_event_ids": [],
        "source_turn_ids": ["turn_c"],
        "source_block_ids": ["book_ch01_b003"],
        "frame_segment_ids": ["frame_1"],
        "reason": "The supplied statement reveals the continuing state.",
    }
    response = _base_response(request)
    target = next(
        row
        for row in response["component_results"]
        if row["component_id"] == component_id
    )
    target.update(
        {
            "disposition": "state_actions_proposed",
            "state_actions": [invalid_action, valid_action],
        }
    )
    second_target = next(
        row
        for row in response["component_results"]
        if row["component_id"] == second_component["component_id"]
    )
    second_target.update(
        {
            "disposition": "state_actions_proposed",
            "state_actions": [second_valid_action],
        }
    )

    artifact = normalize_b3_temporal_response_v1(
        request=request,
        response=response,
    )

    assert len(artifact["new_state_rows"]) == 2
    assert {
        tuple(row["subject_referent_refs"]) for row in artifact["new_state_rows"]
    } == {("ref_a",), ("ref_c",)}
    assert len(artifact["quarantined_actions"]) == 1
    quarantine = artifact["quarantined_actions"][0]
    assert quarantine["component_id"] == component_id
    assert quarantine["action_ordinal"] == 1
    assert quarantine["reason"] == (
        "B3 action uses a foreign or empty referent set"
    )
    assert quarantine["offending_referent_refs"] == ["ref_c"]
    assert quarantine["semantic_authority_granted"] is False
    normalized_result = next(
        row
        for row in artifact["component_results"]
        if row["component_id"] == component_id
    )
    assert normalized_result["action_application_status"] == (
        "partially_quarantined"
    )
    assert normalized_result["state_actions"] == [valid_action]
    assert normalized_result["quarantined_action_ids"] == [
        quarantine["quarantine_id"]
    ]
    normalized_second = next(
        row
        for row in artifact["component_results"]
        if row["component_id"] == second_component["component_id"]
    )
    assert normalized_second["state_actions"] == [second_valid_action]


def test_one_component_schema_failure_quarantines_only_that_component() -> None:
    _data, bundle = _two_component_bundle()
    request = bundle["requests"][0]
    response = _base_response(request)
    invalid = response["component_results"][0]
    invalid["disposition"] = "reinforce_state"

    artifact = normalize_b3_temporal_response_v1(
        request=request,
        response=response,
    )

    assert len(artifact["quarantined_component_results"]) == 1
    quarantine = artifact["quarantined_component_results"][0]
    assert quarantine["component_id"] == invalid["component_id"]
    assert quarantine["raw_component_result"]["disposition"] == "reinforce_state"
    assert quarantine["reason"].startswith(
        "B3 component result schema failure:"
    )
    assert quarantine["semantic_authority_granted"] is False
    retained = next(
        row
        for row in artifact["component_results"]
        if row["component_id"] != invalid["component_id"]
    )
    assert retained["disposition"] == "no_durable_change"
    assert retained.get("component_application_status") != "quarantined"


def test_all_component_schema_failures_still_fail_the_response() -> None:
    _data, bundle = _two_component_bundle()
    request = bundle["requests"][0]
    response = _base_response(request)
    for result in response["component_results"]:
        result["disposition"] = "reinforce_state"

    with pytest.raises(
        B3TemporalContractError,
        match="B3 response schema failure: all component results are invalid",
    ):
        normalize_b3_temporal_response_v1(
            request=request,
            response=response,
        )


def test_chapter_level_covering_frame_is_accepted_only_for_its_component() -> None:
    data = deepcopy(_temporal_input())
    data["salient_events"][0]["event_kind"] = "identity_or_role_change"
    data["salient_events"][0]["event_status"] = "occurred"
    data["salient_events"][0]["memory_role"] = "relationship_evidence"
    unsigned_input = dict(data)
    unsigned_input.pop("input_hash")
    data["input_hash"] = canonical_hash(unsigned_input)
    bundle = build_b3_temporal_phase_a_bundle_v1(
        temporal_input=data,
        profile=_profile(),
    )
    request = bundle["requests"][0]
    component_id, action = _action(request)
    payload = json.loads(request["messages"][1]["content"])
    component = next(
        row for row in payload["components"] if row["component_id"] == component_id
    )
    assert "frame_segments" not in component
    assert component["frame_segment_ids"] == ["frame_1"]
    frame_packet = next(
        row
        for row in payload["frame_packets"]
        if component_id in row["component_ids"]
    )
    assert action["frame_segment_ids"] == [frame_packet["frame_segment_id"]]

    accepted = normalize_b3_temporal_response_v1(
        request=request,
        response=_response_with_action(request, action, component_id),
    )
    assert accepted["component_results"]

    unrelated = deepcopy(request)
    unrelated_payload = json.loads(unrelated["messages"][1]["content"])
    unrelated_component_id = next(
        row["component_id"]
        for row in unrelated_payload["components"]
        if row["component_id"] != component_id
    )
    for row in unrelated_payload["frame_packets"]:
        if row["frame_segment_id"] == frame_packet["frame_segment_id"]:
            row["component_ids"] = [unrelated_component_id]
            break
    for row in unrelated_payload["components"]:
        if row["component_id"] == component_id:
            row["frame_segment_ids"] = []
        elif row["component_id"] == unrelated_component_id:
            row["frame_segment_ids"] = sorted(
                set(row["frame_segment_ids"]).union(
                    {frame_packet["frame_segment_id"]}
                )
            )
    unrelated["messages"][1]["content"] = canonical_json(unrelated_payload)
    unsigned = dict(unrelated)
    unsigned.pop("request_fingerprint")
    unrelated["request_fingerprint"] = canonical_hash(unsigned)
    quarantined = normalize_b3_temporal_response_v1(
        request=unrelated,
        response=_response_with_action(unrelated, action, component_id),
    )
    assert quarantined["new_state_rows"] == []
    assert len(quarantined["quarantined_actions"]) == 1
    assert quarantined["quarantined_actions"][0]["reason"] == (
        "B3 action frame refs differ from source blocks"
    )
    assert (
        quarantined["quarantined_actions"][0]["semantic_authority_granted"]
        is False
    )

    mismatched_index = deepcopy(request)
    mismatched_payload = json.loads(mismatched_index["messages"][1]["content"])
    target = next(
        row
        for row in mismatched_payload["components"]
        if row["component_id"] == component_id
    )
    target["frame_segment_ids"] = []
    mismatched_index["messages"][1]["content"] = canonical_json(mismatched_payload)
    unsigned = dict(mismatched_index)
    unsigned.pop("request_fingerprint")
    mismatched_index["request_fingerprint"] = canonical_hash(unsigned)
    with pytest.raises(B3TemporalContractError, match="frame index differs"):
        normalize_b3_temporal_response_v1(
            request=mismatched_index,
            response=_base_response(mismatched_index),
        )


def test_tampered_request_and_schema_fail_before_application() -> None:
    _data, bundle = _bundle()
    request = bundle["requests"][0]
    response = _base_response(request)
    bad_request = deepcopy(request)
    bad_request["configured_prompt_cap"] += 1
    with pytest.raises(B3TemporalContractError, match="fingerprint"):
        normalize_b3_temporal_response_v1(request=bad_request, response=response)
    bad_schema = deepcopy(request)
    bad_schema["response_schema"]["properties"]["chapter_id"]["maxLength"] = 999
    unsigned = dict(bad_schema)
    unsigned.pop("request_fingerprint")
    bad_schema["request_fingerprint"] = canonical_hash(unsigned)
    with pytest.raises(B3TemporalContractError, match="schema hash"):
        normalize_b3_temporal_response_v1(request=bad_schema, response=response)


def test_replay_with_identical_inputs_is_content_stable() -> None:
    _data, bundle = _bundle()
    request = bundle["requests"][0]
    component_id, action = _action(request)
    response = _response_with_action(request, action, component_id)
    first = normalize_b3_temporal_response_v1(request=request, response=response)
    second = normalize_b3_temporal_response_v1(request=request, response=response)
    assert first == second


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json(payload) + "\n", encoding="utf-8")


def _with_hash(payload: dict, field: str) -> dict:
    return {**payload, field: canonical_hash(payload)}


def _synthetic_b2_root(tmp_path: Path) -> Path:
    source_root = tmp_path / "b1"
    output = tmp_path / "b2"
    prefix_card = {
        "prior_card_id": "card_a",
        "canonical_surface": "Alex Vale",
        "stable_surfaces": ["Alex Vale"],
        "effective_claims": {
            "referent_kind": "person",
            "referential_gender": None,
            "identity_summary": "A named person.",
        },
        "disputed_claims": [],
        "authority_scope": "chapter_confirmed_prefix",
        "first_supported_block_id": "book_ch01_b001",
        "provenance_refs": [{"chapter_id": "book_ch01", "block_id": "book_ch01_b001"}],
        "source_candidate_id": "source_alex",
        "context_card_hash": "context_hash",
    }
    prefix = _with_hash(
        {
            "state_lineage_id": "lineage_1",
            "b0_context_cards": [prefix_card],
            "active_context_cards": [],
            "candidate_only_context_cards": [],
        },
        "prefix_bundle_hash",
    )
    _write_json(source_root / "artifacts/chapters/ch001/final_prefix.json", prefix)
    rendered_card = {
        "candidate_card_id": "card_a",
        "canonical_surface": "Alex Vale",
        "stable_surfaces": ["Alex Vale"],
        "authority_scope": "chapter_confirmed_prefix",
        "effective_claims_as_of": prefix_card["effective_claims"],
        "relevant_claim_transitions": [],
        "uncertainty_flags": [],
        "first_supported_block_id": "book_ch01_b001",
        "provenance_refs": prefix_card["provenance_refs"],
    }
    packet = _with_hash(
        {"candidate_cards": [rendered_card]}, "packet_hash"
    )
    payload = {
        "chapter_id": "book_ch01",
        "window_id": "window_1",
        "active_blocks": [{"block_id": "book_ch01_b001", "text": 'Alex said, "Hello."'}],
        "candidate_packets": packet,
    }
    request = _with_hash(
        {
            "chapter_id": "book_ch01",
            "window_id": "window_1",
            "messages": [{"role": "user", "content": canonical_json(payload)}],
        },
        "request_fingerprint",
    )
    interaction = _with_hash(
        {"window_id": "window_1"}, "artifact_hash"
    )
    interaction_dir = output / "interactions/01_window_1"
    _write_json(interaction_dir / "request.json", request)
    _write_json(interaction_dir / "interaction_artifact.json", interaction)
    seal = _with_hash(
        {
            "output_root": str(output.resolve()),
            "source_run_root": str(source_root.resolve()),
            "source_prefix_bundle_hash": prefix["prefix_bundle_hash"],
            "chapter_id": "book_ch01",
        },
        "seal_hash",
    )
    artifact = _with_hash(
        {
            "schema_version": "literary_b2_slim_chapter_artifact_v1",
            "run_seal_hash": seal["seal_hash"],
            "chapter_id": "book_ch01",
            "source_document_sha256": "doc",
            "interaction_artifacts": [
                {"window_id": "window_1", "artifact_hash": interaction["artifact_hash"]}
            ],
            "frame_segments": [
                {
                    "frame_segment_id": "frame_1",
                    "candidate_card_ids": ["card_a"],
                    "narrative_mode": "direct_current",
                    "covered_block_ids": ["book_ch01_b001"],
                }
            ],
            "speaker_turns": [
                {
                    "speaker_turn_id": "turn_1",
                    "block_id": "book_ch01_b001",
                    "utterance_anchor": "Hello.",
                    "speaker": _endpoint("Alex", ["card_a"], "resolved_candidate"),
                    "addressee": _endpoint(None, [], "unresolved"),
                    "address_terms": [],
                    "register_cue": "neutral",
                    "grounding_status": "grounded",
                    "row_status": "accepted_observation",
                }
            ],
            "salient_events": [],
            "review_requests": [],
            "active_block_coverage": {
                "covered_block_ids": ["book_ch01_b001"],
                "exact_cover": True,
            },
            "production_publish_performed": False,
        },
        "artifact_hash",
    )
    _write_json(output / "run_seal.json", seal)
    _write_json(output / "chapter_b2_artifact.json", artifact)
    return output


def _synthetic_registry_b2_root(tmp_path: Path) -> Path:
    output = _synthetic_b2_root(tmp_path)
    chapter = _registry_chapter(
        "book_ch01",
        "book_ch01_b001",
        'Alex said, "Hello."',
    )
    package = build_b2_registry_input_package_v1(
        document={"document_id": "book", "chapters": [chapter]},
        chapter_registries=[
            _chapter_registry(
                chapter,
                entity_id="card_a",
                surface="Alex Vale",
            )
        ],
        current_git_head="head_a",
    )
    source_root = tmp_path / "b1_registry"
    write_b2_registry_input_package_v1(output_root=source_root, package=package)
    prefix = package["chapters"][0]["prefix_bundle"]
    seal_path = output / "run_seal.json"
    seal = json.loads(seal_path.read_text(encoding="utf-8"))
    seal_body = {
        **{key: value for key, value in seal.items() if key != "seal_hash"},
        "source_run_root": str(source_root.resolve()),
        "source_prefix_bundle_hash": prefix["prefix_bundle_hash"],
        "source_document_sha256": package["source_document_sha256"],
    }
    seal = _with_hash(seal_body, "seal_hash")
    _write_json(seal_path, seal)
    artifact_path = output / "chapter_b2_artifact.json"
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    artifact_body = {
        **{key: value for key, value in artifact.items() if key != "artifact_hash"},
        "run_seal_hash": seal["seal_hash"],
        "source_document_sha256": package["source_document_sha256"],
    }
    _write_json(artifact_path, _with_hash(artifact_body, "artifact_hash"))
    return output


def _write_speaker_recovery_root(tmp_path: Path, b2_root: Path) -> Path:
    artifact_path = b2_root / "chapter_b2_artifact.json"
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    artifact_body = {
        key: deepcopy(value)
        for key, value in artifact.items()
        if key != "artifact_hash"
    }
    turn = artifact_body["speaker_turns"][0]
    turn["speaker"] = _endpoint(None, [], "unresolved")
    turn["speaker_authority_status"] = "pending_review"
    turn["row_status"] = "review_required_speaker_attribution"
    artifact_body["review_requests"] = [
        {
            "review_id": "review_1",
            "review_kind": "speaker_attribution",
            "blocking_kind": "scene_ambiguity",
            "source_block_ids": ["book_ch01_b001"],
            "candidate_card_ids": ["card_a"],
            "reason": "The local turn lacks an explicit speaker tag.",
            "status": "pending",
        }
    ]
    artifact = _with_hash(artifact_body, "artifact_hash")
    _write_json(artifact_path, artifact)

    overlay_body = {
        "speaker_turn_id": turn["speaker_turn_id"],
        "endpoint_role": "speaker",
        "source_frame_segment_id": "frame_1",
        "ticket_id": "ticket_1",
        "ticket_ids": ["ticket_1"],
        "ticket_resolutions": [
            {
                "ticket_id": "ticket_1",
                "action": "attach_existing",
                "source_block_ids": ["book_ch01_b001"],
                "pending_reason": None,
                "narrowed_candidate_card_ids": [],
                "resolution_note": "The bounded source identifies Alex as the speaker.",
            }
        ],
        "source_turn_snapshot_hash": canonical_hash(turn),
        "original_endpoint": deepcopy(turn["speaker"]),
        "original_speaker": deepcopy(turn["speaker"]),
        "action": "attach_existing",
        "effective_endpoint": {
            "surface": "Alex Vale",
            "resolution_status": "resolved_candidate",
            "candidate_card_ids": ["card_a"],
            "resolution_basis": "speaker_recovery_auditor",
        },
        "effective_speaker": {
            "surface": "Alex Vale",
            "resolution_status": "resolved_candidate",
            "candidate_card_ids": ["card_a"],
            "resolution_basis": "speaker_recovery_auditor",
        },
        "narrowed_candidate_card_ids": [],
        "authority_status": "auditor_confirmed_chapter_local",
        "source_block_ids": ["book_ch01_b001"],
        "resolution_note": "The bounded source identifies Alex as the speaker.",
    }
    overlay = {
        "overlay_id": "b2endov1_" + canonical_hash(overlay_body)[:20],
        **overlay_body,
    }
    recovery_body = {
        "schema_version": "literary_b2_slim_speaker_recovery_artifact_v1",
        "chapter_id": "book_ch01",
        "source_b2_artifact_hash": artifact["artifact_hash"],
        "recovery_index_hash": "index_hash",
        "batch_decision_hash": "decision_hash",
        "speaker_overlays": [overlay],
        "addressee_overlays": [],
        "unresolved_ambiguities": [],
        "quarantined_ticket_actions": [],
        "review_dispositions": [
            {
                "review_id": "review_1",
                "ticket_id": "ticket_1",
                "ticket_ids": ["ticket_1"],
                "status": "resolved",
                "decision_action": "attach_existing",
                "narrowed_candidate_card_ids": [],
                "frame_segment_id": "frame_1",
            }
        ],
        "ticketed_speaker_turn_ids": [turn["speaker_turn_id"]],
        "accepted_turn_reinspection_performed": False,
        "unticketed_turn_mutation_performed": False,
        "source_artifact_mutated": False,
        "identity_or_claim_mutation_performed": False,
        "book_global_authority_granted": False,
        "translation_performed": False,
        "production_publish_performed": False,
    }
    recovery = _with_hash(recovery_body, "artifact_hash")
    report_body = {
        "schema_version": "literary_b2_speaker_recovery_canary_report_v1",
        "status": "semantic_accepted",
        "chapter_id": "book_ch01",
        "source_b2_artifact_hash": artifact["artifact_hash"],
        "speaker_recovery_artifact_hash": recovery["artifact_hash"],
        "mandatory_stop_observed": True,
        "accepted_turn_reinspection_performed": False,
        "unticketed_turn_mutation_performed": False,
        "source_artifact_mutated": False,
        "identity_or_claim_mutation_performed": False,
        "book_global_authority_granted": False,
        "production_publish_performed": False,
    }
    root = tmp_path / "speaker_recovery"
    _write_json(root / "speaker_recovery_artifact.json", recovery)
    _write_json(root / "canary_report.json", _with_hash(report_body, "report_hash"))
    return root


def test_real_input_loader_binds_prefix_and_rejects_hash_tamper(tmp_path: Path) -> None:
    root = _synthetic_b2_root(tmp_path)
    loaded = load_b2_temporal_input_v1(root)
    assert loaded["candidate_cards"][0]["referent_ref"].startswith("litref1_")
    artifact_path = root / "chapter_b2_artifact.json"
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    artifact["chapter_id"] = "foreign_chapter"
    _write_json(artifact_path.with_name("tampered.json"), artifact)
    artifact_path.write_text(canonical_json(artifact) + "\n", encoding="utf-8")
    with pytest.raises(B3TemporalContextError, match="hash mismatch"):
        load_b2_temporal_input_v1(root)


def test_registry_prefix_and_ticketed_speaker_overlay_feed_b3_without_identity_merge(
    tmp_path: Path,
) -> None:
    root = _synthetic_registry_b2_root(tmp_path)
    recovery_root = _write_speaker_recovery_root(tmp_path, root)
    loaded = load_b2_temporal_input_v1(
        root,
        speaker_recovery_root=recovery_root,
    )
    card = loaded["candidate_cards"][0]
    turn = loaded["speaker_turns"][0]
    assert loaded["schema_version"] == "literary_b3_temporal_input_v1_1"
    assert card["source_candidate_id"] == "card_a"
    assert card["referent_ref"].startswith("litref1_")
    assert turn["speaker"]["candidate_card_ids"] == ["card_a"]
    assert turn["speaker_recovery_authority_status"] == (
        "auditor_confirmed_chapter_local"
    )
    assert loaded["review_requests"] == []
    assert loaded["speaker_recovery_binding"]["attached_turn_count"] == 1
    assert loaded["speaker_recovery_binding"][
        "identity_or_book_authority_granted"
    ] is False
    components = build_b3_temporal_components_v1(
        temporal_input=loaded,
        profile=_profile(),
    )
    compact_turn = next(
        row
        for component in components
        for row in component["speaker_turns"]
        if row["speaker_turn_id"] == turn["speaker_turn_id"]
    )
    assert compact_turn["evidence_authority"] == (
        "auditor_confirmed_chapter_local"
    )


def test_speaker_overlay_tamper_fails_before_b3_context(tmp_path: Path) -> None:
    root = _synthetic_registry_b2_root(tmp_path)
    recovery_root = _write_speaker_recovery_root(tmp_path, root)
    artifact_path = recovery_root / "speaker_recovery_artifact.json"
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    artifact["speaker_overlays"][0]["effective_speaker"][
        "candidate_card_ids"
    ] = ["foreign_card"]
    artifact_body = {key: value for key, value in artifact.items() if key != "artifact_hash"}
    _write_json(artifact_path, _with_hash(artifact_body, "artifact_hash"))
    with pytest.raises(B3TemporalContextError, match="overlay id differs|foreign"):
        load_b2_temporal_input_v1(root, speaker_recovery_root=recovery_root)


def test_phase_a_binds_speaker_recovery_tree_without_mutating_sources(
    tmp_path: Path,
) -> None:
    root = _synthetic_registry_b2_root(tmp_path)
    recovery_root = _write_speaker_recovery_root(tmp_path, root)
    before = {
        str(path): path.read_bytes()
        for source in (root, recovery_root)
        for path in source.rglob("*")
        if path.is_file()
    }
    output = tmp_path / "phase_a_recovery"
    report = dry_render_b3_temporal_phase_a_v1(
        b2_run_roots=[root],
        speaker_recovery_roots=[recovery_root],
        output_root=output,
        profile=_profile(),
    )
    after = {
        str(path): path.read_bytes()
        for source in (root, recovery_root)
        for path in source.rglob("*")
        if path.is_file()
    }
    manifest = json.loads(
        (output / "chapters/book_ch01/input_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert before == after
    assert report["api_calls_performed"] == 0
    assert manifest["speaker_recovery_binding"]["attached_turn_count"] == 1


def test_phase_a_writer_is_zero_api_and_does_not_mutate_source(tmp_path: Path) -> None:
    root = _synthetic_b2_root(tmp_path)
    before = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }
    output = tmp_path / "phase_a"
    report = dry_render_b3_temporal_phase_a_v1(
        b2_run_roots=[root],
        output_root=output,
        profile=_profile(),
    )
    after = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }
    assert before == after
    assert report["api_calls_performed"] == 0
    assert report["source_artifact_mutated"] is False
    assert (output / "phase_a_plan.json").is_file()
