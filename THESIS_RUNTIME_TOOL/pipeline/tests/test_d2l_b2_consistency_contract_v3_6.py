from __future__ import annotations

from pipeline.prepass import d2l_b2_consistency_contract_v3_5 as v35
from pipeline.prepass import d2l_b2_consistency_contract_v3_6 as v36


def _packet(surfaces: list[str], normalized: str) -> dict:
    return {
        "packet_id": "pkt_variants",
        "chapter_id": "chapter",
        "candidates": [
            {
                "candidate_id": "cand_variants",
                "normalized_surface": normalized,
                "surfaces": surfaces,
                "source_block_ids": ["b1", "b2"],
                "window_ids": ["w1"],
                "evidence_block_ids": ["b1", "b2"],
                "evidence_complete": True,
                "support_block_count": 2,
                "window_count": 1,
            }
        ],
        "source_blocks": [
            {"block_id": "b1", "text": surfaces[0]},
            {"block_id": "b2", "text": surfaces[-1]},
        ],
    }


def _admit(canonical: str) -> dict:
    return {
        "packet_id": "pkt_variants",
        "decisions": [
            {
                "candidate_id": "cand_variants",
                "decision": "admit",
                "canonical_source": canonical,
                "directive": "translate",
                "primary_target_vi": "gradient biến mất và bùng nổ",
                "primary_use": None,
                "alternates": [],
                "evidence_block_ids": ["b1", "b2"],
                "rationale": "The source explicitly teaches this named phenomenon.",
            }
        ],
    }


def test_v36_restores_first_source_surface_for_group_normalized_echo() -> None:
    packet = _packet(
        [
            "Vanishing and Exploding Gradients",
            "Vanishing and exploding gradients",
        ],
        "vanishing and exploding gradients",
    )
    old = v35.validate_output(
        _admit("vanishing and exploding gradients"), packet=packet
    )
    assert old.errors == (
        "decisions[0].canonical_source is not a supplied surface",
    )

    new = v36.validate_output(
        _admit("vanishing and exploding gradients"), packet=packet
    )
    assert new.errors == ()
    assert new.decisions[0].canonical_source == surfaces_first(packet)
    assert new.normalization_warnings == (
        "candidate cand_variants canonical_source was restored by normalize_phrase_exact_v1",
    )


def test_v36_preserves_an_exact_supplied_variant_without_warning() -> None:
    packet = _packet(["Technical Unit", "technical unit"], "technical unit")
    validation = v36.validate_output(_admit("technical unit"), packet=packet)
    assert validation.errors == ()
    assert validation.decisions[0].canonical_source == "technical unit"
    assert validation.normalization_warnings == ()


def test_v36_still_rejects_punctuation_drift() -> None:
    packet = _packet(["fully-connected layers"], "fully-connected layers")
    validation = v36.validate_output(
        _admit("fully connected layers"), packet=packet
    )
    assert validation.errors == (
        "decisions[0].canonical_source is not a supplied surface",
    )


def test_v36_versions_prompt_and_validator_without_changing_user_payload() -> None:
    packet = _packet(["MLP"], "mlp")
    old_messages = v35.render_messages(packet)
    new_messages = v36.render_messages(packet)
    assert v36.PROMPT_VERSION == "d2l_b2_consistency_admission_v3_6"
    assert v36.VALIDATOR_VERSION.endswith("validator_v3_6")
    assert v36.prompt_sha256() != v35.prompt_sha256()
    assert v36.schema_sha256() == v35.schema_sha256()
    assert new_messages[1:] == old_messages[1:]
    assert v36.user_payload_sha256(new_messages) == v35.user_payload_sha256(
        old_messages
    )


def surfaces_first(packet: dict) -> str:
    return packet["candidates"][0]["surfaces"][0]
