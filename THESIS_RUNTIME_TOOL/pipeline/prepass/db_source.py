from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from pipeline.memory.store_init import init_db


def load_document_from_db(
    db_path: str | Path,
    chapter_ids: list[str],
    *,
    doc_id: str | None = None,
    translate_only: bool = True,
) -> dict[str, Any]:
    conn = init_db(db_path)
    try:
        selected_doc_id = doc_id or _single_doc_id(conn)
        return load_document_from_connection(
            conn,
            selected_doc_id,
            chapter_ids,
            translate_only=translate_only,
        )
    finally:
        conn.close()


def load_document_from_connection(
    conn: sqlite3.Connection,
    doc_id: str,
    chapter_ids: list[str],
    *,
    translate_only: bool = True,
) -> dict[str, Any]:
    resolved_chapters = _resolve_chapter_ids(conn, doc_id, chapter_ids)
    chapters: list[dict[str, Any]] = []
    for chapter_id in resolved_chapters:
        if translate_only:
            rows = conn.execute(
                """
                SELECT block_id, chapter_id, order_index, block_type, text,
                       original_text, style_json, translation_mode
                FROM blocks
                WHERE doc_id = ? AND chapter_id = ?
                  AND (
                    COALESCE(translation_mode, 'translate') = 'translate'
                    OR block_type = 'heading'
                  )
                ORDER BY order_index
                """,
                (doc_id, chapter_id),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT block_id, chapter_id, order_index, block_type, text,
                       original_text, style_json, translation_mode
                FROM blocks
                WHERE doc_id = ? AND chapter_id = ?
                ORDER BY order_index
                """,
                (doc_id, chapter_id),
            ).fetchall()
        chapters.append(
            {
                "chapter_id": chapter_id,
                "blocks": [_row_to_block(row) for row in rows],
            }
        )
    return {"doc_id": doc_id, "chapters": chapters}


def _single_doc_id(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        "SELECT doc_id FROM documents ORDER BY doc_id"
    ).fetchall()
    if not rows:
        raise ValueError("No documents found in DB")
    if len(rows) > 1:
        raise ValueError("Multiple documents found; pass doc_id explicitly")
    return str(rows[0]["doc_id"])


def _resolve_chapter_ids(
    conn: sqlite3.Connection,
    doc_id: str,
    requested_chapter_ids: list[str],
) -> list[str]:
    rows = conn.execute(
        """
        SELECT chapter_id, MIN(order_index) AS first_order
        FROM blocks
        WHERE doc_id = ?
        GROUP BY chapter_id
        ORDER BY first_order
        """,
        (doc_id,),
    ).fetchall()
    available = [str(row["chapter_id"]) for row in rows]
    if not requested_chapter_ids:
        return available

    resolved: list[str] = []
    for requested in requested_chapter_ids:
        value = str(requested)
        matches = [
            chapter_id
            for chapter_id in available
            if chapter_id == value or chapter_id.endswith(f"_{value}")
        ]
        if not matches:
            raise ValueError(f"Chapter not found: {requested}")
        resolved.append(matches[0])
    return resolved


def _row_to_block(row: sqlite3.Row) -> dict[str, Any]:
    style = _json_dict(row["style_json"])
    text = str(row["original_text"] or row["text"] or "")
    return {
        "block_id": str(row["block_id"]),
        "chapter_id": str(row["chapter_id"]),
        "order_index": int(row["order_index"]),
        "block_type": str(row["block_type"] or ""),
        "clean_text": text,
        "source_text": text,
        "translation_mode": str(row["translation_mode"] or ""),
        "style": style,
    }


def _json_dict(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}
