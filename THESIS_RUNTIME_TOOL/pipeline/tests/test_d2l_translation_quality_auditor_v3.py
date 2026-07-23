from __future__ import annotations

from copy import deepcopy
import json

import pytest

from pipeline.translate.d2l_translation_quality_auditor_v1 import (
    AuditContractError,
)
from pipeline.translate.d2l_translation_quality_auditor_v3 import (
    INPUT_CONTRACT_VERSION,
    INTEGRITY_POLICY_ID,
    ISSUE_TYPES,
    RESPONSE_CONTRACT_VERSION,
    RESPONSE_SCHEMA,
    build_packet,
    parse_response,
    render_messages,
    validate_packet,
    validate_response,
)


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
                "source_full_text": "The value of $x$ is positive.",
                "target_full_text": "Giá trị của $x$ là dương.",
            }
        ],
        integrity_receipt=_receipt(),
    )


def _response() -> dict:
    return {
        "contract_version": RESPONSE_CONTRACT_VERSION,
        "window_id": "w1",
        "audited_block_ids": ["b1"],
        "findings": [],
    }


def test_v3_packet_and_prompt_are_glossary_blind() -> None:
    packet = _packet()
    rendered = json.dumps(
        {"packet": packet, "messages": render_messages(packet)},
        ensure_ascii=False,
    )

    assert packet["contract_version"] == INPUT_CONTRACT_VERSION
    assert validate_packet(packet) == packet
    assert "glossary_cards" not in rendered
    assert "applicable_glossary_refs" not in rendered
    assert "terminology_context_error" not in rendered
    assert "terminology_context_error" not in ISSUE_TYPES
    assert "terminology_context_error" not in RESPONSE_SCHEMA["properties"][
        "findings"
    ]["items"]["properties"]["issue_type"]["enum"]


def test_v3_validates_exact_semantic_evidence() -> None:
    packet = _packet()
    response = _response()
    response["findings"] = [
        {
            "block_id": "b1",
            "issue_type": "polarity_or_negation_error",
            "severity": "major",
            "source_evidence": "positive",
            "target_evidence": "dương",
            "reason": "Synthetic exact-evidence finding.",
        }
    ]

    assert validate_response(response, packet)["findings"] == response["findings"]
    bad = deepcopy(response)
    bad["findings"][0]["target_evidence"] = "absent"
    with pytest.raises(AuditContractError, match="target evidence is not exact"):
        validate_response(bad, packet)


def test_v3_rejects_terminology_findings() -> None:
    response = _response()
    response["findings"] = [
        {
            "block_id": "b1",
            "issue_type": "terminology_context_error",
            "severity": "major",
            "source_evidence": "value",
            "target_evidence": "Giá trị",
            "reason": "Glossary enforcement is outside this contract.",
        }
    ]

    with pytest.raises(AuditContractError, match="unknown issue_type"):
        validate_response(response, _packet())


def test_v3_rejects_duplicate_keys() -> None:
    with pytest.raises(AuditContractError, match="duplicates keys"):
        parse_response(
            '{"contract_version":"x","contract_version":"y",'
            '"window_id":"w1","audited_block_ids":[],"findings":[]}'
        )


def test_v3_json_fence_and_protected_ref_boundaries() -> None:
    response_json = json.dumps(_response(), separators=(",", ":"))

    assert parse_response(f"```json\n{response_json}\n```") == _response()
    with pytest.raises(AuditContractError, match="JSON parse failed"):
        parse_response(f"discussion\n```json\n{response_json}\n```")
    with pytest.raises(AuditContractError, match="protected reference"):
        build_packet(
            window_id="w1",
            blocks=[
                {
                    "block_id": "b1",
                    "block_type": "prose",
                    "source_full_text": "Source $x$.",
                    "target_full_text": "Đích [[MATH_REF_0001]].",
                }
            ],
            integrity_receipt=_receipt(),
        )
