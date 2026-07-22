from __future__ import annotations

import hashlib
import json
import runpy
import shutil
import zipfile
from pathlib import Path

import pytest

from pipeline.ingest.canonical_source_package import (
    CanonicalSourcePackageError,
    validate_canonical_source_package,
)
from pipeline.ingest.epub_normalizer import normalize_epub, write_epub_normalization
from pipeline.ingest.html_normalizer import normalize_html, write_html_normalization
from pipeline.ingest.markdown_normalizer import (
    normalize_markdown,
    write_markdown_normalization,
)
from pipeline.ingest.source_package_materializer import materialize_source_package
from pipeline.ingest.txt_normalizer import normalize_txt, write_txt_normalization


PANDOC_AVAILABLE = shutil.which("pandoc") is not None
SCHEMA_PATH = (
    Path(__file__).parents[1]
    / "ingest"
    / "schemas"
    / "canonical_asset_manifest_v1.schema.json"
)
RICH_FIXTURE_ROOT = (
    Path(__file__).parent / "fixtures" / "canonical_source_rich_v1"
)


def _load_package(output: Path) -> tuple[dict, dict, dict, dict]:
    document = json.loads((output / "document.json").read_text(encoding="utf-8"))
    structure = json.loads(
        (output / "structure_manifest.json").read_text(encoding="utf-8")
    )
    manifest = json.loads(
        (output / "asset_manifest.json").read_text(encoding="utf-8")
    )
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(manifest)
    report = validate_canonical_source_package(
        document,
        structure,
        manifest,
        package_root=output,
    )
    return document, structure, manifest, report


def _asset_kinds(manifest: dict, binding: dict) -> list[str]:
    by_id = {asset["asset_id"]: asset for asset in manifest["assets"]}
    return [by_id[asset_id]["kind"] for asset_id in binding["asset_ids"]]


def _assert_materialized_assets_are_physical(output: Path, manifest: dict) -> None:
    for asset in manifest["assets"]:
        if asset["availability"] != "materialized":
            continue
        path = output / asset["package_path"]
        assert path.is_file()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == asset["sha256"]


def _rich_fixture_contract() -> dict:
    return json.loads(
        (RICH_FIXTURE_ROOT / "fixture_contract.json").read_text(encoding="utf-8")
    )


def _normalize_rich_fixture(source_format: str):
    contract = _rich_fixture_contract()["formats"][source_format]
    source = RICH_FIXTURE_ROOT / contract["source"]
    if source_format == "txt":
        return normalize_txt(
            source,
            doc_id="canonical_rich_txt",
            pandoc_executable=None,
        ), write_txt_normalization
    if source_format == "markdown":
        return normalize_markdown(
            source,
            doc_id="canonical_rich_markdown",
            pandoc_executable=None,
        ), write_markdown_normalization
    if source_format == "html":
        return normalize_html(
            source,
            doc_id="canonical_rich_html",
            pandoc_executable=None,
        ), write_html_normalization
    if source_format == "epub":
        if not PANDOC_AVAILABLE:
            pytest.skip("Pandoc is required")
        return normalize_epub(
            source,
            doc_id="canonical_rich_epub",
        ), write_epub_normalization
    raise AssertionError(f"unsupported rich fixture format: {source_format}")


@pytest.mark.parametrize("source_format", ["txt", "markdown", "html", "epub"])
def test_rich_fixture_meets_declared_semantic_coverage(
    source_format: str,
    tmp_path: Path,
) -> None:
    fixture_contract = _rich_fixture_contract()
    expected = fixture_contract["formats"][source_format]
    source = RICH_FIXTURE_ROOT / expected["source"]
    assert hashlib.sha256(source.read_bytes()).hexdigest() == fixture_contract[
        "source_files"
    ][expected["source"]]

    result, writer = _normalize_rich_fixture(source_format)
    output = tmp_path / source_format
    writer(result, output)
    document, structure, manifest, report = _load_package(output)

    assert document["schema_version"] == "1.5.0"
    assert [chapter["title"] for chapter in document["chapters"]] == expected[
        "chapter_titles"
    ]
    block_count = sum(
        len(chapter["blocks"]) for chapter in document["chapters"]
    )
    assert block_count == expected["block_count"]
    assert len(manifest["block_bindings"]) == expected["binding_count"]
    assert len(manifest["assets"]) == expected["asset_count"]
    source_kinds = sorted(
        {row["source_kind"] for row in manifest["block_bindings"]}
    )
    assert source_kinds == expected["source_kinds"]
    assert sorted({asset["kind"] for asset in manifest["assets"]}) == expected[
        "asset_kinds"
    ]
    assert [unit["role"] for unit in structure["units"]] == expected["unit_roles"]
    assert [unit["translation_policy"] for unit in structure["units"]] == expected[
        "unit_policies"
    ]
    assert report["status"] == expected["expected_status"]
    assert report["counts"]["missing_assets"] == 0
    assert report["counts"]["review_required_bindings"] == 0
    _assert_materialized_assets_are_physical(output, manifest)


