"""Neutral whole-book source identity shared across literary pipeline stages."""

from __future__ import annotations

from typing import Any, Mapping, TypedDict

from pipeline.literary.checkpoint import canonical_hash, canonical_json
from pipeline.literary.source_anchor import nfc_block_string


BOOK_SOURCE_MANIFEST_SCHEMA_VERSION = "literary_book_source_manifest_v1"
STATE_LINEAGE_SCHEMA_VERSION = "literary_book_lineage_v1"


class BookSourceLineageError(ValueError):
    """Raised when a whole-book source identity is malformed or stale."""


class BookSourceChapter(TypedDict):
    chapter_id: str
    source_hash: str


class BookSourceManifest(TypedDict):
    manifest_schema_version: str
    ordered_chapters: list[BookSourceChapter]
    manifest_hash: str


def _ordered_blocks(chapter: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = [dict(row) for row in chapter.get("blocks") or [] if row.get("block_id")]
    rows.sort(key=lambda row: (int(row.get("order_index") or 0), str(row["block_id"])))
    if len({str(row["block_id"]) for row in rows}) != len(rows):
        raise BookSourceLineageError(
            f"duplicate source block id: {chapter.get('chapter_id')}"
        )
    return rows


def _block_view(block: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "block_id": str(block.get("block_id") or ""),
        "order_index": int(block.get("order_index") or 0),
        "block_type": str(block.get("block_type") or ""),
        "text": nfc_block_string(block),
    }


def chapter_source_hash(chapter: Mapping[str, Any]) -> str:
    return canonical_hash([_block_view(row) for row in _ordered_blocks(chapter)])


def book_source_manifest_body(manifest: Mapping[str, Any]) -> dict[str, Any]:
    if set(manifest) != {
        "manifest_schema_version",
        "ordered_chapters",
        "manifest_hash",
    }:
        raise BookSourceLineageError("book source manifest has an invalid field set")
    if manifest.get("manifest_schema_version") != BOOK_SOURCE_MANIFEST_SCHEMA_VERSION:
        raise BookSourceLineageError("book source manifest schema mismatch")
    raw_rows = manifest.get("ordered_chapters")
    if not isinstance(raw_rows, (list, tuple)) or not raw_rows:
        raise BookSourceLineageError("book source manifest must contain ordered chapters")
    rows: list[dict[str, str]] = []
    for raw_row in raw_rows:
        if not isinstance(raw_row, Mapping) or set(raw_row) != {
            "chapter_id",
            "source_hash",
        }:
            raise BookSourceLineageError("book source manifest chapter row is malformed")
        chapter_id = str(raw_row.get("chapter_id") or "")
        source_hash = str(raw_row.get("source_hash") or "")
        if not chapter_id or not source_hash:
            raise BookSourceLineageError("book source manifest chapter row is incomplete")
        rows.append({"chapter_id": chapter_id, "source_hash": source_hash})
    if len({row["chapter_id"] for row in rows}) != len(rows):
        raise BookSourceLineageError("book source manifest contains duplicate chapter ids")
    return {
        "manifest_schema_version": BOOK_SOURCE_MANIFEST_SCHEMA_VERSION,
        "ordered_chapters": rows,
    }


def build_book_source_manifest(document: Mapping[str, Any]) -> BookSourceManifest:
    chapters = [dict(row) for row in document.get("chapters") or []]
    if not chapters:
        raise BookSourceLineageError("whole-book document has no chapters")
    rows: list[BookSourceChapter] = []
    for chapter in chapters:
        chapter_id = str(chapter.get("chapter_id") or "")
        if not chapter_id:
            raise BookSourceLineageError("whole-book document has a chapter without id")
        rows.append(
            {"chapter_id": chapter_id, "source_hash": chapter_source_hash(chapter)}
        )
    if len({row["chapter_id"] for row in rows}) != len(rows):
        raise BookSourceLineageError("whole-book document contains duplicate chapter ids")
    body = {
        "manifest_schema_version": BOOK_SOURCE_MANIFEST_SCHEMA_VERSION,
        "ordered_chapters": rows,
    }
    return {**body, "manifest_hash": canonical_hash(body)}


def verify_book_source_manifest(
    document: Mapping[str, Any], manifest: Mapping[str, Any]
) -> BookSourceManifest:
    body = book_source_manifest_body(manifest)
    if canonical_hash(body) != str(manifest.get("manifest_hash") or ""):
        raise BookSourceLineageError("book source manifest hash mismatch")
    expected = build_book_source_manifest(document)
    if canonical_json(expected) != canonical_json(manifest):
        raise BookSourceLineageError(
            "book source manifest does not match the whole document"
        )
    return dict(manifest)  # type: ignore[return-value]


def state_lineage_id_for_manifest(manifest: Mapping[str, Any]) -> str:
    body = book_source_manifest_body(manifest)
    manifest_hash = str(manifest.get("manifest_hash") or "")
    if canonical_hash(body) != manifest_hash:
        raise BookSourceLineageError("book source manifest hash mismatch")
    return canonical_hash(
        {
            "lineage_schema_version": STATE_LINEAGE_SCHEMA_VERSION,
            "book_source_manifest_hash": manifest_hash,
        }
    )


__all__ = [
    "BOOK_SOURCE_MANIFEST_SCHEMA_VERSION",
    "STATE_LINEAGE_SCHEMA_VERSION",
    "BookSourceChapter",
    "BookSourceLineageError",
    "BookSourceManifest",
    "book_source_manifest_body",
    "build_book_source_manifest",
    "chapter_source_hash",
    "state_lineage_id_for_manifest",
    "verify_book_source_manifest",
]
