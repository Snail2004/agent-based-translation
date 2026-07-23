from __future__ import annotations

from pipeline.prepass import d2l_b2_consistency_contract_v3_4 as v34
from pipeline.prepass import d2l_b2_consistency_contract_v3_5 as v35


def _packet(surface: str) -> dict:
    return {
        "packet_id": "pkt_case",
        "chapter_id": "chapter",
        "candidates": [
            {
                "candidate_id": "cand_case",
                "normalized_surface": surface.casefold(),
                "surfaces": [surface],
                "source_block_ids": ["b1"],
                "window_ids": ["w1"],
                "evidence_block_ids": ["b1"],
                "evidence_complete": True,
                "support_block_count": 1,
                "window_count": 1,
            }
        ],
        "source_blocks": [{"block_id": "b1", "text": f"The {surface} is taught."}],
    }


def _admit(canonical: str, *, primary: str | None = None) -> dict:
    return {
        "packet_id": "pkt_case",
        "decisions": [
            {
                "candidate_id": "cand_case",
                "decision": "admit",
                "canonical_source": canonical,
                "directive": "preserve",
                "primary_target_vi": primary or canonical,
                "primary_use": None,
                "alternates": [],
                "evidence_block_ids": ["b1"],
                "rationale": "A named technical abbreviation used as a concept.",
            }
        ],
    }


def test_v35_repairs_unique_case_only_echo_byte_exactly() -> None:
    packet = _packet("MLP")
    old = v34.validate_output(_admit("mlp"), packet=packet)
    assert old.errors == (
        "decisions[0].canonical_source is not a supplied surface",
    )

    new = v35.validate_output(_admit("mlp"), packet=packet)
    assert new.errors == ()
    assert new.decisions[0].canonical_source == "MLP"
    assert new.decisions[0].primary_target_vi == "MLP"
    assert new.normalization_warnings == (
        "candidate cand_case canonical_source case was restored byte-exactly",
    )


def test_v35_does_not_repair_punctuation_or_lexical_drift() -> None:
    packet = _packet("fully-connected layers")
    validation = v35.validate_output(
        _admit("fully connected layers"), packet=packet
    )
    assert validation.errors == (
        "decisions[0].canonical_source is not a supplied surface",
    )
    assert validation.normalization_warnings == ()


def test_v35_versions_prompt_and_validator_without_changing_user_payload() -> None:
    packet = _packet("MLP")
    old_messages = v34.render_messages(packet)
    new_messages = v35.render_messages(packet)
    assert v35.PROMPT_VERSION == "d2l_b2_consistency_admission_v3_5"
    assert v35.VALIDATOR_VERSION.endswith("validator_v3_5")
    assert v35.prompt_sha256() != v34.prompt_sha256()
    assert v35.schema_sha256() == v34.schema_sha256()
    assert new_messages[1:] == old_messages[1:]
    assert v35.user_payload_sha256(new_messages) == v34.user_payload_sha256(
        old_messages
    )
