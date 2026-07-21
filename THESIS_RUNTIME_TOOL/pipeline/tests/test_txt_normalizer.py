from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from pipeline.ingest.document_loader import _validate_stripped_document
from pipeline.ingest.normalization_ir import ObservedBlock
from pipeline.ingest.txt_normalizer import normalize_txt, write_txt_normalization


PANDOC_AVAILABLE = shutil.which("pandoc") is not None


def _write(tmp_path: Path, text: str, *, name: str = "source.txt") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _gutenberg(body: str, *, title: str = "Fixture") -> str:
    return (
        "The Project Gutenberg eBook of Fixture\n\n"
        f"Title: {title}\n\n"
        "Author: Example Author\n\n"
        "Language: English\n\n"
        "*** START OF THE PROJECT GUTENBERG EBOOK FIXTURE ***\n\n"
        f"{body.strip()}\n\n"
        "*** END OF THE PROJECT GUTENBERG EBOOK FIXTURE ***\n\n"
        "License text.\n"
    )


def test_txt_normalizer_matches_repeated_toc_ordinals_to_body(tmp_path: Path) -> None:
    source = _write(
        tmp_path,
        _gutenberg(
            "CONTENTS\nI\nII\n\n\n\n"
            "I\n\nFirst chapter text.\n\n"
            "II\n\nSecond chapter text."
        ),
    )
    result = normalize_txt(source, doc_id="fixture", pandoc_executable=None)
    manifest = result.structure_manifest

    assert [unit["role"] for unit in manifest["units"]] == [
        "front_matter",
        "content_unit",
        "content_unit",
        "back_matter",
    ]
    assert [unit["title"] for unit in manifest["units"][1:3]] == ["I", "II"]
    assert all("toc_recurrence_match" in unit["evidence"] for unit in manifest["units"][1:3])
    assert manifest["exact_cover"]["coverage"] == 1.0
    assert result.document["metadata"]["title"] == "Fixture"
    assert result.document["metadata"]["author"] == "Example Author"


def test_txt_normalizer_matches_arbitrary_toc_titles(tmp_path: Path) -> None:
    source = _write(
        tmp_path,
        _gutenberg(
            "CONTENTS\nSTORY OF THE DOOR\nSEARCH FOR THE KEY\n\n\n\n"
            "STORY OF THE DOOR\n\nFirst body.\n\n"
            "SEARCH FOR THE KEY\n\nSecond body."
        ),
    )
    result = normalize_txt(source, doc_id="titles", pandoc_executable=None)
    content = [unit for unit in result.structure_manifest["units"] if unit["role"] == "content_unit"]
    assert [unit["title"] for unit in content] == ["STORY OF THE DOOR", "SEARCH FOR THE KEY"]


def test_txt_normalizer_selects_body_staves_not_toc_staves(tmp_path: Path) -> None:
    source = _write(
        tmp_path,
        _gutenberg(
            "CONTENTS\nSTAVE I: FIRST\nSTAVE II: SECOND\n\n\n\n"
            "STAVE I: FIRST\n\nA long first body paragraph.\n\n"
            "STAVE II: SECOND\n\nA long second body paragraph."
        ),
    )
    result = normalize_txt(source, doc_id="staves", pandoc_executable=None)
    content = [unit for unit in result.structure_manifest["units"] if unit["role"] == "content_unit"]
    assert len(content) == 2
    assert content[0]["block_range"][0] > 5


def test_txt_normalizer_prefers_chapter_ordinals_over_part_headings(tmp_path: Path) -> None:
    source = _write(
        tmp_path,
        _gutenberg(
            "PART ONE\n\nI\n\nFirst.\n\nII\n\nSecond.\n\n"
            "PART TWO\n\nIII\n\nThird."
        ),
    )
    result = normalize_txt(source, doc_id="parts", pandoc_executable=None)
    content = [unit for unit in result.structure_manifest["units"] if unit["role"] == "content_unit"]
    assert [unit["title"] for unit in content] == ["I", "II", "III"]


def test_txt_normalizer_flags_one_bad_ordinal_without_discarding_later_chapters(tmp_path: Path) -> None:
    source = _write(
        tmp_path,
        _gutenberg(
            "I\n\nFirst.\n\nII\n\nSecond.\n\nXXVII\n\nTypo.\n\nIV\n\nFourth."
        ),
    )
    result = normalize_txt(source, doc_id="ordinal_typo", pandoc_executable=None)
    content = [unit for unit in result.structure_manifest["units"] if unit["role"] == "content_unit"]

    assert [unit["title"] for unit in content] == ["I", "II", "XXVII", "IV"]
    assert [unit["review_required"] for unit in content] == [False, False, True, False]
    assert any(warning.startswith("ordinal_sequence_anomaly:") for warning in result.structure_manifest["warnings"])


def test_txt_normalizer_keeps_marker_bounded_story_as_one_content_unit(tmp_path: Path) -> None:
    source = _write(tmp_path, _gutenberg("Book title\n\nA story without chapter headings."))
    result = normalize_txt(source, doc_id="short_story", pandoc_executable=None)
    units = result.structure_manifest["units"]
    assert [unit["role"] for unit in units] == ["front_matter", "content_unit", "back_matter"]
    assert units[1]["title"] == "Body"
    assert units[1]["review_required"] is False


