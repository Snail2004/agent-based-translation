from __future__ import annotations

import copy
import json

import pytest

from pipeline.eval.contracts_v1 import ContractValidationError
from pipeline.eval.scorer_prompts_v3 import (
    SF_BT_SEMANTIC_CANDIDATE_ID,
    SF_BT_SEMANTIC_PROMPT_SHA256,
    render_sf_bt_semantic_passages_v3,
)
from pipeline.eval.sf_bt_band_calibration_packet_v1 import (
    build_sf_bt_band_calibration_packet_v1,
    validate_sf_bt_band_calibration_packet_binding_v1,
    validate_sf_bt_band_calibration_packet_v1,
)
from pipeline.eval.sf_bt_band_calibration_v1 import (
    load_default_sf_bt_band_calibration_fixture,
)


NOW = "2026-07-20T12:00:00Z"
COMMIT = "a" * 40


def test_packet_is_honestly_bound_without_fixture_oracle_fields() -> None:
    fixture = load_default_sf_bt_band_calibration_fixture()
    case = fixture["cases"][0]
    packet = build_sf_bt_band_calibration_packet_v1(
        fixture,
        case_id=case["case_id"],
        orientation="canonical",
        created_at=NOW,
        producer_code_commit=COMMIT,
    )

    assert validate_sf_bt_band_calibration_packet_binding_v1(packet, fixture) == packet
    rendered = json.dumps(packet, sort_keys=True)
    assert "expected_score" not in rendered
    assert "expected_primary_reason" not in rendered
    assert "author_note" not in rendered
    assert case["author_note"] not in rendered
    assert packet["binding"]["fixture_sha256"]
    assert packet["binding"]["case_id"] == case["case_id"]


def test_reversed_orientation_swaps_only_passage_slots() -> None:
    fixture = load_default_sf_bt_band_calibration_fixture()
    case_id = fixture["cases"][0]["case_id"]
    canonical = build_sf_bt_band_calibration_packet_v1(
        fixture,
        case_id=case_id,
        orientation="canonical",
        created_at=NOW,
        producer_code_commit=COMMIT,
    )
    reversed_packet = build_sf_bt_band_calibration_packet_v1(
        fixture,
        case_id=case_id,
        orientation="reversed",
        created_at=NOW,
        producer_code_commit=COMMIT,
    )

    assert canonical["passages"][0]["text"] == reversed_packet["passages"][1]["text"]
    assert canonical["passages"][1]["text"] == reversed_packet["passages"][0]["text"]
    assert canonical["binding"]["presentation_id"] != reversed_packet["binding"][
        "presentation_id"
    ]
    assert canonical["binding"]["fixture_sha256"] == reversed_packet["binding"][
        "fixture_sha256"
    ]


def test_direct_renderer_keeps_existing_prompt_identity_and_hides_packet_metadata() -> None:
    fixture = load_default_sf_bt_band_calibration_fixture()
    packet = build_sf_bt_band_calibration_packet_v1(
        fixture,
        case_id=fixture["cases"][0]["case_id"],
        orientation="canonical",
        created_at=NOW,
        producer_code_commit=COMMIT,
    )
    passages = {row["slot_id"]: row["text"] for row in packet["passages"]}
    rendered = render_sf_bt_semantic_passages_v3(
        passage_a=passages["passage_a"], passage_b=passages["passage_b"]
    )

    assert rendered.candidate_id == SF_BT_SEMANTIC_CANDIDATE_ID
    assert rendered.prompt_sha256 == SF_BT_SEMANTIC_PROMPT_SHA256
    assert passages["passage_a"] in rendered.rendered_prompt
    assert passages["passage_b"] in rendered.rendered_prompt
    assert packet["binding"]["presentation_id"] not in rendered.rendered_prompt
    assert packet["binding"]["case_id"] not in rendered.rendered_prompt


def test_packet_rejects_unapproved_fixture_unknown_case_and_tampering() -> None:
    fixture = load_default_sf_bt_band_calibration_fixture()
    draft = copy.deepcopy(fixture)
    draft["review_status"] = "draft_requires_independent_semantic_review"
    with pytest.raises(ContractValidationError, match="fixture_review"):
        build_sf_bt_band_calibration_packet_v1(
            draft,
            case_id=draft["cases"][0]["case_id"],
            orientation="canonical",
            created_at=NOW,
            producer_code_commit=COMMIT,
        )

    with pytest.raises(ContractValidationError, match="case_reference"):
        build_sf_bt_band_calibration_packet_v1(
            fixture,
            case_id="foreign_case",
            orientation="canonical",
            created_at=NOW,
            producer_code_commit=COMMIT,
        )

    packet = build_sf_bt_band_calibration_packet_v1(
        fixture,
        case_id=fixture["cases"][0]["case_id"],
        orientation="canonical",
        created_at=NOW,
        producer_code_commit=COMMIT,
    )
    tampered = copy.deepcopy(packet)
    tampered["passages"][0]["text"] += " changed"
    with pytest.raises(ContractValidationError, match="passage_hash"):
        validate_sf_bt_band_calibration_packet_v1(tampered)

    tampered = copy.deepcopy(packet)
    tampered["binding"]["case_id"] = fixture["cases"][1]["case_id"]
    with pytest.raises(ContractValidationError, match="packet_hash"):
        validate_sf_bt_band_calibration_packet_v1(tampered)


def test_packet_validation_does_not_mutate_input() -> None:
    fixture = load_default_sf_bt_band_calibration_fixture()
    packet = build_sf_bt_band_calibration_packet_v1(
        fixture,
        case_id=fixture["cases"][0]["case_id"],
        orientation="canonical",
        created_at=NOW,
        producer_code_commit=COMMIT,
    )
    original = copy.deepcopy(packet)

    validate_sf_bt_band_calibration_packet_v1(packet)

    assert packet == original
