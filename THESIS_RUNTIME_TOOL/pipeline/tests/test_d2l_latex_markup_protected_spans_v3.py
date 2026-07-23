from __future__ import annotations

import pytest

from pipeline.translate import d2l_latex_protected_spans_v2 as v2
from pipeline.translate.d2l_latex_markup_protected_spans_v3 import (
    POLICY_ID,
    PROMPT_VERSION,
    protect_blocks,
    protected_span_reask_note,
    restore_translations,
)
from pipeline.translate.d2l_protected_span_policies import get_protected_span_policy


def _block(text: str, block_id: str = "b1") -> dict[str, str]:
    return {"block_id": block_id, "clean_text": text, "source_text": text}


def _expanded(placeholder: str, payload: str) -> str:
    return placeholder[:-2] + f"|{payload}]]"


def test_wraps_translated_emphasis_and_restores_math_byte_exact() -> None:
    source = r"[**The *derivative* of $f$ is defined as**]"
    plan = protect_blocks([_block(source)])
    protected = plan.protected_blocks[0]["clean_text"]
    format_ref = plan.format_spans[0].placeholder
    target = protected.replace(format_ref, _expanded(format_ref, "dao ham"))

    assert plan.policy_id == POLICY_ID
    assert plan.prompt_version == PROMPT_VERSION
    assert len(plan.format_spans) == 1
    assert plan.format_spans[0].source_inner == "derivative"
    assert "*derivative*" not in protected
    assert "$f$" not in protected
    assert "source phrase=\"derivative\"" in plan.prompt_legend({"b1": "T01"})

    restored, issues = restore_translations({"b1": target}, plan)

    assert issues == []
    assert restored["b1"] == r"[**The *dao ham* of $f$ is defined as**]"
    assert v2.math_spans_in_text(restored["b1"]) == ["$f$"]


@pytest.mark.parametrize(
    "build_target,issue_type",
    [
        (lambda refs: _expanded(refs[0], "thu nhat"), "format_placeholder_sequence_mismatch"),
        (
            lambda refs: " ".join(
                [_expanded(refs[0], "thu nhat"), _expanded(refs[0], "lap")]
            ),
            "format_placeholder_sequence_mismatch",
        ),
        (
            lambda refs: " ".join(
                [_expanded(refs[1], "thu hai"), _expanded(refs[0], "thu nhat")]
            ),
            "format_placeholder_sequence_mismatch",
        ),
        (
            lambda refs: " ".join(
                ["[[FORMAT_REF_9999|la]]", _expanded(refs[1], "thu hai")]
            ),
            "format_placeholder_sequence_mismatch",
        ),
        (
            lambda refs: " ".join([refs[0], _expanded(refs[1], "thu hai")]),
            "format_placeholder_malformed",
        ),
        (
            lambda refs: " ".join(
                ["[[FORMAT_REF_0001|]]", _expanded(refs[1], "thu hai")]
            ),
            "format_placeholder_malformed",
        ),
    ],
)
def test_rejects_missing_duplicate_reordered_foreign_or_malformed_format_refs(
    build_target,
    issue_type: str,
) -> None:
    plan = protect_blocks([_block("Use *first* and **second**.")])
    refs = [span.placeholder for span in plan.format_spans]

    restored, issues = restore_translations({"b1": build_target(refs)}, plan)

    assert restored == {}
    assert issues[0].issue_type == issue_type


@pytest.mark.parametrize(
    "payload",
    [" dao ham", "dao ham ", "dao *ham*", r"\frac{1}{2}", "$x$", "~~x~~"],
)
def test_rejects_unsafe_format_payload(payload: str) -> None:
    plan = protect_blocks([_block("Use *derivative*.")])
    reference = plan.format_spans[0].placeholder

    restored, issues = restore_translations(
        {"b1": _expanded(reference, payload)},
        plan,
    )

    assert restored == {}
    assert issues[0].issue_type in {
        "format_placeholder_malformed",
        "format_payload_invalid",
    }


@pytest.mark.parametrize(
    "source,payload,expected",
    [
        ("Use *term*.", "thuat ngu", "Use *thuat ngu*."),
        ("Use **term**.", "thuat ngu", "Use **thuat ngu**."),
        ("Use ***term***.", "thuat ngu", "Use ***thuat ngu***."),
        ("Use ~~old term~~.", "thuat ngu cu", "Use ~~thuat ngu cu~~."),
    ],
)
def test_restores_original_simple_markdown_wrapper(
    source: str,
    payload: str,
    expected: str,
) -> None:
    plan = protect_blocks([_block(source)])
    reference = plan.format_spans[0].placeholder

    translated = plan.protected_blocks[0]["clean_text"].replace(
        reference,
        _expanded(reference, payload),
    )
    restored, issues = restore_translations(
        {"b1": translated},
        plan,
    )

    assert issues == []
    assert restored["b1"] == expected


def test_nested_outer_markup_remains_on_v2_path() -> None:
    plan = protect_blocks([_block(r"[**The *derivative* of $f$**]")])

    assert [span.source_inner for span in plan.format_spans] == ["derivative"]
    assert [span.source for span in plan.base_plan.spans if not span.is_math] == [
        "[**",
        "**]",
    ]
    assert plan.math_span_count == 1


def test_rejects_reordering_between_format_and_math_references() -> None:
    plan = protect_blocks([_block(r"Use *term* with $x$.")])
    format_ref = plan.format_spans[0].placeholder
    math_ref = next(span.placeholder for span in plan.base_plan.spans if span.is_math)
    target = f"Use {math_ref} with {_expanded(format_ref, 'thuat ngu')}."

    restored, issues = restore_translations({"b1": target}, plan)

    assert restored == {}
    assert issues[0].issue_type == "combined_placeholder_sequence_mismatch"


def test_inline_code_is_not_promoted_to_translatable_format() -> None:
    plan = protect_blocks([_block("Call `*literal*` then use *term*.")])

    assert [span.source_inner for span in plan.format_spans] == ["term"]
    assert any(span.kind == "inline_code" for span in plan.base_plan.spans)


def test_policy_dispatch_keeps_v2_and_v3_independent() -> None:
    old_policy = get_protected_span_policy(v2.POLICY_ID)
    new_policy = get_protected_span_policy(POLICY_ID)

    assert old_policy is not None
    assert old_policy.prompt_version == v2.PROMPT_VERSION
    assert old_policy.protect_blocks is v2.protect_blocks
    assert new_policy is not None
    assert new_policy.prompt_version == PROMPT_VERSION
    assert new_policy.protect_blocks is protect_blocks


def test_reask_note_does_not_disclose_source_phrase_or_formula() -> None:
    plan = protect_blocks([_block(r"Use *derivative* of $f$.")])
    _, issues = restore_translations({"b1": "Use derivative."}, plan)

    note = protected_span_reask_note(issues)

    assert "FORMAT_REF" in note
    assert "derivative" not in note
    assert "$f$" not in note
