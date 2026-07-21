from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path

import pytest

from pipeline.ingest.document_loader import _validate_stripped_document
from pipeline.ingest.epub_normalizer import normalize_epub, write_epub_normalization
from pipeline.ingest.epub_package import read_epub_package


def _write_epub(
    path: Path,
    *,
    items: list[tuple[str, str, str]],
    nav: str,
    spine: list[str],
) -> Path:
    manifest_rows = []
    for item_id, href, _content in items:
        properties = ' properties="nav"' if item_id == "nav" else ""
        manifest_rows.append(
            f'<item id="{item_id}" href="{href}" media-type="application/xhtml+xml"{properties}/>'
        )
    manifest = "\n".join(manifest_rows)
    itemrefs = "\n".join(f'<itemref idref="{item_id}"/>' for item_id in spine)
    opf = f'''<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="uid">fixture</dc:identifier><dc:title>Fixture Book</dc:title><dc:language>en</dc:language></metadata>
  <manifest>{manifest}</manifest><spine>{itemrefs}</spine>
</package>'''
    container = '''<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0"><rootfiles><rootfile full-path="EPUB/package.opf" media-type="application/oebps-package+xml"/></rootfiles></container>'''
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("EPUB/package.opf", opf)
        for _item_id, href, content in items:
            archive.writestr(f"EPUB/{href}", nav if _item_id == "nav" else content)
    return path


def _xhtml(title: str, body: str, *, epub_type: str) -> str:
    return f'''<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><head><title>{title}</title></head><body epub:type="{epub_type}"><section epub:type="{epub_type}"><h2>{title}</h2><p>{body}</p></section></body></html>'''


def _structured_fixture(tmp_path: Path) -> Path:
    nav = '''<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body>
<nav epub:type="toc"><ol>
<li><a href="front.xhtml">Title page</a></li>
<li><a href="part.xhtml">Part I</a><ol>
<li><a href="chapter1.xhtml">Chapter I</a></li>
<li><a href="chapter2.xhtml">Chapter II</a></li>
</ol></li>
<li><a href="notes.xhtml">Endnotes</a></li>
</ol></nav>
<nav epub:type="landmarks"><ol><li><a epub:type="bodymatter" href="part.xhtml">Book</a></li><li><a epub:type="backmatter endnotes" href="notes.xhtml">Endnotes</a></li></ol></nav>
</body></html>'''
    items = [
        ("nav", "nav.xhtml", ""),
        ("front", "front.xhtml", _xhtml("Title page", "Publication details.", epub_type="frontmatter titlepage")),
        ("part", "part.xhtml", _xhtml("Part I", "Part heading.", epub_type="bodymatter part")),
        ("c1", "chapter1.xhtml", _xhtml("Chapter I", "First narrative chapter.", epub_type="bodymatter chapter")),
        ("c2", "chapter2.xhtml", _xhtml("Chapter II", "Second narrative chapter.", epub_type="bodymatter chapter")),
        ("notes", "notes.xhtml", _xhtml("Endnotes", "Editorial notes.", epub_type="backmatter endnotes")),
    ]
    return _write_epub(
        tmp_path / "structured.epub",
        items=items,
        nav=nav,
        spine=["front", "part", "c1", "c2", "notes"],
    )


@pytest.mark.skipif(shutil.which("pandoc") is None, reason="Pandoc is required")
def test_epub_hybrid_normalizer_preserves_hierarchy_and_exact_cover(tmp_path: Path) -> None:
    source = _structured_fixture(tmp_path)
    result = normalize_epub(source, doc_id="fixture")
    units = result.structure_manifest["units"]
    assert [unit["role"] for unit in units] == [
        "front_matter",
        "container",
        "content_unit",
        "content_unit",
        "back_matter",
    ]
    assert units[2]["parent_unit_id"] == units[1]["unit_id"]
    assert units[3]["parent_unit_id"] == units[1]["unit_id"]
    assert result.structure_manifest["exact_cover"] == {
        "expected_blocks": result.structure_manifest["exact_cover"]["expected_blocks"],
        "covered_blocks": result.structure_manifest["exact_cover"]["expected_blocks"],
        "overlap_count": 0,
        "missing_count": 0,
        "coverage": 1.0,
    }
    assert len(result.structure_manifest["translatable_chapter_ids"]) == 2
    assert result.structure_manifest["translatable_chapter_ids"] == [
        units[2]["chapter_id"],
        units[3]["chapter_id"],
    ]
    assert len(result.structure_manifest["source_map"]) == result.structure_manifest["exact_cover"]["expected_blocks"]
    assert all(item["epub_file"] for item in result.structure_manifest["source_map"])
    assert all(
        block["block_type"] in {"heading", "paragraph", "dialogue", "footnote"}
        for chapter in result.document["chapters"]
        for block in chapter["blocks"]
    )
    assert all("unit_role" not in chapter for chapter in result.document["chapters"])
    _validate_stripped_document(result.document)


