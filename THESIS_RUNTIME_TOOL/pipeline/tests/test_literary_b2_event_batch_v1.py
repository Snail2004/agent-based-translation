from __future__ import annotations

from copy import deepcopy
import hashlib

import pytest

from pipeline.literary.b2_context_v1 import B2_REQUEST_SCHEMA_VERSION
from pipeline.literary.b2_event_batch_v1 import (
    EVENT_REVIEW_BATCH_SYSTEM_PROMPT_V1,
    render_event_review_batch_request_v1,
    validate_event_review_batch_response_v1,
)
from pipeline.literary.b2_live_canary_v1 import CHAPTER_ARTIFACT_SCHEMA_VERSION
from pipeline.literary.b2_prompts_v2 import (
    B2_INTERACTION_PROMPT_ID_V2,
    B2_INTERACTION_SYSTEM_PROMPT_V2,
    b2_interaction_response_schema_v2,
)
from pipeline.literary.b2_recovery_v1 import (
    B2RecoveryContractError,
    build_b2_recovery_index_v1,
    build_registry_recovery_ledger_v1,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json
from pipeline.scripts.run_literary_b2_event_batch_canary_v1 import _comparison


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
        "first_supported_block_id": "ch1_b01",
        "provenance_refs": [{"chapter_id": "ch1", "block_id": "ch1_b01"}],
        "relevant_claim_transitions": [],
        "uncertainty_flags": [],
    }


def _endpoint(surface: str, card_id: str) -> dict:
    return {
        "surface": surface,
        "reference_form": "proper_name",
        "resolution_status": "resolved_candidate",
        "candidate_card_ids": [card_id],
        "resolution_basis": "explicit_name",
    }


def _fixture() -> tuple[dict, dict, dict]:
    blocks = [
        {
            "block_id": f"ch1_b{ordinal:02d}",
            "block_type": "paragraph",
            "text": f"Robin greeted Vale in passage {ordinal:02d}.",
        }
        for ordinal in range(1, 14)
    ]
    cards = [_card("card_robin", "Robin"), _card("card_vale", "Vale")]
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
    response_schema = b2_interaction_response_schema_v2()
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
        "response_schema": response_schema,
        "response_schema_hash": canonical_hash(response_schema),
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
    events = []
    for ordinal, block in enumerate(blocks, 1):
        anchor = "Robin greeted Vale"
        events.append(
            {
                "interaction_event_id": f"event_{ordinal:02d}",
                "block_id": block["block_id"],
                "event_anchor": anchor,
                "actor": _endpoint("Robin", "card_robin"),
                "target": _endpoint("Vale", "card_vale"),
                "interaction_kind": "conversation_or_social",
                "action_summary": "Robin greets Vale.",
                "observed_valence": "neutral",
                "source_spans": [
                    {"char_start": 0, "char_end": len(anchor)}
                ],
                "grounding_status": "grounded",
                "row_status": "accepted_observation",
            }
        )
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
    index = build_b2_recovery_index_v1(
        chapter_artifact=artifact,
        interaction_requests=[request],
    )
    assert len(index["event_components"]) == 2
    assert index["registry_components"] == []
    ledger = build_registry_recovery_ledger_v1(index=index, decisions=[])
    return artifact, index, ledger


def _response(artifact: dict, index: dict, ledger: dict) -> tuple[dict, object]:
    component_ids = [
        row["component_id"] for row in index["event_components"]
    ]
    rendered = render_event_review_batch_request_v1(
        index=index,
        component_ids=component_ids,
        chapter_artifact=artifact,
        registry_ledger=ledger,
    )
    cases = {
        row["case_id"]: row for row in index["event_review_cases"]
    }
    component_results = []
    for component in index["event_components"]:
        actions = []
        for case_id in component["case_ids"]:
            case = cases[case_id]
            actions.append(
                {
                    "case_id": case_id,
                    "action": "keep",
                    "replacement_events": [],
                    "effective_event_assessments": [
                        {
                            "directionality": "one_way",
                            "actuality": "occurred",
                            "endpoint_status": "resolved",
                        }
                    ],
                    "source_block_ids": list(case["source_block_ids"]),
                    "pending_reason": None,
                    "resolution_note": "The supplied event is source-supported.",
                }
            )
        component_results.append(
            {
                "component_id": component["component_id"],
                "case_channels": [
                    {
                        "case_id": case_id,
                        "observation_channel": "non_speech_observation",
                    }
                    for case_id in component["case_ids"]
                ],
                "result": {
                    "schema_version": "literary_b2_event_review_response_v2",
                    "chapter_id": index["chapter_id"],
                    "component_id": component["component_id"],
                    "event_actions": actions,
                },
            }
        )
    response = {
        "schema_version": "literary_b2_event_review_batch_response_v1_1",
        "chapter_id": index["chapter_id"],
        "batch_id": rendered.component_id,
        "component_results": component_results,
    }
    return response, rendered


