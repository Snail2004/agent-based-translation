from __future__ import annotations

import json

import pytest

from pipeline.prepass.d2l_b2_consistency_contract_v3 import (
    B2V3ContractError,
    PROMPT_VERSION,
    RESPONSE_FORMAT,
    SYSTEM_PROMPT,
    VALIDATOR_VERSION,
    parse_response_json,
    prompt_sha256,
    render_messages,
    schema_sha256,
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
            {"block_id": "b2", "text": "The technical unit has another use."},
        ],
    }


def _admit() -> dict:
    return {
        "packet_id": "pkt_a",
        "decisions": [
            {
                "candidate_id": "cand_a",
                "decision": "admit",
                "canonical_source": "technical unit",
                "directive": "translate",
                "primary_target_vi": "don vi ky thuat",
                "primary_use": None,
                "alternates": [],
                "evidence_block_ids": ["b1"],
                "rationale": "Persistent control prevents inconsistent rendering.",
            }
        ],
    }


def test_prompt_is_versioned_book_neutral_and_consistency_driven() -> None:
    assert PROMPT_VERSION == "d2l_b2_consistency_admission_v3"
    assert VALIDATOR_VERSION == "d2l_b2_consistency_admission_validator_v3_2"
    assert prompt_sha256() == "A913E892A2E8BC1F3BAF7ABDDD96007FF8F11A158E3E649E8D4C6C4FF606CE62"
    assert schema_sha256() == "2E5CB7E416037431E98470702FB2AC540ADEC6C06558E10C49300C5AE9414D0A"
    assert "persistent book-level translation rule" in SYSTEM_PROMPT
    assert "Technicality, frequency, and subject relevance" in SYSTEM_PROMPT
    assert "Without an affirmative need for persistent control, use reject" in SYSTEM_PROMPT
    assert "stylistic synonym" in SYSTEM_PROMPT
    for leaked_answer in (
        "tensor",
        "matrix",
        "probability",
        "automatic differentiation",
        "community-gold",
    ):
        assert leaked_answer not in SYSTEM_PROMPT.casefold()


def test_schema_closes_secondary_target_shape() -> None:
    schema = RESPONSE_FORMAT["json_schema"]["schema"]
    decision = schema["properties"]["decisions"]["items"]
    assert schema["additionalProperties"] is False
    assert decision["additionalProperties"] is False
    secondary = decision["properties"]["alternates"]
    assert secondary["maxItems"] == 2
    assert secondary["items"]["additionalProperties"] is False
    assert set(secondary["items"]["required"]) == {
        "target_vi",
        "use_when",
        "evidence_block_ids",
    }


def test_renderer_preserves_joined_candidate_and_source_bytes() -> None:
    messages = render_messages(_packet())
    assert messages[0] == {"role": "system", "content": SYSTEM_PROMPT}
    user = messages[1]["content"]
    payload = user.split("CANDIDATE_PACKET_JSON\n", 1)[1].split(
        "\n\nENGLISH_SOURCE_BLOCKS", 1
    )[0]
    rendered = json.loads(payload)
    assert rendered["candidates"][0]["evidence_block_ids"] == ["b1", "b2"]
    assert user.count("[b1]") == 1
    assert user.count("[b2]") == 1
    assert "MEMORY_PACK" not in user


def test_simple_admit_has_one_primary_and_no_secondary() -> None:
    validation = validate_output(parse_response_json(_admit()), packet=_packet())
    assert validation.errors == ()
    assert validation.decisions[0].primary_target_vi == "don vi ky thuat"
    assert validation.decisions[0].alternates == ()


@pytest.mark.parametrize("decision", ["reject", "review"])
def test_non_admit_cannot_carry_translation_payload(decision: str) -> None:
    payload = _admit()
    payload["decisions"][0]["decision"] = decision
    validation = validate_output(payload, packet=_packet())
    assert any("must not carry translation payload" in row for row in validation.errors)


def test_contextual_secondary_requires_scope_and_evidence() -> None:
    payload = _admit()
    row = payload["decisions"][0]
    row["directive"] = "contextual"
    row["primary_use"] = "Use for the first supplied technical sense."
    row["alternates"] = [
        {
            "target_vi": "don vi chuyen mon",
            "use_when": "Use only in the second supplied technical use class.",
            "evidence_block_ids": ["b2"],
        }
    ]
    validation = validate_output(payload, packet=_packet())
    assert validation.errors == ()
    assert len(validation.decisions[0].alternates) == 1


def test_non_contextual_secondary_is_rejected() -> None:
    payload = _admit()
    payload["decisions"][0]["alternates"] = [
        {
            "target_vi": "don vi chuyen mon",
            "use_when": "Use in a distinct supplied technical sense.",
            "evidence_block_ids": ["b2"],
        }
    ]
    validation = validate_output(payload, packet=_packet())
    assert any("non-contextual admit" in row for row in validation.errors)


