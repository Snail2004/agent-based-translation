from __future__ import annotations

from pipeline.prepass import d2l_b2_consistency_contract_v3_3 as v33
from pipeline.prepass import d2l_b2_consistency_contract_v3_4 as v34


def _packet() -> dict:
    return {
        "packet_id": "pkt_a",
        "chapter_id": "chapter",
        "candidates": [
            {
                "candidate_id": "cand_a",
                "normalized_surface": "technical unit",
                "surfaces": ["Technical Unit"],
                "source_block_ids": ["b1"],
                "window_ids": ["w1"],
                "evidence_block_ids": ["b1"],
                "evidence_complete": True,
                "support_block_count": 1,
                "window_count": 1,
            }
        ],
        "source_blocks": [
            {"block_id": "b1", "text": "A Technical Unit appears here."}
        ],
    }


def test_v34_is_prompt_only_delta_from_v33() -> None:
    old_messages = v33.render_messages(_packet())
    new_messages = v34.render_messages(_packet())
    assert v34.PROMPT_VERSION == "d2l_b2_consistency_admission_v3_4"
    assert v34.prompt_sha256() != v33.prompt_sha256()
    assert v34.schema_sha256() == v33.schema_sha256()
    assert v34.VALIDATOR_VERSION == v33.VALIDATOR_VERSION
    assert new_messages[1:] == old_messages[1:]
    assert v34.user_payload_sha256(new_messages) == v33.user_payload_sha256(
        old_messages
    )


def test_v34_applies_closed_operational_evidence_gate() -> None:
    prompt = v34.SYSTEM_PROMPT
    compact_prompt = " ".join(prompt.split())
    assert "EVIDENCE ROLE GATE - APPLY BEFORE THE TWO TESTS" in prompt
    assert "If all supplied evidence is operational, reject" in prompt
    assert "same implementation subsystem or workflow" in prompt
    assert "Do not infer unseen book-wide use" in prompt
    assert "explains its principles rather than merely using it" in compact_prompt
    assert "Prompt version: d2l_b2_consistency_admission_v3_3" not in prompt
