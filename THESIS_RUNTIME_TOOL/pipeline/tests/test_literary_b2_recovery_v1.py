from __future__ import annotations

from copy import deepcopy
import hashlib
import json

import pytest

from pipeline.literary.b2_context_v1 import B2_REQUEST_SCHEMA_VERSION
from pipeline.literary.b2_live_canary_v1 import CHAPTER_ARTIFACT_SCHEMA_VERSION
from pipeline.literary.b2_prompts_v2 import (
    B2_INTERACTION_PROMPT_ID_V2,
    B2_INTERACTION_SYSTEM_PROMPT_V2,
    b2_interaction_response_schema_v2,
)
from pipeline.literary.b2_recovery_prompts_v1 import (
    EVENT_REVIEW_SYSTEM_PROMPT_V1,
    REGISTRY_RECOVERY_SYSTEM_PROMPT_V1,
)
from pipeline.literary.b2_event_authority_prompts_v2 import (
    EVENT_REVIEW_SYSTEM_PROMPT_V2,
)
from pipeline.literary.b2_recovery_v1 import (
    B2RecoveryContractError,
    build_b2_recovery_index_v1,
    build_effective_b2_projection_v1,
    build_effective_b2_projection_v2,
    build_event_revision_ledger_v1,
    build_event_revision_ledger_v2,
    build_registry_recovery_ledger_v1,
    classify_recovery_reopen_v1,
    _relation_projection_status_v2,
    overlay_b2_rows_with_registry_recovery_v1,
    render_event_review_request_v1,
    render_event_review_request_v2,
    render_registry_recovery_request_v1,
    validate_event_review_response_v1,
    validate_event_review_response_v2,
    validate_registry_recovery_response_v1,
    verify_b2_recovery_index_v1,
    verify_event_review_decision_v2,
    verify_event_revision_ledger_v2,
    verify_registry_recovery_decision_v1,
    verify_registry_recovery_ledger_v1,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json


def _card(card_id: str, surface: str) -> dict:
    return {
        "candidate_card_id": card_id,
        "canonical_surface": surface,
        "stable_surfaces": [surface],
        "authority_scope": "chapter_confirmed_prefix",
        "effective_claims_as_of": {
            "identity_summary": f"The person named {surface}.",
            "referent_kind": "person",
            "referential_gender": None,
        },
        "first_supported_block_id": "ch1_b1",
        "provenance_refs": [{"chapter_id": "ch1", "block_id": "ch1_b1"}],
        "relevant_claim_transitions": [],
        "uncertainty_flags": [],
    }


def _endpoint(
    surface: str | None,
    *,
    status: str,
    card_ids: list[str] | None = None,
    reference_form: str = "descriptor",
) -> dict:
    ids = list(card_ids or [])
    basis = "unknown"
    if status == "resolved_candidate":
        basis = "explicit_name"
    elif status == "resolved_joint_candidates":
        basis = "group_expression"
    return {
        "surface": surface,
        "reference_form": reference_form,
        "resolution_status": status,
        "candidate_card_ids": ids,
        "resolution_basis": basis,
    }


def _event(
    event_id: str,
    block_id: str,
    anchor: str,
    summary: str,
    actor: dict,
    target: dict,
) -> dict:
    return {
        "interaction_event_id": event_id,
        "block_id": block_id,
        "event_anchor": anchor,
        "actor": actor,
        "target": target,
        "interaction_kind": "physical_contact",
        "action_summary": summary,
        "observed_valence": "neutral",
        "source_spans": [{"char_start": 0, "char_end": len(anchor)}],
        "grounding_status": "grounded",
        "row_status": "accepted_observation",
    }


def _fixture() -> tuple[dict, list[dict]]:
    blocks = [
        {
            "block_id": "ch1_b1",
            "block_type": "paragraph",
            "text": "Robin set a bowl before the hound.",
        },
        {
            "block_id": "ch1_b2",
            "block_type": "paragraph",
            "text": "The hound sprang at Vale. Vale pushed it away.",
        },
        {
            "block_id": "ch1_b3",
            "block_type": "paragraph",
            "text": "Mara entered and calmed the hound while Vale and Robin watched.",
        },
    ]
    cards = [
        _card("card_robin", "Robin"),
        _card("card_vale", "Vale"),
        _card("card_mara", "Mara"),
    ]
    packet_body = {
        "schema_version": "literary_b2_candidate_packet_v1",
        "chapter_id": "ch1",
        "active_block_ids": [row["block_id"] for row in blocks],
        "preceding_tail_block_ids": [],
        "candidate_cards": cards,
        "surface_groups": [],
        "identity_uncertainties": [],
        "prefix_bundle_hash": "a" * 64,
        "claim_transition_coverage": "not_available_in_prefix_v1",
        "overflow": False,
        "overflow_reasons": [],
    }
    packet = {**packet_body, "packet_hash": canonical_hash(packet_body)}
    payload = {
        "request_kind": "window_interaction",
        "chapter_id": "ch1",
        "window_id": "w1",
        "active_blocks": blocks,
        "preceding_tail": [],
        "frame_context_status": "ready",
        "frame_context": {},
        "candidate_packets": packet,
        "prior_relation_states": [],
    }
    schema = b2_interaction_response_schema_v2()
    request_body = {
        "schema_version": B2_REQUEST_SCHEMA_VERSION,
        "request_kind": "window_interaction",
        "prompt_id": B2_INTERACTION_PROMPT_ID_V2,
        "prompt_sha256": hashlib.sha256(
            B2_INTERACTION_SYSTEM_PROMPT_V2.encode("utf-8")
        ).hexdigest(),
        "chapter_id": "ch1",
        "window_id": "w1",
        "messages": [
            {"role": "system", "content": B2_INTERACTION_SYSTEM_PROMPT_V2},
            {"role": "user", "content": canonical_json(payload)},
        ],
        "response_schema": schema,
        "response_schema_hash": canonical_hash(schema),
        "token_reserve": {},
        "configured_prompt_cap": 16000,
        "dependency_status": "ready",
        "api_eligible": False,
        "api_ineligible_reasons": ["test"],
        "context_hashes": {
            "candidate_packet_hash": packet["packet_hash"],
            "window_hash": "b" * 64,
        },
        "production_publish_performed": False,
    }
    request = {
        **request_body,
        "request_fingerprint": canonical_hash(request_body),
    }
    events = [
        _event(
            "event_1",
            "ch1_b1",
            "Robin set a bowl before the hound",
            "Robin gives a bowl to the hound.",
            _endpoint(
                "Robin",
                status="resolved_candidate",
                card_ids=["card_robin"],
                reference_form="proper_name",
            ),
            _endpoint("the hound", status="unresolved"),
        ),
        _event(
            "event_2",
            "ch1_b2",
            "The hound sprang at Vale. Vale pushed it away",
            "The hound attacks Vale, who pushes it away.",
            _endpoint("The hound", status="unresolved"),
            _endpoint(
                "Vale",
                status="resolved_candidate",
                card_ids=["card_vale"],
                reference_form="proper_name",
            ),
        ),
        _event(
            "event_3",
            "ch1_b3",
            "Mara entered and calmed the hound while Vale and Robin watched",
            "Mara calms the hound.",
            _endpoint(
                "Mara",
                status="resolved_candidate",
                card_ids=["card_mara"],
                reference_form="proper_name",
            ),
            _endpoint(
                "Vale and Robin",
                status="resolved_joint_candidates",
                card_ids=["card_vale", "card_robin"],
                reference_form="group",
            ),
        ),
    ]
    artifact_body = {
        "schema_version": CHAPTER_ARTIFACT_SCHEMA_VERSION,
        "chapter_id": "ch1",
        "interaction_artifacts": [{"window_id": "w1", "artifact_hash": "c" * 64}],
        "speaker_turns": [],
        "interaction_events": events,
        "review_requests": [],
        "identity_or_claim_mutation_performed": False,
    }
    artifact = {**artifact_body, "artifact_hash": canonical_hash(artifact_body)}
    return artifact, [request]


def _registry_decision(index: dict) -> dict:
    component = index["registry_components"][0]
    rendered = render_registry_recovery_request_v1(
        index=index, component_id=component["component_id"]
    )
    actions = []
    for ticket in index["registry_gap_tickets"]:
        actions.append(
            {
                "ticket_id": ticket["ticket_id"],
                "action": "create_chapter_local",
                "target_candidate_card_id": None,
                "provisional_group_key": "hound_in_chapter",
                "canonical_surface": "the hound",
                "referent_kind": "animal",
                "identity_summary": "The hound involved in the chapter interactions.",
                "source_block_ids": sorted(
                    set(ticket["source_block_ids"] + ["ch1_b1"])
                ),
                "pending_reason": None,
                "resolution_note": "The supplied source supports one recurring hound.",
            }
        )
    response = {
        "schema_version": "literary_b2_registry_recovery_response_v1",
        "chapter_id": "ch1",
        "component_id": component["component_id"],
        "ticket_actions": actions,
    }
    return validate_registry_recovery_response_v1(
        response,
        index=index,
        component_id=component["component_id"],
        request_fingerprint=rendered.request_fingerprint,
    )


def test_zero_registry_gaps_builds_an_empty_authority_ledger() -> None:
    artifact, requests = _fixture()
    resolved = deepcopy(artifact)
    resolved["interaction_events"][0]["target"] = _endpoint(
        "Vale",
        status="resolved_candidate",
        card_ids=["card_vale"],
        reference_form="proper_name",
    )
    resolved["interaction_events"][1]["actor"] = _endpoint(
        "Robin",
        status="resolved_candidate",
        card_ids=["card_robin"],
        reference_form="proper_name",
    )
    body = dict(resolved)
    body.pop("artifact_hash")
    resolved["artifact_hash"] = canonical_hash(body)

    index = build_b2_recovery_index_v1(
        chapter_artifact=resolved,
        interaction_requests=requests,
    )
    assert index["registry_components"] == []
    assert index["registry_gap_tickets"] == []

    ledger = build_registry_recovery_ledger_v1(index=index, decisions=[])
    verified = verify_registry_recovery_ledger_v1(ledger, index=index)
    assert verified["decisions"] == []
    assert verified["local_candidate_cards"] == []
    assert verified["ticket_resolutions"] == []


def test_one_local_referent_preserves_multiple_source_surfaces() -> None:
    artifact, requests = _fixture()
    index = build_b2_recovery_index_v1(
        chapter_artifact=artifact, interaction_requests=requests
    )
    component = index["registry_components"][0]
    rendered = render_registry_recovery_request_v1(
        index=index, component_id=component["component_id"]
    )
    actions = [
        {
            "ticket_id": ticket["ticket_id"],
            "action": "create_chapter_local",
            "target_candidate_card_id": None,
            "provisional_group_key": "one_hound_multiple_surfaces",
            "canonical_surface": ticket["observed_surface"],
            "referent_kind": "animal",
            "identity_summary": "The hound involved in the chapter interactions.",
            "source_block_ids": ticket["source_block_ids"],
            "pending_reason": None,
            "resolution_note": "The evidence supports one chapter-local hound.",
        }
        for ticket in index["registry_gap_tickets"]
    ]
    decision = validate_registry_recovery_response_v1(
        {
            "schema_version": "literary_b2_registry_recovery_response_v1",
            "chapter_id": "ch1",
            "component_id": component["component_id"],
            "ticket_actions": actions,
        },
        index=index,
        component_id=component["component_id"],
        request_fingerprint=rendered.request_fingerprint,
    )
    ledger = build_registry_recovery_ledger_v1(
        index=index, decisions=[decision]
    )
    card = ledger["local_candidate_cards"][0]

    assert card["canonical_surface"] == "the hound"
    assert card["stable_surfaces"] == []
    assert {row["surface"] for row in card["local_surface_evidence"]} == {
        "the hound",
        "The hound",
    }


def test_contextual_endpoint_may_create_a_missing_chapter_local_referent() -> None:
    artifact, requests = _fixture()
    index = build_b2_recovery_index_v1(
        chapter_artifact=artifact, interaction_requests=requests
    )
    ticket = index["registry_gap_tickets"][0]
    ticket["issue_kind"] = "contextual_speaker_attribution"
    body = deepcopy(index)
    body.pop("recovery_index_hash")
    index["recovery_index_hash"] = canonical_hash(body)
    component = index["registry_components"][0]
    rendered = render_registry_recovery_request_v1(
        index=index, component_id=component["component_id"]
    )
    response = {
        "schema_version": "literary_b2_registry_recovery_response_v1",
        "chapter_id": "ch1",
        "component_id": component["component_id"],
        "ticket_actions": [
            {
                "ticket_id": row["ticket_id"],
                "action": "create_chapter_local",
                "target_candidate_card_id": None,
                "provisional_group_key": "missing_local_speaker",
                "canonical_surface": row["observed_surface"],
                "referent_kind": "animal",
                "identity_summary": "The locally observed speaker.",
                "source_block_ids": row["source_block_ids"],
                "pending_reason": None,
                "resolution_note": "The speaker is present but absent from the cards.",
            }
            for row in index["registry_gap_tickets"]
        ],
    }
    accepted = validate_registry_recovery_response_v1(
        response,
        index=index,
        component_id=component["component_id"],
        request_fingerprint=rendered.request_fingerprint,
    )
    assert all(
        row["action"] == "create_chapter_local"
        for row in accepted["ticket_actions"]
    )


def test_registry_wrong_chapter_echo_survives_decision_replay() -> None:
    artifact, requests = _fixture()
    index = build_b2_recovery_index_v1(
        chapter_artifact=artifact, interaction_requests=requests
    )
    component = index["registry_components"][0]
    rendered = render_registry_recovery_request_v1(
        index=index, component_id=component["component_id"]
    )
    response = {
        "schema_version": "literary_b2_registry_recovery_response_v1",
        "chapter_id": "copied_example_chapter",
        "component_id": component["component_id"],
        "ticket_actions": [
            {
                "ticket_id": ticket["ticket_id"],
                "action": "keep_pending",
                "target_candidate_card_id": None,
                "narrowed_candidate_card_ids": [],
                "provisional_group_key": None,
                "canonical_surface": None,
                "referent_kind": None,
                "identity_summary": None,
                "source_block_ids": ticket["source_block_ids"],
                "pending_reason": "The supplied context is insufficient.",
                "resolution_note": "The supplied context is insufficient.",
            }
            for ticket in index["registry_gap_tickets"]
        ],
    }

    decision = validate_registry_recovery_response_v1(
        response,
        index=index,
        component_id=component["component_id"],
        request_fingerprint=rendered.request_fingerprint,
    )

    assert decision["response_normalization_notes"][0]["field"] == "chapter_id"
    assert verify_registry_recovery_decision_v1(decision, index=index) == decision


def _resolved_endpoint(card_id: str, surface: str) -> dict:
    return {
        "surface": surface,
        "reference_form": "descriptor",
        "resolution_status": "resolved_candidate",
        "candidate_card_ids": [card_id],
        "resolution_basis": "registry_recovery",
    }


def _event_decision(
    artifact: dict, index: dict, registry_ledger: dict
) -> dict:
    component = index["event_components"][0]
    rendered = render_event_review_request_v1(
        index=index,
        component_id=component["component_id"],
        chapter_artifact=artifact,
        registry_ledger=registry_ledger,
    )
    local_card_id = registry_ledger["local_candidate_cards"][0][
        "candidate_card_id"
    ]
    cases = {
        row["interaction_event_id"]: row["case_id"]
        for row in index["event_review_cases"]
    }
    actions = [
        {
            "case_id": cases["event_1"],
            "action": "keep",
            "replacement_events": [],
            "source_block_ids": ["ch1_b1"],
            "pending_reason": None,
            "resolution_note": "The directed transfer is source-supported.",
        },
        {
            "case_id": cases["event_2"],
            "action": "split",
            "replacement_events": [
                {
                    "block_id": "ch1_b2",
                    "event_anchor": "The hound sprang at Vale",
                    "actor": _resolved_endpoint(local_card_id, "The hound"),
                    "target": _endpoint(
                        "Vale",
                        status="resolved_candidate",
                        card_ids=["card_vale"],
                        reference_form="proper_name",
                    ),
                    "interaction_kind": "conflict_or_hostility",
                    "action_summary": "The hound springs at Vale.",
                    "observed_valence": "negative",
                },
                {
                    "block_id": "ch1_b2",
                    "event_anchor": "Vale pushed it away",
                    "actor": _endpoint(
                        "Vale",
                        status="resolved_candidate",
                        card_ids=["card_vale"],
                        reference_form="proper_name",
                    ),
                    "target": _resolved_endpoint(local_card_id, "it"),
                    "interaction_kind": "conflict_or_hostility",
                    "action_summary": "Vale pushes the hound away.",
                    "observed_valence": "negative",
                },
            ],
            "source_block_ids": ["ch1_b2"],
            "pending_reason": None,
            "resolution_note": "The anchor contains actions in opposite directions.",
        },
        {
            "case_id": cases["event_3"],
            "action": "revise",
            "replacement_events": [
                {
                    "block_id": "ch1_b3",
                    "event_anchor": "Mara entered and calmed the hound",
                    "actor": _endpoint(
                        "Mara",
                        status="resolved_candidate",
                        card_ids=["card_mara"],
                        reference_form="proper_name",
                    ),
                    "target": _resolved_endpoint(local_card_id, "the hound"),
                    "interaction_kind": "coercion_or_control",
                    "action_summary": "Mara calms the hound.",
                    "observed_valence": "positive",
                }
            ],
            "source_block_ids": ["ch1_b3"],
            "pending_reason": None,
            "resolution_note": "The supplied group merely watches and is not the target.",
        },
    ]
    response = {
        "schema_version": "literary_b2_event_review_response_v1",
        "chapter_id": "ch1",
        "component_id": component["component_id"],
        "event_actions": actions,
    }
    return validate_event_review_response_v1(
        response,
        index=index,
        component_id=component["component_id"],
        chapter_artifact=artifact,
        registry_ledger=registry_ledger,
        request_fingerprint=rendered.request_fingerprint,
    )


def _event_response_v2(
    artifact: dict, index: dict, registry_ledger: dict
) -> tuple[dict, object]:
    base = _event_decision(artifact, index, registry_ledger)
    actions = []
    for raw in base["event_actions"]:
        action = deepcopy(raw)
        action.pop("review_input_event_hash", None)
        for replacement in action["replacement_events"]:
            replacement.pop("source_spans", None)
            replacement.pop("grounding_status", None)
            replacement.pop("row_status", None)
        effective_count = {
            "keep": 1,
            "revise": 1,
            "split": len(action["replacement_events"]),
            "pending": 0,
            "reject": 0,
        }[action["action"]]
        action["effective_event_assessments"] = [
            {
                "directionality": "one_way",
                "actuality": "occurred",
                "endpoint_status": "resolved",
            }
            for _ in range(effective_count)
        ]
        actions.append(action)
    component = index["event_components"][0]
    rendered = render_event_review_request_v2(
        index=index,
        component_id=component["component_id"],
        chapter_artifact=artifact,
        registry_ledger=registry_ledger,
    )
    return (
        {
            "schema_version": "literary_b2_event_review_response_v2",
            "chapter_id": "ch1",
            "component_id": component["component_id"],
            "event_actions": actions,
        },
        rendered,
    )


def _event_decision_v2(
    artifact: dict, index: dict, registry_ledger: dict
) -> dict:
    response, rendered = _event_response_v2(artifact, index, registry_ledger)
    return validate_event_review_response_v2(
        response,
        index=index,
        component_id=response["component_id"],
        chapter_artifact=artifact,
        registry_ledger=registry_ledger,
        request_fingerprint=rendered.request_fingerprint,
    )


def test_prompts_are_book_neutral_and_lock_the_two_authority_paths() -> None:
    combined = (
        REGISTRY_RECOVERY_SYSTEM_PROMPT_V1 + EVENT_REVIEW_SYSTEM_PROMPT_V1
    ).lower()
    compact = " ".join(combined.split())
    assert "wuthering" not in combined
    assert "heathcliff" not in combined
    assert "candidate cards are possible referents, not answers" in compact
    assert "contextual_speaker_attribution" in compact
    assert "a nearby reaction" in compact
    assert "one effective event represents one directed" in compact
    assert "chapter-local recovery card is not a global entity" in compact


def test_event_v2_prompt_is_book_neutral_and_preserves_real_self_action() -> None:
    compact = " ".join(EVENT_REVIEW_SYSTEM_PROMPT_V2.lower().split())
    assert "wuthering" not in compact
    assert "heathcliff" not in compact
    assert "genuine self-directed action must be kept" in compact
    assert "keep, pending, or reject: return an empty replacement_events" in compact
    assert "for keep, assess the supplied event itself" in compact
    assert "pending or reject: an empty effective_event_assessments" in compact
    assert "reported" in compact
    assert "hypothetical or negated" in compact


def test_index_exact_covers_every_gap_endpoint_and_event() -> None:
    artifact, requests = _fixture()
    index = build_b2_recovery_index_v1(
        chapter_artifact=artifact, interaction_requests=requests
    )
    assert index["counts"]["registry_gap_tickets"] == 2
    assert index["counts"]["event_review_cases"] == 3
    assert {
        row["endpoint_role"] for row in index["registry_gap_tickets"]
    } == {"actor", "target"}
    assert all(row["authority_effect"] == "none" for row in (
        index["registry_gap_tickets"] + index["event_review_cases"]
    ))
    assert verify_b2_recovery_index_v1(index) == index


@pytest.mark.parametrize(
    "speaker_authority_status",
    ["provisional_contextual", "pending_review"],
)
@pytest.mark.parametrize(
    "registry_action",
    ["attach_existing", "keep_pending"],
)
def test_contextual_single_candidate_speaker_enters_registry_review(
    speaker_authority_status: str,
    registry_action: str,
) -> None:
    artifact, requests = _fixture()
    artifact["speaker_turns"] = [
        {
            "speaker_turn_id": "turn_contextual_1",
            "block_id": "ch1_b2",
            "utterance_anchor": "Vale pushed it away",
            "speaker": _endpoint(
                "Vale",
                status="resolved_candidate",
                card_ids=["card_vale"],
                reference_form="proper_name",
            ),
            "addressee": _endpoint(
                "Robin",
                status="resolved_candidate",
                card_ids=["card_robin"],
                reference_form="proper_name",
            ),
            "speaker_support": {
                "source_block_id": "ch1_b2",
                "support_anchor": "Vale pushed it away",
                "support_kind": "nearby_context",
                "source_spans": [{"char_start": 26, "char_end": 45}],
                "grounding_status": "grounded",
            },
            "address_terms": [],
            "speech_function": "statement",
            "register_cue": "neutral",
            "source_spans": [{"char_start": 26, "char_end": 45}],
            "grounding_status": "grounded",
            "row_status": (
                "review_required_speaker_attribution"
                if speaker_authority_status == "pending_review"
                else "accepted_observation"
            ),
            "speaker_authority_status": speaker_authority_status,
        }
    ]
    body = dict(artifact)
    body.pop("artifact_hash")
    artifact["artifact_hash"] = canonical_hash(body)

    index = build_b2_recovery_index_v1(
        chapter_artifact=artifact,
        interaction_requests=requests,
    )
    contextual = [
        row
        for row in index["registry_gap_tickets"]
        if row["issue_kind"] == "contextual_speaker_attribution"
    ]

    assert len(contextual) == 1
    assert contextual[0]["source_row_id"] == "turn_contextual_1"
    assert contextual[0]["endpoint_role"] == "speaker"
    assert contextual[0]["candidate_card_ids"] == ["card_vale"]
    request = render_registry_recovery_request_v1(
        index=index,
        component_id=index["registry_components"][0]["component_id"],
    )
    rendered_ticket = next(
        row
        for row in request.semantic_payload["tickets"]
        if row["ticket_id"] == contextual[0]["ticket_id"]
    )
    assert rendered_ticket["issue_kind"] == "contextual_speaker_attribution"

    actions = []
    for ticket in index["registry_gap_tickets"]:
        is_contextual = ticket["ticket_id"] == contextual[0]["ticket_id"]
        actions.append(
            {
                "ticket_id": ticket["ticket_id"],
                "action": registry_action if is_contextual else "reject_non_registry",
                "target_candidate_card_id": (
                    "card_vale"
                    if is_contextual and registry_action == "attach_existing"
                    else None
                ),
                **(
                    {"narrowed_candidate_card_ids": []}
                    if is_contextual and registry_action == "keep_pending"
                    else {}
                ),
                "provisional_group_key": None,
                "canonical_surface": None,
                "referent_kind": None,
                "identity_summary": None,
                "source_block_ids": ticket["source_block_ids"],
                "pending_reason": (
                    "The local context does not settle the speaker."
                    if is_contextual and registry_action == "keep_pending"
                    else None
                ),
                "resolution_note": (
                    (
                        "The local context confirms Vale as the speaker."
                        if registry_action == "attach_existing"
                        else "Keep the contextual speaker attribution pending."
                    )
                    if is_contextual
                    else "The unresolved endpoint is not needed in this fixture."
                ),
            }
        )
    decision = validate_registry_recovery_response_v1(
        {
            "schema_version": "literary_b2_registry_recovery_response_v1",
            "chapter_id": "ch1",
            "component_id": index["registry_components"][0]["component_id"],
            "ticket_actions": actions,
        },
        index=index,
        component_id=index["registry_components"][0]["component_id"],
        request_fingerprint=request.request_fingerprint,
    )
    ledger = build_registry_recovery_ledger_v1(
        index=index,
        decisions=[decision],
    )
    overlaid = overlay_b2_rows_with_registry_recovery_v1(
        chapter_artifact=artifact,
        index=index,
        registry_ledger=ledger,
    )
    turn = overlaid["speaker_turns"][0]
    if registry_action == "keep_pending":
        assert turn["speaker"]["resolution_status"] == "pending_contract_conflict"
        assert turn["speaker"]["candidate_card_ids"] == []
        assert turn["speaker_authority_status"] == "pending_review"
        assert turn["row_status"] == "review_required_speaker_attribution"
        assert turn["registry_recovery_pending"][0]["original_endpoint"][
            "candidate_card_ids"
        ] == ["card_vale"]
    else:
        assert turn["speaker"]["resolution_status"] == "resolved_candidate"
        assert turn["speaker"]["candidate_card_ids"] == ["card_vale"]
        assert turn["speaker_authority_status"] == "auditor_confirmed_contextual"
        assert turn["row_status"] == "accepted_observation"
        authority = turn["speaker_recovery_authority"]
        assert authority["authority_scope"] == "chapter_local_speaker_attribution"
        assert authority["candidate_card_id"] == "card_vale"
        assert authority["book_global_identity_authority_granted"] is False
        assert authority["original_speaker_authority_status"] == (
            speaker_authority_status
        )


def test_full_recovery_split_and_wrong_target_revision_are_append_only() -> None:
    artifact, requests = _fixture()
    original = deepcopy(artifact)
    index = build_b2_recovery_index_v1(
        chapter_artifact=artifact, interaction_requests=requests
    )
    registry_decision = _registry_decision(index)
    registry_ledger = build_registry_recovery_ledger_v1(
        index=index, decisions=[registry_decision]
    )
    assert len(registry_ledger["local_candidate_cards"]) == 1
    card = registry_ledger["local_candidate_cards"][0]
    assert card["authority_scope"] == "chapter_local_recovery"
    assert card["stable_surfaces"] == []
    assert registry_ledger["global_alias_authority_granted"] is False

    event_decision = _event_decision(artifact, index, registry_ledger)
    event_ledger = build_event_revision_ledger_v1(
        index=index,
        chapter_artifact=artifact,
        registry_ledger=registry_ledger,
        decisions=[event_decision],
    )
    projection = build_effective_b2_projection_v1(
        chapter_artifact=artifact,
        index=index,
        registry_ledger=registry_ledger,
        event_ledger=event_ledger,
    )
    assert artifact == original
    assert len(projection["interaction_events"]) == 4
    assert projection["book_global_identity_mutation_performed"] is False
    assert projection["global_alias_authority_granted"] is False
    assert projection["pending_registry_tickets"] == []
    assert projection["pending_event_cases"] == []
    assert len(projection["original_b2_history"]["interaction_events"]) == 3
    revised = [
        row
        for row in projection["interaction_events"]
        if row["block_id"] == "ch1_b3"
    ][0]
    assert revised["target"]["candidate_card_ids"] == [
        card["candidate_card_id"]
    ]


def test_event_v2_classifies_effective_events_without_an_extra_component() -> None:
    artifact, requests = _fixture()
    index = build_b2_recovery_index_v1(
        chapter_artifact=artifact, interaction_requests=requests
    )
    registry_ledger = build_registry_recovery_ledger_v1(
        index=index, decisions=[_registry_decision(index)]
    )
    rendered = render_event_review_request_v2(
        index=index,
        component_id=index["event_components"][0]["component_id"],
        chapter_artifact=artifact,
        registry_ledger=registry_ledger,
    )
    assert len(rendered.semantic_payload["event_cases"]) == 3
    flags = {
        row["interaction_event_id"]: row["mechanical_review_flags"]
        for row in rendered.semantic_payload["event_cases"]
    }
    assert "joint_target" in flags["event_3"]

    event_ledger = build_event_revision_ledger_v2(
        index=index,
        chapter_artifact=artifact,
        registry_ledger=registry_ledger,
        decisions=[_event_decision_v2(artifact, index, registry_ledger)],
    )
    projection = build_effective_b2_projection_v2(
        chapter_artifact=artifact,
        index=index,
        registry_ledger=registry_ledger,
        event_ledger=event_ledger,
    )
    assert len(projection["interaction_events"]) == 4
    assert projection["held_event_mentions"] == []
    assert all(
        row["actuality"] == "occurred"
        and row["endpoint_status"] == "resolved"
        for row in projection["interaction_events"]
    )
    assert all(
        row["projection_status"] == "eligible_pairwise_directed"
        for row in projection["relation_event_projection"]
    )


def _replace_first_v2_action_with_self_event(
    response: dict, *, directionality: str
) -> None:
    action = next(
        row
        for row in response["event_actions"]
        if row["source_block_ids"] == ["ch1_b1"]
    )
    action["action"] = "revise"
    action["replacement_events"] = [
        {
            "block_id": "ch1_b1",
            "event_anchor": "Robin set a bowl before the hound",
            "actor": _endpoint(
                "Robin",
                status="resolved_candidate",
                card_ids=["card_robin"],
                reference_form="proper_name",
            ),
            "target": _endpoint(
                "himself",
                status="resolved_candidate",
                card_ids=["card_robin"],
                reference_form="pronoun",
            ),
            "interaction_kind": "physical_contact",
            "action_summary": "Robin directs the action at himself.",
            "observed_valence": "neutral",
        }
    ]
    action["effective_event_assessments"] = [
        {
            "directionality": directionality,
            "actuality": "occurred",
            "endpoint_status": "resolved",
        }
    ]
    action["resolution_note"] = "Synthetic contract probe for a reflexive action."


def test_event_v2_preserves_self_directed_observation_without_pairwise_edge() -> None:
    artifact, requests = _fixture()
    index = build_b2_recovery_index_v1(
        chapter_artifact=artifact, interaction_requests=requests
    )
    registry_ledger = build_registry_recovery_ledger_v1(
        index=index, decisions=[_registry_decision(index)]
    )
    response, rendered = _event_response_v2(artifact, index, registry_ledger)
    _replace_first_v2_action_with_self_event(
        response, directionality="self_directed"
    )
    decision = validate_event_review_response_v2(
        response,
        index=index,
        component_id=response["component_id"],
        chapter_artifact=artifact,
        registry_ledger=registry_ledger,
        request_fingerprint=rendered.request_fingerprint,
    )
    ledger = build_event_revision_ledger_v2(
        index=index,
        chapter_artifact=artifact,
        registry_ledger=registry_ledger,
        decisions=[decision],
    )
    projection = build_effective_b2_projection_v2(
        chapter_artifact=artifact,
        index=index,
        registry_ledger=registry_ledger,
        event_ledger=ledger,
    )
    self_event = next(
        row
        for row in projection["interaction_events"]
        if row["directionality"] == "self_directed"
    )
    assert (
        self_event["relation_edge_projection_status"]
        == "non_pairwise_self_directed"
    )
    relation_row = next(
        row
        for row in projection["relation_event_projection"]
        if row["interaction_event_id"] == self_event["interaction_event_id"]
    )
    assert relation_row["authority_effect"] == "none"


def test_event_v2_wrong_chapter_echo_survives_decision_replay() -> None:
    artifact, requests = _fixture()
    index = build_b2_recovery_index_v1(
        chapter_artifact=artifact, interaction_requests=requests
    )
    registry_ledger = build_registry_recovery_ledger_v1(
        index=index, decisions=[_registry_decision(index)]
    )
    response, rendered = _event_response_v2(artifact, index, registry_ledger)
    response["chapter_id"] = "copied_example_chapter"

    decision = validate_event_review_response_v2(
        response,
        index=index,
        component_id=response["component_id"],
        chapter_artifact=artifact,
        registry_ledger=registry_ledger,
        request_fingerprint=rendered.request_fingerprint,
    )

    assert decision["response_normalization_notes"][0]["field"] == "chapter_id"
    assert verify_event_review_decision_v2(
        decision,
        index=index,
        chapter_artifact=artifact,
        registry_ledger=registry_ledger,
    ) == decision


def test_event_v2_rejects_overlapping_pairwise_endpoint_authority() -> None:
    artifact, requests = _fixture()
    index = build_b2_recovery_index_v1(
        chapter_artifact=artifact, interaction_requests=requests
    )
    registry_ledger = build_registry_recovery_ledger_v1(
        index=index, decisions=[_registry_decision(index)]
    )
    response, rendered = _event_response_v2(artifact, index, registry_ledger)
    _replace_first_v2_action_with_self_event(response, directionality="one_way")
    with pytest.raises(B2RecoveryContractError, match="overlapping endpoint"):
        validate_event_review_response_v2(
            response,
            index=index,
            component_id=response["component_id"],
            chapter_artifact=artifact,
            registry_ledger=registry_ledger,
            request_fingerprint=rendered.request_fingerprint,
        )


def test_event_v2_rejects_actor_hidden_inside_joint_target_authority() -> None:
    artifact, requests = _fixture()
    index = build_b2_recovery_index_v1(
        chapter_artifact=artifact, interaction_requests=requests
    )
    registry_ledger = build_registry_recovery_ledger_v1(
        index=index, decisions=[_registry_decision(index)]
    )
    local_card_id = registry_ledger["local_candidate_cards"][0][
        "candidate_card_id"
    ]
    response, rendered = _event_response_v2(artifact, index, registry_ledger)
    action = next(
        row
        for row in response["event_actions"]
        if row["source_block_ids"] == ["ch1_b1"]
    )
    action["action"] = "revise"
    action["replacement_events"] = [
        {
            "block_id": "ch1_b1",
            "event_anchor": "Robin set a bowl before the hound",
            "actor": _endpoint(
                "Robin",
                status="resolved_candidate",
                card_ids=["card_robin"],
                reference_form="proper_name",
            ),
            "target": _endpoint(
                "Robin and the hound",
                status="resolved_joint_candidates",
                card_ids=["card_robin", local_card_id],
                reference_form="group",
            ),
            "interaction_kind": "meeting_or_separation",
            "action_summary": "Robin acts toward a group that includes Robin.",
            "observed_valence": "neutral",
        }
    ]
    action["effective_event_assessments"] = [
        {
            "directionality": "one_way",
            "actuality": "occurred",
            "endpoint_status": "resolved",
        }
    ]
    action["resolution_note"] = "Synthetic joint-target overlap probe."
    with pytest.raises(B2RecoveryContractError, match="overlapping endpoint"):
        validate_event_review_response_v2(
            response,
            index=index,
            component_id=response["component_id"],
            chapter_artifact=artifact,
            registry_ledger=registry_ledger,
            request_fingerprint=rendered.request_fingerprint,
        )


def test_event_v2_holds_reported_event_outside_effective_authority() -> None:
    artifact, requests = _fixture()
    index = build_b2_recovery_index_v1(
        chapter_artifact=artifact, interaction_requests=requests
    )
    registry_ledger = build_registry_recovery_ledger_v1(
        index=index, decisions=[_registry_decision(index)]
    )
    response, rendered = _event_response_v2(artifact, index, registry_ledger)
    action = next(
        row
        for row in response["event_actions"]
        if row["source_block_ids"] == ["ch1_b1"]
    )
    action["effective_event_assessments"][0]["actuality"] = "reported"
    decision = validate_event_review_response_v2(
        response,
        index=index,
        component_id=response["component_id"],
        chapter_artifact=artifact,
        registry_ledger=registry_ledger,
        request_fingerprint=rendered.request_fingerprint,
    )
    ledger = build_event_revision_ledger_v2(
        index=index,
        chapter_artifact=artifact,
        registry_ledger=registry_ledger,
        decisions=[decision],
    )
    projection = build_effective_b2_projection_v2(
        chapter_artifact=artifact,
        index=index,
        registry_ledger=registry_ledger,
        event_ledger=ledger,
    )
    assert len(projection["interaction_events"]) == 3
    assert len(projection["held_event_mentions"]) == 1
    assert (
        projection["held_event_mentions"][0]["relation_edge_projection_status"]
        == "held_reported"
    )


def test_event_v2_keeps_actual_partial_event_but_holds_relation_edge() -> None:
    artifact, requests = _fixture()
    index = build_b2_recovery_index_v1(
        chapter_artifact=artifact, interaction_requests=requests
    )
    registry_ledger = build_registry_recovery_ledger_v1(
        index=index, decisions=[_registry_decision(index)]
    )
    response, rendered = _event_response_v2(artifact, index, registry_ledger)
    action = next(
        row
        for row in response["event_actions"]
        if row["source_block_ids"] == ["ch1_b1"]
    )
    action["action"] = "revise"
    action["replacement_events"] = [
        {
            "block_id": "ch1_b1",
            "event_anchor": "Robin set a bowl before the hound",
            "actor": _endpoint(
                "Robin",
                status="resolved_candidate",
                card_ids=["card_robin"],
                reference_form="proper_name",
            ),
            "target": _endpoint("the hound", status="unresolved"),
            "interaction_kind": "exchange_or_transfer",
            "action_summary": "Robin sets a bowl before an unresolved hound.",
            "observed_valence": "neutral",
        }
    ]
    action["effective_event_assessments"] = [
        {
            "directionality": "one_way",
            "actuality": "occurred",
            "endpoint_status": "partial",
        }
    ]
    action["resolution_note"] = "The action occurred but one endpoint is unresolved."
    decision = validate_event_review_response_v2(
        response,
        index=index,
        component_id=response["component_id"],
        chapter_artifact=artifact,
        registry_ledger=registry_ledger,
        request_fingerprint=rendered.request_fingerprint,
    )
    ledger = build_event_revision_ledger_v2(
        index=index,
        chapter_artifact=artifact,
        registry_ledger=registry_ledger,
        decisions=[decision],
    )
    projection = build_effective_b2_projection_v2(
        chapter_artifact=artifact,
        index=index,
        registry_ledger=registry_ledger,
        event_ledger=ledger,
    )
    partial = next(
        row
        for row in projection["interaction_events"]
        if row["endpoint_status"] == "partial"
    )
    assert partial["relation_edge_projection_status"] == "held_endpoint_partial"


def test_event_v2_downscopes_assessment_overclaim_and_keeps_model_value() -> None:
    artifact, requests = _fixture()
    index = build_b2_recovery_index_v1(
        chapter_artifact=artifact, interaction_requests=requests
    )
    registry_ledger = build_registry_recovery_ledger_v1(
        index=index, decisions=[_registry_decision(index)]
    )
    response, rendered = _event_response_v2(artifact, index, registry_ledger)
    action = next(
        row
        for row in response["event_actions"]
        if row["source_block_ids"] == ["ch1_b1"]
    )
    action["action"] = "revise"
    action["replacement_events"] = [
        {
            "block_id": "ch1_b1",
            "event_anchor": "Robin set a bowl before the hound",
            "actor": _endpoint(
                "Robin",
                status="resolved_candidate",
                card_ids=["card_robin"],
                reference_form="proper_name",
            ),
            "target": _endpoint(
                "the uncertain observer",
                status="ambiguous_candidates",
                card_ids=["card_vale", "card_mara"],
            ),
            "interaction_kind": "exchange_or_transfer",
            "action_summary": "Robin acts toward an ambiguously identified observer.",
            "observed_valence": "neutral",
        }
    ]
    action["effective_event_assessments"] = [
        {
            "directionality": "one_way",
            "actuality": "occurred",
            "endpoint_status": "resolved",
        }
    ]
    action["resolution_note"] = (
        "The observation remains useful while the target identity is ambiguous."
    )

    decision = validate_event_review_response_v2(
        response,
        index=index,
        component_id=response["component_id"],
        chapter_artifact=artifact,
        registry_ledger=registry_ledger,
        request_fingerprint=rendered.request_fingerprint,
    )
    normalized_action = next(
        row for row in decision["event_actions"] if row["case_id"] == action["case_id"]
    )
    assert action["effective_event_assessments"][0]["endpoint_status"] == "resolved"
    assert (
        normalized_action["effective_event_assessments"][0]["endpoint_status"]
        == "partial"
    )
    assert normalized_action["assessment_contract_downgrades"] == [
        {
            "assessment_ordinal": 0,
            "reason_code": "endpoint_status_exceeds_mechanical_maximum",
            "original_endpoint_status": "resolved",
            "normalized_endpoint_status": "partial",
            "mechanically_resolved_endpoint_count": 1,
        }
    ]
    assert verify_event_review_decision_v2(
        decision,
        index=index,
        chapter_artifact=artifact,
        registry_ledger=registry_ledger,
    ) == decision

    ledger = build_event_revision_ledger_v2(
        index=index,
        chapter_artifact=artifact,
        registry_ledger=registry_ledger,
        decisions=[decision],
    )
    revision = next(
        row for row in ledger["event_revisions"] if row["case_id"] == action["case_id"]
    )
    assert revision["assessment_contract_downgrades"] == normalized_action[
        "assessment_contract_downgrades"
    ]
    projection = build_effective_b2_projection_v2(
        chapter_artifact=artifact,
        index=index,
        registry_ledger=registry_ledger,
        event_ledger=ledger,
    )
    partial = next(
        row
        for row in projection["interaction_events"]
        if row["action_summary"]
        == "Robin acts toward an ambiguously identified observer."
    )
    assert partial["endpoint_status"] == "partial"
    assert partial["relation_edge_projection_status"] == "held_endpoint_partial"


def test_event_v2_downscopes_cardinality_mismatch_without_losing_observation() -> None:
    artifact, requests = _fixture()
    index = build_b2_recovery_index_v1(
        chapter_artifact=artifact, interaction_requests=requests
    )
    registry_ledger = build_registry_recovery_ledger_v1(
        index=index, decisions=[_registry_decision(index)]
    )
    local_card_id = registry_ledger["local_candidate_cards"][0][
        "candidate_card_id"
    ]
    response, rendered = _event_response_v2(artifact, index, registry_ledger)
    action = next(
        row
        for row in response["event_actions"]
        if row["source_block_ids"] == ["ch1_b1"]
    )
    action["action"] = "revise"
    action["replacement_events"] = [
        {
            "block_id": "ch1_b1",
            "event_anchor": "Robin set a bowl before the hound",
            "actor": _endpoint(
                "Robin",
                status="resolved_candidate",
                card_ids=["card_robin"],
                reference_form="proper_name",
            ),
            "target": _endpoint(
                "the wider disturbance",
                status="ambiguous_candidates",
                card_ids=[local_card_id],
            ),
            "interaction_kind": "exchange_or_transfer",
            "action_summary": "Robin acts toward a wider unresolved disturbance.",
            "observed_valence": "neutral",
        }
    ]
    action["effective_event_assessments"] = [
        {
            "directionality": "one_way",
            "actuality": "occurred",
            "endpoint_status": "partial",
        }
    ]
    action["resolution_note"] = (
        "The supplied candidate covers only part of the described target."
    )

    decision = validate_event_review_response_v2(
        response,
        index=index,
        component_id=response["component_id"],
        chapter_artifact=artifact,
        registry_ledger=registry_ledger,
        request_fingerprint=rendered.request_fingerprint,
    )
    assert verify_event_review_decision_v2(
        decision,
        index=index,
        chapter_artifact=artifact,
        registry_ledger=registry_ledger,
    ) == decision
    normalized_action = next(
        row for row in decision["event_actions"] if row["case_id"] == action["case_id"]
    )
    target = normalized_action["replacement_events"][0]["target"]
    assert target["resolution_status"] == "unresolved"
    assert target["candidate_card_ids"] == []
    assert target["resolution_basis"] == "unknown"
    assert normalized_action["contract_downgrades"] == [
        {
            "replacement_ordinal": 0,
            "endpoint_role": "target",
            "reason_code": "status_candidate_cardinality_mismatch",
            "original_resolution_status": "ambiguous_candidates",
            "original_candidate_card_ids": [local_card_id],
            "normalized_resolution_status": "unresolved",
            "normalized_candidate_card_ids": [],
        }
    ]

    ledger = build_event_revision_ledger_v2(
        index=index,
        chapter_artifact=artifact,
        registry_ledger=registry_ledger,
        decisions=[decision],
    )
    assert verify_event_revision_ledger_v2(
        ledger,
        index=index,
        chapter_artifact=artifact,
        registry_ledger=registry_ledger,
    ) == ledger
    projection = build_effective_b2_projection_v2(
        chapter_artifact=artifact,
        index=index,
        registry_ledger=registry_ledger,
        event_ledger=ledger,
    )
    partial = next(
        row
        for row in projection["interaction_events"]
        if row["action_summary"]
        == "Robin acts toward a wider unresolved disturbance."
    )
    assert partial["endpoint_status"] == "partial"
    assert partial["relation_edge_projection_status"] == "held_endpoint_partial"
    revision = next(
        row for row in ledger["event_revisions"] if row["case_id"] == action["case_id"]
    )
    assert revision["contract_downgrades"] == normalized_action[
        "contract_downgrades"
    ]


def test_event_v2_does_not_downscope_a_foreign_candidate_id() -> None:
    artifact, requests = _fixture()
    index = build_b2_recovery_index_v1(
        chapter_artifact=artifact, interaction_requests=requests
    )
    registry_ledger = build_registry_recovery_ledger_v1(
        index=index, decisions=[_registry_decision(index)]
    )
    response, rendered = _event_response_v2(artifact, index, registry_ledger)
    action = next(
        row
        for row in response["event_actions"]
        if row["source_block_ids"] == ["ch1_b1"]
    )
    action["action"] = "revise"
    action["replacement_events"] = [
        {
            "block_id": "ch1_b1",
            "event_anchor": "Robin set a bowl before the hound",
            "actor": _endpoint(
                "Robin",
                status="resolved_candidate",
                card_ids=["card_robin"],
            ),
            "target": _endpoint(
                "the hound",
                status="ambiguous_candidates",
                card_ids=["foreign_card"],
            ),
            "interaction_kind": "exchange_or_transfer",
            "action_summary": "Robin acts toward a foreign candidate.",
            "observed_valence": "neutral",
        }
    ]
    action["effective_event_assessments"] = [
        {
            "directionality": "one_way",
            "actuality": "occurred",
            "endpoint_status": "partial",
        }
    ]
    with pytest.raises(B2RecoveryContractError, match="foreign candidate"):
        validate_event_review_response_v2(
            response,
            index=index,
            component_id=response["component_id"],
            chapter_artifact=artifact,
            registry_ledger=registry_ledger,
            request_fingerprint=rendered.request_fingerprint,
        )


def test_event_v2_represents_reciprocal_event_without_two_directed_rows() -> None:
    artifact, requests = _fixture()
    index = build_b2_recovery_index_v1(
        chapter_artifact=artifact, interaction_requests=requests
    )
    registry_ledger = build_registry_recovery_ledger_v1(
        index=index, decisions=[_registry_decision(index)]
    )
    response, rendered = _event_response_v2(artifact, index, registry_ledger)
    action = next(
        row
        for row in response["event_actions"]
        if row["source_block_ids"] == ["ch1_b1"]
    )
    action["effective_event_assessments"][0]["directionality"] = "reciprocal"
    decision = validate_event_review_response_v2(
        response,
        index=index,
        component_id=response["component_id"],
        chapter_artifact=artifact,
        registry_ledger=registry_ledger,
        request_fingerprint=rendered.request_fingerprint,
    )
    ledger = build_event_revision_ledger_v2(
        index=index,
        chapter_artifact=artifact,
        registry_ledger=registry_ledger,
        decisions=[decision],
    )
    projection = build_effective_b2_projection_v2(
        chapter_artifact=artifact,
        index=index,
        registry_ledger=registry_ledger,
        event_ledger=ledger,
    )
    reciprocal = [
        row
        for row in projection["relation_event_projection"]
        if row["directionality"] == "reciprocal"
    ]
    assert len(reciprocal) == 1
    assert reciprocal[0]["projection_status"] == "eligible_pairwise_reciprocal"


def test_event_v2_object_endpoint_is_not_an_interpersonal_edge() -> None:
    event = {
        "actuality": "occurred",
        "endpoint_status": "resolved",
        "directionality": "one_way",
        "actor": _endpoint(
            "Robin",
            status="resolved_candidate",
            card_ids=["card_robin"],
            reference_form="proper_name",
        ),
        "target": _endpoint(
            "the gate",
            status="resolved_candidate",
            card_ids=["card_gate"],
        ),
    }
    cards = {
        "card_robin": _card("card_robin", "Robin"),
        "card_gate": {
            **_card("card_gate", "the gate"),
            "effective_claims_as_of": {
                "identity_summary": "A gate.",
                "referent_kind": "object",
                "referential_gender": None,
            },
        },
    }
    assert (
        _relation_projection_status_v2(event, cards=cards)
        == "non_pairwise_referent_kind"
    )


@pytest.mark.parametrize(
    "referent_kind",
    ["group_reference", "nonhuman_character", "unknown"],
)
def test_event_v2_character_like_kinds_remain_pairwise_eligible(
    referent_kind: str,
) -> None:
    event = {
        "actuality": "occurred",
        "endpoint_status": "resolved",
        "directionality": "one_way",
        "actor": _endpoint(
            "Robin",
            status="resolved_candidate",
            card_ids=["card_robin"],
            reference_form="proper_name",
        ),
        "target": _endpoint(
            "the visitors",
            status="resolved_candidate",
            card_ids=["card_visitors"],
        ),
    }
    cards = {
        "card_robin": _card("card_robin", "Robin"),
        "card_visitors": {
            **_card("card_visitors", "the visitors"),
            "effective_claims_as_of": {
                "identity_summary": "A character-like counterpart.",
                "referent_kind": referent_kind,
                "referential_gender": None,
            },
        },
    }
    assert (
        _relation_projection_status_v2(event, cards=cards)
        == "eligible_pairwise_directed"
    )


def test_registry_response_must_exact_cover_component() -> None:
    artifact, requests = _fixture()
    index = build_b2_recovery_index_v1(
        chapter_artifact=artifact, interaction_requests=requests
    )
    component = index["registry_components"][0]
    rendered = render_registry_recovery_request_v1(
        index=index, component_id=component["component_id"]
    )
    response = {
        "schema_version": "literary_b2_registry_recovery_response_v1",
        "chapter_id": "ch1",
        "component_id": component["component_id"],
        "ticket_actions": [],
    }
    with pytest.raises(B2RecoveryContractError, match="exact-cover"):
        validate_registry_recovery_response_v1(
            response,
            index=index,
            component_id=component["component_id"],
            request_fingerprint=rendered.request_fingerprint,
        )


def test_unlocatable_new_card_surface_fails_closed() -> None:
    artifact, requests = _fixture()
    index = build_b2_recovery_index_v1(
        chapter_artifact=artifact, interaction_requests=requests
    )
    component = index["registry_components"][0]
    rendered = render_registry_recovery_request_v1(
        index=index, component_id=component["component_id"]
    )
    actions = []
    for ticket in index["registry_gap_tickets"]:
        actions.append(
            {
                "ticket_id": ticket["ticket_id"],
                "action": "create_chapter_local",
                "target_candidate_card_id": None,
                "provisional_group_key": "invented",
                "canonical_surface": "not in any source block",
                "referent_kind": "animal",
                "identity_summary": "An invented row.",
                "source_block_ids": ticket["source_block_ids"],
                "pending_reason": None,
                "resolution_note": "Invalid on purpose.",
            }
        )
    with pytest.raises(B2RecoveryContractError, match="not exact source text"):
        validate_registry_recovery_response_v1(
            {
                "schema_version": "literary_b2_registry_recovery_response_v1",
                "chapter_id": "ch1",
                "component_id": component["component_id"],
                "ticket_actions": actions,
            },
            index=index,
            component_id=component["component_id"],
            request_fingerprint=rendered.request_fingerprint,
        )


def test_event_replacement_cannot_mint_or_reference_a_foreign_card() -> None:
    artifact, requests = _fixture()
    index = build_b2_recovery_index_v1(
        chapter_artifact=artifact, interaction_requests=requests
    )
    registry_ledger = build_registry_recovery_ledger_v1(
        index=index, decisions=[_registry_decision(index)]
    )
    component = index["event_components"][0]
    rendered = render_event_review_request_v1(
        index=index,
        component_id=component["component_id"],
        chapter_artifact=artifact,
        registry_ledger=registry_ledger,
    )
    case = index["event_review_cases"][0]
    original = case["event_snapshot"]
    replacement = {
        key: deepcopy(original[key])
        for key in (
            "block_id",
            "event_anchor",
            "actor",
            "target",
            "interaction_kind",
            "action_summary",
            "observed_valence",
        )
    }
    replacement["target"] = _resolved_endpoint("foreign_card", "the hound")
    replacement["action_summary"] = "A changed but invalid event."
    actions = []
    for row in index["event_review_cases"]:
        if row["case_id"] == case["case_id"]:
            actions.append(
                {
                    "case_id": row["case_id"],
                    "action": "revise",
                    "replacement_events": [replacement],
                    "source_block_ids": row["source_block_ids"],
                    "pending_reason": None,
                    "resolution_note": "Invalid foreign card.",
                }
            )
        else:
            actions.append(
                {
                    "case_id": row["case_id"],
                    "action": "keep",
                    "replacement_events": [],
                    "source_block_ids": row["source_block_ids"],
                    "pending_reason": None,
                    "resolution_note": "Keep.",
                }
            )
    with pytest.raises(B2RecoveryContractError, match="foreign candidate"):
        validate_event_review_response_v1(
            {
                "schema_version": "literary_b2_event_review_response_v1",
                "chapter_id": "ch1",
                "component_id": component["component_id"],
                "event_actions": actions,
            },
            index=index,
            component_id=component["component_id"],
            chapter_artifact=artifact,
            registry_ledger=registry_ledger,
            request_fingerprint=rendered.request_fingerprint,
        )


def test_pending_registry_and_events_never_enter_effective_authority() -> None:
    artifact, requests = _fixture()
    index = build_b2_recovery_index_v1(
        chapter_artifact=artifact, interaction_requests=requests
    )
    registry_component = index["registry_components"][0]
    registry_request = render_registry_recovery_request_v1(
        index=index, component_id=registry_component["component_id"]
    )
    registry_decision = validate_registry_recovery_response_v1(
        {
            "schema_version": "literary_b2_registry_recovery_response_v1",
            "chapter_id": "ch1",
            "component_id": registry_component["component_id"],
            "ticket_actions": [
                {
                    "ticket_id": ticket["ticket_id"],
                    "action": "keep_pending",
                    "target_candidate_card_id": None,
                    "narrowed_candidate_card_ids": [],
                    "provisional_group_key": None,
                    "canonical_surface": None,
                    "referent_kind": None,
                    "identity_summary": None,
                    "source_block_ids": ticket["source_block_ids"],
                    "pending_reason": None,
                    "resolution_note": "Keep the endpoint visible without authority.",
                }
                for ticket in index["registry_gap_tickets"]
            ],
        },
        index=index,
        component_id=registry_component["component_id"],
        request_fingerprint=registry_request.request_fingerprint,
    )
    registry_ledger = build_registry_recovery_ledger_v1(
        index=index, decisions=[registry_decision]
    )
    assert all(
        row["pending_reason"] == row["resolution_note"]
        for row in registry_decision["ticket_actions"]
    )
    event_component = index["event_components"][0]
    event_request = render_event_review_request_v1(
        index=index,
        component_id=event_component["component_id"],
        chapter_artifact=artifact,
        registry_ledger=registry_ledger,
    )
    event_decision = validate_event_review_response_v1(
        {
            "schema_version": "literary_b2_event_review_response_v1",
            "chapter_id": "ch1",
            "component_id": event_component["component_id"],
            "event_actions": [
                {
                    "case_id": case["case_id"],
                    "action": "pending",
                    "replacement_events": [],
                    "source_block_ids": case["source_block_ids"],
                    "pending_reason": None,
                    "resolution_note": "Retain history but grant no event authority.",
                }
                for case in index["event_review_cases"]
            ],
        },
        index=index,
        component_id=event_component["component_id"],
        chapter_artifact=artifact,
        registry_ledger=registry_ledger,
        request_fingerprint=event_request.request_fingerprint,
    )
    event_ledger = build_event_revision_ledger_v1(
        index=index,
        chapter_artifact=artifact,
        registry_ledger=registry_ledger,
        decisions=[event_decision],
    )
    assert all(
        row["pending_reason"] == row["resolution_note"]
        for row in event_decision["event_actions"]
    )
    projection = build_effective_b2_projection_v1(
        chapter_artifact=artifact,
        index=index,
        registry_ledger=registry_ledger,
        event_ledger=event_ledger,
    )
    assert projection["recovered_candidate_cards"] == []
    assert len(projection["pending_registry_tickets"]) == 2
    assert len(projection["pending_event_cases"]) == 3
    assert projection["interaction_events"] == []


def test_pending_reopens_only_for_new_evidence_and_at_most_twice() -> None:
    previous = {
        "lifecycle_state": "pending",
        "hearing_count": 1,
        "evidence_hash": "a" * 64,
    }
    assert classify_recovery_reopen_v1(
        previous_resolution=previous, new_evidence_hash="b" * 64
    )["eligible"]
    assert not classify_recovery_reopen_v1(
        previous_resolution=previous, new_evidence_hash="a" * 64
    )["eligible"]
    previous["hearing_count"] = 2
    result = classify_recovery_reopen_v1(
        previous_resolution=previous, new_evidence_hash="b" * 64
    )
    assert not result["eligible"]
    assert result["next_state"] == "book_end_review"


def test_tampered_index_hash_fails_closed() -> None:
    artifact, requests = _fixture()
    index = build_b2_recovery_index_v1(
        chapter_artifact=artifact, interaction_requests=requests
    )
    tampered = deepcopy(index)
    tampered["counts"]["registry_gap_tickets"] = 99
    with pytest.raises(B2RecoveryContractError, match="hash mismatch"):
        verify_b2_recovery_index_v1(tampered)


def test_rehashed_ledger_cannot_invent_a_different_binding() -> None:
    artifact, requests = _fixture()
    index = build_b2_recovery_index_v1(
        chapter_artifact=artifact, interaction_requests=requests
    )
    ledger = build_registry_recovery_ledger_v1(
        index=index, decisions=[_registry_decision(index)]
    )
    tampered = deepcopy(ledger)
    tampered["ticket_resolutions"][0][
        "bound_candidate_card_id"
    ] = "invented_after_validation"
    body = deepcopy(tampered)
    body.pop("registry_recovery_ledger_hash")
    tampered["registry_recovery_ledger_hash"] = canonical_hash(body)
    with pytest.raises(
        B2RecoveryContractError,
        match="differs from validated decisions",
    ):
        verify_registry_recovery_ledger_v1(tampered, index=index)
