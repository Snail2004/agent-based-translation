from __future__ import annotations

from copy import deepcopy
import json

import pytest

from pipeline.translate.d2l_translation_quality_auditor_v1 import (
    AuditContractError,
    AuditPacketCaps,
)
from pipeline.translate.d2l_translation_quality_auditor_v2 import (
    INPUT_CONTRACT_VERSION,
    INTEGRITY_POLICY_ID,
    PROMPT_ID,
    RESPONSE_CONTRACT_VERSION,
    RESPONSE_SCHEMA,
    build_packet,
    parse_response,
    render_messages,
    validate_packet,
    validate_response,
)


CAPS = AuditPacketCaps(4, 10_000)


def _receipt() -> dict:
    return {
        "policy_id": INTEGRITY_POLICY_ID,
        "full_text_restored": True,
        "source_target_math_byte_exact": True,
        "source_target_structure_order_equal": True,
        "forbidden_control_characters_absent": True,
        "protected_content_read_only": True,
    }


def _packet() -> dict:
    return build_packet(
        window_id="w1",
        blocks=[
            {
                "block_id": "b1",
                "block_type": "prose",
                "source_full_text": "The *derivative* of $f(x)=x^2$ is $2x$.",
                "target_full_text": "*Đạo hàm* của $f(x)=x^2$ là $2x$.",
                "applicable_glossary_refs": ["g1"],
            }
        ],
        glossary_cards=[
            {
                "glossary_ref": "g1",
                "source_term": "derivative",
                "allowed_target_variants": ["đạo hàm"],
                "policy": "mandatory",
            }
        ],
        integrity_receipt=_receipt(),
        caps=CAPS,
        glossary_token_counter=len,
    )


def _clean_response() -> dict:
    return {
        "contract_version": RESPONSE_CONTRACT_VERSION,
        "window_id": "w1",
        "audited_block_ids": ["b1"],
        "findings": [],
    }


def test_v2_packet_exposes_restored_latex_and_not_opaque_refs() -> None:
    packet = _packet()
    rendered = json.dumps(packet, ensure_ascii=False)
    messages = render_messages(packet, caps=CAPS, glossary_token_counter=len)

    assert packet["contract_version"] == INPUT_CONTRACT_VERSION
    assert PROMPT_ID == "d2l_translation_quality_audit_full_text_v2_1"
    assert "$f(x)=x^2$" in rendered
    assert "*derivative*" in rendered
    assert "MATH_REF" not in rendered
    assert "FORMAT_REF" not in rendered
    assert "same complete text that downstream publication will use" in messages[0]["content"]
    assert validate_packet(packet, caps=CAPS, glossary_token_counter=len) == packet


@pytest.mark.parametrize(
    "bad_text",
    [
        "Value [[MATH_REF_0001]].",
        "Value [[FORMAT_REF_0001|term]].",
        "Value \x0c broken.",
        "",
    ],
)
def test_v2_packet_rejects_non_restored_or_unsafe_full_text(bad_text: str) -> None:
    with pytest.raises(AuditContractError):
        build_packet(
            window_id="w1",
            blocks=[
                {
                    "block_id": "b1",
                    "block_type": "prose",
                    "source_full_text": "Source $x$.",
                    "target_full_text": bad_text,
                    "applicable_glossary_refs": [],
                }
            ],
            glossary_cards=[],
            integrity_receipt=_receipt(),
            caps=CAPS,
            glossary_token_counter=len,
        )


def test_v2_receipt_must_be_fully_true() -> None:
    receipt = _receipt()
    receipt["source_target_math_byte_exact"] = False
    with pytest.raises(AuditContractError, match="math_byte_exact"):
        build_packet(
            window_id="w1",
            blocks=[
                {
                    "block_id": "b1",
                    "block_type": "prose",
                    "source_full_text": "Source $x$.",
                    "target_full_text": "Đích $x$.",
                    "applicable_glossary_refs": [],
                }
            ],
            glossary_cards=[],
            integrity_receipt=receipt,
            caps=CAPS,
            glossary_token_counter=len,
        )


def test_v2_response_exact_cover_and_exact_evidence() -> None:
    packet = _packet()
    response = _clean_response()
    response["findings"] = [
        {
            "block_id": "b1",
            "issue_type": "terminology_context_error",
            "severity": "major",
            "source_evidence": "derivative",
            "target_evidence": "Đạo hàm",
            "reason": "The target term is claimed to be wrong in context.",
        }
    ]
    assert validate_response(response, packet)["findings"] == response["findings"]

    foreign = deepcopy(response)
    foreign["findings"][0]["target_evidence"] = "not in target"
    with pytest.raises(AuditContractError, match="target evidence is not exact"):
        validate_response(foreign, packet)

    missing = deepcopy(response)
    missing["audited_block_ids"] = []
    with pytest.raises(AuditContractError, match="exact-cover"):
        validate_response(missing, packet)


def test_v2_parser_rejects_duplicate_json_keys_and_schema_is_closed() -> None:
    assert RESPONSE_SCHEMA["additionalProperties"] is False
    with pytest.raises(AuditContractError, match="duplicates keys"):
        parse_response(
            '{"contract_version":"x","contract_version":"y",'
            '"window_id":"w1","audited_block_ids":[],"findings":[]}'
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("severity", "minor", "unknown severity 'minor'"),
        (
            "issue_type",
            "meaningful_english_content_untranslated",
            "unknown issue_type 'meaningful_english_content_untranslated'",
        ),
    ],
)
def test_v2_validator_names_unknown_closed_labels(
    field: str, value: str, message: str
) -> None:
    packet = _packet()
    response = _clean_response()
    finding = {
        "block_id": "b1",
        "issue_type": "untranslated_source_content",
        "severity": "major",
        "source_evidence": "derivative",
        "target_evidence": "Đạo hàm",
        "reason": "Test finding.",
    }
    finding[field] = value
    response["findings"] = [finding]

    with pytest.raises(AuditContractError, match=message):
        validate_response(response, packet)
