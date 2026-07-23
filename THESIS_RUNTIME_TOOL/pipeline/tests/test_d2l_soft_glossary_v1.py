from __future__ import annotations

from types import SimpleNamespace

import pytest

from pipeline.translate.d2l_soft_glossary_v1 import (
    OVERRIDE_MATCH_RULE_ID,
    POLICY_ID,
    TERM_OVERRIDES_KEY,
    injected_override_preferences,
    injected_override_sources,
    render_soft_glossary_context,
    split_term_override_metadata,
)
from pipeline.translate.prompt import build_messages, prompt_version_for_config


def _pack():
    return SimpleNamespace(
        glossary_lines=["defaults -> vo no"],
        context_sensitive_lines=[
            "example -> mau (context-sensitive; alternatives: vi du when: "
            "the source is illustrative; do not force)"
        ],
        preserve_lines=["API (keep unchanged)"],
        entity_lines=[],
        address_lines=[],
    )


def _valid_payload() -> dict:
    return {
        "b1": "Gia tri mac dinh duoc su dung.",
        TERM_OVERRIDES_KEY: [
            {
                "source_term": "defaults",
                "preferred_target_vi": "vo no",
                "used_target_vi": "mac dinh",
                "block_id": "b1",
                "reason_code": "different_source_sense",
            }
        ],
    }


def test_soft_context_separates_preferred_contextual_and_preserve() -> None:
    rendered = render_soft_glossary_context(_pack())
    assert "PREFERRED TECHNICAL TERMS" in rendered
    assert "CONTEXT-SENSITIVE TERMS" in rendered
    assert "PRESERVE EXACTLY" in rendered
    assert "MANDATORY TERMINOLOGY" not in rendered
    assert POLICY_ID == "d2l_soft_glossary_policy_v1_3"
    assert OVERRIDE_MATCH_RULE_ID == (
        "unicode_nfkc_casefold_alnum_tokens_exact_once_v1"
    )
    assert injected_override_sources(_pack()) == {"defaults", "example"}
    assert injected_override_preferences(_pack()) == {
        "defaults": {"vo no"},
        "example": {"mau"},
    }
    assert "UNORDERED set of allowed choices" in rendered
    assert "storage label 'alternatives' carry no semantic preference" in rendered
    assert "never default to the arrow target" in rendered
    assert "stored lineage metadata, not a semantic preference" in rendered
    assert "A listed contextual alternative is still an override" in rendered


def test_technical_s1_prompt_is_versioned_and_s0_remains_pure() -> None:
    blocks = [{"block_id": "b1", "clean_text": "Software defaults are useful."}]
    s1 = build_messages(
        blocks,
        config="S1",
        context_pack=_pack(),
        profile_name="technical_d2l_v1",
    )
    s0 = build_messages(blocks, config="S0", profile_name="technical_d2l_v1")
    s1_text = "\n".join(row["content"] for row in s1)
    s0_text = "\n".join(row["content"] for row in s0)
    assert prompt_version_for_config("S1", "technical_d2l_v1") == (
        "s1_d2l_soft_glossary_v2_3"
    )
    assert "s1_d2l_soft_glossary_v2_3" in s1_text
    assert TERM_OVERRIDES_KEY in s1_text
    assert "preferred terminology" in s1_text.casefold()
    assert "s0_d2l_v1" in s0_text
    assert "defaults -> vo no" not in s0_text
    assert TERM_OVERRIDES_KEY not in s0_text


def test_valid_override_is_split_from_translation_payload() -> None:
    payload, rows, present, errors = split_term_override_metadata(
        _valid_payload(),
        expected_block_ids=["b1"],
        allowed_source_terms={"defaults"},
    )
    assert payload == {"b1": "Gia tri mac dinh duoc su dung."}
    assert rows == _valid_payload()[TERM_OVERRIDES_KEY]
    assert present is True
    assert errors == []


