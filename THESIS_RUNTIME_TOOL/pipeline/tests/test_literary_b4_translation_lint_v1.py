from __future__ import annotations

from copy import deepcopy

import pytest

from pipeline.literary.b4_translation_lint_v1 import (
    B4TranslationLintError,
    CORRECTED_TRANSLATION_SCHEMA_VERSION,
    LINT_POLICY_SCHEMA_VERSION,
    lint_translation_chapter_v1,
)
from pipeline.literary.b4_translator_pack_v1 import (
    SCHEMA_VERSION as TRANSLATOR_PACK_SCHEMA_VERSION,
)
from pipeline.literary.checkpoint import canonical_hash


def _seal(body: dict) -> dict:
    return {**deepcopy(body), "artifact_hash": canonical_hash(body)}


def _chapter() -> dict:
    return {
        "chapter_id": "bk_ch01",
        "blocks": [
            {"block_id": "bk_ch01_b001", "clean_text": "Source one."},
            {"block_id": "bk_ch01_b002", "clean_text": "Source two."},
        ],
    }


def _plan() -> dict:
    return {
        "chapter_id": "bk_ch01",
        "window_plan_hash": "1" * 64,
        "windows": [
            {
                "window_id": "window_1",
                "active_block_ids": ["bk_ch01_b001", "bk_ch01_b002"],
            }
        ],
    }


def _translation() -> dict:
    body = {
        "schema_version": "literary_b4_translation_chapter_v5",
        "book_id": "fixture_book",
        "chapter_id": "bk_ch01",
        "window_plan_hash": "1" * 64,
        "blocks": [
            {
                "block_id": "bk_ch01_b001",
                "source_text": "Source one.",
                "target_text": "\u201cMr. Lockwood den.\u201d",
            },
            {
                "block_id": "bk_ch01_b002",
                "source_text": "Source two.",
                "target_text": "Van ban sach.",
            },
        ],
        "provider_calls": 2,
    }
    return _seal(body)


def _policy() -> dict:
    return {
        "schema_version": LINT_POLICY_SCHEMA_VERSION,
        "replacements": [
            {
                "rule_id": "title_lockwood",
                "from": "Mr. Lockwood",
                "to": "ong Lockwood",
                "issue_kind": "noncanonical_character_title",
            }
        ],
        "watch_literals": [],
        "glossary_rules": [],
    }


def _translator_pack() -> dict:
    body = {
        "schema_version": TRANSLATOR_PACK_SCHEMA_VERSION,
        "book_id": "fixture_book",
        "chapter_id": "bk_ch01",
        "chapter_order": 1,
        "story_bible_artifact_hash": "2" * 64,
        "address_anchor_artifact_hash": "3" * 64,
        "entities": [
            {
                "effective_entity_id": "entity_pointer",
                "canonical_surface": "pointer",
                "stable_surfaces": ["the pointer"],
                "aliases": [],
            }
        ],
        "relations": [],
        "states": [],
        "idiolect": [],
        "narrative_position": {},
        "open_questions": {},
        "pack_budget": {
            "fits": True,
            "omissions": [],
        },
    }
    return _seal(body)


def test_lint_reports_and_applies_only_declared_literal_replacements() -> None:
    report, corrected = lint_translation_chapter_v1(
        translation_artifact=_translation(),
        chapter=_chapter(),
        window_plan=_plan(),
        mechanical_policy=_policy(),
        apply_mechanical_fixes=True,
    )

    assert report["provider_calls"] == 0
    assert report["issue_by_kind"] == {
        "noncanonical_character_title": 1,
        "untranslated_english_honorific": 1,
    }
    assert report["mechanical_correction_count"] == 1
    assert report["remaining_issue_count"] == 0
    assert corrected is not None
    assert corrected["schema_version"] == CORRECTED_TRANSLATION_SCHEMA_VERSION
    assert corrected["blocks"][0]["target_text"] == "\u201cong Lockwood den.\u201d"
    assert corrected["blocks"][1] == _translation()["blocks"][1]
    assert corrected["semantic_record_mutation_performed"] is False


