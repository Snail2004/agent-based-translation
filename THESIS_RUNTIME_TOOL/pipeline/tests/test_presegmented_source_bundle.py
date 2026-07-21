from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from pipeline.ingest.presegmented_source_bundle import (
    BLOCK_MAP_SCHEMA_VERSION,
    PresegmentedBundleError,
    SCHEMA_VERSION,
    load_presegmented_bundle,
)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _write_bundle(
    root: Path,
    *,
    blocks: list[tuple[str, str, str, str]],
    source_format: str = "markdown",
    marker_lines: list[str] | None = None,
    manifest_overrides: dict[str, object] | None = None,
    map_overrides: dict[str, object] | None = None,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    chapter_ids: list[str] = []
    chapter_titles: dict[str, str] = {}
    rows: list[dict[str, object]] = []
    source_parts: list[str] = []
    for index, (marker, block_id, chapter_id, text) in enumerate(blocks):
        if chapter_id not in chapter_ids:
            chapter_ids.append(chapter_id)
            chapter_titles[chapter_id] = chapter_id.replace("_", " ").title()
        canonical = text.strip()
        rows.append(
            {
                "marker": marker,
                "block_id": block_id,
                "chapter_id": chapter_id,
                "order_index": index,
                "block_type": "heading" if index == 0 else "prose",
                "source_sha256": _sha256(canonical.encode("utf-8")),
                "source_utf8_bytes": len(canonical.encode("utf-8")),
            }
        )
        source_parts.extend([f"[[{marker}]]\n", text, "\n"])
    source_bytes = "".join(source_parts).encode("utf-8")
    chapters = [
        {"chapter_id": chapter_id, "order_index": index, "title": chapter_titles[chapter_id]}
        for index, chapter_id in enumerate(chapter_ids)
    ]
    block_map: dict[str, object] = {
        "schema_version": BLOCK_MAP_SCHEMA_VERSION,
        "document_id": "fixture_book",
        "chapters": chapters,
        "rows": rows,
    }
    if map_overrides:
        block_map.update(map_overrides)
    map_bytes = _json_bytes(block_map)
    manifest: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "document_id": "fixture_book",
        "source_format": source_format,
        "source_file": "source.md",
        "source_sha256": _sha256(source_bytes),
        "source_utf8_bytes": len(source_bytes),
        "block_map_file": "block_map.json",
        "block_map_sha256": _sha256(map_bytes),
        "block_count": len(rows),
        "chapter_count": len(chapters),
        "encoding": "UTF-8",
        "line_endings": "LF",
        "text_policy": "strip_outer_whitespace_v1",
        "marker_syntax": {"prefix": "[[", "suffix": "]]"},
    }
    if manifest_overrides:
        manifest.update(manifest_overrides)
    if marker_lines is not None:
        source_bytes = ("\n".join(marker_lines) + "\n").encode("utf-8")
        manifest["source_sha256"] = _sha256(source_bytes)
        manifest["source_utf8_bytes"] = len(source_bytes)
    (root / "source.md").write_bytes(source_bytes)
    (root / "block_map.json").write_bytes(map_bytes)
    (root / "manifest.json").write_bytes(_json_bytes(manifest))
    return root


def _base_bundle(tmp_path: Path) -> Path:
    return _write_bundle(
        tmp_path / "bundle",
        blocks=[
            ("M0001", "fixture_ch01_b001", "chapter_1", "  Chapter 1  "),
            ("M0002", "fixture_ch01_b002", "chapter_1", "A paragraph."),
            ("M0003", "fixture_ch02_b001", "chapter_2", "Chapter 2"),
            ("M0004", "fixture_ch02_b002", "chapter_2", "A code-like value."),
        ],
    )


def _swap_first_two_marker_lines(source: str) -> str:
    lines = source.splitlines(keepends=True)
    first = next(index for index, line in enumerate(lines) if line == "[[M0001]]\n")
    second = next(index for index, line in enumerate(lines) if line == "[[M0002]]\n")
    lines[first], lines[second] = lines[second], lines[first]
    return "".join(lines)


def test_valid_bundle_accepts_generic_and_d2l_like_markers(tmp_path: Path) -> None:
    bundle = _base_bundle(tmp_path)

    result = load_presegmented_bundle(bundle)

    assert result.document_id == "fixture_book"
    assert result.block_count == 4
    assert result.chapter_count == 2
    assert [block.marker for block in result.blocks] == ["M0001", "M0002", "M0003", "M0004"]
    assert [block.block_id for block in result.blocks] == [
        "fixture_ch01_b001",
        "fixture_ch01_b002",
        "fixture_ch02_b001",
        "fixture_ch02_b002",
    ]
    assert result.blocks[0].source_text == "Chapter 1"
    assert result.chapters[0].first_block_index == 0
    assert result.chapters[0].last_block_index == 1
    assert result.chapters[1].first_block_index == 2
    assert result.identity_sha256 == load_presegmented_bundle(bundle).identity_sha256


