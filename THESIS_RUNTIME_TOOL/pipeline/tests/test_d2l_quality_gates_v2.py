from __future__ import annotations

from dataclasses import replace
import unicodedata

import pytest

from pipeline.translate.d2l_quality_gates_v2 import (
    DEFAULT_POLICY,
    DeterministicQualityPolicy,
    QualityGateBlock,
    detect_quality_findings,
)


def _issues(block: QualityGateBlock) -> list[dict]:
    return [row.to_dict() for row in detect_quality_findings([block])]


def _issue_types(block: QualityGateBlock) -> list[str]:
    return [row["issue_type"] for row in _issues(block)]


def test_historical_persian_class_is_flagged_without_word_blacklist() -> None:
    persian = "\u0647\u0646\u0648\u0632"
    rows = _issues(
        QualityGateBlock(
            block_id="technical_b038",
            source_text="The library does not yet know the input dimensions.",
            target_text=f"Thu vien {persian} chua biet so chieu dau vao.",
        )
    )

    finding = next(row for row in rows if row["issue_type"] == "unexpected_output_script")
    assert finding["details"]["script"] == "Arabic"
    assert finding["details"]["surface"] == persian


@pytest.mark.parametrize(
    ("surface", "script"),
    [
        ("\u043b\u0438\u0431\u043e", "Cyrillic"),
        ("\u05e2\u05d5\u05d3", "Hebrew"),
        ("\u4ecd\u7136", "CJK"),
        ("\uc544\uc9c1", "Hangul"),
        ("\u0e22\u0e31\u0e07", "Thai"),
    ],
)
def test_output_only_non_latin_scripts_are_generalized(surface: str, script: str) -> None:
    rows = _issues(
        QualityGateBlock(
            block_id="b1",
            source_text="The value remains unknown.",
            target_text=f"Gia tri {surface} chua duoc biet.",
        )
    )

    finding = next(row for row in rows if row["issue_type"] == "unexpected_output_script")
    assert finding["details"]["script"] == script
    assert finding["details"]["surface"] == surface


def test_non_latin_source_token_is_allowed_when_preserved_exactly() -> None:
    source_token = "\u03b1\u03b2"
    assert "unexpected_output_script" not in _issue_types(
        QualityGateBlock(
            block_id="b1",
            source_text=f"Keep the source token {source_token}.",
            target_text=f"Giu nguyen token nguon {source_token}.",
        )
    )


def test_exact_source_derived_excluded_span_is_not_language_audited() -> None:
    source_token = "\u0647\u0646\u0648\u0632"
    assert "unexpected_output_script" not in _issue_types(
        QualityGateBlock(
            block_id="b1",
            source_text=f"Protected payload {source_token}.",
            target_text=f"Du lieu {source_token} duoc bao ve.",
            excluded_exact_spans=(source_token,),
        )
    )


def test_target_only_exclusion_cannot_hide_model_generated_script() -> None:
    generated_token = "\u0647\u0646\u0648\u0632"
    with pytest.raises(ValueError, match="exact-cover source and target"):
        detect_quality_findings(
            [
                QualityGateBlock(
                    block_id="b1",
                    source_text="Protected payload.",
                    target_text=f"Du lieu {generated_token} duoc bao ve.",
                    excluded_exact_spans=(generated_token,),
                )
            ]
        )


def test_vietnamese_nfd_combining_marks_are_not_foreign_script() -> None:
    target = unicodedata.normalize("NFD", "Tieng Viet co dau: tieng Viet.")
    assert "unexpected_output_script" not in _issue_types(
        QualityGateBlock("b1", "Vietnamese has diacritics.", target)
    )


@pytest.mark.parametrize("control", ["\x00", "\x07", "\x0b", "\x0c", "\x1f", "\x7f"])
def test_forbidden_control_characters_are_reported(control: str) -> None:
    rows = _issues(QualityGateBlock("b1", "Source text.", f"Ban dich{control}loi."))
    finding = next(row for row in rows if row["issue_type"] == "forbidden_control_character")
    assert finding["details"]["codepoint"] == f"U+{ord(control):04X}"


def test_exact_heading_and_prose_use_distinct_findings() -> None:
    heading = _issue_types(QualityGateBlock("h1", "Linear Algebra", "Linear Algebra", "heading"))
    prose = _issue_types(QualityGateBlock("p1", "Exact source sentence.", "Exact source sentence."))

    assert "untranslated_heading" in heading
    assert "target_equals_source" not in heading
    assert "target_equals_source" in prose
    assert "untranslated_heading" not in prose


def test_empty_translation_is_major_and_stops_secondary_heuristics() -> None:
    rows = _issues(QualityGateBlock("b1", "A long source sentence.", "   "))
    assert [row["issue_type"] for row in rows] == ["empty_translation"]
    assert rows[0]["severity"] == "major"