def test_event_batch_deduplicates_cards_and_exact_covers_components() -> None:
    artifact, index, ledger = _fixture()
    component_ids = [
        row["component_id"] for row in index["event_components"]
    ]
    response, rendered = _response(artifact, index, ledger)

    assert len(rendered.semantic_payload["shared_candidate_cards"]) == 2
    assert len(rendered.semantic_payload["components"]) == 2
    assert all(
        len(row["relevant_candidate_card_ids"]) == 2
        for row in rendered.semantic_payload["components"]
    )

    decision = validate_event_review_batch_response_v1(
        response,
        index=index,
        component_ids=component_ids,
        chapter_artifact=artifact,
        registry_ledger=ledger,
        request_fingerprint=rendered.request_fingerprint,
    )
    assert len(decision["component_decisions"]) == 2
    assert sum(
        len(row["event_actions"])
        for row in decision["component_decisions"]
    ) == 13
    assert decision["batch_request_fingerprint"] == rendered.request_fingerprint
    assert len(decision["case_channels"]) == 13
    assert decision["relation_authority_holds"] == []


def test_event_batch_normalizes_top_level_and_nested_routing_echoes() -> None:
    artifact, index, ledger = _fixture()
    component_ids = [
        row["component_id"] for row in index["event_components"]
    ]
    response, rendered = _response(artifact, index, ledger)
    response["chapter_id"] = "copied_example_chapter"
    response["batch_id"] = "copied_example_batch"
    for row in response["component_results"]:
        row["result"]["chapter_id"] = "copied_nested_chapter"

    decision = validate_event_review_batch_response_v1(
        response,
        index=index,
        component_ids=component_ids,
        chapter_artifact=artifact,
        registry_ledger=ledger,
        request_fingerprint=rendered.request_fingerprint,
    )

    assert decision["chapter_id"] == index["chapter_id"]
    assert decision["batch_id"] == rendered.component_id
    notes = decision["response_normalization_notes"]
    assert [row["field"] for row in notes].count("chapter_id") == 3
    assert [row["field"] for row in notes].count("batch_id") == 1
    assert sum("field_path" in row for row in notes) == 2


def test_event_batch_holds_communication_channel_without_dropping_event() -> None:
    artifact, index, ledger = _fixture()
    component_ids = [
        row["component_id"] for row in index["event_components"]
    ]
    response, rendered = _response(artifact, index, ledger)
    held = response["component_results"][0]["case_channels"][0]
    held["observation_channel"] = "communication_or_speech"

    decision = validate_event_review_batch_response_v1(
        response,
        index=index,
        component_ids=component_ids,
        chapter_artifact=artifact,
        registry_ledger=ledger,
        request_fingerprint=rendered.request_fingerprint,
    )

    assert sum(
        len(row["event_actions"])
        for row in decision["component_decisions"]
    ) == 13
    assert decision["relation_authority_holds"] == [
        {
            "case_id": held["case_id"],
            "observation_channel": "communication_or_speech",
            "reason_code": "communication_or_uncertain_channel_holds_pairwise_authority",
        }
    ]


def test_event_batch_rejects_missing_component_channel() -> None:
    artifact, index, ledger = _fixture()
    component_ids = [
        row["component_id"] for row in index["event_components"]
    ]
    response, rendered = _response(artifact, index, ledger)
    response["component_results"][0]["case_channels"].pop()

    with pytest.raises(B2RecoveryContractError, match="exact-cover"):
        validate_event_review_batch_response_v1(
            response,
            index=index,
            component_ids=component_ids,
            chapter_artifact=artifact,
            registry_ledger=ledger,
            request_fingerprint=rendered.request_fingerprint,
        )


