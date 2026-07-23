from __future__ import annotations

import pytest

from pipeline.translate.d2l_translation_integrity_v1 import (
    inspect_translations,
    render_retry_note,
    retry_findings,
    warning_findings,
)


def _block(text: str, *, block_type: str = "prose") -> dict:
    return {
        "block_id": "b1",
        "block_type": block_type,
        "clean_text": text,
        "source_text": text,
    }


def test_integrity_accepts_vietnamese_with_exact_math_and_markup() -> None:
    source = r"The *gradient* is $\mathbf{x}$."
    findings = inspect_translations(
        [_block(source)],
        {"b1": r"*Gradient* la $\mathbf{x}$."},
    )

    assert retry_findings(findings) == []


def test_integrity_accepts_equivalent_adjacent_markdown_delimiter_grouping() -> None:
    source = r"[**The *derivative* of $f$ is defined as**]"
    target = r"[***Dao ham* cua $f$ duoc dinh nghia la**]"

    findings = inspect_translations([_block(source)], {"b1": target})

    assert "protected_structure_or_order_changed" not in {
        row.issue_type for row in retry_findings(findings)
    }


def test_integrity_rejects_missing_adjacent_markdown_delimiter() -> None:
    source = r"[**The *derivative* of $f$ is defined as**]"
    target = r"[***Dao ham* cua $f$ duoc dinh nghia la*]"

    findings = inspect_translations([_block(source)], {"b1": target})

    assert "protected_structure_or_order_changed" in {
        row.issue_type for row in retry_findings(findings)
    }


@pytest.mark.parametrize(
    ("target", "issue_type"),
    [
        (r"Gia tri la $\mathbf{y}$.", "math_bytes_or_order_changed"),
        ("Gia tri \x0c bi hong.", "forbidden_control_character"),
        ("Gia tri [[MATH_REF_0001]].", "protected_reference_not_restored"),
        ("Bắt đầu شروع.", "unexpected_output_script"),
        ("", "empty_translation"),
    ],
)
def test_integrity_retries_certain_mechanical_failures(
    target: str, issue_type: str
) -> None:
    findings = inspect_translations(
        [_block(r"The value is $\mathbf{x}$.")],
        {"b1": target},
    )

    assert issue_type in {row.issue_type for row in retry_findings(findings)}


def test_integrity_flags_exact_source_heading_as_retry() -> None:
    findings = inspect_translations(
        [_block("Probability distributions", block_type="heading")],
        {"b1": "Probability distributions"},
    )

    assert {row.issue_type for row in retry_findings(findings)} == {
        "untranslated_heading"
    }


def test_integrity_keeps_gross_length_as_warning_only() -> None:
    source = " ".join(["technical"] * 20)
    findings = inspect_translations([_block(source)], {"b1": "Ngắn."})

    assert "gross_length_anomaly" in {
        row.issue_type for row in warning_findings(findings)
    }
    assert "gross_length_anomaly" not in {
        row.issue_type for row in retry_findings(findings)
    }


def test_integrity_requires_exact_block_cover() -> None:
    with pytest.raises(ValueError, match="exact-cover"):
        inspect_translations([_block("Source")], {})


def test_retry_note_uses_short_slot_without_replacement_translation() -> None:
    findings = inspect_translations(
        [_block("Heading", block_type="heading")],
        {"b1": "Heading"},
    )

    note = render_retry_note(findings, block_to_slot={"b1": "T01"})

    assert "T01:untranslated_heading" in note
    assert "b1:untranslated_heading" not in note