def test_omitted_override_metadata_is_distinct_from_explicit_empty() -> None:
    omitted = split_term_override_metadata(
        {"b1": "x"}, expected_block_ids=["b1"], allowed_source_terms={"defaults"}
    )
    explicit = split_term_override_metadata(
        {"b1": "x", TERM_OVERRIDES_KEY: []},
        expected_block_ids=["b1"],
        allowed_source_terms={"defaults"},
    )
    assert omitted[1:] == ([], False, [])
    assert explicit[1:] == ([], True, [])


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda row: row.update(source_term="foreign"), "was not injected"),
        (lambda row: row.update(block_id="b2"), "outside the source window"),
        (lambda row: row.update(reason_code="guess"), "reason_code is invalid"),
        (lambda row: row.update(used_target_vi="vo no"), "actual override"),
        (lambda row: row.pop("reason_code"), "invalid fields"),
    ],
)
def test_invalid_override_metadata_fails_closed(mutate, message: str) -> None:
    value = _valid_payload()
    mutate(value[TERM_OVERRIDES_KEY][0])
    _, _, present, errors = split_term_override_metadata(
        value,
        expected_block_ids=["b1"],
        allowed_source_terms={"defaults"},
    )
    assert present is True
    assert any(message in error for error in errors)


def test_duplicate_override_in_same_block_fails_closed() -> None:
    value = _valid_payload()
    value[TERM_OVERRIDES_KEY].append(dict(value[TERM_OVERRIDES_KEY][0]))
    _, _, _, errors = split_term_override_metadata(
        value,
        expected_block_ids=["b1"],
        allowed_source_terms={"defaults"},
    )
    assert any("duplicates an override" in error for error in errors)


def test_override_preferred_target_must_match_injected_rule() -> None:
    value = _valid_payload()
    value[TERM_OVERRIDES_KEY][0]["preferred_target_vi"] = "sai lech"
    _, _, _, errors = split_term_override_metadata(
        value,
        expected_block_ids=["b1"],
        allowed_source_terms={"defaults"},
        allowed_preferred_targets={"defaults": {"vo no"}},
    )
    assert any("does not match the injected preference" in error for error in errors)


def test_preserve_rule_is_not_eligible_for_override() -> None:
    value = _valid_payload()
    value[TERM_OVERRIDES_KEY][0].update(
        source_term="API",
        preferred_target_vi="API",
        used_target_vi="giao dien lap trinh",
    )
    _, _, _, errors = split_term_override_metadata(
        value,
        expected_block_ids=["b1"],
        allowed_source_terms=injected_override_sources(_pack()),
    )
    assert any("was not injected as a soft term" in error for error in errors)


def test_override_used_target_must_exist_in_its_translated_block() -> None:
    value = _valid_payload()
    value["b1"] = "Ban dich khong he co tu da khai."
    _, _, _, errors = split_term_override_metadata(
        value,
        expected_block_ids=["b1"],
        allowed_source_terms={"defaults"},
    )
    assert any("used_target_vi is absent" in error for error in errors)


def test_override_used_target_must_be_unambiguous_in_its_translated_block() -> None:
    value = _valid_payload()
    value["b1"] = "Mac dinh dau va mac dinh cuoi."
    _, _, _, errors = split_term_override_metadata(
        value,
        expected_block_ids=["b1"],
        allowed_source_terms={"defaults"},
    )
    assert any("used_target_vi is ambiguous" in error for error in errors)


def test_override_match_rule_normalizes_unicode_case_and_punctuation() -> None:
    value = _valid_payload()
    value["b1"] = "Giá trị MẶC-ĐỊNH được dùng."
    value[TERM_OVERRIDES_KEY][0]["used_target_vi"] = "mặc định"
    _, rows, present, errors = split_term_override_metadata(
        value,
        expected_block_ids=["b1"],
        allowed_source_terms={"defaults"},
    )
    assert present is True
    assert len(rows) == 1
    assert errors == []