def test_event_batch_rejects_cross_component_block_evidence() -> None:
    artifact, index, ledger = _fixture()
    component_ids = [
        row["component_id"] for row in index["event_components"]
    ]
    response, rendered = _response(artifact, index, ledger)
    response["component_results"][0]["result"]["event_actions"][0][
        "source_block_ids"
    ] = list(index["event_components"][1]["source_block_ids"])

    with pytest.raises(B2RecoveryContractError, match="foreign evidence"):
        validate_event_review_batch_response_v1(
            response,
            index=index,
            component_ids=component_ids,
            chapter_artifact=artifact,
            registry_ledger=ledger,
            request_fingerprint=rendered.request_fingerprint,
        )


def test_event_batch_rejects_missing_component_result() -> None:
    artifact, index, ledger = _fixture()
    component_ids = [
        row["component_id"] for row in index["event_components"]
    ]
    response, rendered = _response(artifact, index, ledger)
    response["component_results"].pop()

    with pytest.raises(B2RecoveryContractError, match="violates schema"):
        validate_event_review_batch_response_v1(
            response,
            index=index,
            component_ids=component_ids,
            chapter_artifact=artifact,
            registry_ledger=ledger,
            request_fingerprint=rendered.request_fingerprint,
        )


def test_event_batch_requires_disjoint_component_blocks() -> None:
    artifact, index, ledger = _fixture()
    modified = deepcopy(index)
    modified["event_components"][1]["source_block_ids"].append(
        modified["event_components"][0]["source_block_ids"][0]
    )
    body = dict(modified)
    body.pop("recovery_index_hash")
    modified["recovery_index_hash"] = canonical_hash(body)
    modified_ledger = build_registry_recovery_ledger_v1(
        index=modified, decisions=[]
    )

    with pytest.raises(B2RecoveryContractError, match="disjoint"):
        render_event_review_batch_request_v1(
            index=modified,
            component_ids=[
                row["component_id"] for row in modified["event_components"]
            ],
            chapter_artifact=artifact,
            registry_ledger=modified_ledger,
        )


def test_event_batch_render_is_deterministic_and_book_neutral() -> None:
    artifact, index, ledger = _fixture()
    component_ids = [
        row["component_id"] for row in reversed(index["event_components"])
    ]
    first = render_event_review_batch_request_v1(
        index=index,
        component_ids=component_ids,
        chapter_artifact=artifact,
        registry_ledger=ledger,
    )
    second = render_event_review_batch_request_v1(
        index=index,
        component_ids=list(reversed(component_ids)),
        chapter_artifact=artifact,
        registry_ledger=ledger,
    )

    assert first.request_fingerprint == second.request_fingerprint
    assert first.messages == second.messages
    prompt = EVENT_REVIEW_BATCH_SYSTEM_PROMPT_V1.lower()
    assert "wuthering" not in prompt
    assert "heathcliff" not in prompt
    assert "never use another component's evidence" in prompt
    assert "a command, request, instruction, question, or reply is speech" in prompt
    assert "exact-cover case_channels" in prompt


def test_event_batch_comparison_does_not_treat_partial_revision_as_authority() -> None:
    comparison = _comparison(
        decisions=[
            {
                "event_actions": [
                    {
                        "case_id": "case_1",
                        "action": "revise",
                        "effective_event_assessments": [
                            {
                                "actuality": "occurred",
                                "directionality": "one_way",
                                "endpoint_status": "partial",
                            }
                        ],
                    }
                ]
            }
        ],
        baseline={
            "case_1": {
                "action": "pending",
                "ordinary_pairwise_authority": False,
            }
        },
        authority_hold_case_ids=[],
    )

    assert comparison["authority_increase_count"] == 0
    assert comparison["unheld_authority_increase_count"] == 0


def test_event_batch_comparison_holds_resolved_communication_authority() -> None:
    comparison = _comparison(
        decisions=[
            {
                "event_actions": [
                    {
                        "case_id": "case_1",
                        "action": "keep",
                        "effective_event_assessments": [
                            {
                                "actuality": "occurred",
                                "directionality": "one_way",
                                "endpoint_status": "resolved",
                            }
                        ],
                    }
                ]
            }
        ],
        baseline={
            "case_1": {
                "action": "reject",
                "ordinary_pairwise_authority": False,
            }
        },
        authority_hold_case_ids=["case_1"],
    )

    assert comparison["authority_increase_count"] == 1
    assert comparison["unheld_authority_increase_count"] == 0
