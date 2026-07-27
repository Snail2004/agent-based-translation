from __future__ import annotations

from copy import deepcopy

import pytest

from pipeline.literary.b2_recovery_batch_v1 import (
    REGISTRY_RECOVERY_BATCH_RESPONSE_SCHEMA_VERSION_V1,
    registry_recovery_batch_response_schema_v1,
    render_registry_recovery_batch_request_v1,
    validate_registry_recovery_batch_response_v1,
)
from pipeline.literary.b2_recovery_v1 import (
    B2RecoveryContractError,
    build_b2_recovery_index_v1,
    build_registry_recovery_ledger_v1,
    render_registry_recovery_request_v1,
    verify_registry_recovery_ledger_v1,
)
from pipeline.literary.checkpoint import canonical_hash
from pipeline.tests.test_literary_b2_recovery_v1 import _fixture


def _multi_component_index() -> dict:
    artifact, requests = _fixture()
    base_events = deepcopy(artifact["interaction_events"])
    events = []
    for ordinal in range(25):
        event = deepcopy(base_events[ordinal % len(base_events)])
        event["interaction_event_id"] = f"event_batch_{ordinal:02d}"
        events.append(event)
    artifact_body = {
        **artifact,
        "interaction_events": events,
    }
    artifact_body.pop("artifact_hash")
    artifact_body["artifact_hash"] = canonical_hash(artifact_body)
    index = build_b2_recovery_index_v1(
        chapter_artifact=artifact_body,
        interaction_requests=requests,
    )
    assert len(index["registry_components"]) == 2
    return index


def _pending_result(index: dict, component_id: str) -> dict:
    component = next(
        row
        for row in index["registry_components"]
        if row["component_id"] == component_id
    )
    tickets = {
        row["ticket_id"]: row for row in index["registry_gap_tickets"]
    }
    return {
        "schema_version": "literary_b2_registry_recovery_response_v1",
        "chapter_id": index["chapter_id"],
        "component_id": component_id,
        "ticket_actions": [
            {
                "ticket_id": ticket_id,
                "action": "keep_pending",
                "target_candidate_card_id": None,
                "narrowed_candidate_card_ids": [],
                "provisional_group_key": None,
                "canonical_surface": None,
                "referent_kind": None,
                "identity_summary": None,
                "source_block_ids": list(tickets[ticket_id]["source_block_ids"]),
                "pending_reason": "The bounded source does not settle identity.",
                "resolution_note": "Retain the endpoint without authority.",
            }
            for ticket_id in component["ticket_ids"]
        ],
    }


def _batch_response(index: dict, component_ids: list[str]) -> tuple[dict, object]:
    request = render_registry_recovery_batch_request_v1(
        index=index,
        component_ids=component_ids,
    )
    response = {
        "schema_version": REGISTRY_RECOVERY_BATCH_RESPONSE_SCHEMA_VERSION_V1,
        "chapter_id": index["chapter_id"],
        "batch_id": request.component_id,
        "component_results": [
            {
                "component_id": component_id,
                "result": _pending_result(index, component_id),
            }
            for component_id in component_ids
        ],
    }
    return response, request


def test_batch_deduplicates_shared_cards_without_changing_components() -> None:
    index = _multi_component_index()
    component_ids = [
        row["component_id"] for row in index["registry_components"]
    ]
    batch = render_registry_recovery_batch_request_v1(
        index=index,
        component_ids=component_ids,
    )
    singles = [
        render_registry_recovery_request_v1(
            index=index,
            component_id=component_id,
        )
        for component_id in component_ids
    ]

    shared_ids = [
        row["candidate_card_id"]
        for row in batch.semantic_payload["shared_candidate_cards"]
    ]
    repeated_count = sum(
        len(row.semantic_payload["candidate_cards"]) for row in singles
    )
    assert len(shared_ids) == len(set(shared_ids))
    assert len(shared_ids) < repeated_count
    assert [
        row["component_id"] for row in batch.semantic_payload["components"]
    ] == component_ids