def test_rich_fixture_files_match_their_reviewed_hashes() -> None:
    contract = _rich_fixture_contract()
    assert contract["primary_source"] == "source.epub"
    for relative_path, expected_sha256 in contract["source_files"].items():
        path = RICH_FIXTURE_ROOT / relative_path
        assert path.is_file()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected_sha256


def test_rich_epub_fixture_is_reproducible_and_epub_conformant(
    tmp_path: Path,
) -> None:
    namespace = runpy.run_path(str(RICH_FIXTURE_ROOT / "build_epub.py"))
    rebuilt = namespace["build"](tmp_path / "rebuilt.epub")
    reviewed = RICH_FIXTURE_ROOT / "source.epub"

    assert rebuilt.read_bytes() == reviewed.read_bytes()
    with zipfile.ZipFile(reviewed) as archive:
        first = archive.infolist()[0]
        assert first.filename == "mimetype"
        assert first.compress_type == zipfile.ZIP_STORED
        assert archive.read("mimetype") == b"application/epub+zip"
        assert {
            "EPUB/nav.xhtml",
            "EPUB/package.opf",
            "EPUB/text/chapter1.xhtml",
            "EPUB/text/chapter2.xhtml",
            "EPUB/media/diagram.svg",
            "EPUB/media/chart.svg",
            "EPUB/styles/book.css",
        } <= set(archive.namelist())
        chapter_one = archive.read("EPUB/text/chapter1.xhtml").decode("utf-8")
        chapter_two = archive.read("EPUB/text/chapter2.xhtml").decode("utf-8")
        assert all(
            marker in chapter_one
            for marker in ("<figure", "<table", "<pre><code", "<math")
        )
        assert all(
            marker in chapter_two
            for marker in (
                "chapter1.xhtml#sample-trend-chart",
                'epub:type="noteref"',
                'epub:type="footnote"',
                '<img src="../media/chart.svg"/>',
            )
        )


def test_txt_writer_emits_text_only_exact_cover_without_changing_runtime(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.txt"
    source.write_text("CHAPTER I\n\nNarrative text.\n", encoding="utf-8")
    result = normalize_txt(source, doc_id="txt_fixture", pandoc_executable=None)
    original_document = json.loads(json.dumps(result.document))

    returned = write_txt_normalization(result, tmp_path / "out")
    document, structure, manifest, report = _load_package(tmp_path / "out")

    assert len(returned) == 2
    assert document == original_document
    assert document["schema_version"] == "1.5.0"
    assert manifest["assets"] == []
    assert report["status"] == "text_only"
    assert [row["block_id"] for row in manifest["block_bindings"]] == [
        block["block_id"]
        for chapter in document["chapters"]
        for block in chapter["blocks"]
    ]
    assert [unit["chapter_id"] for unit in structure["units"]] == [
        chapter["chapter_id"] for chapter in document["chapters"]
    ]
    assert all("unit_id" not in chapter for chapter in document["chapters"])


def test_markdown_writer_materializes_rich_and_mixed_content(
    tmp_path: Path,
) -> None:
    image = tmp_path / "figure.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nmarkdown-fixture")
    source = tmp_path / "source.md"
    source.write_text(
        "# Chapter One\n\n"
        "Text with $x+y$ and ![plot](figure.png).\n\n"
        "![standalone](figure.png)\n\n"
        "```python\nprint('hello')\n```\n\n"
        "$$\na=b\n$$\n\n"
        "| Term | Meaning |\n| --- | --- |\n| x | value |\n\n"
        "<div data-kind=\"note\">Raw note</div>\n",
        encoding="utf-8",
    )
    result = normalize_markdown(
        source,
        doc_id="markdown_fixture",
        pandoc_executable=None,
    )

    write_markdown_normalization(result, tmp_path / "out")
    document, _structure, manifest, report = _load_package(tmp_path / "out")
    bindings = {row["source_kind"]: row for row in manifest["block_bindings"]}
    mixed = next(
        row
        for row in manifest["block_bindings"]
        if row["semantic_subtype"] == "mixed_structured_content"
    )

    assert document == result.document
    assert report["counts"]["bindings"] == sum(
        len(chapter["blocks"]) for chapter in document["chapters"]
    )
    assert mixed["translation_policy"] == "translate_structured"
    assert set(_asset_kinds(manifest, mixed)) == {
        "raw_fragment",
        "image",
        "equation",
    }
    assert _asset_kinds(manifest, bindings["code"]) == ["code"]
    assert _asset_kinds(manifest, bindings["math_block"]) == ["equation"]
    assert _asset_kinds(manifest, bindings["table"]) == ["table"]
    assert _asset_kinds(manifest, bindings["raw_html"]) == ["raw_fragment"]
    image_binding = next(
        row for row in manifest["block_bindings"] if row["source_kind"] == "image"
    )
    assert set(_asset_kinds(manifest, image_binding)) == {
        "raw_fragment",
        "image",
    }
    _assert_materialized_assets_are_physical(tmp_path / "out", manifest)
    assert not list((tmp_path / "out").glob(".*.tmp"))