@pytest.mark.parametrize("source_format", ["markdown", "html", "txt", "epub", "pdf"])
def test_source_format_is_metadata_not_marker_convention(tmp_path: Path, source_format: str) -> None:
    bundle = _write_bundle(
        tmp_path / source_format,
        source_format=source_format,
        blocks=[("B0001", "book_b001", "unit_1", "A block")],
    )

    result = load_presegmented_bundle(bundle)

    assert result.source_format == source_format


def test_rich_source_kinds_are_preserved_as_source_kinds(tmp_path: Path) -> None:
    root = _write_bundle(
        tmp_path / "rich",
        blocks=[
            ("M0001", "book_b001", "unit_1", "Heading"),
            ("M0002", "book_b002", "unit_1", "code"),
        ],
    )
    block_map = json.loads((root / "block_map.json").read_text(encoding="utf-8"))
    block_map["rows"][1]["block_type"] = "math_block"
    map_bytes = _json_bytes(block_map)
    (root / "block_map.json").write_bytes(map_bytes)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    manifest["block_map_sha256"] = _sha256(map_bytes)
    (root / "manifest.json").write_bytes(_json_bytes(manifest))

    result = load_presegmented_bundle(root)

    assert result.blocks[1].block_type == "math_block"


@pytest.mark.parametrize(
    ("name", "mutator"),
    [
        ("missing_marker", lambda source: source.replace("[[M0003]]", "[[M9999]]")),
        (
            "reordered_markers",
            _swap_first_two_marker_lines,
        ),
        ("malformed_marker", lambda source: source.replace("[[M0002]]", "[[M0002]] extra", 1)),
    ],
)
def test_marker_errors_fail_closed(tmp_path: Path, name: str, mutator) -> None:
    root = _base_bundle(tmp_path / name)
    source = (root / "source.md").read_text(encoding="utf-8")
    mutated = mutator(source)
    source_bytes = mutated.encode("utf-8")
    (root / "source.md").write_bytes(source_bytes)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    manifest["source_sha256"] = _sha256(source_bytes)
    manifest["source_utf8_bytes"] = len(source_bytes)
    (root / "manifest.json").write_bytes(_json_bytes(manifest))

    with pytest.raises(PresegmentedBundleError):
        load_presegmented_bundle(root)


def test_inline_brackets_remain_content(tmp_path: Path) -> None:
    root = _write_bundle(
        tmp_path,
        blocks=[
            ("M0001", "book_b001", "unit_1", "Heading"),
            (
                "M0002",
                "book_b002",
                "unit_1",
                "X = tensor([[[0.0], [1.0]],\n               [[2.0], [3.0]]])",
            ),
        ],
    )

    result = load_presegmented_bundle(root)

    assert result.blocks[1].source_text == "X = tensor([[[0.0], [1.0]],\n               [[2.0], [3.0]]])"


@pytest.mark.parametrize(
    "mutator",
    [
        lambda rows: rows.reverse(),
        lambda rows: rows.__setitem__(1, rows[0].copy()),
        lambda rows: rows.__setitem__(1, {**rows[1], "chapter_id": "unknown"}),
        lambda rows: rows.__setitem__(1, {**rows[1], "order_index": 7}),
    ],
)
def test_block_map_identity_and_order_errors_fail_closed(tmp_path: Path, mutator) -> None:
    root = _base_bundle(tmp_path)
    block_map = json.loads((root / "block_map.json").read_text(encoding="utf-8"))
    mutator(block_map["rows"])
    map_bytes = _json_bytes(block_map)
    (root / "block_map.json").write_bytes(map_bytes)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    manifest["block_map_sha256"] = _sha256(map_bytes)
    (root / "manifest.json").write_bytes(_json_bytes(manifest))

    with pytest.raises(PresegmentedBundleError):
        load_presegmented_bundle(root)


def test_source_and_component_tamper_fail_before_parse(tmp_path: Path) -> None:
    root = _base_bundle(tmp_path)
    (root / "source.md").write_bytes((root / "source.md").read_bytes() + b"tamper")
    with pytest.raises(PresegmentedBundleError, match="source_sha256"):
        load_presegmented_bundle(root)

    root = _base_bundle(tmp_path / "map")
    (root / "block_map.json").write_bytes((root / "block_map.json").read_bytes() + b" ")
    with pytest.raises(PresegmentedBundleError, match="block_map_sha256"):
        load_presegmented_bundle(root)


