from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from pipeline.ingest.document_loader import _validate_stripped_document
from pipeline.ingest.html_normalizer import normalize_html, write_html_normalization
from pipeline.ingest.normalization_ir import ObservedBlock


PANDOC_AVAILABLE = shutil.which("pandoc") is not None


def _write_html(tmp_path: Path, body: str, *, title: str = "Fixture") -> Path:
    source = tmp_path / "source.html"
    source.write_text(
        f"<!doctype html><html lang='en'><head><title>{title}</title>"
        f"<meta name='author' content='Example Author'></head><body>{body}</body></html>",
        encoding="utf-8",
    )
    return source


def test_html_normalizer_uses_native_semantics_and_exact_cover(tmp_path: Path) -> None:
    source = _write_html(
        tmp_path,
        """
        <header id="front"><h1>Fixture Book</h1><p>Publication details.</p></header>
        <nav id="toc"><p>One; Two</p></nav>
        <main>
          <article id="chapter-1"><h2>One</h2><p>First chapter.</p></article>
          <section class="chapter"><h2>Two</h2><p>Second chapter.</p></section>
        </main>
        <footer id="back"><h2>Notes</h2><p>Editorial notes.</p></footer>
        """,
    )
    result = normalize_html(source, doc_id="fixture", pandoc_executable=None)
    manifest = result.structure_manifest

    assert [unit["role"] for unit in manifest["units"]] == [
        "front_matter",
        "content_unit",
        "content_unit",
        "back_matter",
    ]
    assert [unit["title"] for unit in manifest["units"]][1:3] == ["One", "Two"]
    assert manifest["exact_cover"] == {
        "expected_blocks": manifest["exact_cover"]["expected_blocks"],
        "covered_blocks": manifest["exact_cover"]["expected_blocks"],
        "overlap_count": 0,
        "missing_count": 0,
        "coverage": 1.0,
    }
    assert len(manifest["translatable_chapter_ids"]) == 2
    assert manifest["review_required_unit_ids"] == []
    assert all(block["quality_flags"] == [] for chapter in result.document["chapters"] for block in chapter["blocks"])
    assert result.document["metadata"]["title"] == "Fixture"
    assert result.document["metadata"]["author"] == "Example Author"


def test_html_normalizer_uses_repeated_roman_heading_family(tmp_path: Path) -> None:
    source = _write_html(
        tmp_path,
        """
        <h1>A Book</h1><h2>List of Illustrations</h2><p>Plate one.</p>
        <h2 id="one">I</h2><p>First narrative.</p>
        <h2 id="two">II</h2><p>Second narrative.</p>
        <h2 id="license">THE FULL PROJECT GUTENBERG LICENSE</h2><p>License text.</p>
        """,
    )
    result = normalize_html(source, doc_id="roman", pandoc_executable=None)
    units = result.structure_manifest["units"]
    assert [(unit["title"], unit["role"]) for unit in units] == [
        ("Front matter", "front_matter"),
        ("I", "content_unit"),
        ("II", "content_unit"),
        ("THE FULL PROJECT GUTENBERG LICENSE", "back_matter"),
    ]


def test_html_normalizer_does_not_promote_ambiguous_sections(tmp_path: Path) -> None:
    source = _write_html(
        tmp_path,
        "<h1>Manual</h1><h2>Overview</h2><p>A.</p><h2>Details</h2><p>B.</p>",
    )
    result = normalize_html(source, doc_id="ambiguous", pandoc_executable=None)
    manifest = result.structure_manifest
    assert manifest["translatable_chapter_ids"] == []
    assert manifest["review_required_unit_ids"]
    assert all(unit["role"] == "unknown" for unit in manifest["units"])


def test_html_normalizer_skips_nonvisible_markup_and_preserves_native_provenance(tmp_path: Path) -> None:
    source = _write_html(
        tmp_path,
        """
        <main><article id="chapter-1"><h2>Chapter 1</h2><p>Visible text.</p>
        <script>secret_script_text</script><style>.secret_style_text{}</style>
        <p hidden>secret_hidden_text</p></article></main>
        """,
    )
    result = normalize_html(source, doc_id="visible", pandoc_executable=None)
    encoded = json.dumps(result.document)
    assert "Visible text" in encoded
    assert "secret_script_text" not in encoded
    assert "secret_style_text" not in encoded
    assert "secret_hidden_text" not in encoded
    assert all(row["html_path"].startswith("/") for row in result.structure_manifest["source_map"])
    assert all(len(row["line_range"]) == 2 for row in result.structure_manifest["source_map"])


