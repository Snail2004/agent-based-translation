from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from pipeline.ingest.document_loader import _validate_stripped_document
from pipeline.ingest.markdown_normalizer import normalize_markdown, write_markdown_normalization
from pipeline.ingest.normalization_ir import ObservedBlock


PANDOC_AVAILABLE = shutil.which("pandoc") is not None


def _write(tmp_path: Path, text: str, *, name: str = "source.md") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def test_markdown_normalizer_separates_front_content_and_back_matter(tmp_path: Path) -> None:
    source = _write(
        tmp_path,
        """---
title: Fixture Book
author: Example Author
---
# Fixture Book

Publication note.

## Chapter 1

First chapter.

## Chapter 2

Second chapter.

## References

Reference entry.
""",
    )
    result = normalize_markdown(source, doc_id="fixture", pandoc_executable=None)
    manifest = result.structure_manifest

    assert [unit["role"] for unit in manifest["units"]] == [
        "front_matter",
        "content_unit",
        "content_unit",
        "back_matter",
    ]
    assert len(manifest["translatable_chapter_ids"]) == 2
    assert manifest["exact_cover"]["coverage"] == 1.0
    assert result.document["metadata"]["title"] == "Fixture Book"
    assert result.document["metadata"]["author"] == "Example Author"
    encoded = json.dumps(result.document)
    assert "title: Fixture Book" not in encoded


def test_markdown_normalizer_treats_one_top_level_heading_as_one_unit(tmp_path: Path) -> None:
    source = _write(tmp_path, "# Technical Note\n\nText.\n\n## Details\n\nMore text.\n")
    result = normalize_markdown(source, doc_id="single", pandoc_executable=None)
    assert [(unit["title"], unit["role"]) for unit in result.structure_manifest["units"]] == [
        ("Technical Note", "content_unit")
    ]
    assert len(result.structure_manifest["translatable_chapter_ids"]) == 1


def test_markdown_normalizer_keeps_code_math_and_table_atomic(tmp_path: Path) -> None:
    source = _write(
        tmp_path,
        """# Chapter

```python
print("hello")

print("world")
```

$$
x + y = z
$$

| Term | Meaning |
| --- | --- |
| x | value |
""",
    )
    result = normalize_markdown(source, doc_id="structured", pandoc_executable=None)
    blocks = result.document["chapters"][0]["blocks"]
    assert [block["block_type"] for block in blocks] == [
        "heading",
        "paragraph",
        "paragraph",
        "paragraph",
    ]
    assert [row["source_block_kind"] for row in result.structure_manifest["source_map"]] == [
        "heading",
        "code",
        "math_block",
        "table",
    ]
    assert 'print("world")' in blocks[1]["source_text"]
    policies = {
        row["block_id"]: row["translation_policy"]
        for row in result.structure_manifest["block_policies"]
    }
    assert policies[blocks[1]["block_id"]] == "preserve"
    assert policies[blocks[2]["block_id"]] == "preserve"
    assert policies[blocks[3]["block_id"]] == "translate_structured"


def test_markdown_normalizer_separates_runtime_text_from_source_structure(
    tmp_path: Path,
) -> None:
    source = _write(
        tmp_path,
        """# Chapter {#chapter-anchor}

> A quoted line.

- First item.
- Second item.

$$x + y = z$$
""",
    )

    result = normalize_markdown(source, doc_id="runtime_text", pandoc_executable=None)
    blocks = result.document["chapters"][0]["blocks"]

    assert [block["block_type"] for block in blocks] == [
        "heading",
        "paragraph",
        "paragraph",
        "paragraph",
    ]
    assert [row["source_block_kind"] for row in result.structure_manifest["source_map"]] == [
        "heading",
        "block_quote",
        "list",
        "math_block",
    ]
    assert blocks[0]["source_text"] == "# Chapter {#chapter-anchor}"
    assert blocks[0]["clean_text"] == "Chapter"
    assert blocks[1]["source_text"] == "> A quoted line."
    assert blocks[1]["clean_text"] == "A quoted line."
    assert blocks[2]["clean_text"].startswith("- First item.")
    assert blocks[3]["source_text"] == "$$x + y = z$$"
    assert result.structure_manifest["source_map"][0]["markdown_anchor"] == "chapter-anchor"

    policies = {
        row["block_id"]: row["translation_policy"]
        for row in result.structure_manifest["block_policies"]
    }
    assert policies[blocks[2]["block_id"]] == "translate_structured"
    assert policies[blocks[3]["block_id"]] == "preserve"


