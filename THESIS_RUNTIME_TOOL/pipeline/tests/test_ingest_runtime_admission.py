from __future__ import annotations

import copy
import json
import shutil
import zipfile
from pathlib import Path

import pytest

from pipeline.ingest.admitted_projection import (
    PRESERVE_ONLY_SOURCE_KINDS,
    AdmissionProjectionError,
    validate_admitted_projection,
)
from pipeline.ingest.canonical_source_package import (
    CanonicalSourcePackageError,
    canonical_json_sha256,
    validate_canonical_source_package,
)
from pipeline.ingest.document_contract import (
    DocumentContractError,
    RUNTIME_BLOCK_TYPES,
    validate_locked_document,
)
from pipeline.ingest.markdown_normalizer import normalize_markdown
from pipeline.ingest.unified_source_normalizer import (
    normalize_source,
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


def _four_sources(tmp_path: Path) -> list[tuple[Path, str]]:
    return [
        (_write_epub(tmp_path / "book.epub"), "epub"),
        (
            _write(
                tmp_path / "book.html",
                "<html><head><title>Fixture</title></head><body>"
                "<h1>Chapter I</h1><p>Story text.</p></body></html>",
            ),
            "html",
        ),
        (_write(tmp_path / "book.md", "# Chapter I\n\nStory text.\n"), "markdown"),
        (_write(tmp_path / "book.txt", "CHAPTER I\n\nStory text.\n"), "txt"),
    ]


def _load_package(output: Path) -> tuple[dict, dict, dict, dict]:
    return tuple(
        json.loads((output / name).read_text(encoding="utf-8"))
        for name in (
            "document.json",
            "structure_manifest.json",
            "asset_manifest.json",
            "admitted_projection_v1.json",
        )
    )


def _rich_markdown(tmp_path: Path) -> Path:
    return _write(
        tmp_path / "rich.md",
        """# Chapter I

Plain narrative.

> Quoted evidence.

[^note]: Footnote evidence.

```python
print("preserve")
```

$$
x + y = z
$$

| Term | Meaning |
| --- | --- |
| x | value |

- First item.
- Second item.

<div data-raw="yes">Raw fragment.</div>

:preserve-directive:

***

![Missing illustration](missing.png)
""",
    )


@pytest.mark.skipif(not PANDOC_AVAILABLE, reason="Pandoc is required")
def test_all_four_formats_pass_locked_document_schema_and_emit_projection(
    tmp_path: Path,
) -> None:
    for index, (source, source_format) in enumerate(_four_sources(tmp_path), start=1):
        result = normalize_source(source, doc_id=f"fixture_{index}")
        validate_locked_document(result.document)
        assert result.document["metadata"]["source_format"] == source_format
        assert "normalizer_version" not in result.document["metadata"]
        assert "structure_sha256" not in result.document["metadata"]
        assert {
            block["block_type"]
            for chapter in result.document["chapters"]
            for block in chapter["blocks"]
        }.issubset(RUNTIME_BLOCK_TYPES)

        output = tmp_path / f"out_{index}"
        write_unified_normalization(result, output)
        document, structure, assets, projection = _load_package(output)
        validate_admitted_projection(projection, document, structure, assets)
        assert projection["integrity"]["row_count"] == result.receipt["counts"]["blocks"]


def test_locked_document_schema_rejects_missing_malformed_and_unknown_metadata(
    tmp_path: Path,
) -> None:
    source = _write(tmp_path / "book.md", "# Chapter I\n\nStory text.\n")
    document = normalize_markdown(
        source,
        doc_id="fixture",
        pandoc_executable=None,
    ).document
    validate_locked_document(document)

    missing = copy.deepcopy(document)
    del missing["metadata"]["license"]
    with pytest.raises(DocumentContractError, match="license"):
        validate_locked_document(missing)

    malformed = copy.deepcopy(document)
    malformed["metadata"]["source_language"] = "English"
    with pytest.raises(DocumentContractError, match="source_language"):
        validate_locked_document(malformed)

    unknown = copy.deepcopy(document)
    unknown["metadata"]["normalizer_version"] = "must-live-in-sidecar"
    with pytest.raises(DocumentContractError, match="normalizer_version"):
        validate_locked_document(unknown)


def test_package_validators_preserve_their_public_error_types(tmp_path: Path) -> None:
    result = normalize_source(
        _rich_markdown(tmp_path),
        doc_id="error_types",
        pandoc_executable=None,
    )
    output = tmp_path / "out"
    write_unified_normalization(result, output)
    document, structure, assets, projection = _load_package(output)
    invalid = copy.deepcopy(document)
    invalid["metadata"]["contamination_risk"] = "none"

    with pytest.raises(CanonicalSourcePackageError, match="contamination_risk"):
        validate_canonical_source_package(invalid, structure, assets)
    with pytest.raises(AdmissionProjectionError, match="contamination_risk"):
        validate_admitted_projection(projection, invalid, structure, assets)


def test_rich_kinds_remain_in_sidecars_and_never_leak_into_runtime_enum(
    tmp_path: Path,
) -> None:
    result = normalize_source(
        _rich_markdown(tmp_path),
        doc_id="rich",
        pandoc_executable=None,
    )
    output = tmp_path / "out"
    write_unified_normalization(result, output)
    document, structure, assets, projection = _load_package(output)
    validate_locked_document(document)
    validate_admitted_projection(projection, document, structure, assets)

    runtime_kinds = {
        block["block_type"]
        for chapter in document["chapters"]
        for block in chapter["blocks"]
    }
    source_kinds = {row["source_block_kind"] for row in structure["source_map"]}
    assert runtime_kinds.issubset(RUNTIME_BLOCK_TYPES)
    assert {
        "block_quote",
        "code",
        "directive",
        "footnote",
        "image",
        "list",
        "math_block",
        "raw_html",
        "separator",
        "table",
    }.issubset(
        source_kinds
    )

    row_by_block = {row["block_id"]: row for row in projection["rows"]}
    for binding in assets["block_bindings"]:
        channel = row_by_block[binding["block_id"]]["channel"]
        if binding["source_kind"] in PRESERVE_ONLY_SOURCE_KINDS or binding["asset_ids"]:
            assert channel != "semantic_text"
    assert any(row["channel"] == "semantic_text" for row in projection["rows"])
    assert any(row["channel"] == "structured_translate" for row in projection["rows"])
    assert any(row["channel"] in {"preserve_only", "review_required"} for row in projection["rows"])


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "reordered", "hash", "channel"])
def test_admitted_projection_rejects_exact_cover_and_integrity_tampering(
    tmp_path: Path,
    mutation: str,
) -> None:
    result = normalize_source(
        _rich_markdown(tmp_path),
        doc_id="tamper",
        pandoc_executable=None,
    )
    output = tmp_path / "out"
    write_unified_normalization(result, output)
    document, structure, assets, projection = _load_package(output)
    tampered = copy.deepcopy(projection)

    if mutation == "missing":
        tampered["rows"] = tampered["rows"][:-1]
    elif mutation == "duplicate":
        tampered["rows"].append(copy.deepcopy(tampered["rows"][-1]))
    elif mutation == "reordered":
        tampered["rows"][0], tampered["rows"][1] = (
            tampered["rows"][1],
            tampered["rows"][0],
        )
    elif mutation == "hash":
        tampered["integrity"]["payload_sha256"] = "0" * 64
    else:
        preserve_index = next(
            index
            for index, binding in enumerate(assets["block_bindings"])
            if binding["source_kind"] in PRESERVE_ONLY_SOURCE_KINDS
        )
        tampered["rows"][preserve_index]["channel"] = "semantic_text"
        tampered["integrity"]["payload_sha256"] = canonical_json_sha256(
            {key: value for key, value in tampered.items() if key != "integrity"}
        )

    with pytest.raises(AdmissionProjectionError):
        validate_admitted_projection(tampered, document, structure, assets)


@pytest.mark.skipif(not PANDOC_AVAILABLE, reason="Pandoc is required")
def test_projection_hashes_are_deterministic_for_all_four_formats(tmp_path: Path) -> None:
    for index, (source, _source_format) in enumerate(_four_sources(tmp_path), start=1):
        hashes: list[str] = []
        for rerun in (1, 2):
            result = normalize_source(source, doc_id=f"stable_{index}")
            output = tmp_path / f"stable_{index}_{rerun}"
            write_unified_normalization(result, output)
            projection = json.loads(
                (output / "admitted_projection_v1.json").read_text(encoding="utf-8")
            )
            hashes.append(projection["integrity"]["payload_sha256"])
        assert hashes[0] == hashes[1]
