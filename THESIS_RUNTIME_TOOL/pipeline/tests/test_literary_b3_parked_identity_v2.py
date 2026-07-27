from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from pipeline.literary.b0_chapter_summary_v1 import collapse_unresolved_cases_v1
from pipeline.literary.b3_parked_identity_v1 import B3ParkedIdentityError
from pipeline.literary.b3_parked_identity_v2 import (
    attach_parked_identities_to_candidate_cards_v2,
    build_parked_identity_index_v2,
    verify_parked_identity_index_v2,
)
from pipeline.literary.b3_temporal_capability_contract_v4 import (
    synthetic_b3_probe_request_v7,
)
from pipeline.literary.b3_temporal_contract_v1 import B3TemporalContractError
from pipeline.literary.b3_temporal_contract_v7 import (
    normalize_b3_temporal_response_v7,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json


CARD_ID = "b0ent_shared"
REFERENT = "litref_shared"
HEARINGS = (
    ("b1xhear_broad", "Need a dated family record.", "b0ent_prior_broad"),
    ("b1xhear_narrow", "Need a block linking the title.", "b0ent_prior_narrow"),
)


def _hearing_root(tmp_path: Path) -> Path:
    root = tmp_path / "hearing"
    decisions = []
    for ordinal, (component_id, condition, prior_id) in enumerate(HEARINGS, 1):
        component_root = root / "components" / f"{ordinal:03d}_{component_id}"
        component_root.mkdir(parents=True)
        sections = {
            "current_card_snapshots": [{"entity_id": CARD_ID}],
            "prior_card_ids": [prior_id],
        }
        request = {
            "schema_version": "hearing_request_v1",
            "component_id": component_id,
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
                "component_id": component_id,
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
        decisions.append(
            {
                "component_id": component_id,
                "verdict": "insufficient_evidence",
                "resolution_condition": condition,
            }
        )
    (root / "validated_decisions.json").write_text(
        canonical_json(decisions) + "\n", encoding="utf-8"
    )
    return root


def _markers(index: dict) -> list[dict]:
    attached = attach_parked_identities_to_candidate_cards_v2(
        candidate_cards={
            CARD_ID: {
                "candidate_card_id": CARD_ID,
                "canonical_surface": "Edgar",
                "referent_ref": REFERENT,
            }
        },
        index=index,
    )
    return attached[CARD_ID]["parked_identities"]


def _parked_request(markers: list[dict]) -> dict:
    request = synthetic_b3_probe_request_v7()
    payload = json.loads(request["messages"][1]["content"])
    component = payload["components"][0]
    component["referent_refs"] = [REFERENT]
    payload["referent_packets"] = [
        {
            "referent_ref": REFERENT,
            "component_ids": [component["component_id"]],
            "candidate_card": {
                "candidate_card_id": CARD_ID,
                "referent_ref": REFERENT,
                "identity_scope": "chapter_provisional",
                "parked_identities": markers,
            },
        }
    ]
    request["messages"][1]["content"] = canonical_json(payload)
    unsigned = dict(request)
    unsigned.pop("request_fingerprint")
    return {**unsigned, "request_fingerprint": canonical_hash(unsigned)}


def _response(request: dict, markers: list[dict]) -> dict:
    inherited = [
        {
            "hearing_component_id": marker["hearing_component_id"],
            "resolution_condition": marker["resolution_condition"],
        }
        for marker in markers
    ]
    return {
        "schema_version": "literary_b3_temporal_response_v3",
        "chapter_id": request["chapter_id"],
        "batch_id": request["batch_id"],
        "component_results": [
            {
                "component_id": request["component_ids"][0],
                "disposition": "pending_review",
                "state_actions": [],
                "pending_route": "inherited_identity_block",
                "pending_reason": "The state depends on both parked questions.",
                "inherited_parked_identities": inherited,
            }
        ],
    }


def test_one_card_preserves_two_distinct_parked_hearings(tmp_path: Path) -> None:
    index = build_parked_identity_index_v2(_hearing_root(tmp_path))
    assert len(index["parked_identities"]) == 2
    markers = _markers(index)
    assert [row["hearing_component_id"] for row in markers] == [
        "b1xhear_broad",
        "b1xhear_narrow",
    ]
    assert all(row["parked_set_partially_supplied"] for row in markers)


def test_v7_pending_case_carries_every_applicable_hearing(tmp_path: Path) -> None:
    index = build_parked_identity_index_v2(_hearing_root(tmp_path))
    markers = _markers(index)
    request = _parked_request(markers)
    artifact = normalize_b3_temporal_response_v7(
        request=request,
        response=_response(request, markers),
    )
    pending = artifact["pending_cases"][0]
    assert pending["review_route"] == "inherited_identity_block"
    assert len(pending["inherited_parked_identities"]) == 2


def test_v7_foreign_hearing_marker_fails_closed(tmp_path: Path) -> None:
    index = build_parked_identity_index_v2(_hearing_root(tmp_path))
    markers = _markers(index)
    request = _parked_request(markers)
    response = _response(request, markers)
    response["component_results"][0]["inherited_parked_identities"][0][
        "resolution_condition"
    ] = "Invented condition."
    with pytest.raises(B3TemporalContractError, match="differs from supplied"):
        normalize_b3_temporal_response_v7(request=request, response=response)


def test_b0_surfaces_two_parked_nodes_for_one_b3_case(tmp_path: Path) -> None:
    index = build_parked_identity_index_v2(_hearing_root(tmp_path))
    markers = _response(_parked_request(_markers(index)), _markers(index))[
        "component_results"
    ][0]["inherited_parked_identities"]
    rows = collapse_unresolved_cases_v1(
        b1_cases=[],
        b2_cases=[],
        b3_cases=[
            {
                "pending_case_id": "b3pend_shared",
                "review_route": "inherited_identity_block",
                "inherited_parked_identities": markers,
            }
        ],
        parked_identity_index=index,
    )
    parked = [row for row in rows if row["kind"] == "parked_identity"]
    assert {row["case_id"] for row in parked} == {
        "b1xhear_broad",
        "b1xhear_narrow",
    }


def test_duplicate_hearing_component_still_fails_closed(tmp_path: Path) -> None:
    index = build_parked_identity_index_v2(_hearing_root(tmp_path))
    tampered = deepcopy(index)
    tampered["parked_identities"].append(
        deepcopy(tampered["parked_identities"][0])
    )
    body = dict(tampered)
    body.pop("index_hash")
    tampered["index_hash"] = canonical_hash(body)
    with pytest.raises(B3ParkedIdentityError, match="component repeats"):
        verify_parked_identity_index_v2(tampered)