def test_bom_and_crlf_are_rejected(tmp_path: Path) -> None:
    root = _base_bundle(tmp_path / "bom")
    source = (root / "source.md").read_bytes()
    (root / "source.md").write_bytes(b"\xef\xbb\xbf" + source)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    source_bytes = (root / "source.md").read_bytes()
    manifest["source_sha256"] = _sha256(source_bytes)
    manifest["source_utf8_bytes"] = len(source_bytes)
    (root / "manifest.json").write_bytes(_json_bytes(manifest))
    with pytest.raises(PresegmentedBundleError, match="BOM"):
        load_presegmented_bundle(root)

    root = _base_bundle(tmp_path / "crlf")
    source = (root / "source.md").read_bytes().replace(b"\n", b"\r\n")
    (root / "source.md").write_bytes(source)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    manifest["source_sha256"] = _sha256(source)
    manifest["source_utf8_bytes"] = len(source)
    (root / "manifest.json").write_bytes(_json_bytes(manifest))
    with pytest.raises(PresegmentedBundleError, match="LF"):
        load_presegmented_bundle(root)


def test_json_duplicate_keys_and_unknown_manifest_keys_are_rejected(tmp_path: Path) -> None:
    root = _base_bundle(tmp_path / "duplicate")
    (root / "manifest.json").write_text(
        '{"schema_version":"presegmented_source_bundle_v1","schema_version":"bad"}',
        encoding="utf-8",
        newline="\n",
    )
    with pytest.raises(PresegmentedBundleError, match="duplicate"):
        load_presegmented_bundle(root)

    root = _base_bundle(tmp_path / "unknown")
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    manifest["unknown"] = True
    (root / "manifest.json").write_bytes(_json_bytes(manifest))
    with pytest.raises(PresegmentedBundleError, match="unknown keys"):
        load_presegmented_bundle(root)


def test_chapter_interleaving_and_empty_chapter_are_rejected(tmp_path: Path) -> None:
    root = _write_bundle(
        tmp_path / "interleaved",
        blocks=[
            ("M0001", "b1", "chapter_1", "one"),
            ("M0002", "b2", "chapter_2", "two"),
            ("M0003", "b3", "chapter_1", "three"),
        ],
    )
    with pytest.raises(PresegmentedBundleError, match="contiguous"):
        load_presegmented_bundle(root)

    root = _write_bundle(
        tmp_path / "empty",
        blocks=[("M0001", "b1", "chapter_1", "one")],
        map_overrides={
            "chapters": [
                {"chapter_id": "chapter_1", "order_index": 0, "title": "One"},
                {"chapter_id": "chapter_2", "order_index": 1, "title": "Empty"},
            ]
        },
    )
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    map_bytes = (root / "block_map.json").read_bytes()
    manifest["block_map_sha256"] = _sha256(map_bytes)
    manifest["chapter_count"] = 2
    (root / "manifest.json").write_bytes(_json_bytes(manifest))
    with pytest.raises(PresegmentedBundleError):
        load_presegmented_bundle(root)


def test_path_traversal_is_rejected(tmp_path: Path) -> None:
    root = _base_bundle(tmp_path)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    manifest["source_file"] = "../source.md"
    (root / "manifest.json").write_bytes(_json_bytes(manifest))
    with pytest.raises(PresegmentedBundleError, match="unsafe|escapes"):
        load_presegmented_bundle(root)


def test_symlink_component_is_rejected_when_supported(tmp_path: Path) -> None:
    root = _base_bundle(tmp_path)
    original = root / "source.md"
    moved = root / "source.real.md"
    original.rename(moved)
    try:
        original.symlink_to(moved)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not available")
    with pytest.raises(PresegmentedBundleError):
        load_presegmented_bundle(root)


def test_no_network_or_existing_normalizer_dependency_is_needed() -> None:
    source = Path(__file__).parents[1] / "ingest" / "presegmented_source_bundle.py"
    text = source.read_text(encoding="utf-8")
    assert "urllib" not in text
    assert "requests" not in text
    assert "unified_source_normalizer" not in text
    assert "d2l_markdown_loader" not in text


def test_published_schema_is_a_valid_draft_2020_12_schema() -> None:
    schema_path = Path(__file__).parents[1] / "ingest" / "schemas" / "presegmented_source_bundle_v1.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    assert schema["$defs"]["block_map"]["properties"]["rows"]["items"]["$ref"] == "#/$defs/block_row"