def test_html_normalizer_preserves_image_placement_and_authored_line_layout(
    tmp_path: Path,
) -> None:
    source = _write_html(
        tmp_path,
        """
        <article id="chapter-1"><h2>Chapter I</h2>
        <img src="images/cover.jpg" alt="">
        <p class="poem">First verse line.<br>Second verse line.<br><br>Final stanza.</p>
        <pre>     A shaped line
  narrows here</pre>
        <pre class="code"><code>def example():
    return 1</code></pre>
        </article>
        """,
    )

    result = normalize_html(source, doc_id="layout", pandoc_executable=None)
    source_map = result.structure_manifest["source_map"]
    kinds = [row["source_block_kind"] for row in source_map]
    blocks = [block for chapter in result.document["chapters"] for block in chapter["blocks"]]
    by_kind = {
        row["source_block_kind"]: block
        for row, block in zip(source_map, blocks, strict=True)
    }

    assert kinds == ["heading", "image", "verse", "preformatted", "code"]
    assert by_kind["image"]["clean_text"] == "cover.jpg"
    assert by_kind["verse"]["clean_text"] == (
        "First verse line.\nSecond verse line.\n\nFinal stanza."
    )
    assert by_kind["preformatted"]["clean_text"].startswith("     A shaped line")
    assert "\n  narrows here" in by_kind["preformatted"]["clean_text"]
    assert by_kind["code"]["clean_text"] == "def example():\n    return 1"


def test_html_normalizer_is_deterministic(tmp_path: Path) -> None:
    source = _write_html(
        tmp_path,
        "<main><article id='chapter-1'><h2>Chapter 1</h2><p>Text.</p></article></main>",
    )
    first = normalize_html(source, doc_id="stable", pandoc_executable=None)
    second = normalize_html(source, doc_id="stable", pandoc_executable=None)
    assert first.structure_manifest["structure_sha256"] == second.structure_manifest["structure_sha256"]
    assert first.document == second.document


def test_html_normalizer_writes_loader_compatible_artifacts(tmp_path: Path) -> None:
    source = _write_html(
        tmp_path,
        "<article id='chapter-1'><h2>Chapter 1</h2><p>Text.</p></article>",
    )
    result = normalize_html(source, doc_id="fixture", pandoc_executable=None)
    document_path, manifest_path = write_html_normalization(result, tmp_path / "out")
    document = json.loads(document_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    _validate_stripped_document(document)
    assert manifest["doc_id"] == document["doc_id"]
    assert manifest["source"]["sha256"] == document["metadata"]["raw_sha256"]
    assert [unit["chapter_id"] for unit in manifest["units"]] == [
        chapter["chapter_id"] for chapter in document["chapters"]
    ]


@pytest.mark.skipif(not PANDOC_AVAILABLE, reason="Pandoc is required")
def test_html_normalizer_cross_checks_content_with_pandoc(tmp_path: Path) -> None:
    source = _write_html(
        tmp_path,
        "<main><article id='chapter-1'><h2>Chapter 1</h2><p>Text for comparison.</p></article></main>",
    )
    result = normalize_html(source, doc_id="cross_checked")
    cross_check = result.structure_manifest["cross_check"]
    assert cross_check["status"] == "ok"
    assert cross_check["native_content_covered_by_pandoc"] == 1.0
    assert cross_check["review_required"] is False


def test_html_normalizer_fails_closed_when_pandoc_misses_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_html(
        tmp_path,
        "<article id='chapter-1'><h2>Chapter 1</h2><p>Load-bearing narrative evidence.</p></article>",
    )
    fake = SimpleNamespace(
        adapter_version="pandoc-test",
        blocks=(ObservedBlock(ordinal=0, kind="paragraph", text="unrelated output"),),
    )
    monkeypatch.setattr("pipeline.ingest.html_normalizer.run_pandoc", lambda *_args, **_kwargs: fake)

    result = normalize_html(source, doc_id="cross_check_failure")
    manifest = result.structure_manifest

    assert manifest["cross_check"]["review_required"] is True
    assert manifest["translatable_chapter_ids"] == []
    assert manifest["review_required_unit_ids"]
    content = next(unit for unit in manifest["units"] if unit["role"] == "content_unit")
    assert "pandoc_content_cross_check_failed" in content["evidence"]


def test_runtime_html_normalizer_contains_no_book_specific_exceptions() -> None:
    root = Path(__file__).parents[1] / "ingest"
    source = (root / "html_normalizer.py").read_text(encoding="utf-8").casefold()
    forbidden = ["canterville", "gatsby", "jekyll", "wuthering", "treasure island"]
    assert not any(name in source for name in forbidden)
