"""Mechanical loader for an already-extracted literary source document.

Stage runners historically reopened one named EPUB and selected a chapter from
it.  The chapter-loop runner instead receives the project's sealed
``document.json``.  This module keeps that handoff book-neutral without changing
any Builder or Auditor semantics.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping


class LiteraryChapterSourceError(ValueError):
    pass


def load_literary_source_document_v1(path: Path) -> dict[str, Any]:
    source = Path(path).resolve()
    try:
        value = json.loads(source.read_text(encoding="utf-8-sig"))
    except (OSError, TypeError, ValueError) as exc:
        raise LiteraryChapterSourceError(
            f"cannot load literary source document: {source}"
        ) from exc
    if not isinstance(value, Mapping):
        raise LiteraryChapterSourceError("literary source document must be an object")
    document = dict(value)
    chapters = document.get("chapters")
    if not isinstance(chapters, list) or not chapters:
        raise LiteraryChapterSourceError("literary source document has no chapters")
    chapter_ids: list[str] = []
    block_ids: set[str] = set()
    for chapter_index, raw_chapter in enumerate(chapters, start=1):
        if not isinstance(raw_chapter, Mapping):
            raise LiteraryChapterSourceError(
                f"chapter {chapter_index} must be an object"
            )
        chapter_id = raw_chapter.get("chapter_id")
        if not isinstance(chapter_id, str) or not chapter_id:
            raise LiteraryChapterSourceError(
                f"chapter {chapter_index} has no chapter_id"
            )
        chapter_ids.append(chapter_id)
        blocks = raw_chapter.get("blocks")
        if not isinstance(blocks, list) or not blocks:
            raise LiteraryChapterSourceError(f"chapter {chapter_id} has no blocks")
        for block_index, raw_block in enumerate(blocks, start=1):
            if not isinstance(raw_block, Mapping):
                raise LiteraryChapterSourceError(
                    f"chapter {chapter_id} block {block_index} must be an object"
                )
            block_id = raw_block.get("block_id")
            if not isinstance(block_id, str) or not block_id:
                raise LiteraryChapterSourceError(
                    f"chapter {chapter_id} block {block_index} has no block_id"
                )
            if block_id in block_ids:
                raise LiteraryChapterSourceError(
                    f"literary source document repeats block_id {block_id}"
                )
            block_ids.add(block_id)
            text = raw_block.get("clean_text")
            if not isinstance(text, str):
                raise LiteraryChapterSourceError(
                    f"chapter {chapter_id} block {block_id} has no clean_text"
                )
    if len(chapter_ids) != len(set(chapter_ids)):
        raise LiteraryChapterSourceError("literary source document repeats chapter_id")
    if not any(document.get(key) for key in ("doc_id", "document_id", "id")):
        raise LiteraryChapterSourceError(
            "literary source document has no stable document identifier"
        )
    return document


def chapter_from_document_v1(
    document: Mapping[str, Any], chapter_id: str
) -> dict[str, Any]:
    for row in document.get("chapters") or []:
        if isinstance(row, Mapping) and row.get("chapter_id") == chapter_id:
            return dict(row)
    raise LiteraryChapterSourceError(f"chapter is absent: {chapter_id}")


__all__ = [
    "LiteraryChapterSourceError",
    "chapter_from_document_v1",
    "load_literary_source_document_v1",
]
