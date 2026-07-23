from __future__ import annotations

import pytest

from pipeline.translate.d2l_latex_protected_spans_v2 import (
    D2LLatexProtectionError,
    POLICY_ID,
    math_spans_in_text,
    protect_blocks,
    protected_span_reask_note,
    restore_translations,
)


def _block(text: str, block_id: str = "b1") -> dict[str, str]:
    return {"block_id": block_id, "clean_text": text, "source_text": text}


def _book_neutral_math_fixture() -> str:
    return (
        r"Vector $\mathbf{x}$, rank $i^\mathrm{th}$, space "
        r"\(\mathbb{R}^{m\times n}\), product $\odot$, quotient "
        r"$\frac{f(x+h)-f(x)}{h}$, bound "
        r"$P(A=a,B=b)\leq P(A=a)$, transpose $\mathbf{A}^{\top}$, "
        r"and $$\begin{bmatrix}1 & 0 \\ 0 & 1\end{bmatrix}$$."
    )


def test_hides_all_fixture_latex_and_restores_exact_source_bytes() -> None:
    source = _book_neutral_math_fixture()
    plan = protect_blocks([_block(source)])
    protected = plan.protected_blocks[0]["clean_text"]
    legend = plan.prompt_legend({"b1": "T01"})

    assert plan.policy_id == POLICY_ID
    assert plan.math_span_count == 8
    assert protected.count("[[MATH_REF_") == 8
    for latex in math_spans_in_text(source):
        assert latex not in protected
        assert latex not in legend
    assert r"\mathbf" not in protected + legend
    assert "- T01:" in legend

    restored, issues = restore_translations(
        {"b1": "Ban dich: " + protected},
        plan,
    )

    assert issues == []
    assert restored["b1"] == "Ban dich: " + source
    assert math_spans_in_text(restored["b1"]) == math_spans_in_text(source)


@pytest.mark.parametrize(
    "build_target",
    [
        lambda refs: refs[1],
        lambda refs: f"{refs[0]} {refs[0]} {refs[1]}",
        lambda refs: f"{refs[1]} {refs[0]}",
        lambda refs: f"{refs[0]} [[MATH_REF_9999]] {refs[1]}",
    ],
)
def test_rejects_missing_duplicate_reordered_or_foreign_references(
    build_target,
) -> None:
    plan = protect_blocks([_block(r"Use $x$ with \(y\).")])
    refs = [span.placeholder for span in plan.spans]

    restored, issues = restore_translations({"b1": build_target(refs)}, plan)

    assert restored == {}
    assert issues[0].issue_type == "placeholder_sequence_mismatch"


@pytest.mark.parametrize(
    "added",
    [
        "$y$",
        r"\(y\)",
        r"\frac{1}{2}",
    ],
)
def test_rejects_model_authored_formula_or_tex(added: str) -> None:
    plan = protect_blocks([_block(r"Use $x$ here.")])
    reference = plan.spans[0].placeholder

    restored, issues = restore_translations(
        {"b1": f"Use {reference} and add {added}."},
        plan,
    )

    assert restored == {}
    assert issues[0].issue_type == "model_added_math"


@pytest.mark.parametrize("control", ["\x00", "\x08", "\x0b", "\x0c", "\x1b", "\x7f"])
def test_rejects_forbidden_controls_in_source_and_target(control: str) -> None:
    with pytest.raises(D2LLatexProtectionError, match="forbidden controls"):
        protect_blocks([_block(f"Source {control} $x$.")])

    plan = protect_blocks([_block(r"Source $x$.")])
    restored, issues = restore_translations(
        {"b1": f"Target {control} {plan.spans[0].placeholder}."},
        plan,
    )
    assert restored == {}
    assert issues[0].issue_type == "forbidden_control_character"


@pytest.mark.parametrize(
    "source",
    [
        "$x",
        "$$x",
        r"\(x",
        r"\[x",
        r"x\)",
        r"x\]",
    ],
)
def test_rejects_unbalanced_math_delimiters(source: str) -> None:
    with pytest.raises(D2LLatexProtectionError):
        protect_blocks([_block(source)])


def test_ordinary_and_escaped_dollars_are_not_math() -> None:
    source = (
        r"Costs $5, escaped \$10, and the $ symbol remain; "
        r"variable $x$ is mathematical."
    )
    plan = protect_blocks([_block(source)])

    assert plan.math_span_count == 1
    assert "$5" in plan.protected_blocks[0]["clean_text"]
    assert r"\$10" in plan.protected_blocks[0]["clean_text"]
    assert "the $ symbol" in plan.protected_blocks[0]["clean_text"]
    assert "$x$" not in plan.protected_blocks[0]["clean_text"]


def test_code_and_sphinx_roles_are_structural_not_math() -> None:
    source = r"Call `f($x$)` and see :eqref:`eq_x`; then use $x$."
    plan = protect_blocks([_block(source)])

    assert [span.kind for span in plan.spans] == [
        "inline_code",
        "sphinx_role",
        "math_inline",
    ]
    assert plan.math_span_count == 1
    assert all(span.source not in plan.prompt_legend() for span in plan.spans)


def test_reask_note_never_discloses_source_formula() -> None:
    plan = protect_blocks([_block(r"Use $x$.")])
    _, issues = restore_translations({"b1": "Use x."}, plan)
    note = protected_span_reask_note(issues)

    assert "MATH_REF" in note
    assert "$x$" not in note