def test_html_writer_preserves_mixed_placement_and_altless_images(
    tmp_path: Path,
) -> None:
    (tmp_path / "inline.png").write_bytes(b"inline-image")
    (tmp_path / "altless.png").write_bytes(b"altless-image")
    source = tmp_path / "source.html"
    source.write_text(
        "<!doctype html><html><head><title>Fixture</title></head><body>"
        "<article id='chapter-1'><h1>Chapter One</h1>"
        "<p>Text <img src='inline.png' alt='plot'/> and "
        "<math><mi>x</mi><mo>+</mo><mi>y</mi></math>.</p>"
        "<table><tr><td>A</td></tr></table>"
        "<pre><code>print(1)</code></pre>"
        "<img src='altless.png'/>"
        "</article></body></html>",
        encoding="utf-8",
    )
    result = normalize_html(source, doc_id="html_fixture", pandoc_executable=None)

    write_html_normalization(result, tmp_path / "out")
    document, _structure, manifest, report = _load_package(tmp_path / "out")
    mixed = next(
        row
        for row in manifest["block_bindings"]
        if row["semantic_subtype"] == "mixed_structured_content"
    )
    asset_by_id = {asset["asset_id"]: asset for asset in manifest["assets"]}
    inventory_images = [
        asset
        for asset in manifest["assets"]
        if asset["kind"] == "image"
        and asset["source_locator"].get("inventory_scope") == "source_rich_node"
    ]

    assert document == result.document
    assert report["status"] == "preservation_complete"
    assert set(_asset_kinds(manifest, mixed)) == {
        "raw_fragment",
        "image",
        "equation",
    }
    assert len(inventory_images) == 2
    assert any(
        asset["metadata"].get("alt_text") == "" for asset in inventory_images
    )
    assert any(
        asset_by_id[asset_id]["source_locator"].get("fragment_scope")
        == "mixed_block"
        for asset_id in mixed["asset_ids"]
    )
    _assert_materialized_assets_are_physical(tmp_path / "out", manifest)


def _write_rich_epub(path: Path) -> Path:
    container = """<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
  <rootfiles><rootfile full-path="EPUB/package.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>"""
    package = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="uid">fixture</dc:identifier><dc:title>Fixture</dc:title><dc:language>en</dc:language>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/>
    <item id="image" href="media/plot.png" media-type="image/png"/>
    <item id="style" href="style.css" media-type="text/css"/>
  </manifest>
  <spine><itemref idref="chapter"/></spine>
</package>"""
    nav = """<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<body><nav epub:type="toc"><ol><li><a href="chapter.xhtml">Chapter I</a></li></ol></nav></body></html>"""
    chapter = """<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><title>Chapter I</title><link rel="stylesheet" href="style.css"/></head>
<body epub:type="bodymatter chapter"><section epub:type="bodymatter chapter">
<h1>Chapter I</h1><p>Before <img src="media/plot.png" alt="Plot"/> after.</p>
<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>
<pre><code>print(1)</code></pre>
<p>Equation <math xmlns="http://www.w3.org/1998/Math/MathML"><mi>x</mi><mo>+</mo><mi>y</mi></math>.</p>
</section></body></html>"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("EPUB/package.opf", package)
        archive.writestr("EPUB/nav.xhtml", nav)
        archive.writestr("EPUB/chapter.xhtml", chapter)
        archive.writestr("EPUB/media/plot.png", b"\x89PNG\r\n\x1a\nepub-fixture")
        archive.writestr("EPUB/style.css", b"body { font-family: serif; }")
    return path


