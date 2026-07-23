from __future__ import annotations

import json

import pytest

from pipeline.prepass.d2l_b2_packet_contract_v2 import (
    B2PacketContractError,
    PROMPT_VERSION,
    RESPONSE_FORMAT,
    SYSTEM_PROMPT,
    parse_response_json,
    prompt_sha256,
    render_messages,
    validate_output,
)


def _packet() -> dict:
    return {
        "packet_id": "pkt_a",
        "chapter_id": "chapter",
        "candidates": [
            {
                "candidate_id": "cand_a",
                "normalized_surface": "technical unit",
                "surfaces": ["Technical Unit", "technical unit"],
                "source_block_ids": ["b1", "b2"],
                "window_ids": ["w1"],
                "evidence_block_ids": ["b1", "b2"],
                "evidence_complete": True,
                "support_block_count": 2,
                "window_count": 1,
            }
        ],
        "source_blocks": [
            {"block_id": "b1", "text": "A technical unit appears here."},
            {"block_id": "b2", "text": "The technical unit is reused."},
        ],
    }


def test_prompt_is_narrow_book_neutral_and_discovery_free() -> None:
    assert PROMPT_VERSION == "d2l_b2_admission_translation_v2"
    assert len(prompt_sha256()) == 64
    assert "admit" in SYSTEM_PROMPT
    assert "reject" in SYSTEM_PROMPT
    assert "review" in SYSTEM_PROMPT
    assert "Do not add an omitted candidate" in SYSTEM_PROMPT
    assert "rank candidates" in SYSTEM_PROMPT
    assert "added_from_source" not in SYSTEM_PROMPT
    for leaked_answer in (
        "tensor",
        "matrix",
        "probability",
        "automatic differentiation",
        "community-gold",
    ):
        assert leaked_answer not in SYSTEM_PROMPT.casefold()


def test_response_schema_is_closed_and_typed() -> None:
    schema = RESPONSE_FORMAT["json_schema"]["schema"]
    assert schema["additionalProperties"] is False
    decision = schema["properties"]["decisions"]["items"]
    assert decision["additionalProperties"] is False
    assert decision["properties"]["decision"]["enum"] == [
        "admit",
        "reject",
        "review",
    ]
    assert decision["properties"]["target_proposals"]["maxItems"] == 3


def test_renderer_keeps_candidates_and_evidence_joined() -> None:
    messages = render_messages(_packet())
    assert messages[0] == {"role": "system", "content": SYSTEM_PROMPT}
    user = messages[1]["content"]
    payload = user.split("CANDIDATE_PACKET_JSON\n", 1)[1].split(
        "\n\nENGLISH_SOURCE_BLOCKS", 1
    )[0]
    rendered = json.loads(payload)
    assert rendered["candidates"][0]["candidate_id"] == "cand_a"
    assert rendered["candidates"][0]["evidence_block_ids"] == ["b1", "b2"]
    assert user.count("[b1]") == 1
    assert user.count("[b2]") == 1
    assert "MEMORY_PACK" not in user


def test_renderer_rejects_evidence_absent_from_packet() -> None:
    packet = _packet()
    packet["source_blocks"].pop()
    with pytest.raises(B2PacketContractError, match="absent from packet source"):
        render_messages(packet)


def test_renderer_rejects_duplicate_source_blocks() -> None:
    packet = _packet()
    packet["source_blocks"].append(dict(packet["source_blocks"][0]))
    with pytest.raises(B2PacketContractError, match="block_id is invalid"):
        render_messages(packet)


def _valid_admit_payload() -> dict:
    return {
        "packet_id": "pkt_a",
        "decisions": [
            {
                "candidate_id": "cand_a",
                "decision": "admit",
                "canonical_source": "technical unit",
                "target_proposals": [
                    {"target_vi": "don vi ky thuat", "applicability": None}
                ],
                "directive": "translate",
                "evidence_block_ids": ["b1"],
                "rationale": "The expression is reused as a technical unit.",
            }
        ],
    }


def test_output_validator_exact_covers_and_accepts_typed_admit() -> None:
    validation = validate_output(
        parse_response_json(_valid_admit_payload()), packet=_packet()
    )
    assert validation.errors == ()
    assert validation.missing_candidate_ids == ()
    assert validation.normalization_warnings == ()
    assert validation.decisions[0].canonical_source == "technical unit"
    assert validation.decisions[0].target_proposals[0].target_vi == "don vi ky thuat"


@pytest.mark.parametrize("decision", ["reject", "review"])
def test_non_admit_cannot_smuggle_translation_payload(decision: str) -> None:
    payload = _valid_admit_payload()
    payload["decisions"][0]["decision"] = decision
    validation = validate_output(payload, packet=_packet())
    assert any("must not carry translation payload" in row for row in validation.errors)


@pytest.mark.parametrize("decision", ["reject", "review"])
def test_non_admit_normalizes_only_redundant_supplied_surface(
    decision: str,
) -> None:
    payload = _valid_admit_payload()
    row = payload["decisions"][0]
    row["decision"] = decision
    row["target_proposals"] = []
    row["directive"] = None
    validation = validate_output(payload, packet=_packet())
    assert validation.errors == ()
    assert validation.decisions[0].canonical_source is None
    assert len(validation.normalization_warnings) == 1
    assert "redundant canonical_source" in validation.normalization_warnings[0]


def test_non_admit_rejects_invented_surface_even_without_translation_payload() -> None:
    payload = _valid_admit_payload()
    row = payload["decisions"][0]
    row["decision"] = "reject"
    row["canonical_source"] = "invented surface"
    row["target_proposals"] = []
    row["directive"] = None
    validation = validate_output(payload, packet=_packet())
    assert any("must not carry translation payload" in row for row in validation.errors)
    assert validation.normalization_warnings == ()


def test_output_validator_rejects_unsupplied_surface_and_evidence() -> None:
    payload = _valid_admit_payload()
    payload["decisions"][0]["canonical_source"] = "invented surface"
    payload["decisions"][0]["evidence_block_ids"] = ["outside"]
    validation = validate_output(payload, packet=_packet())
    assert validation.errors


def test_output_validator_reports_missing_and_duplicate_candidates() -> None:
    payload = _valid_admit_payload()
    payload["decisions"].append(dict(payload["decisions"][0]))
    validation = validate_output(payload, packet=_packet())
    assert validation.duplicate_candidate_ids == ("cand_a",)
    payload["decisions"] = []
    validation = validate_output(payload, packet=_packet())
    assert validation.missing_candidate_ids == ("cand_a",)