def test_txt_normalizer_fails_closed_without_reliable_structure(tmp_path: Path) -> None:
    source = _write(tmp_path, "Paragraph one.\n\nParagraph two.\n")
    result = normalize_txt(source, doc_id="unknown", pandoc_executable=None)
    manifest = result.structure_manifest
    assert manifest["translatable_chapter_ids"] == []
    assert manifest["units"][0]["role"] == "unknown"
    assert manifest["units"][0]["review_required"] is True


def test_txt_normalizer_single_explicit_chapter_is_accepted(tmp_path: Path) -> None:
    source = _write(tmp_path, "Publication note.\n\nCHAPTER ONE\n\nStory text.\n")
    result = normalize_txt(source, doc_id="single", pandoc_executable=None)
    assert [unit["role"] for unit in result.structure_manifest["units"]] == [
        "front_matter",
        "content_unit",
    ]


def test_txt_normalizer_missing_gutenberg_end_marker_is_review_required(tmp_path: Path) -> None:
    source = _write(
        tmp_path,
        "Front.\n\n*** START OF THE PROJECT GUTENBERG EBOOK FIXTURE ***\n\n"
        "CHAPTER I\n\nStory text.\n",
    )
    result = normalize_txt(source, doc_id="unclosed", pandoc_executable=None)
    assert "gutenberg_end_marker_missing" in result.structure_manifest["warnings"]
    assert result.structure_manifest["translatable_chapter_ids"] == []
    assert any(unit["review_required"] for unit in result.structure_manifest["units"])


def test_txt_normalizer_classifies_dialogue_and_preserves_separators(tmp_path: Path) -> None:
    source = _write(
        tmp_path,
        "CHAPTER I\n\n\"Come here,\" she said.\n\n***\n\nNarration.\n",
    )
    result = normalize_txt(source, doc_id="block_types", pandoc_executable=None)
    blocks = result.document["chapters"][0]["blocks"]
    assert [block["block_type"] for block in blocks] == [
        "heading",
        "dialogue",
        "paragraph",
        "paragraph",
    ]
    assert [row["source_block_kind"] for row in result.structure_manifest["source_map"]] == [
        "heading",
        "dialogue",
        "separator",
        "paragraph",
    ]
    policies = {
        row["block_id"]: row["translation_policy"]
        for row in result.structure_manifest["block_policies"]
    }
    assert policies[blocks[2]["block_id"]] == "preserve"


def test_txt_normalizer_writes_loader_compatible_artifacts(tmp_path: Path) -> None:
    source = _write(tmp_path, "CHAPTER I\n\nText.\n")
    result = normalize_txt(source, doc_id="loader", pandoc_executable=None)
    document_path, manifest_path = write_txt_normalization(result, tmp_path / "out")
    document = json.loads(document_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    _validate_stripped_document(document)
    assert manifest["source"]["sha256"] == document["metadata"]["raw_sha256"]
    assert all(row["provenance_precision"] == "txt_exact_line_range" for row in manifest["source_map"])


def test_txt_normalizer_is_deterministic_and_path_independent(tmp_path: Path) -> None:
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    text = "CHAPTER I\n\nText.\n"
    first_source = _write(first_dir, text)
    second_source = _write(second_dir, text)
    first = normalize_txt(first_source, doc_id="stable", pandoc_executable=None)
    second = normalize_txt(second_source, doc_id="stable", pandoc_executable=None)
    assert first.document == second.document
    assert first.structure_manifest["structure_sha256"] == second.structure_manifest["structure_sha256"]


@pytest.mark.skipif(not PANDOC_AVAILABLE, reason="Pandoc is required")
def test_txt_normalizer_cross_checks_with_pandoc(tmp_path: Path) -> None:
    source = _write(tmp_path, "CHAPTER I\n\nText for comparison.\n")
    result = normalize_txt(source, doc_id="cross_checked")
    cross_check = result.structure_manifest["cross_check"]
    assert cross_check["status"] == "ok"
    assert cross_check["native_covered_by_pandoc"] == 1.0
    assert cross_check["review_required"] is False


def test_txt_normalizer_fails_closed_when_cross_check_loses_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write(tmp_path, "CHAPTER I\n\nLoad-bearing evidence.\n")
    fake = SimpleNamespace(
        adapter_version="pandoc-test",
        blocks=(ObservedBlock(ordinal=0, kind="paragraph", text="unrelated output"),),
    )
    monkeypatch.setattr("pipeline.ingest.txt_normalizer.run_pandoc", lambda *_args, **_kwargs: fake)
    result = normalize_txt(source, doc_id="cross_check_failure")
    assert result.structure_manifest["cross_check"]["review_required"] is True
    assert result.structure_manifest["translatable_chapter_ids"] == []
    content = next(unit for unit in result.structure_manifest["units"] if unit["role"] == "content_unit")
    assert "pandoc_content_cross_check_failed" in content["evidence"]


def test_runtime_txt_normalizer_contains_no_source_specific_exceptions() -> None:
    root = Path(__file__).parents[1] / "ingest"
    source = (root / "txt_normalizer.py").read_text(encoding="utf-8").casefold()
    forbidden = ["canterville", "gatsby", "jekyll", "wuthering", "treasure island", "d2l"]
    assert not any(name in source for name in forbidden)
