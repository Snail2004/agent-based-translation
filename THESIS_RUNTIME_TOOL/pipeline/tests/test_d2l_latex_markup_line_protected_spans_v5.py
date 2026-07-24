from __future__ import annotations

from pipeline.translate.d2l_latex_markup_line_protected_spans_v5 import (
    POLICY_ID,
    PROMPT_VERSION,
    fixed_only_block_ids,
    fixed_only_protected_translations,
    protect_blocks,
    restore_translations,
)


def _block(text: str, block_id: str = "b1") -> dict[str, str]:
    return {"block_id": block_id, "clean_text": text, "source_text": text}


def _expanded(placeholder: str, payload: str) -> str:
    return placeholder[:-2] + f"|{payload}]]"


def test_bracketed_emphasis_wrapper_is_code_restored() -> None:
    source = (
        "As with an ordinary Python array,\n"
        "we [**can access the length of a tensor**]\n"
        "by calling Python's built-in `len()` function."
    )
    plan = protect_blocks([_block(source)])
    protected = plan.protected_blocks[0]["clean_text"]
    span = plan.inner_plan.base_plan.format_spans[0]

    assert plan.policy_id == POLICY_ID
    assert plan.prompt_version == PROMPT_VERSION
    assert span.kind == "markdown_bracketed_strong"
    assert span.open_marker == "[**"
    assert span.close_marker == "**]"
    assert "[**" not in protected
    assert "**]" not in protected
    assert "we [[FORMAT_REF_0001]]" in protected

    target = protected.replace(
        span.placeholder,
        _expanded(span.placeholder, "có thể truy cập độ dài của một tensor"),
    )
    restored, issues = restore_translations({"b1": target}, plan)

    assert issues == []
    assert restored["b1"] == (
        "As with an ordinary Python array,\n"
        "we [**có thể truy cập độ dài của một tensor**]\n"
        "by calling Python's built-in `len()` function."
    )


def test_fixed_only_payload_is_derived_from_protected_source() -> None:
    source = (
        r"(**$$f'(x) = \lim_{h \rightarrow 0} "
        r"\frac{f(x+h) - f(x)}{h},$$**)"
        "\n:eqlabel:`eq_derivative`"
    )
    plan = protect_blocks([_block(source)])

    assert fixed_only_block_ids(plan) == {"b1"}
    protected = fixed_only_protected_translations(plan)
    assert protected == {
        "b1": plan.protected_blocks[0]["clean_text"],
    }

    restored, issues = restore_translations(protected, plan)
    assert issues == []
    assert restored == {"b1": source}
