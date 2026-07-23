from __future__ import annotations

import pytest

from pipeline.translate import d2l_latex_markup_protected_spans_v3 as v3
from pipeline.translate.d2l_latex_markup_line_protected_spans_v4 import (
    POLICY_ID,
    PROMPT_VERSION,
    context_source_blocks,
    lexical_source_blocks,
    protect_blocks,
    protected_span_reask_note,
    restore_translations,
)
from pipeline.translate.d2l_protected_span_policies import get_protected_span_policy
from pipeline.translate.d2l_translation_slots_v1 import glossary_review_rows
from pipeline.retrieval.context_builder import Anchors, ContextPack


def _block(text: str, block_id: str = "b1") -> dict[str, str]:
    return {"block_id": block_id, "clean_text": text, "source_text": text}


def _expanded(reference: str, payload: str) -> str:
    return reference[:-2] + f"|{payload}]]"


def test_restores_flat_model_output_to_exact_unordered_list_skeleton() -> None:
    source = "* First $x$.\n* Second *term*.\n* Third."
    plan = protect_blocks([_block(source)])
    protected = plan.protected_blocks[0]["clean_text"]
    format_ref = plan.base_plan.format_spans[0].placeholder
    target = (
        protected.replace("First", "Thu nhat")
        .replace("Second", "Thu hai")
        .replace("Third", "Thu ba")
        .replace(format_ref, _expanded(format_ref, "thuat ngu"))
    )

    assert "\n" not in target
    restored, issues = restore_translations({"b1": target}, plan)

    assert issues == []
    assert restored["b1"] == "* Thu nhat $x$.\n* Thu hai *thuat ngu*.\n* Thu ba."
    assert [span.source for span in plan.line_spans] == ["* ", "\n* ", "\n* "]


def test_restores_standalone_directive_on_its_source_line() -> None:
    source = "We define $f$.\n:eqlabel:`eq_derivative`"
    plan = protect_blocks([_block(source)])
    protected = plan.protected_blocks[0]["clean_text"]
    target = protected.replace("We define", "Ta dinh nghia")

    restored, issues = restore_translations({"b1": target}, plan)

    assert issues == []
    assert restored["b1"] == "Ta dinh nghia $f$.\n:eqlabel:`eq_derivative`"
    assert "eq_derivative" not in protected


@pytest.mark.parametrize(
    "mutate,issue_type",
    [
        (
            lambda refs, target: target.replace(refs[1], "", 1),
            "line_placeholder_sequence_mismatch",
        ),
        (
            lambda refs, target: target.replace(refs[1], refs[0], 1),
            "line_placeholder_sequence_mismatch",
        ),
        (
            lambda refs, target: target.replace(refs[0], "[[LINE_REF_9999]]", 1),
            "line_placeholder_sequence_mismatch",
        ),
        (
            lambda refs, target: target.replace(refs[1], "\n" + refs[1], 1),
            "model_authored_line_break",
        ),
        (
            lambda refs, target: "Moved " + target,
            "leading_line_reference_moved",
        ),
    ],
)
def test_rejects_changed_or_model_authored_line_skeleton(mutate, issue_type: str) -> None:
    plan = protect_blocks([_block("* First.\n* Second.")])
    refs = [span.placeholder for span in plan.line_spans]
    target = plan.protected_blocks[0]["clean_text"]

    restored, issues = restore_translations(
        {"b1": mutate(refs, target)},
        plan,
    )

    assert restored == {}
    assert issues[0].issue_type == issue_type


def test_lexical_view_keeps_prose_term_but_blanks_protected_identifier() -> None:
    blocks = [
        _block("Use *derivative* of $f$.", "b1"),
        _block("We define $f$.\n:eqlabel:`eq_derivative`", "b2"),
    ]
    plan = protect_blocks(blocks)
    lexical = lexical_source_blocks(plan)

    assert "derivative" in lexical[0]["clean_text"]
    assert "$f$" not in lexical[0]["clean_text"]
    assert "eq_derivative" not in lexical[1]["clean_text"]

    context = ContextPack(
        glossary_lines=["derivative -> dao ham"],
        preserve_lines=[],
        context_sensitive_lines=[],
        entity_lines=[],
        address_lines=[],
        token_estimate=5,
        anchors=Anchors(
            doc_id="doc",
            block_ids=["b1", "b2"],
            term_block_ids={"term_derivative": ["b1", "b2"]},
            term_counts={"term_derivative": 2},
            entity_block_ids={},
            entity_counts={},
            has_dialogue=False,
        ),
    )
    reviews = glossary_review_rows(
        lexical,
        {"b1": "Dung dao ham.", "b2": "Ta dinh nghia."},
        context,
    )

    assert reviews == []


def test_context_view_retains_inline_code_but_blanks_math_and_directives() -> None:
    source = "Use `ones` with $f$.\n:eqlabel:`eq_derivative`"
    plan = protect_blocks([_block(source)])
    context = context_source_blocks(plan)[0]["clean_text"]
    lexical = lexical_source_blocks(plan)[0]["clean_text"]

    assert "`ones`" in context
    assert "ones" not in lexical
    assert "$f$" not in context
    assert "eq_derivative" not in context


def test_regular_multiline_prose_is_not_forced_into_line_skeleton() -> None:
    plan = protect_blocks([_block("First prose line.\nSecond prose line.")])

    assert plan.line_spans == ()
    assert "\n" in plan.protected_blocks[0]["clean_text"]


def test_policy_dispatch_keeps_v3_and_v4_independent() -> None:
    old_policy = get_protected_span_policy(v3.POLICY_ID)
    new_policy = get_protected_span_policy(POLICY_ID)

    assert old_policy is not None
    assert old_policy.prompt_version == v3.PROMPT_VERSION
    assert old_policy.lexical_source_blocks is None
    assert old_policy.context_source_blocks is None
    assert new_policy is not None
    assert new_policy.prompt_version == PROMPT_VERSION
    assert new_policy.protect_blocks is protect_blocks
    assert new_policy.context_source_blocks is context_source_blocks
    assert new_policy.lexical_source_blocks is lexical_source_blocks


def test_reask_note_does_not_disclose_line_bytes_or_formula() -> None:
    plan = protect_blocks([_block("* Use $f$.\n* Continue.")])
    _, issues = restore_translations({"b1": "Use f."}, plan)

    note = protected_span_reask_note(issues)

    assert "LINE_REF" in note
    assert "* Use" not in note
    assert "$f$" not in note
