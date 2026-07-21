from __future__ import annotations

import copy
import json
import shutil
import zipfile
from pathlib import Path

import pytest

from pipeline.ingest.document_loader import load_document
from pipeline.ingest.canonical_source_package import validate_canonical_source_package
from pipeline.ingest.unified_source_normalizer import (
    UnifiedContractError,
    detect_source_format,
    normalize_source,
    validate_normalization_contract,
    write_unified_normalization,
)


PANDOC_AVAILABLE = shutil.which("pandoc") is not None


def _write(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8", newline="\n")
    return path


def _write_epub(path: Path) -> Path:
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
  </manifest>
  <spine><itemref idref="chapter"/></spine>
</package>"""
    nav = """<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<body><nav epub:type="toc"><ol><li><a href="chapter.xhtml">Chapter I</a></li></ol></nav></body></html>"""
    chapter = """<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><title>Chapter I</title></head><body epub:type="bodymatter chapter">
<section epub:type="bodymatter chapter"><h1>Chapter I</h1><p>Story text.</p></section></body></html>"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("EPUB/package.opf", package)
        archive.writestr("EPUB/nav.xhtml", nav)
        archive.writestr("EPUB/chapter.xhtml", chapter)
    return path


def _fixtures(tmp_path: Path) -> list[tuple[Path, str]]:
    return [
        (_write_epub(tmp_path / "book.epub"), "epub"),
        (
            _write(
                tmp_path / "book.html",
                "<html><head><title>Fixture</title></head><body><h1>Chapter I</h1><p>Story text.</p></body></html>",
            ),
            "html",
        ),
        (_write(tmp_path / "book.md", "# Chapter I\n\nStory text.\n"), "markdown"),
        (
            _write(
                tmp_path / "book.txt",
                "*** START OF THE PROJECT GUTENBERG EBOOK FIXTURE ***\n\nCHAPTER I\n\nStory text.\n\n"
                "*** END OF THE PROJECT GUTENBERG EBOOK FIXTURE ***\n",
            ),
            "txt",
        ),
    ]


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("book.epub", "epub"),
        ("book.HTML", "html"),
        ("book.htm", "html"),
        ("book.md", "markdown"),
        ("book.markdown", "markdown"),
        ("book.pdf", "pdf"),
        ("book.txt", "txt"),
    ],
)
def test_detect_source_format_uses_closed_extension_map(filename: str, expected: str) -> None:
    assert detect_source_format(filename) == expected


def test_detect_source_format_rejects_unknown_extension() -> None:
    with pytest.raises(ValueError, match="Unsupported source format"):
        detect_source_format("book.docx")


@pytest.mark.skipif(not PANDOC_AVAILABLE, reason="Pandoc is required")
def test_unified_entrypoint_normalizes_all_supported_formats(tmp_path: Path) -> None:
    for index, (source, expected_format) in enumerate(_fixtures(tmp_path), start=1):
        result = normalize_source(source, doc_id=f"fixture_{index}")
        receipt = result.receipt

        assert receipt["schema_version"] == "normalization_receipt_v1"
        assert receipt["source_format"] == expected_format
        assert receipt["document_schema_version"] == "1.5.0"
        assert receipt["counts"]["units"] == len(result.document["chapters"])
        assert receipt["counts"]["blocks"] > 0
        assert receipt["status"] in {
            "ready",
            "review_required",
            "no_translatable_content",
        }

        output = tmp_path / f"out_{index}"
        document_path, manifest_path, receipt_path = write_unified_normalization(result, output)
        assert json.loads(receipt_path.read_text(encoding="utf-8")) == receipt
        structure = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert structure["doc_id"] == f"fixture_{index}"
        asset_manifest = json.loads(
            (output / "asset_manifest.json").read_text(encoding="utf-8")
        )
        package_report = validate_canonical_source_package(
            result.document,
            structure,
            asset_manifest,
            package_root=output,
        )
        assert package_report["counts"]["bindings"] == receipt["counts"]["blocks"]
        report = load_document(output / "memory.sqlite3", document_path)
        assert report.blocks == receipt["counts"]["blocks"]
        assert report.warnings == []
        assert not list(output.glob(".*.tmp"))


def test_unified_entrypoint_marks_ambiguous_txt_for_review(tmp_path: Path) -> None:
    source = _write(tmp_path / "notes.txt", "Paragraph one.\n\nParagraph two.\n")
    result = normalize_source(source, doc_id="notes", pandoc_executable=None)

    assert result.receipt["status"] == "review_required"
    assert result.receipt["counts"]["translatable_units"] == 0
    assert result.receipt["counts"]["review_required_units"] == 1


def test_unified_entrypoint_requires_pandoc_for_epub(tmp_path: Path) -> None:
    source = _write_epub(tmp_path / "book.epub")
    with pytest.raises(ValueError, match="requires a Pandoc executable"):
        normalize_source(source, doc_id="fixture", pandoc_executable=None)


@pytest.mark.skipif(not PANDOC_AVAILABLE, reason="Pandoc is required")
def test_common_contract_rejects_cross_artifact_tampering(tmp_path: Path) -> None:
    source = _write(tmp_path / "book.md", "# Chapter I\n\nStory text.\n")
    result = normalize_source(source, doc_id="fixture")

    bad_manifest = copy.deepcopy(result.structure_manifest)
    bad_manifest["source_map"] = bad_manifest["source_map"][:-1]
    with pytest.raises(UnifiedContractError, match="source_map"):
        validate_normalization_contract(result.document, bad_manifest, expected_format="markdown")

    bad_document = copy.deepcopy(result.document)
    bad_document["chapters"][0]["blocks"].append(
        copy.deepcopy(bad_document["chapters"][0]["blocks"][0])
    )
    with pytest.raises(UnifiedContractError, match="duplicate block_id"):
        validate_normalization_contract(bad_document, result.structure_manifest, expected_format="markdown")

    bad_manifest = copy.deepcopy(result.structure_manifest)
    bad_manifest["translatable_chapter_ids"].append("foreign_chapter")
    with pytest.raises(UnifiedContractError, match="translatable chapter ids"):
        validate_normalization_contract(result.document, bad_manifest, expected_format="markdown")

    bad_manifest = copy.deepcopy(result.structure_manifest)
    bad_manifest["translatable_chapter_ids"] = []
    with pytest.raises(UnifiedContractError, match="manifest unit decisions"):
        validate_normalization_contract(result.document, bad_manifest, expected_format="markdown")

    bad_manifest = copy.deepcopy(result.structure_manifest)
    bad_manifest["units"][0]["review_required"] = True
    with pytest.raises(UnifiedContractError, match="review lists"):
        validate_normalization_contract(result.document, bad_manifest, expected_format="markdown")


def test_unified_module_contains_no_source_specific_exceptions() -> None:
    source = Path(__file__).parents[1] / "ingest" / "unified_source_normalizer.py"
    text = source.read_text(encoding="utf-8").casefold()
    for forbidden in ("canterville", "gatsby", "wuthering", "treasure", "jekyll"):
        assert forbidden not in text
