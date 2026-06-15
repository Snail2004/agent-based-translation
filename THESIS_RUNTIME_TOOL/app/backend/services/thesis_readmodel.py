from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

from config import THESIS_JOBS_ROOT


JOB_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class ThesisReadModelError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _safe_job_id(job_id: str) -> str:
    value = (job_id or "").strip()
    if not value or not JOB_ID_RE.match(value):
        raise ThesisReadModelError("invalid_job_id", "Invalid thesis job id.", 400)
    return value


def _db_path(job_id: str, jobs_root: Path | None = None) -> Path:
    safe_job_id = _safe_job_id(job_id)
    root = (jobs_root or THESIS_JOBS_ROOT).resolve()
    path = (root / safe_job_id / "memory.sqlite3").resolve()
    if root not in path.parents:
        raise ThesisReadModelError("invalid_job_path", "Resolved DB path escapes jobs root.", 400)
    if not path.exists():
        raise ThesisReadModelError("job_not_found", f"memory.sqlite3 not found for job {safe_job_id}.", 404)
    return path


def _connect_readonly(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _table_exists(con: sqlite3.Connection, table: str) -> bool:
    row = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(con, table):
        return set()
    return {row[1] for row in con.execute(f"PRAGMA table_info({table})").fetchall()}


def _rows(con: sqlite3.Connection, table: str, order_by: str | None = None) -> list[dict[str, Any]]:
    if not _table_exists(con, table):
        return []
    sql = f"SELECT * FROM {table}"
    if order_by:
        cols = _columns(con, table)
        safe_parts = [part.strip() for part in order_by.split(",") if part.strip().split()[0] in cols]
        if safe_parts:
            sql += " ORDER BY " + ", ".join(safe_parts)
    return [dict(row) for row in con.execute(sql).fetchall()]


def _count(con: sqlite3.Connection, table: str) -> int:
    if not _table_exists(con, table):
        return 0
    return int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _json_load(value: Any, default: Any):
    if value in (None, ""):
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _first_heading(blocks: list[dict[str, Any]], chapter_id: str) -> str:
    for block in blocks:
        if block.get("chapter_id") != chapter_id:
            continue
        text = block.get("text") or block.get("original_text") or ""
        if block.get("block_type") == "heading" or text.lstrip().startswith("#"):
            return text.lstrip("#").strip() or chapter_id
    return chapter_id


def _document_from_row(row: dict[str, Any] | None, job_id: str) -> dict[str, Any]:
    row = row or {}
    metadata = _json_load(row.get("metadata_json"), {})
    title = metadata.get("title") or metadata.get("source_title") or row.get("source_filename") or job_id
    return {
        "doc_id": row.get("doc_id") or job_id,
        "job_id": row.get("job_id") or job_id,
        "title": title,
        "source_filename": row.get("source_filename"),
        "source_lang": row.get("source_lang"),
        "target_lang": row.get("target_lang"),
        "metadata": metadata,
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _block_to_readmodel(row: dict[str, Any]) -> dict[str, Any]:
    source_text = row.get("original_text") or row.get("text") or ""
    return {
        **row,
        "clean_text": row.get("text") or source_text,
        "source_text": source_text,
        "bbox": _json_load(row.get("bbox_json"), None),
        "style": _json_load(row.get("style_json"), None),
        "translations": {},
        "provenance": {"branch": "source", "source": "blocks"},
        "read_only": True,
    }


def _glossary_to_runtime(row: dict[str, Any]) -> dict[str, Any]:
    return {
        **row,
        "term_id": row.get("glossary_id"),
        "expected_target": row.get("target_term") or "",
        "allowed_variants": _json_load(row.get("allowed_variants_json"), []),
        "forbidden_variants": _json_load(row.get("forbidden_variants_json"), []),
        "examples": _json_load(row.get("examples_json"), []),
        "evidence_span_ids": _json_load(row.get("evidence_span_ids_json"), []),
        "occurrences": [],
        "chapter_scope": row.get("chapter_id") or row.get("scope") or "document",
        "provenance": {"branch": "runtime_memory", "source": "glossary_entries", "label": "agent-built"},
        "read_only": True,
    }


def _entity_to_runtime(row: dict[str, Any]) -> dict[str, Any]:
    preferred_forms = _json_load(row.get("preferred_vietnamese_forms_json"), [])
    return {
        **row,
        "aliases_source": _json_load(row.get("aliases_source_json"), []),
        "aliases_target": _json_load(row.get("aliases_target_json"), []),
        "source_pronouns": _json_load(row.get("source_pronouns_json"), []),
        "preferred_vietnamese_forms": preferred_forms,
        "pronoun_policy": ", ".join(preferred_forms),
        "relations": _json_load(row.get("relations_json"), []),
        "evidence_span_ids": _json_load(row.get("evidence_span_ids_json"), []),
        "supersedes": _json_load(row.get("supersedes_json"), []),
        "conflicts_with": _json_load(row.get("conflicts_with_json"), []),
        "mentions": [],
        "provenance": {"branch": "runtime_memory", "source": "entities", "label": "agent-built"},
        "read_only": True,
    }


def _relation_to_runtime(row: dict[str, Any]) -> dict[str, Any]:
    return {
        **row,
        "address_policy": _json_load(row.get("address_policy_json"), {}),
        "evidence": _json_load(row.get("evidence_json"), []),
        "provenance": {"branch": "runtime_memory", "source": "entity_relations", "label": "agent-built"},
        "read_only": True,
    }


def _gold_to_eval(row: dict[str, Any]) -> dict[str, Any]:
    return {
        **row,
        "provenance": {
            "branch": "eval_only",
            "source": "eval_glossary_gold",
            "label": "gold eval-only",
            "injectable": False,
        },
        "read_only": True,
    }


def _reference_to_eval(row: dict[str, Any]) -> dict[str, Any]:
    return {
        **row,
        "reference_vi": row.get("target_text") or "",
        "provenance": {
            "branch": "eval_only",
            "source": "reference_eval_only",
            "label": row.get("provenance") or "reference eval-only",
            "injectable": False,
        },
        "read_only": True,
    }


def _translation_to_readmodel(row: dict[str, Any]) -> dict[str, Any]:
    return {
        **row,
        "target_text": row.get("output_text") or "",
        "provenance": {
            "branch": "translations",
            "source": "translation_runs",
            "label": row.get("config") or row.get("stage") or "translation",
        },
        "read_only": True,
    }


def _available_runs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[Any, Any, Any, Any, Any, Any], dict[str, Any]] = {}
    for row in rows:
        key = (
            row.get("experiment_id"),
            row.get("config"),
            row.get("stage"),
            row.get("prompt_version"),
            row.get("model"),
            row.get("seed"),
        )
        item = grouped.setdefault(key, {
            "experiment_id": row.get("experiment_id"),
            "config": row.get("config"),
            "stage": row.get("stage"),
            "prompt_version": row.get("prompt_version"),
            "model": row.get("model"),
            "seed": row.get("seed"),
            "block_count": 0,
            "window_count": 0,
            "latest_created_at": row.get("created_at"),
            "_windows": set(),
        })
        item["block_count"] += 1
        item["_windows"].add(row.get("window_id"))
        created = row.get("created_at")
        if created and (not item.get("latest_created_at") or created > item["latest_created_at"]):
            item["latest_created_at"] = created
    result = []
    for item in grouped.values():
        windows = {value for value in item.pop("_windows", set()) if value}
        item["window_count"] = len(windows)
        result.append(item)
    return sorted(result, key=lambda r: (str(r.get("experiment_id") or ""), str(r.get("config") or ""), str(r.get("stage") or "")))


def list_thesis_datasets(jobs_root: Path | None = None) -> list[dict[str, Any]]:
    root = (jobs_root or THESIS_JOBS_ROOT).resolve()
    if not root.exists():
        return []
    datasets: list[dict[str, Any]] = []
    for db_path in sorted(root.glob("*/memory.sqlite3")):
        job_id = db_path.parent.name
        if job_id.startswith("_") or not JOB_ID_RE.match(job_id):
            continue
        item = {
            "job_id": job_id,
            "doc_id": f"thesis:{job_id}",
            "status": "available",
            "source": "thesis",
            "db_path": str(db_path),
        }
        try:
            with _connect_readonly(db_path) as con:
                doc_rows = _rows(con, "documents")
                doc = _document_from_row(doc_rows[0] if doc_rows else None, job_id)
                item.update({
                    "title": doc.get("title"),
                    "document_doc_id": doc.get("doc_id"),
                    "counts": {
                        table: _count(con, table)
                        for table in ("blocks", "glossary_entries", "entities", "entity_relations", "translation_runs", "eval_glossary_gold", "reference_eval_only")
                    },
                })
        except sqlite3.Error as exc:
            item["status"] = "error"
            item["error"] = str(exc)
        datasets.append(item)
    return datasets


def load_thesis_dataset(
    job_id: str,
    experiment_id: str | None = None,
    stage: str | None = None,
    jobs_root: Path | None = None,
) -> dict[str, Any]:
    path = _db_path(job_id, jobs_root)
    with _connect_readonly(path) as con:
        doc_rows = _rows(con, "documents")
        document = _document_from_row(doc_rows[0] if doc_rows else None, job_id)
        blocks = [_block_to_readmodel(row) for row in _rows(con, "blocks", "order_index, block_id")]

        chapter_ids = []
        for block in blocks:
            chapter_id = block.get("chapter_id") or ""
            if chapter_id and chapter_id not in chapter_ids:
                chapter_ids.append(chapter_id)
        chapters = [
            {
                "chapter_id": chapter_id,
                "title": _first_heading(blocks, chapter_id),
                "order_index": index,
                "block_count": sum(1 for block in blocks if block.get("chapter_id") == chapter_id),
                "read_only": True,
            }
            for index, chapter_id in enumerate(chapter_ids)
        ]

        glossary = [_glossary_to_runtime(row) for row in _rows(con, "glossary_entries", "source_term")]
        entities = [_entity_to_runtime(row) for row in _rows(con, "entities", "entity_id")]
        relations = [_relation_to_runtime(row) for row in _rows(con, "entity_relations", "relation_id")]
        gold_glossary = [_gold_to_eval(row) for row in _rows(con, "eval_glossary_gold", "source_term")]
        references = [_reference_to_eval(row) for row in _rows(con, "reference_eval_only", "block_id")]

        all_translation_rows = _rows(con, "translation_runs", "config, stage, window_id, block_id")
        translation_rows = [
            row for row in all_translation_rows
            if (not experiment_id or row.get("experiment_id") == experiment_id)
            and (not stage or row.get("stage") == stage)
        ]
        translations: dict[str, list[dict[str, Any]]] = defaultdict(list)
        block_by_id = {block["block_id"]: block for block in blocks}
        for row in translation_rows:
            item = _translation_to_readmodel(row)
            key = row.get("config") or row.get("stage") or "translation"
            translations[key].append(item)
            block = block_by_id.get(row.get("block_id"))
            if block is not None:
                block.setdefault("translations", {})[key] = item

        counts = {
            "blocks": len(blocks),
            "chapters": len(chapters),
            "runtime_glossary": len(glossary),
            "runtime_entities": len(entities),
            "runtime_relations": len(relations),
            "translation_rows": len(translation_rows),
            "eval_gold_glossary": len(gold_glossary),
            "eval_references": len(references),
        }

        return {
            "meta": {
                "source": "thesis_sqlite_readmodel",
                "job_id": job_id,
                "db_path": str(path),
                "read_only": True,
                "document": document,
                "selected": {
                    "experiment_id": experiment_id,
                    "stage": stage,
                },
                "available_runs": _available_runs(all_translation_rows),
                "counts": counts,
                "provenance": {
                    "runtime_memory": "agent-built from pipeline SQLite tables",
                    "eval_only": "gold/reference eval-only; never injectable",
                    "translations": "translation_runs rows keyed by config",
                },
            },
            "document": document,
            "chapters": chapters,
            "blocks": blocks,
            "runtime_memory": {
                "glossary_entries": glossary,
                "entities": entities,
                "entity_relations": relations,
            },
            "eval_only": {
                "gold_glossary": gold_glossary,
                "references": references,
            },
            "translations": dict(translations),
        }
