from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from pipeline.memory.store_init import init_db


@dataclass(frozen=True)
class LoadReport:
    doc_id: str
    chapters: int
    blocks: int
    warnings: list[str]

    def to_json_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_document(db_path: str | Path, document_json_path: str | Path) -> LoadReport:
    """Load stripped document.json into documents/blocks.

    Block ids are preserved verbatim for oracle alignment, but `blocks.order_index`
    is a global 0..N-1 counter over the full document, not the per-chapter index in
    document.json. Chapter metadata remains derivable from `blocks.chapter_id` and
    heading/opening blocks; no chapter table is introduced. Extras without dedicated
    schema columns are stored in `blocks.style_json`.
    """

    document_path = Path(document_json_path)
    document = json.loads(document_path.read_text(encoding="utf-8"))
    _validate_stripped_document(document)

    db = Path(db_path)
    if db.exists():
        connection = sqlite3.connect(db)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
    else:
        connection = init_db(db)

    try:
        report = _load_into_connection(connection, document, document_path)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return report


def _load_into_connection(
    connection: sqlite3.Connection,
    document: dict[str, Any],
    document_path: Path,
) -> LoadReport:
    doc_id = str(document["doc_id"])
    metadata = dict(document.get("metadata") or {})
    chapters = list(document.get("chapters") or [])
    flattened_blocks = _flatten_blocks(chapters)
    warnings = _collect_warnings(flattened_blocks)

    connection.execute(
        """
        INSERT INTO documents (
          doc_id, job_id, source_filename, source_lang, target_lang,
          metadata_json, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(doc_id) DO UPDATE SET
          job_id = excluded.job_id,
          source_filename = excluded.source_filename,
          source_lang = excluded.source_lang,
          target_lang = excluded.target_lang,
          metadata_json = excluded.metadata_json,
          updated_at = CURRENT_TIMESTAMP
        """,
        (
            doc_id,
            doc_id,
            document_path.name,
            str(metadata.get("source_language") or metadata.get("source_lang") or "en"),
            str(metadata.get("target_language") or metadata.get("target_lang") or "vi"),
            json.dumps(metadata, ensure_ascii=False, sort_keys=True),
        ),
    )
    connection.execute("DELETE FROM blocks WHERE doc_id = ?", (doc_id,))

    for global_order, (chapter_id, block) in enumerate(flattened_blocks):
        style_json = {
            "is_chapter_opening": bool(block.get("is_chapter_opening")),
            "page_ids": block.get("page_ids") or [],
            "quality_flags": block.get("quality_flags") or [],
        }
        connection.execute(
            """
            INSERT INTO blocks (
              block_id, doc_id, order_index, block_type, chapter_id,
              text, original_text, style_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(block["block_id"]),
                doc_id,
                global_order,
                str(block.get("block_type") or ""),
                chapter_id,
                str(block.get("clean_text") or ""),
                str(block.get("source_text") or ""),
                json.dumps(style_json, ensure_ascii=False, sort_keys=True),
            ),
        )

    return LoadReport(
        doc_id=doc_id,
        chapters=len(chapters),
        blocks=len(flattened_blocks),
        warnings=warnings,
    )


def _validate_stripped_document(document: dict[str, Any]) -> None:
    seen: set[str] = set()
    duplicates: list[str] = []
    for chapter in document.get("chapters") or []:
        for block in chapter.get("blocks") or []:
            block_id = str(block.get("block_id") or "")
            if block_id in seen:
                duplicates.append(block_id)
            seen.add(block_id)
            annotations = block.get("annotations") or {}
            if annotations:
                raise ValueError(
                    f"Block {block_id} still has annotations; run prepare_source first"
                )
    if duplicates:
        raise ValueError(f"Duplicate block_id values: {sorted(set(duplicates))}")


def _flatten_blocks(chapters: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    flattened: list[tuple[str, dict[str, Any]]] = []
    for chapter in chapters:
        chapter_id = str(chapter.get("chapter_id") or "")
        for block in chapter.get("blocks") or []:
            flattened.append((chapter_id, block))
    return flattened


def _collect_warnings(flattened_blocks: list[tuple[str, dict[str, Any]]]) -> list[str]:
    warnings: list[str] = []
    for _chapter_id, block in flattened_blocks:
        block_type = str(block.get("block_type") or "")
        clean_text = str(block.get("clean_text") or "")
        if block_type in {"paragraph", "dialogue"} and not clean_text:
            warnings.append(f"{block.get('block_id')}: empty clean_text for {block_type}")
    return warnings
