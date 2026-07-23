from __future__ import annotations

from types import SimpleNamespace

from pipeline.translate.d2l_translation_slots_v1 import (
    GLOSSARY_REVIEW_POLICY_ID,
    POLICY_ID,
    build_slot_map,
    extract_slot_translations,
    glossary_review_rows,
    parse_slot_json_text,
    render_system_prompt,
    slotize_blocks,
)


def test_slot_map_is_ordered_unique_and_prompt_blocks_are_short() -> None:
    slot_map = build_slot_map(["chapter_long_b001", "chapter_long_b002"])
    blocks = [
        {"block_id": "chapter_long_b001", "clean_text": "First."},
        {"block_id": "chapter_long_b002", "clean_text": "Second."},
    ]

    assert slot_map == {
        "T01": "chapter_long_b001",
        "T02": "chapter_long_b002",
    }
    assert [row["block_id"] for row in slotize_blocks(blocks, slot_map)] == [
        "T01",
        "T02",
    ]
    prompt = render_system_prompt("prompt_v1")
    assert POLICY_ID == "d2l_translation_slots_v1"
    assert '"translations"' in prompt
    assert "prompt_v1" in prompt
    assert "__term_overrides__" not in prompt


def test_slot_output_exact_cover_maps_back_to_canonical_ids() -> None:
    slot_map = build_slot_map(["b001", "b002"])
    translations, errors = extract_slot_translations(
        {"translations": {"T01": "Một.", "T02": "Hai."}},
        slot_map,
    )

    assert errors == []
    assert translations == {"b001": "Một.", "b002": "Hai."}


def test_slot_output_rejects_missing_extra_and_model_metadata() -> None:
    slot_map = build_slot_map(["b001", "b002"])
    _, errors = extract_slot_translations(
        {
            "translations": {"T01": "Một.", "T03": "Ba."},
            "__term_overrides__": [],
        },
        slot_map,
    )

    assert "Unexpected top-level key: __term_overrides__" in errors
    assert "Missing translation slot: T02" in errors
    assert "Unexpected translation slot: T03" in errors


def test_raw_slot_parser_rejects_duplicate_keys_before_dict_collapse() -> None:
    parsed, errors = parse_slot_json_text(
        '{"translations":{"T01":"Một.","T01":"Hai."}}'
    )

    assert parsed is None
    assert errors == ["Duplicate JSON key: T01"]


def test_glossary_review_is_nonsemantic_presence_check() -> None:
    pack = SimpleNamespace(
        glossary_lines=["probability distribution -> phân phối xác suất"],
        context_sensitive_lines=[],
    )
    blocks = [
        {
            "block_id": "b001",
            "source_text": "A probability distribution assigns mass.",
        },
        {
            "block_id": "b002",
            "source_text": "This sentence has no injected source term.",
        },
    ]

    satisfied = glossary_review_rows(
        blocks,
        {
            "b001": "Một phân phối xác suất gán khối lượng.",
            "b002": "Câu này không có thuật ngữ nguồn.",
        },
        pack,
    )
    flagged = glossary_review_rows(
        blocks,
        {
            "b001": "Một phân bố gán khối lượng.",
            "b002": "Câu này không có thuật ngữ nguồn.",
        },
        pack,
    )

    assert satisfied == []
    assert len(flagged) == 1
    assert flagged[0]["policy_id"] == GLOSSARY_REVIEW_POLICY_ID
    assert flagged[0]["status"] == "review_required"
    assert flagged[0]["block_id"] == "b001"
    assert flagged[0]["source_occurrences"] == 1


def test_repeated_preferred_target_does_not_create_false_review() -> None:
    pack = SimpleNamespace(
        glossary_lines=["gradient -> gradient"],
        context_sensitive_lines=[],
    )
    rows = glossary_review_rows(
        [{"block_id": "b1", "source_text": "The gradient changes."}],
        {"b1": "gradient thay đổi; gradient được cập nhật."},
        pack,
    )

    assert rows == []


def test_contextual_alternative_is_treated_as_allowed_not_missing() -> None:
    pack = SimpleNamespace(
        glossary_lines=[],
        context_sensitive_lines=[
            "example -> mẫu (context-sensitive; alternatives: ví dụ when: "
            "the prose introduces an illustration; do not force)"
        ],
    )
    rows = glossary_review_rows(
        [{"block_id": "b1", "source_text": "For example, consider x."}],
        {"b1": "Ví dụ, hãy xét x."},
        pack,
    )

    assert rows == []