@pytest.mark.skipif(not PANDOC_AVAILABLE, reason="Pandoc is required")
def test_epub_writer_preserves_opf_resources_and_xhtml_fragments(
    tmp_path: Path,
) -> None:
    source = _write_rich_epub(tmp_path / "source.epub")
    result = normalize_epub(source, doc_id="epub_fixture")

    write_epub_normalization(result, tmp_path / "out")
    document, structure, manifest, report = _load_package(tmp_path / "out")
    mixed = [
        row
        for row in manifest["block_bindings"]
        if row["semantic_subtype"] == "mixed_structured_content"
    ]
    source_kinds = {row["source_kind"] for row in manifest["block_bindings"]}

    assert document == result.document
    assert report["status"] == "preservation_complete"
    assert {"table", "code"} <= source_kinds
    assert all("raw_fragment" in _asset_kinds(manifest, row) for row in mixed)
    assert any(
        asset["media_type"] == "image/png"
        and asset["source_locator"].get("epub_member") == "EPUB/media/plot.png"
        for asset in manifest["assets"]
    )
    assert any(
        asset["media_type"] == "text/css"
        and asset["kind"] == "embedded_file"
        for asset in manifest["assets"]
    )
    assert any(row.get("resource_targets") for row in structure["source_map"])
    assert any(row.get("math_fragments") for row in structure["source_map"])
    _assert_materialized_assets_are_physical(tmp_path / "out", manifest)


def test_authored_and_synthetic_units_both_materialize_without_runtime_migration(
    tmp_path: Path,
) -> None:
    authored_source = tmp_path / "authored.md"
    authored_source.write_text("# Chapter One\n\nText.\n", encoding="utf-8")
    synthetic_source = tmp_path / "synthetic.md"
    synthetic_source.write_text(
        "A document without a reliable heading.\n\nSecond paragraph.\n",
        encoding="utf-8",
    )
    authored = normalize_markdown(
        authored_source,
        doc_id="authored",
        pandoc_executable=None,
    )
    synthetic = normalize_markdown(
        synthetic_source,
        doc_id="synthetic",
        pandoc_executable=None,
    )

    for name, result in (("authored", authored), ("synthetic", synthetic)):
        output = tmp_path / name
        write_markdown_normalization(result, output)
        document, structure, manifest, _report = _load_package(output)
        assert [unit["chapter_id"] for unit in structure["units"]] == [
            chapter["chapter_id"] for chapter in document["chapters"]
        ]
        assert len(manifest["block_bindings"]) == sum(
            len(chapter["blocks"]) for chapter in document["chapters"]
        )
        assert all("unit_id" not in chapter for chapter in document["chapters"])

    assert authored.structure_manifest["units"][0]["role"] == "content_unit"
    assert synthetic.structure_manifest["units"][0]["role"] == "unknown"
    assert synthetic.structure_manifest["units"][0]["review_required"] is True


def test_writer_rejects_source_changed_after_normalization(tmp_path: Path) -> None:
    source = tmp_path / "source.md"
    source.write_text("# Chapter One\n\nOriginal text.\n", encoding="utf-8")
    result = normalize_markdown(source, doc_id="drift", pandoc_executable=None)
    source.write_text("# Chapter One\n\nChanged text.\n", encoding="utf-8")

    with pytest.raises(
        CanonicalSourcePackageError,
        match="source bytes changed after normalization",
    ):
        write_markdown_normalization(result, tmp_path / "out")
    assert not (tmp_path / "out" / "asset_manifest.json").exists()


def test_failed_rewrite_removes_stale_asset_manifest(tmp_path: Path) -> None:
    source = tmp_path / "source.md"
    source.write_text("# Chapter One\n\nOriginal text.\n", encoding="utf-8")
    result = normalize_markdown(source, doc_id="stale", pandoc_executable=None)
    output = tmp_path / "out"
    write_markdown_normalization(result, output)
    assert (output / "asset_manifest.json").is_file()

    source.write_text("# Chapter One\n\nChanged text.\n", encoding="utf-8")
    with pytest.raises(CanonicalSourcePackageError, match="source bytes changed"):
        write_markdown_normalization(result, output)

    assert not (output / "asset_manifest.json").exists()


