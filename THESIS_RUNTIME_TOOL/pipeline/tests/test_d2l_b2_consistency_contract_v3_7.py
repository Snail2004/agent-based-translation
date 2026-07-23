from __future__ import annotations

import json

from pipeline.prepass import d2l_b2_consistency_contract_v3_7 as v37


def _packet() -> dict:
    return {
        "packet_id": "pkt_projection",
        "chapter_id": "chapter",
        "candidates": [
            {
                "candidate_id": "cand_loss",
                "normalized_surface": "loss function",
                "surfaces": ["loss function"],
                "source_block_ids": ["b1", "provenance_only_b9"],
                "window_ids": ["w1", "w9"],
                "evidence_block_ids": ["b1"],
                "evidence_complete": False,
                "support_block_count": 2,
                "window_count": 2,
            }
        ],
        "source_blocks": [
            {"block_id": "b1", "text": "We minimize the loss function."},
            {
                "block_id": "b2",
                "text": "The loss function appears in shared packet context.",
            },
        ],
    }


def _admit(evidence: list[str]) -> dict:
    return {
        "packet_id": "pkt_projection",
        "decisions": [
            {
                "candidate_id": "cand_loss",
                "decision": "admit",
                "canonical_source": "loss function",
                "directive": "translate",
                "primary_target_vi": "hàm mất mát",
                "primary_use": None,
                "alternates": [],
                "evidence_block_ids": evidence,
                "rationale": "The source teaches this optimization concept.",
            }
        ],
    }


def test_v37_model_packet_omits_broad_code_owned_provenance() -> None:
    packet = _packet()
    messages = v37.render_messages(packet)
    user = messages[1]["content"]
    candidate_json = user.split("\n\nENGLISH_SOURCE_BLOCKS\n", 1)[0]
    projected = json.loads(candidate_json.split("\n", 1)[1])
    candidate = projected["candidates"][0]
    assert set(candidate) == set(v37.MODEL_CANDIDATE_FIELDS)
    assert candidate["evidence_block_ids"] == ["b1"]
    assert candidate["support_block_count"] == 2
    assert "source_block_ids" not in user
    assert "window_ids" not in user
    assert "normalized_surface" not in user
    assert "provenance_only_b9" not in user


def test_v37_keeps_full_packet_unchanged_code_side() -> None:
    packet = _packet()
    before = json.dumps(packet, sort_keys=True)
    v37.render_messages(packet)
    assert json.dumps(packet, sort_keys=True) == before
    assert packet["candidates"][0]["source_block_ids"] == [
        "b1",
        "provenance_only_b9",
    ]


def test_v37_accepts_only_supplied_candidate_evidence_ids() -> None:
    packet = _packet()
    valid = v37.validate_output(_admit(["b1"]), packet=packet)
    assert valid.errors == ()

    invalid = v37.validate_output(_admit(["b1", "b2"]), packet=packet)
    assert (
        "decisions[0].evidence_block_ids must use only the supplied candidate "
        "evidence_block_ids"
    ) in invalid.errors


def test_v37_versions_prompt_and_projection_without_changing_response_schema() -> None:
    assert v37.PROMPT_VERSION == "d2l_b2_consistency_admission_v3_7"
    assert v37.VALIDATOR_VERSION.endswith("validator_v3_7")
    assert v37.MODEL_INPUT_PROJECTION_VERSION == (
        "d2l_b2_model_packet_projection_v1"
    )