def test_source_residue_requires_both_token_and_character_thresholds() -> None:
    source = "The source contains alpha beta gamma epsilon in this discussion."
    hit = QualityGateBlock(
        "b1",
        source,
        "Ban dich van con alpha beta gamma epsilon trong cau.",
    )
    too_few_tokens = QualityGateBlock(
        "b2",
        source,
        "Ban dich van con alpha beta gamma trong cau.",
    )
    too_few_characters = QualityGateBlock(
        "b3",
        "The source has aa bb cc dd tokens.",
        "Ban dich aa bb cc dd o day.",
    )

    finding = next(
        row for row in _issues(hit) if row["issue_type"] == "source_language_residue_candidate"
    )
    assert finding["severity"] == "candidate"
    assert finding["details"]["matched_tokens"] == 4
    assert finding["details"]["matched_characters"] == 21
    assert "source_language_residue_candidate" not in _issue_types(too_few_tokens)
    assert "source_language_residue_candidate" not in _issue_types(too_few_characters)


@pytest.mark.parametrize(
    ("tokens", "flagged"),
    [
        (("aaaa", "bbbb", "cccc", "ddddddd"), False),
        (("aaaa", "bbbb", "cccc", "dddddddd"), True),
        (("aaaa", "bbbb", "cccc", "ddddddddd"), True),
    ],
)
def test_residue_character_threshold_has_exact_boundary(
    tokens: tuple[str, ...], flagged: bool
) -> None:
    phrase = " ".join(tokens)
    block = QualityGateBlock(
        "b1",
        f"The source preserves {phrase} in sequence.",
        f"Ban dich con giu {phrase} trong cau.",
    )

    assert ("source_language_residue_candidate" in _issue_types(block)) is flagged


@pytest.mark.parametrize(
    ("tokens", "flagged"),
    [
        (("aaaaaaaa", "bbbbbbbb", "cccccccc"), False),
        (("aaaaa", "bbbbb", "ccccc", "ddddd"), True),
        (("aaaa", "bbbb", "cccc", "dddd", "eeee"), True),
    ],
)
def test_residue_token_threshold_has_exact_boundary(
    tokens: tuple[str, ...], flagged: bool
) -> None:
    phrase = " ".join(tokens)
    block = QualityGateBlock(
        "b1",
        f"The source preserves {phrase} in sequence.",
        f"Ban dich con giu {phrase} trong cau.",
    )

    assert ("source_language_residue_candidate" in _issue_types(block)) is flagged


def test_residue_exclusion_removes_preserved_source_phrase() -> None:
    phrase = "alpha beta gamma epsilon"
    block = QualityGateBlock(
        "b1",
        f"Keep {phrase} as an explicit label.",
        f"Giu {phrase} lam nhan.",
        excluded_exact_spans=(phrase,),
    )

    assert "source_language_residue_candidate" not in _issue_types(block)


def test_excluded_span_must_have_equal_source_and_target_cardinality() -> None:
    phrase = "alpha beta gamma epsilon"
    with pytest.raises(ValueError, match="exact-cover source and target"):
        detect_quality_findings(
            [
                QualityGateBlock(
                    "b1",
                    f"Keep {phrase} once.",
                    f"Giu {phrase}, khong lap lai {phrase}.",
                    excluded_exact_spans=(phrase,),
                )
            ]
        )


def test_excluded_spans_must_be_unique() -> None:
    phrase = "alpha beta gamma epsilon"
    with pytest.raises(ValueError, match="must be unique"):
        detect_quality_findings(
            [
                QualityGateBlock(
                    "b1",
                    f"Keep {phrase} once.",
                    f"Giu {phrase} mot lan.",
                    excluded_exact_spans=(phrase, phrase),
                )
            ]
        )


@pytest.mark.parametrize(
    ("target_length", "flagged"),
    [(20, False), (19, True), (320, False), (321, True)],
)
def test_gross_length_ratio_has_exact_inclusive_boundaries(
    target_length: int, flagged: bool
) -> None:
    block = QualityGateBlock("b1", "a" * 80, "b" * target_length)
    assert ("gross_length_anomaly" in _issue_types(block)) is flagged


@pytest.mark.parametrize(
    ("source_length", "flagged"),
    [(79, False), (80, True), (81, True)],
)
def test_length_eligibility_has_exact_boundary(
    source_length: int, flagged: bool
) -> None:
    block = QualityGateBlock("b1", "a" * source_length, "b")
    assert ("gross_length_anomaly" in _issue_types(block)) is flagged


def test_policy_hash_is_stable_and_material_to_thresholds() -> None:
    first = DEFAULT_POLICY.sha256()
    assert first == DeterministicQualityPolicy().sha256()
    assert first != replace(DEFAULT_POLICY, residue_min_tokens=5).sha256()


def test_invalid_policy_and_duplicate_block_ids_fail_closed() -> None:
    with pytest.raises(ValueError, match="Residue thresholds"):
        replace(DEFAULT_POLICY, residue_min_tokens=0).validate()
    with pytest.raises(ValueError, match="Duplicate quality gate block_id"):
        detect_quality_findings(
            [
                QualityGateBlock("b1", "One.", "Mot."),
                QualityGateBlock("b1", "Two.", "Hai."),
            ]
        )
