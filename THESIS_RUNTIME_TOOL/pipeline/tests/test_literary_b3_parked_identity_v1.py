from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from pipeline.literary.b3_parked_identity_v1 import (
    B3ParkedIdentityError,
    attach_parked_identity_to_candidate_cards_v1,
    build_parked_identity_index_v1,
)
from pipeline.literary.b3_temporal_contract_v1 import B3TemporalContractError
from pipeline.literary.b3_temporal_context_v1 import _deduplicated_batch_context
from pipeline.literary.b3_temporal_contract_v5 import (
    normalize_b3_temporal_response_v5,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json
from pipeline.literary.openai_b3_json_object_capability_probe_v1 import (
    synthetic_b3_probe_request_v5,
)


COMPONENT_ID = "b1xhear_parked"
RESOLUTION = "A later dated source block must settle the linkage."
CARD_IDS = ["b0ent_current", "b0ent_prior"]
REFERENT = "litref1_current"


def _hearing_root(tmp_path: Path) -> Path:
    root = tmp_path / "hearing"
    component_root = root / "components" / f"001_{COMPONENT_ID}"
    component_root.mkdir(parents=True)
    sections = {
        "current_card_snapshots": [{"entity_id": CARD_IDS[0]}],
        "prior_card_ids": [CARD_IDS[1]],
    }
    request = {
        "schema_version": "hearing_request_v1",
        "component_id": COMPONENT_ID,
        "queue_hash": "a" * 64,
        "prompt_id": "prompt",
        "prompt_sha256": "b" * 64,
        "response_schema_hash": "c" * 64,
        "model_contract": {"mode": "prompt_validated"},
        "sections": sections,
    }
    request["request_fingerprint"] = canonical_hash(
        {
            "request_schema_version": request["schema_version"],
            "component_id": COMPONENT_ID,
            "queue_hash": request["queue_hash"],
            "prompt_id": request["prompt_id"],
            "prompt_sha256": request["prompt_sha256"],
            "response_schema_hash": request["response_schema_hash"],
            "model_contract": request["model_contract"],
            "sections_hash": canonical_hash(sections),
        }
    )
    (component_root / "request.json").write_text(
        canonical_json(request) + "\n", encoding="utf-8"
    )
    decisions = [
        {
            "component_id": COMPONENT_ID,
            "verdict": "insufficient_evidence",
            "resolution_condition": RESOLUTION,
        },
        {
            "component_id": "b1xhear_resolved",
            "verdict": "alias_confirmed",
            "resolution_condition": None,
        },
    ]
    (root / "validated_decisions.json").write_text(
        canonical_json(decisions) + "\n", encoding="utf-8"
    )
    return root


def test_index_joins_validated_decision_to_exact_component_request(tmp_path: Path) -> None:
    index = build_parked_identity_index_v1(_hearing_root(tmp_path))
    assert index["parked_identities"] == [
        {
            "hearing_component_id": COMPONENT_ID,
            "resolution_condition": RESOLUTION,
            "card_ids": CARD_IDS,
        }
    ]


def test_attachment_uses_card_id_not_surface_and_marks_partial(tmp_path: Path) -> None:
    index = build_parked_identity_index_v1(_hearing_root(tmp_path))
    cards = {
        CARD_IDS[0]: {
            "candidate_card_id": CARD_IDS[0],
            "canonical_surface": "Hareton Earnshaw",
            "referent_ref": REFERENT,
        },
        "b0ent_same_surface": {
            "candidate_card_id": "b0ent_same_surface",
            "canonical_surface": "Hareton Earnshaw",
            "referent_ref": "litref1_other",
        },
    }
    attached = attach_parked_identity_to_candidate_cards_v1(
        candidate_cards=cards,
        index=index,
    )
    assert "parked_identity" not in attached["b0ent_same_surface"]
    marker = attached[CARD_IDS[0]]["parked_identity"]
    assert marker["hearing_component_id"] == COMPONENT_ID
    assert marker["co_parked_referent_refs"] == []
    assert marker["parked_set_partially_supplied"] is True


def test_batch_scope_removes_unsupplied_co_parked_referents() -> None:
    component = {
        "component_id": "b3comp_current",
        "component_hash": "a" * 64,
        "component_kind": "entity_state_evidence",
        "domain_hints": [],
        "referent_refs": [REFERENT],
        "candidate_cards": [
            {
                "candidate_card_id": CARD_IDS[0],
                "referent_ref": REFERENT,
                "parked_identity": {
                    "hearing_component_id": COMPONENT_ID,
                    "resolution_condition": RESOLUTION,
                    "co_parked_referent_refs": ["litref1_prior"],
                    "parked_set_partially_supplied": False,
                },
            }
        ],
        "speaker_turns": [],
        "salient_events": [],
        "source_blocks": [],
        "frame_segments": [],
        "prior_open_states": [],
        "b2_review_requests": [],
    }
    packet = _deduplicated_batch_context([component])["referent_packets"][0]
    marker = packet["candidate_card"]["parked_identity"]
    assert marker["co_parked_referent_refs"] == []
    assert marker["parked_set_partially_supplied"] is True


def test_duplicate_card_across_parked_hearings_fails_closed(tmp_path: Path) -> None:
    root = _hearing_root(tmp_path)
    request = json.loads(
        (root / "components" / f"001_{COMPONENT_ID}" / "request.json").read_text(
            encoding="utf-8"
        )
    )
    second = deepcopy(request)
    second["component_id"] = "b1xhear_second"
    second["request_fingerprint"] = canonical_hash(
        {
            "request_schema_version": second["schema_version"],
            "component_id": second["component_id"],
            "queue_hash": second["queue_hash"],
            "prompt_id": second["prompt_id"],
            "prompt_sha256": second["prompt_sha256"],
            "response_schema_hash": second["response_schema_hash"],
            "model_contract": second["model_contract"],
            "sections_hash": canonical_hash(second["sections"]),
        }
    )
    second_root = root / "components" / "002_b1xhear_second"
    second_root.mkdir()
    (second_root / "request.json").write_text(
        canonical_json(second) + "\n", encoding="utf-8"
    )
    decisions_path = root / "validated_decisions.json"
    decisions = json.loads(decisions_path.read_text(encoding="utf-8"))
    decisions.append(
        {
            "component_id": "b1xhear_second",
            "verdict": "insufficient_evidence",
            "resolution_condition": "Another condition.",
        }
    )
    decisions_path.write_text(canonical_json(decisions) + "\n", encoding="utf-8")
    with pytest.raises(B3ParkedIdentityError, match="multiple parked"):
        build_parked_identity_index_v1(root)


def _parked_request() -> dict:
    request = synthetic_b3_probe_request_v5()
    payload = json.loads(request["messages"][1]["content"])
    component = payload["components"][0]
    component["referent_refs"] = [REFERENT]
    component["frame_segment_ids"] = ["frame_1"]
    component["speaker_turns"] = [
        {
            "speaker_turn_id": "turn_1",
            "block_id": "block_1",
            "evidence_authority": "provisional_grounded",
        }
    ]
    payload["referent_packets"] = [
        {
            "referent_ref": REFERENT,
            "component_ids": [component["component_id"]],
            "candidate_card": {
                "referent_ref": REFERENT,
                "identity_scope": "chapter_provisional",
                "parked_identity": {
                    "hearing_component_id": COMPONENT_ID,
                    "resolution_condition": RESOLUTION,
                    "co_parked_referent_refs": [],
                    "parked_set_partially_supplied": True,
                },
            },
        }
    ]
    payload["source_packets"] = [
        {
            "block_id": "block_1",
            "component_ids": [component["component_id"]],
            "source_text_sha256": "d" * 64,
        }
    ]
    payload["frame_packets"] = [
        {
            "frame_segment_id": "frame_1",
            "component_ids": [component["component_id"]],
            "frame": {
                "frame_segment_id": "frame_1",
                "relevant_block_ids": ["block_1"],
                "narrative_mode": "direct_current",
            },
        }
    ]
    request["messages"][1]["content"] = canonical_json(payload)
    unsigned = dict(request)
    unsigned.pop("request_fingerprint")
    return {**unsigned, "request_fingerprint": canonical_hash(unsigned)}


def _base_result(request: dict) -> dict:
    return {
        "schema_version": "literary_b3_temporal_response_v2",
        "chapter_id": request["chapter_id"],
        "batch_id": request["batch_id"],
        "component_results": [
            {
                "component_id": request["component_ids"][0],
                "disposition": "pending_review",
                "state_actions": [],
                "pending_route": "inherited_identity_block",
                "pending_reason": "The state depends on the parked linkage.",
                "inherited_parked_identity": {
                    "hearing_component_id": COMPONENT_ID,
                    "resolution_condition": RESOLUTION,
                },
            }
        ],
    }


def test_inherited_route_carries_exact_park_and_has_no_identity_rehearing() -> None:
    request = _parked_request()
    artifact = normalize_b3_temporal_response_v5(
        request=request,
        response=_base_result(request),
    )
    assert [row["review_route"] for row in artifact["pending_cases"]] == [
        "inherited_identity_block"
    ]
    assert artifact["pending_cases"][0]["inherited_parked_identity"] == {
        "hearing_component_id": COMPONENT_ID,
        "resolution_condition": RESOLUTION,
    }


def test_inherited_route_without_exact_supplied_park_fails_closed() -> None:
    request = _parked_request()
    response = _base_result(request)
    response["component_results"][0]["inherited_parked_identity"][
        "resolution_condition"
    ] = "invented condition"
    with pytest.raises(B3TemporalContractError, match="differs from supplied"):
        normalize_b3_temporal_response_v5(request=request, response=response)


def test_grounded_state_on_parked_referent_is_still_recorded() -> None:
    request = _parked_request()
    response = _base_result(request)
    result = response["component_results"][0]
    result.update(
        {
            "disposition": "state_actions_proposed",
            "pending_route": "none",
            "pending_reason": None,
            "inherited_parked_identity": {
                "hearing_component_id": COMPONENT_ID,
                "resolution_condition": RESOLUTION,
            },
            "state_actions": [
                {
                    "operation": "open_state",
                    "state_domain": "durable_disposition",
                    "subject_referent_refs": [REFERENT],
                    "counterpart_referent_refs": [],
                    "state_value": "remains openly hostile to the visitor",
                    "event_status": "occurred",
                    "temporal_position": "current_progression",
                    "source_event_ids": [],
                    "source_turn_ids": ["turn_1"],
                    "source_block_ids": ["block_1"],
                    "frame_segment_ids": ["frame_1"],
                    "reason": "The grounded turn supports a durable disposition.",
                }
            ],
        }
    )
    artifact = normalize_b3_temporal_response_v5(request=request, response=response)
    assert len(artifact["new_state_rows"]) == 1
    assert artifact["pending_cases"] == []
    assert artifact["new_state_rows"][0]["subject_referent_refs"] == [REFERENT]


def test_no_change_with_a_parked_identity_annotation_is_quarantined() -> None:
    request = _parked_request()
    response = _base_result(request)
    result = response["component_results"][0]
    result.update(
        {
            "disposition": "no_durable_change",
            "pending_route": "none",
            "pending_reason": None,
            "state_actions": [],
        }
    )
    artifact = normalize_b3_temporal_response_v5(
        request=request,
        response=response,
    )

    assert artifact["new_state_rows"] == []
    assert artifact["pending_cases"] == []
    assert len(artifact["quarantined_component_results"]) == 1
    quarantine = artifact["quarantined_component_results"][0]
    assert quarantine["component_id"] == request["component_ids"][0]
    assert quarantine["reason"] == "parked identity annotation has no proposed state"
    assert quarantine["raw_component_result"] == result
    assert quarantine["semantic_authority_granted"] is False
    normalized = artifact["component_results"][0]
    assert normalized["disposition"] == "quarantined"
    assert normalized["component_application_status"] == "quarantined"
    assert normalized["quarantined_component_result_id"] == (
        quarantine["quarantine_id"]
    )


def test_parked_identity_with_another_pending_route_is_quarantined() -> None:
    request = _parked_request()
    response = _base_result(request)
    result = response["component_results"][0]
    result["pending_route"] = "identity_review"

    artifact = normalize_b3_temporal_response_v5(
        request=request,
        response=response,
    )

    assert artifact["new_state_rows"] == []
    assert artifact["pending_cases"] == []
    assert len(artifact["quarantined_component_results"]) == 1
    quarantine = artifact["quarantined_component_results"][0]
    assert quarantine["component_id"] == request["component_ids"][0]
    assert (
        quarantine["reason"]
        == "parked identity pending case uses another review route"
    )
    assert quarantine["raw_component_result"] == result
    assert quarantine["semantic_authority_granted"] is False
    normalized = artifact["component_results"][0]
    assert normalized["disposition"] == "quarantined"
    assert normalized["component_application_status"] == "quarantined"