def test_batch_reuses_single_component_validator_elementwise() -> None:
    index = _multi_component_index()
    component_ids = [
        row["component_id"] for row in index["registry_components"]
    ]
    response, request = _batch_response(index, component_ids)

    decision = validate_registry_recovery_batch_response_v1(
        response,
        index=index,
        component_ids=component_ids,
        request_fingerprint=request.request_fingerprint,
    )

    assert {
        row["component_id"] for row in decision["component_decisions"]
    } == set(component_ids)
    assert sum(
        len(row["ticket_actions"]) for row in decision["component_decisions"]
    ) == len(index["registry_gap_tickets"])
    assert decision["contract_normalizations"] == []


def test_batch_discards_authority_fields_from_non_authoritative_actions() -> None:
    index = _multi_component_index()
    component_ids = [
        row["component_id"] for row in index["registry_components"]
    ]
    response, request = _batch_response(index, component_ids)
    action = response["component_results"][0]["result"]["ticket_actions"][0]
    action["referent_kind"] = "person"

    decision = validate_registry_recovery_batch_response_v1(
        response,
        index=index,
        component_ids=component_ids,
        request_fingerprint=request.request_fingerprint,
    )

    assert len(decision["contract_normalizations"]) == 1
    normalization = decision["contract_normalizations"][0]
    assert normalization["field"] == "referent_kind"
    normalized_action = decision["component_decisions"][0]["ticket_actions"][0]
    assert normalized_action["referent_kind"] is None


def test_batch_rejects_missing_or_duplicate_component() -> None:
    index = _multi_component_index()
    component_ids = [
        row["component_id"] for row in index["registry_components"]
    ]
    response, request = _batch_response(index, component_ids)
    response["component_results"] = response["component_results"][:1]
    with pytest.raises(B2RecoveryContractError, match="exact-cover"):
        validate_registry_recovery_batch_response_v1(
            response,
            index=index,
            component_ids=component_ids,
            request_fingerprint=request.request_fingerprint,
        )

    response, request = _batch_response(index, component_ids)
    response["component_results"][1] = deepcopy(
        response["component_results"][0]
    )
    with pytest.raises(B2RecoveryContractError):
        validate_registry_recovery_batch_response_v1(
            response,
            index=index,
            component_ids=component_ids,
            request_fingerprint=request.request_fingerprint,
        )


def test_batch_schema_is_stable_while_validator_binds_exact_ids() -> None:
    first_schema = registry_recovery_batch_response_schema_v1(
        chapter_id="chapter_one",
        batch_id="batch_one",
        component_ids=["component_one"],
    )
    second_schema = registry_recovery_batch_response_schema_v1(
        chapter_id="chapter_two",
        batch_id="batch_two",
        component_ids=["component_two", "component_three"],
    )
    assert first_schema == second_schema

    index = _multi_component_index()
    component_ids = [row["component_id"] for row in index["registry_components"]]
    response, request = _batch_response(index, component_ids)
    response["chapter_id"] = "foreign_chapter"
    decision = validate_registry_recovery_batch_response_v1(
        response,
        index=index,
        component_ids=component_ids,
        request_fingerprint=request.request_fingerprint,
    )
    assert decision["chapter_id"] == index["chapter_id"]
    assert decision["response_normalization_notes"][0]["field"] == "chapter_id"

    response, request = _batch_response(index, component_ids)
    response["batch_id"] = "foreign_batch"
    for row in response["component_results"]:
        row["result"]["chapter_id"] = "copied_nested_chapter"
    decision = validate_registry_recovery_batch_response_v1(
        response,
        index=index,
        component_ids=component_ids,
        request_fingerprint=request.request_fingerprint,
    )
    assert decision["batch_id"] == request.component_id
    assert decision["response_normalization_notes"][0]["field"] == "batch_id"
    assert all(
        row["response_normalization_notes"][0]["field"] == "chapter_id"
        for row in decision["component_decisions"]
    )