def test_writer_rechecks_source_after_asset_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pipeline.ingest import source_package_materializer as materializer

    source = tmp_path / "source.md"
    source.write_text("# Chapter One\n\nOriginal text.\n", encoding="utf-8")
    result = normalize_markdown(source, doc_id="midflight", pandoc_executable=None)
    original = materializer._markdown_assets
    mutated = False

    def mutate_after_read(*args, **kwargs):
        nonlocal mutated
        value = original(*args, **kwargs)
        if not mutated:
            source.write_text("# Chapter One\n\nMutated during write.\n", encoding="utf-8")
            mutated = True
        return value

    monkeypatch.setattr(materializer, "_markdown_assets", mutate_after_read)
    with pytest.raises(
        CanonicalSourcePackageError,
        match="source bytes changed while materializing",
    ):
        write_markdown_normalization(result, tmp_path / "out")
    assert not (tmp_path / "out" / "asset_manifest.json").exists()


def test_materializer_is_book_neutral() -> None:
    source = (
        Path(__file__).parents[1] / "ingest" / "source_package_materializer.py"
    ).read_text(encoding="utf-8").casefold()
    forbidden = [
        "canterville",
        "gatsby",
        "wuthering",
        "treasure island",
        "jekyll",
        "d2l",
    ]
    assert not any(name in source for name in forbidden)


def test_label_is_preserve_only_structural_content(tmp_path: Path) -> None:
    source = tmp_path / "source.md"
    source.write_text(":label: chapter_one\n", encoding="utf-8", newline="\n")
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    document = {
        "schema_version": "1.5.0",
        "doc_id": "label_fixture",
        "metadata": {
            "title": "Label fixture",
            "author": "",
            "domain": "technical",
            "genre": "technical_book",
            "source_language": "en",
            "target_language": "vi",
            "source_format": "markdown",
            "license": "unknown",
            "raw_sha256": source_sha256,
            "extraction_tool": "test",
            "pipeline_version": "test",
            "contamination_risk": "low",
        },
        "chapters": [
            {
                "chapter_id": "chapter_one",
                "order_index": 0,
                "title": "Chapter One",
                "blocks": [
                    {
                        "block_id": "label_b001",
                        "order_index": 0,
                        "page_ids": [],
                        "block_type": "paragraph",
                        "is_chapter_opening": True,
                        "source_text": ":label: chapter_one",
                        "clean_text": ":label: chapter_one",
                        "sentences": [],
                        "quality_flags": [],
                        "annotations": {},
                    }
                ],
            }
        ],
    }
    structure = {
        "schema_version": "test_structure_v1",
        "normalizer_version": "test",
        "doc_id": "label_fixture",
        "source": {"path": str(source), "sha256": source_sha256, "format": "markdown"},
        "units": [
            {
                "unit_id": "chapter_one",
                "chapter_id": "chapter_one",
                "order_index": 0,
                "title": "Chapter One",
                "block_range": [0, 1],
                "role": "content_unit",
                "translation_policy": "translate",
                "confidence": 1.0,
                "evidence": ["test"],
                "review_required": False,
            }
        ],
        "translatable_chapter_ids": ["chapter_one"],
        "review_required_unit_ids": [],
        "review_required_chapter_ids": [],
        "exact_cover": {
            "expected_blocks": 1,
            "covered_blocks": 1,
            "overlap_count": 0,
            "missing_count": 0,
            "coverage": 1.0,
        },
        "source_map": [
            {
                "block_id": "label_b001",
                "source_path": source.name,
                "line_range": [1, 1],
                "source_block_kind": "label",
                "provenance_precision": "test",
            }
        ],
        "block_policies": [
            {"block_id": "label_b001", "translation_policy": "preserve"}
        ],
        "structure_sha256": "0" * 64,
    }
    materialize_source_package(document, structure, tmp_path / "package")
    manifest = json.loads(
        (tmp_path / "package" / "asset_manifest.json").read_text(encoding="utf-8")
    )
    projection = json.loads(
        (tmp_path / "package" / "admitted_projection_v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["block_bindings"][0]["semantic_kind"] == "structural"
    assert manifest["block_bindings"][0]["render_role"] == "structural"
    assert manifest["block_bindings"][0]["asset_ids"] == []
    assert projection["rows"] == [
        {
            "chapter_id": "chapter_one",
            "block_id": "label_b001",
            "channel": "preserve_only",
        }
    ]