def test_markdown_materializer_distinguishes_wrapped_display_math_from_inline_math(
    tmp_path: Path,
) -> None:
    source = _write(
        tmp_path,
        "# Chapter\n\nInline $x + 1$ expression.\n\n(**$$y = 2$$**)\n",
    )
    result = normalize_markdown(source, doc_id="math_display", pandoc_executable=None)
    output = tmp_path / "out"
    write_markdown_normalization(result, output)
    asset_manifest = json.loads(
        (output / "asset_manifest.json").read_text(encoding="utf-8")
    )
    equation_assets = [
        asset for asset in asset_manifest["assets"] if asset["kind"] == "equation"
    ]

    assert len(equation_assets) == 2
    assert [asset["metadata"]["display"] for asset in equation_assets] == [
        "inline",
        "block",
    ]


def test_markdown_normalizer_closes_display_math_without_swallowing_following_text(
    tmp_path: Path,
) -> None:
    source = _write(
        tmp_path,
        "# Chapter\n\n"
        "$$x =\n"
        "y.$$\n\n"
        "Between equations.\n\n"
        "$$z =\n"
        "2\n"
        "$$\n",
    )

    result = normalize_markdown(source, doc_id="math_boundaries", pandoc_executable=None)
    blocks = result.document["chapters"][0]["blocks"]

    assert [block["block_type"] for block in blocks] == [
        "heading",
        "paragraph",
        "paragraph",
        "paragraph",
    ]
    assert [row["source_block_kind"] for row in result.structure_manifest["source_map"]] == [
        "heading",
        "math_block",
        "paragraph",
        "math_block",
    ]
    assert blocks[1]["source_text"] == "$$x =\ny.$$"
    assert blocks[2]["source_text"] == "Between equations."
    assert blocks[3]["source_text"] == "$$z =\n2\n$$"
    assert result.structure_manifest["warnings"] == []


def test_markdown_normalizer_does_not_reopen_decorated_display_math_closer(
    tmp_path: Path,
) -> None:
    source = _write(
        tmp_path,
        "# Chapter\n\n"
        "[**$$\n"
        "x + y = z.\n"
        "$$**]\n\n"
        "## Following section\n\n"
        "Text after the decorated equation.\n",
    )

    result = normalize_markdown(
        source,
        doc_id="decorated_math_closer",
        pandoc_executable=None,
    )
    blocks = result.document["chapters"][0]["blocks"]

    assert [block["block_type"] for block in blocks] == [
        "heading",
        "paragraph",
        "heading",
        "paragraph",
    ]
    assert blocks[1]["source_text"] == "[**$$\nx + y = z.\n$$**]"
    assert blocks[2]["clean_text"] == "Following section"
    assert blocks[3]["clean_text"] == "Text after the decorated equation."


def test_markdown_normalizer_recognizes_setext_headings(tmp_path: Path) -> None:
    source = _write(
        tmp_path,
        """First chapter
=============

Text one.

Second chapter
==============

Text two.
""",
    )
    result = normalize_markdown(source, doc_id="setext", pandoc_executable=None)
    assert [chapter["title"] for chapter in result.document["chapters"]] == [
        "First chapter",
        "Second chapter",
    ]
    assert all(unit["role"] == "content_unit" for unit in result.structure_manifest["units"])


def test_markdown_normalizer_fails_closed_without_reliable_heading(tmp_path: Path) -> None:
    source = _write(tmp_path, "A paragraph without structural headings.\n\nAnother paragraph.\n")
    result = normalize_markdown(source, doc_id="ambiguous", pandoc_executable=None)
    manifest = result.structure_manifest
    assert manifest["translatable_chapter_ids"] == []
    assert manifest["units"][0]["role"] == "unknown"
    assert manifest["units"][0]["review_required"] is True


def test_markdown_normalizer_flags_unclosed_fence(tmp_path: Path) -> None:
    source = _write(tmp_path, "# Chapter\n\n```python\nprint('open')\n")
    result = normalize_markdown(source, doc_id="unclosed", pandoc_executable=None)
    assert result.structure_manifest["warnings"] == ["unclosed_code_fence:line_3"]
    assert result.structure_manifest["translatable_chapter_ids"] == []
    assert result.structure_manifest["review_required_unit_ids"]