def test_batch_quarantines_one_invalid_component_without_losing_the_other() -> None:
    index = _multi_component_index()
    component_ids = [
        row["component_id"] for row in index["registry_components"]
    ]
    response, request = _batch_response(index, component_ids)
    foreign_ticket = response["component_results"][1]["result"][
        "ticket_actions"
    ][0]["ticket_id"]
    response["component_results"][0]["result"]["ticket_actions"][0][
        "ticket_id"
    ] = foreign_ticket
    decision = validate_registry_recovery_batch_response_v1(
        response,
        index=index,
        component_ids=component_ids,
        request_fingerprint=request.request_fingerprint,
    )
    assert len(decision["component_decisions"]) == 1
    assert len(decision["quarantined_components"]) == 1
    quarantine = decision["quarantined_components"][0]
    assert quarantine["component_id"] == component_ids[0]
    assert quarantine["state"] == "unreviewed"
    assert quarantine["reason_code"] == "component_semantic_contract_rejected"
    assert "exact-cover" in quarantine["reason"]

    response, request = _batch_response(index, component_ids)
    action = response["component_results"][0]["result"]["ticket_actions"][0]
    action.update(
        {
            "action": "attach_existing",
            "target_candidate_card_id": "foreign_card",
            "pending_reason": None,
        }
    )
    action.pop("narrowed_candidate_card_ids", None)
    decision = validate_registry_recovery_batch_response_v1(
        response,
        index=index,
        component_ids=component_ids,
        request_fingerprint=request.request_fingerprint,
    )
    assert len(decision["component_decisions"]) == 1
    assert decision["quarantined_components"][0]["component_id"] == component_ids[0]
    assert "foreign candidate" in decision["quarantined_components"][0]["reason"]


def test_batch_rejects_when_every_component_result_is_invalid() -> None:
    index = _multi_component_index()
    component_ids = [
        row["component_id"] for row in index["registry_components"]
    ]
    response, request = _batch_response(index, component_ids)
    for component_result in response["component_results"]:
        action = component_result["result"]["ticket_actions"][0]
        action.update(
            {
                "action": "attach_existing",
                "target_candidate_card_id": "foreign_card",
                "pending_reason": None,
            }
        )
        action.pop("narrowed_candidate_card_ids", None)

    with pytest.raises(B2RecoveryContractError, match="no valid component"):
        validate_registry_recovery_batch_response_v1(
            response,
            index=index,
            component_ids=component_ids,
            request_fingerprint=request.request_fingerprint,
        )


def test_partial_batch_quarantine_round_trips_through_registry_ledger() -> None:
    index = _multi_component_index()
    component_ids = [
        row["component_id"] for row in index["registry_components"]
    ]
    response, request = _batch_response(index, component_ids)
    action = response["component_results"][0]["result"]["ticket_actions"][0]
    action.update(
        {
            "action": "attach_existing",
            "target_candidate_card_id": "foreign_card",
            "pending_reason": None,
        }
    )
    action.pop("narrowed_candidate_card_ids", None)
    decision = validate_registry_recovery_batch_response_v1(
        response,
        index=index,
        component_ids=component_ids,
        request_fingerprint=request.request_fingerprint,
    )

    ledger = build_registry_recovery_ledger_v1(
        index=index,
        decisions=decision["component_decisions"],
        quarantined_components=decision["quarantined_components"],
    )
    verified = verify_registry_recovery_ledger_v1(ledger, index=index)
    quarantined_ticket_ids = {
        ticket_id
        for row in verified["quarantined_components"]
        for ticket_id in row["ticket_ids"]
    }
    resolved_ticket_ids = {
        row["ticket_id"] for row in verified["ticket_resolutions"]
    }
    assert not quarantined_ticket_ids.intersection(resolved_ticket_ids)
    assert quarantined_ticket_ids.union(resolved_ticket_ids) == {
        row["ticket_id"] for row in index["registry_gap_tickets"]
    }


def test_batch_render_is_deterministic_and_fingerprint_bound() -> None:
    index = _multi_component_index()
    component_ids = [
        row["component_id"] for row in index["registry_components"]
    ]
    first = render_registry_recovery_batch_request_v1(
        index=index,
        component_ids=component_ids,
    )
    second = render_registry_recovery_batch_request_v1(
        index=index,
        component_ids=component_ids,
    )
    assert first == second

    response, _ = _batch_response(index, component_ids)
    with pytest.raises(B2RecoveryContractError, match="fingerprint"):
        validate_registry_recovery_batch_response_v1(
            response,
            index=index,
            component_ids=component_ids,
            request_fingerprint="0" * 64,
        )