def test_lint_never_autofixes_without_an_explicit_policy() -> None:
    report, corrected = lint_translation_chapter_v1(
        translation_artifact=_translation(),
        chapter=_chapter(),
        window_plan=_plan(),
        mechanical_policy=None,
        apply_mechanical_fixes=True,
    )

    assert report["issue_by_kind"] == {
        "untranslated_english_honorific": 1
    }
    assert report["mechanical_correction_count"] == 0
    assert corrected is not None
    assert corrected["blocks"] == _translation()["blocks"]


def test_lint_halts_on_translation_exact_cover_failure() -> None:
    translation = _translation()
    body = deepcopy(translation)
    body.pop("artifact_hash")
    body["blocks"] = body["blocks"][:1]
    translation = _seal(body)

    with pytest.raises(
        B4TranslationLintError,
        match="exact-cover window plan",
    ):
        lint_translation_chapter_v1(
            translation_artifact=translation,
            chapter=_chapter(),
            window_plan=_plan(),
        )


def test_lint_reports_encoding_typography_and_structured_glossary_issues() -> None:
    translation = _translation()
    body = deepcopy(translation)
    body.pop("artifact_hash")
    body["blocks"][0]["source_text"] = "Dialect token."
    body["blocks"][0]["target_text"] = (
        "\u201c\u0416 \u00c3\u00b4ng noi."
    )
    chapter = _chapter()
    chapter["blocks"][0]["clean_text"] = "Dialect token."
    translation = _seal(body)
    policy = {
        "schema_version": LINT_POLICY_SCHEMA_VERSION,
        "replacements": [],
        "watch_literals": [],
        "glossary_rules": [
            {
                "rule_id": "dialect_1",
                "source_terms": ["Dialect token"],
                "required_targets": ["tu da chot"],
                "forbidden_targets": [],
                "block_ids": ["bk_ch01_b001"],
            }
        ],
    }

    report, _ = lint_translation_chapter_v1(
        translation_artifact=translation,
        chapter=chapter,
        window_plan=_plan(),
        mechanical_policy=policy,
    )

    assert report["issue_by_kind"] == {
        "glossary_target_missing": 1,
        "possible_mojibake": 1,
        "unbalanced_curly_double_quotes": 1,
        "unexpected_cyrillic": 1,
    }


def test_lint_observes_verbatim_source_carry_through_without_gating() -> None:
    pack = _translator_pack()
    chapter = _chapter()
    chapter["blocks"][0]["clean_text"] = (
        "A pint passed the pointer to tin."
    )
    translation = _translation()
    body = deepcopy(translation)
    body.pop("artifact_hash")
    body["translator_pack_artifact_hash"] = pack["artifact_hash"]
    body["blocks"][0]["source_text"] = chapter["blocks"][0]["clean_text"]
    body["blocks"][0]["target_text"] = (
        "Một pint được đưa cho pointer to tin."
    )
    translation = _seal(body)

    report, corrected = lint_translation_chapter_v1(
        translation_artifact=translation,
        chapter=chapter,
        window_plan=_plan(),
        translator_pack=pack,
    )

    assert report["status"] == "clean"
    assert report["issue_count"] == 0
    assert report["source_carry_through_checked"] is True
    assert report["observation_count"] == 1
    assert report["observation_by_kind"] == {
        "verbatim_source_token_carry_through": 1
    }
    assert report["observations"][0]["token"] == "pint"
    assert corrected is None


def test_lint_halts_when_translator_pack_lineage_differs() -> None:
    pack = _translator_pack()
    translation = _translation()
    body = deepcopy(translation)
    body.pop("artifact_hash")
    body["translator_pack_artifact_hash"] = "f" * 64
    translation = _seal(body)

    with pytest.raises(
        B4TranslationLintError,
        match="Translator Pack and translation lineage differ",
    ):
        lint_translation_chapter_v1(
            translation_artifact=translation,
            chapter=_chapter(),
            window_plan=_plan(),
            translator_pack=pack,
        )