def test_markdown_normalizer_writes_loader_compatible_artifacts(tmp_path: Path) -> None:
    source = _write(tmp_path, "# Chapter\n\nText.\n")
    result = normalize_markdown(source, doc_id="fixture", pandoc_executable=None)
    document_path, manifest_path = write_markdown_normalization(result, tmp_path / "out")
    document = json.loads(document_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    _validate_stripped_document(document)
    assert manifest["source"]["sha256"] == document["metadata"]["raw_sha256"]
    assert all(row["provenance_precision"] == "markdown_exact_line_range" for row in manifest["source_map"])


def test_markdown_normalizer_is_deterministic(tmp_path: Path) -> None:
    source = _write(tmp_path, "# Chapter\n\nText.\n")
    first = normalize_markdown(source, doc_id="stable", pandoc_executable=None)
    second = normalize_markdown(source, doc_id="stable", pandoc_executable=None)
    assert first.document == second.document
    assert first.structure_manifest["structure_sha256"] == second.structure_manifest["structure_sha256"]


def test_markdown_structure_hash_is_independent_of_absolute_source_path(tmp_path: Path) -> None:
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    first_source = _write(first_dir, "# Chapter\n\nText.\n")
    second_source = _write(second_dir, "# Chapter\n\nText.\n")

    first = normalize_markdown(first_source, doc_id="stable", pandoc_executable=None)
    second = normalize_markdown(second_source, doc_id="stable", pandoc_executable=None)

    assert first.structure_manifest["structure_sha256"] == second.structure_manifest["structure_sha256"]


@pytest.mark.skipif(not PANDOC_AVAILABLE, reason="Pandoc is required")
def test_markdown_normalizer_cross_checks_with_pandoc(tmp_path: Path) -> None:
    source = _write(tmp_path, "# Chapter\n\nText for comparison.\n")
    result = normalize_markdown(source, doc_id="cross_checked")
    cross_check = result.structure_manifest["cross_check"]
    assert cross_check["status"] == "ok"
    assert cross_check["native_covered_by_pandoc"] == 1.0
    assert cross_check["review_required"] is False


def test_markdown_normalizer_fails_closed_when_cross_check_loses_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write(tmp_path, "# Chapter\n\nLoad-bearing evidence.\n")
    fake = SimpleNamespace(
        adapter_version="pandoc-test",
        blocks=(ObservedBlock(ordinal=0, kind="paragraph", text="unrelated output"),),
    )
    monkeypatch.setattr("pipeline.ingest.markdown_normalizer.run_pandoc", lambda *_args, **_kwargs: fake)
    result = normalize_markdown(source, doc_id="cross_check_failure")
    assert result.structure_manifest["cross_check"]["review_required"] is True
    assert result.structure_manifest["translatable_chapter_ids"] == []
    assert "pandoc_content_cross_check_failed" in result.structure_manifest["units"][0]["evidence"]


def test_markdown_cross_check_excludes_control_syntax_and_link_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write(
        tmp_path,
        "# Chapter {#chapter}\n\n"
        "[Visible link](https://example.invalid/hidden-target)\n\n"
        "```python\nprint('answer')\n```\n\n"
        ":input: python\n",
    )
    fake = SimpleNamespace(
        adapter_version="pandoc-test",
        blocks=(
            ObservedBlock(ordinal=0, kind="heading", text="Chapter"),
            ObservedBlock(ordinal=1, kind="paragraph", text="Visible link"),
            ObservedBlock(ordinal=2, kind="code", text="print('answer')"),
        ),
    )
    monkeypatch.setattr("pipeline.ingest.markdown_normalizer.run_pandoc", lambda *_args, **_kwargs: fake)

    result = normalize_markdown(source, doc_id="semantic_cross_check")

    cross_check = result.structure_manifest["cross_check"]
    assert cross_check["native_covered_by_pandoc"] == 1.0
    assert cross_check["review_required"] is False


def test_runtime_markdown_normalizer_contains_no_source_specific_exceptions() -> None:
    root = Path(__file__).parents[1] / "ingest"
    source = (root / "markdown_normalizer.py").read_text(encoding="utf-8").casefold()
    forbidden = ["canterville", "gatsby", "jekyll", "wuthering", "treasure island", "d2l"]
    assert not any(name in source for name in forbidden)