def test_exact_duplicate_secondary_is_dropped_with_warning() -> None:
    payload = _admit()
    row = payload["decisions"][0]
    row["directive"] = "contextual"
    row["primary_use"] = "Use in the primary source class."
    row["alternates"] = [
        {
            "target_vi": "don vi ky thuat",
            "use_when": "Use in another source-use class.",
            "evidence_block_ids": ["b2"],
        }
    ]
    validation = validate_output(payload, packet=_packet())
    assert validation.errors == ()
    assert validation.decisions[0].alternates == ()
    assert len(validation.normalization_warnings) == 1
    assert "duplicated an earlier target" in validation.normalization_warnings[0]


def test_malformed_duplicate_secondary_still_invalidates_candidate() -> None:
    payload = _admit()
    row = payload["decisions"][0]
    row["directive"] = "contextual"
    row["primary_use"] = "Use in the primary source class."
    row["alternates"] = [
        {
            "target_vi": "don vi ky thuat",
            "use_when": "",
            "evidence_block_ids": ["b2"],
        }
    ]
    validation = validate_output(payload, packet=_packet())
    assert validation.decisions == ()
    assert validation.normalization_warnings == ()
    assert any("use_when is invalid" in row for row in validation.errors)


def test_preserve_requires_exact_source_target() -> None:
    payload = _admit()
    row = payload["decisions"][0]
    row["directive"] = "preserve"
    validation = validate_output(payload, packet=_packet())
    assert any("must equal canonical_source" in row for row in validation.errors)
    row["primary_target_vi"] = "technical unit"
    validation = validate_output(payload, packet=_packet())
    assert validation.errors == ()


def test_foreign_evidence_missing_and_duplicate_fail_closed() -> None:
    payload = _admit()
    payload["decisions"][0]["evidence_block_ids"] = ["outside"]
    validation = validate_output(payload, packet=_packet())
    assert validation.errors

    payload = _admit()
    payload["decisions"].append(dict(payload["decisions"][0]))
    validation = validate_output(payload, packet=_packet())
    assert validation.duplicate_candidate_ids == ("cand_a",)

    payload["decisions"] = []
    validation = validate_output(payload, packet=_packet())
    assert validation.missing_candidate_ids == ("cand_a",)


def test_rendered_candidate_occurrence_outside_sample_is_allowed_with_warning() -> None:
    packet = _packet()
    packet["source_blocks"].append(
        {"block_id": "b3", "text": "The technical unit is used again."}
    )
    packet["candidates"][0]["source_block_ids"].append("b3")
    payload = _admit()
    payload["decisions"][0]["evidence_block_ids"] = ["b3"]
    validation = validate_output(payload, packet=packet)
    assert validation.errors == ()
    assert len(validation.normalization_warnings) == 1
    assert "rendered candidate occurrence b3" in validation.normalization_warnings[0]


def test_rendered_block_for_another_candidate_remains_invalid() -> None:
    packet = _packet()
    packet["source_blocks"].append(
        {"block_id": "b3", "text": "A different concept occurs here."}
    )
    payload = _admit()
    payload["decisions"][0]["evidence_block_ids"] = ["b3"]
    validation = validate_output(payload, packet=packet)
    assert validation.decisions == ()
    assert any("outside the candidate packet" in row for row in validation.errors)


def test_surface_locator_does_not_accept_a_substring_inside_another_word() -> None:
    packet = _packet()
    packet["candidates"][0]["surfaces"] = ["mean"]
    packet["source_blocks"].append(
        {"block_id": "b3", "text": "The meaning remains local prose."}
    )
    payload = _admit()
    payload["decisions"][0]["canonical_source"] = "mean"
    payload["decisions"][0]["evidence_block_ids"] = ["b3"]
    validation = validate_output(payload, packet=packet)
    assert validation.decisions == ()
    assert any("outside the candidate packet" in row for row in validation.errors)


def test_unrendered_candidate_occurrence_remains_invalid() -> None:
    packet = _packet()
    packet["candidates"][0]["source_block_ids"].append("b9")
    payload = _admit()
    payload["decisions"][0]["evidence_block_ids"] = ["b9"]
    validation = validate_output(payload, packet=packet)
    assert validation.decisions == ()
    assert any("outside the candidate packet" in row for row in validation.errors)


def test_renderer_rejects_evidence_absent_from_source_packet() -> None:
    packet = _packet()
    packet["source_blocks"].pop()
    with pytest.raises(B2V3ContractError, match="absent from packet source"):
        render_messages(packet)