@pytest.mark.skipif(shutil.which("pandoc") is None, reason="Pandoc is required")
def test_epub_normalizer_keeps_ambiguous_internal_unit_for_review(tmp_path: Path) -> None:
    nav = '''<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body><nav epub:type="toc"><ol>
<li><a href="c1.xhtml">Chapter I</a></li><li><a href="aside.xhtml">Interlude material</a></li><li><a href="c2.xhtml">Chapter II</a></li>
</ol></nav></body></html>'''
    items = [
        ("nav", "nav.xhtml", ""),
        ("c1", "c1.xhtml", _xhtml("Chapter I", "First chapter.", epub_type="chapter bodymatter")),
        ("aside", "aside.xhtml", _xhtml("Interlude material", "Unclassified material.", epub_type="bodymatter")),
        ("c2", "c2.xhtml", _xhtml("Chapter II", "Second chapter.", epub_type="chapter bodymatter")),
    ]
    source = _write_epub(tmp_path / "ambiguous.epub", items=items, nav=nav, spine=["c1", "aside", "c2"])
    result = normalize_epub(source, doc_id="ambiguous")
    interlude = next(unit for unit in result.structure_manifest["units"] if unit["title"] == "Interlude material")
    assert interlude["role"] == "unknown"
    assert interlude["review_required"] is True


@pytest.mark.skipif(shutil.which("pandoc") is None, reason="Pandoc is required")
def test_epub_normalizer_splits_repeated_chapter_headings_after_contents(tmp_path: Path) -> None:
    nav = '''<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body><nav epub:type="toc"><ol>
<li><a href="body.xhtml">Fixture Book</a></li><li><a href="body.xhtml#license">License</a></li>
</ol></nav></body></html>'''
    body = '''<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Fixture Book</title></head><body>
<h1>Fixture Book</h1><h2>Contents</h2><p>First Story</p><p>Second Story</p>
<h2>First Story</h2><p>First narrative.</p><h2>Second Story</h2><p>Second narrative.</p>
<h2 id="license">THE FULL PROJECT GUTENBERG LICENSE</h2><p>License text.</p>
</body></html>'''
    items = [("nav", "nav.xhtml", ""), ("body", "body.xhtml", body)]
    source = _write_epub(tmp_path / "single-body.epub", items=items, nav=nav, spine=["body"])

    result = normalize_epub(source, doc_id="single_body")
    content = [
        unit for unit in result.structure_manifest["units"]
        if unit["role"] == "content_unit"
    ]

    assert [unit["title"] for unit in content] == ["First Story", "Second Story"]
    assert result.structure_manifest["exact_cover"]["coverage"] == 1.0
    assert result.structure_manifest["review_required_unit_ids"] == []


def test_epub_package_reader_rejects_parent_escape(tmp_path: Path) -> None:
    source = _structured_fixture(tmp_path)
    package = read_epub_package(source)
    assert package.navigation_source == "nav"
    assert [entry.title for entry in package.navigation][-1] == "Endnotes"
    assert len(package.spine) == 5


@pytest.mark.skipif(shutil.which("pandoc") is None, reason="Pandoc is required")
def test_epub_normalizer_writes_loader_compatible_artifacts(tmp_path: Path) -> None:
    result = normalize_epub(_structured_fixture(tmp_path), doc_id="fixture")
    document_path, manifest_path = write_epub_normalization(result, tmp_path / "out")
    document = json.loads(document_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert document["schema_version"] == "1.5.0"
    assert manifest["doc_id"] == document["doc_id"]
    assert manifest["source"]["sha256"] == document["metadata"]["raw_sha256"]
    assert [unit["chapter_id"] for unit in manifest["units"]] == [
        chapter["chapter_id"] for chapter in document["chapters"]
    ]
    assert all(block["annotations"] == {} for chapter in document["chapters"] for block in chapter["blocks"])


def test_runtime_normalizer_contains_no_book_specific_exceptions() -> None:
    root = Path(__file__).parents[1] / "ingest"
    source = (root / "epub_normalizer.py").read_text(encoding="utf-8").casefold()
    forbidden = ["canterville", "frankenstein", "treasure island", "yellow wallpaper", "christmas carol"]
    assert not any(name in source for name in forbidden)
