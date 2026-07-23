from __future__ import annotations

import pytest

from pipeline.translate.d2l_protected_spans_v1 import (
    D2LProtectedSpanError,
    POLICY_ID,
    protect_blocks,
    protected_span_reask_note,
    restore_translations,
)


def _block(text: str, block_id: str = "b1") -> dict[str, str]:
    return {"block_id": block_id, "clean_text": text, "source_text": text}


def test_protects_math_code_directives_and_markdown_without_fragmenting_prose() -> None:
    source = (
        ":begin_tab:`mxnet` In $f: \\mathbb{R} \\rightarrow \\mathbb{R}$, "
        "call `reshape` and keep (~~or 1~~). :end_tab:"
    )
    plan = protect_blocks([_block(source)])
    protected = plan.protected_blocks[0]["clean_text"]

    assert "In " in protected
    assert "call " in protected
    assert "$f:" not in protected
    assert "`reshape`" not in protected
    assert ":begin_tab:" not in protected
    assert "~~" not in protected
    assert plan.protected_span_count == 6
    assert POLICY_ID in plan.prompt_legend()
    assert source.split(" In ", 1)[0] in plan.prompt_legend()


def test_restores_exact_source_bytes_after_full_sentence_translation() -> None:
    source = "We denote $f: \\mathbb{R} \\rightarrow \\mathbb{R}$ using `f`."
    plan = protect_blocks([_block(source)])
    placeholders = [span.placeholder for span in plan.spans]
    translated = f"Ta biểu diễn {placeholders[0]} bằng {placeholders[1]}."

    restored, issues = restore_translations({"b1": translated}, plan)

    assert issues == []
    assert restored["b1"] == (
        "Ta biểu diễn $f: \\mathbb{R} \\rightarrow \\mathbb{R}$ bằng `f`."
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda values: values[1],
        lambda values: values[0] + " " + values[0] + " " + values[1],
        lambda values: values[1] + " " + values[0],
        lambda values: values[0] + " [[D2LPS_9999]] " + values[1],
    ],
)
def test_rejects_missing_duplicate_reordered_or_foreign_placeholders(mutate) -> None:
    plan = protect_blocks([_block("Use $x$ with `f`.")])
    placeholders = [span.placeholder for span in plan.spans]

    restored, issues = restore_translations({"b1": mutate(placeholders)}, plan)

    assert restored == {}
    assert issues[0].issue_type == "placeholder_sequence_mismatch"


def test_rejects_model_added_protected_markup() -> None:
    plan = protect_blocks([_block("Use $x$ here.")])
    placeholder = plan.spans[0].placeholder

    restored, issues = restore_translations(
        {"b1": f"Dùng {placeholder} **tại đây**."},
        plan,
    )

    assert restored == {}
    assert issues[0].issue_type == "restored_span_sequence_mismatch"


def test_protects_heading_and_d2l_highlight_wrappers_but_leaves_text_visible() -> None:
    plan = protect_blocks([_block("## [**Operations**] and (***shape***)")])
    protected = plan.protected_blocks[0]["clean_text"]

    assert "Operations" in protected
    assert "shape" in protected
    assert "## " not in protected
    assert "[**" not in protected
    assert "**]" not in protected


def test_reserved_placeholder_collision_fails_closed() -> None:
    with pytest.raises(D2LProtectedSpanError, match="reserved placeholder"):
        protect_blocks([_block("Source contains [[D2LPS_0001]].")])


def test_reask_note_is_book_neutral_and_does_not_include_answers() -> None:
    plan = protect_blocks([_block("Use $x$.")])
    _, issues = restore_translations({"b1": "Dùng x."}, plan)
    note = protected_span_reask_note(issues)

    assert "[[D2LPS_####]]" in note
    assert "$x$" not in note


def test_protects_complete_sphinx_role_including_role_prefix() -> None:
    source = "See :eqref:`eq_derivative` and :numref:`sec_ndarray`."
    plan = protect_blocks([_block(source)])

    assert plan.protected_span_count == 2
    assert [span.kind for span in plan.spans] == ["sphinx_role", "sphinx_role"]
    assert [span.source for span in plan.spans] == [
        ":eqref:`eq_derivative`",
        ":numref:`sec_ndarray`",
    ]
    assert ":eqref:" not in plan.protected_blocks[0]["clean_text"]


def test_prompt_legend_can_use_short_slots_without_changing_provenance() -> None:
    plan = protect_blocks([_block("Use $x$.", block_id="long_block_id")])
    legend = plan.prompt_legend({"long_block_id": "T01"})

    assert "- T01 [[D2LPS_0001]]" in legend
    assert plan.spans[0].block_id == "long_block_id"
