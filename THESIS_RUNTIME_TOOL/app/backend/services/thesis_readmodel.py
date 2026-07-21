from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

from config import THESIS_JOBS_ROOT, THESIS_REPORTS_ROOT
from pipeline.eval.surface_match import SurfaceOwner, allocate_spans


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


def _normalized_term(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _parse_context_term_line(line: Any, policy: str) -> tuple[str, str]:
    value = str(line or "").strip()
    if policy == "preserve":
        suffix = " (keep unchanged)"
        source = value[:-len(suffix)] if value.endswith(suffix) else value
        return source.strip(), source.strip()

    source, separator, target = value.partition(" -> ")
    if not separator:
        return value, ""
    if policy == "context_sensitive":
        suffix = " (context-sensitive; do not force)"
        if target.endswith(suffix):
            target = target[:-len(suffix)]
    return source.strip(), target.strip()


def _run_context_memory(
    translation_rows: list[dict[str, Any]],
    memory_pack_rows: list[dict[str, Any]],
    glossary_rows: list[dict[str, Any]],
    block_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Reconstruct only the memory directives actually persisted in selected packs."""

    selected_pack_ids = {
        str(row.get("pack_id") or "")
        for row in translation_rows
        if row.get("pack_id")
    }
    packs_by_id = {
        str(row.get("pack_id") or ""): row
        for row in memory_pack_rows
        if row.get("pack_id")
    }
    source_by_glossary_id = {
        str(row.get("glossary_id") or ""): str(row.get("source_term") or "")
        for row in glossary_rows
        if row.get("glossary_id")
    }

    records: dict[str, dict[str, Any]] = {}
    context_pack_ids: set[str] = set()
    warnings: list[str] = []
    category_policy = {
        "glossary_lines": "mandatory",
        "preserve_lines": "preserve",
        "context_sensitive_lines": "context_sensitive",
    }

    def list_value(value: Any, warning_ref: str) -> list[Any]:
        if value is None:
            return []
        if not isinstance(value, list):
            warnings.append(f"invalid_context_list:{warning_ref}")
            return []
        return value

    for pack_id in sorted(selected_pack_ids):
        pack = packs_by_id.get(pack_id)
        if not pack:
            warnings.append(f"missing_memory_pack:{pack_id}")
            continue
        payload = _json_load(pack.get("payload_json"), {})
        context_pack = payload.get("context_pack") if isinstance(payload, dict) else None
        if not isinstance(context_pack, dict):
            continue

        context_lines = {
            field: list_value(context_pack.get(field), f"{pack_id}:{field}")
            for field in category_policy
        }
        rendered_lines = sum(len(lines) for lines in context_lines.values())
        if not rendered_lines:
            continue
        context_pack_ids.add(pack_id)

        applied_block_ids = [
            str(block_id)
            for block_id in list_value(payload.get("block_ids"), f"{pack_id}:block_ids")
            if str(block_id) in block_by_id
        ]
        anchors = context_pack.get("anchors") or {}
        raw_term_blocks = anchors.get("term_block_ids") or {}
        anchor_blocks_by_source: dict[str, set[str]] = defaultdict(set)
        if isinstance(raw_term_blocks, dict):
            for term_ref, block_ids in raw_term_blocks.items():
                source = source_by_glossary_id.get(str(term_ref), str(term_ref))
                source_key = _normalized_term(source)
                for block_id in list_value(block_ids, f"{pack_id}:term_block_ids:{term_ref}"):
                    if str(block_id) in block_by_id:
                        anchor_blocks_by_source[source_key].add(str(block_id))

        for field, policy in category_policy.items():
            for raw_line in context_lines[field]:
                source, target = _parse_context_term_line(raw_line, policy)
                source_key = _normalized_term(source)
                if not source_key:
                    warnings.append(f"empty_context_term:{pack_id}:{field}")
                    continue
                record = records.setdefault(source_key, {
                    "source_term": source,
                    "directives": {},
                    "source_anchor_block_ids": set(),
                    "usage_block_ids": set(),
                })
                record["source_anchor_block_ids"].update(anchor_blocks_by_source.get(source_key, set()))
                record["usage_block_ids"].update(applied_block_ids)
                directive_key = (policy, target, str(raw_line))
                directive = record["directives"].setdefault(directive_key, {
                    "policy": policy,
                    "target_term": target,
                    "instruction": str(raw_line),
                    "pack_ids": set(),
                    "block_ids": set(),
                })
                directive["pack_ids"].add(pack_id)
                directive["block_ids"].update(applied_block_ids)

    glossary: list[dict[str, Any]] = []
    for source_key in sorted(records):
        record = records[source_key]
        directives = []
        for directive in sorted(
            record["directives"].values(),
            key=lambda row: (row["policy"], row["target_term"], row["instruction"]),
        ):
            directives.append({
                **directive,
                "pack_ids": sorted(directive["pack_ids"]),
                "block_ids": sorted(directive["block_ids"]),
            })
        targets = sorted({row["target_term"] for row in directives if row["target_term"]})
        policies = sorted({row["policy"] for row in directives if row["policy"]})
        if len(targets) > 1:
            status = "target_conflict"
        elif len(policies) > 1:
            status = "mixed_policy"
        else:
            status = policies[0] if policies else "unclassified"

        source_anchor_blocks = sorted(record["source_anchor_block_ids"])
        usage_blocks = sorted(record["usage_block_ids"])
        evidence_blocks = source_anchor_blocks or usage_blocks
        chapter_ids = sorted({
            str(block_by_id[block_id].get("chapter_id") or "")
            for block_id in usage_blocks
            if block_id in block_by_id and block_by_id[block_id].get("chapter_id")
        })
        stable_id = hashlib.sha256(source_key.encode("utf-8")).hexdigest()[:20]
        glossary.append({
            "glossary_id": f"run_context_{stable_id}",
            "term_id": f"run_context_{stable_id}",
            "source_term": record["source_term"],
            "target_term": targets[0] if len(targets) == 1 else "",
            "expected_target": targets[0] if len(targets) == 1 else "",
            "allowed_variants": [],
            "forbidden_variants": [],
            "scope": "selected_run",
            "status": status,
            "injection_policy": policies[0] if len(policies) == 1 else "mixed",
            "directive_count": len(directives),
            "directives": directives,
            "source_anchor_block_ids": source_anchor_blocks,
            "usage_block_ids": usage_blocks,
            "chapter_ids": chapter_ids,
            "occurrences": [
                {
                    "block_id": block_id,
                    "evidence_kind": "source_anchor" if source_anchor_blocks else "context_applied",
                }
                for block_id in evidence_blocks
            ],
            "provenance": {
                "branch": "run_context",
                "source": "memory_packs.context_pack",
                "label": "persisted translator context",
            },
            "read_only": True,
        })

    _localize_run_context_glossary(glossary, block_by_id)

    run_block_ids = sorted({
        str(row.get("block_id") or "")
        for row in translation_rows
        if row.get("block_id") in block_by_id
    })
    run_chapter_ids = sorted({
        str(block_by_id[block_id].get("chapter_id") or "")
        for block_id in run_block_ids
        if block_by_id[block_id].get("chapter_id")
    })
    return {
        "scope": {
            "available": bool(translation_rows),
            "experiment_ids": sorted({str(row.get("experiment_id")) for row in translation_rows if row.get("experiment_id")}),
            "configs": sorted({str(row.get("config")) for row in translation_rows if row.get("config")}),
            "stages": sorted({str(row.get("stage")) for row in translation_rows if row.get("stage")}),
            "translation_rows": len(translation_rows),
            "block_ids": run_block_ids,
            "chapter_ids": run_chapter_ids,
            "referenced_pack_count": len(selected_pack_ids),
            "context_pack_count": len(context_pack_ids),
            "context_term_count": len(glossary),
            "warnings": warnings,
        },
        "glossary_entries": glossary,
        "entities": [],
        "entity_relations": [],
        "summaries": [],
    }


def _localize_run_context_glossary(
    glossary: list[dict[str, Any]],
    block_by_id: dict[str, dict[str, Any]],
) -> None:
    """Attach source spans using only persisted run directives and source blocks."""

    owners_by_block: dict[str, list[SurfaceOwner]] = defaultdict(list)
    candidate_blocks_by_id: dict[str, list[str]] = {}
    for term in glossary:
        term_id = str(term.get("term_id") or term.get("glossary_id") or "")
        source_term = str(term.get("source_term") or "").strip()
        candidate_blocks = list(term.get("source_anchor_block_ids") or term.get("usage_block_ids") or [])
        candidate_blocks = [block_id for block_id in candidate_blocks if block_id in block_by_id]
        candidate_blocks_by_id[term_id] = candidate_blocks
        if not term_id or not source_term:
            continue
        for block_id in candidate_blocks:
            owners_by_block[block_id].append(SurfaceOwner(term_id, source_term, False))

    occurrences_by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    localized_blocks_by_id: dict[str, set[str]] = defaultdict(set)
    for block_id, owners in owners_by_block.items():
        block = block_by_id[block_id]
        text = str(block.get("clean_text") or block.get("source_text") or block.get("text") or "")
        for term_id, spans in allocate_spans(text, owners, language="en").items():
            for span in spans:
                occurrences_by_id[term_id].append({
                    "block_id": block_id,
                    "span": [span.start, span.end],
                    "surface": span.surface,
                    "evidence_kind": "source_anchor",
                })
                localized_blocks_by_id[term_id].add(block_id)

    for term in glossary:
        term_id = str(term.get("term_id") or term.get("glossary_id") or "")
        occurrences = occurrences_by_id[term_id]
        for block_id in candidate_blocks_by_id.get(term_id, []):
            if block_id not in localized_blocks_by_id[term_id]:
                occurrences.append({
                    "block_id": block_id,
                    "evidence_kind": "source_anchor_unlocalized",
                })
        term["occurrences"] = sorted(
            occurrences,
            key=lambda row: (str(row.get("block_id") or ""), list(row.get("span") or [])),
        )


def _experiment_manifest_for_job(job_id: str, reports_root: Path | None = None) -> tuple[str | None, dict[str, Any]]:
    root = (reports_root or THESIS_REPORTS_ROOT).resolve()
    if not root.exists():
        return None, {}
    matches: list[tuple[str, dict[str, Any]]] = []
    for path in sorted(root.glob("*/manifest.json")):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if str(manifest.get("job_id") or "") == job_id:
            matches.append((path.parent.name, manifest))
    if len(matches) != 1:
        return None, {}
    return matches[0]


def list_thesis_datasets(jobs_root: Path | None = None) -> list[dict[str, Any]]:
    root = (jobs_root or THESIS_JOBS_ROOT).resolve()
    if not root.exists():
        return []
    datasets: list[dict[str, Any]] = []
    for db_path in sorted(root.glob("*/memory.sqlite3")):
        job_id = db_path.parent.name
        if job_id.startswith("_") or not JOB_ID_RE.match(job_id):
            continue
        source_manifest = db_path.parent / "source_manifest.json"
        if source_manifest.is_file():
            try:
                source_contract = json.loads(source_manifest.read_text(encoding="utf-8")).get("contract_version")
            except (OSError, json.JSONDecodeError):
                source_contract = None
            if source_contract == "project_runtime_source_v1":
                # This DB is an execution index owned by an imported project. It is
                # reachable through that project and must not appear as a duplicate
                # top-level thesis dataset in the project picker.
                continue
        item = {
            "job_id": job_id,
            "doc_id": f"thesis:{job_id}",
            "status": "available",
            "source": "thesis",
            "db_path": str(db_path),
        }
        experiment_id, experiment_manifest = _experiment_manifest_for_job(job_id, reports_root=THESIS_REPORTS_ROOT)
        if experiment_id:
            reports = experiment_manifest.get("reports") or {}
            item["experiment_id"] = experiment_id
            item["has_score_manifest"] = bool(reports.get("score"))
            item["has_overlay_manifest"] = bool(reports.get("overlay"))
        try:
            with _connect_readonly(db_path) as con:
                doc_rows = _rows(con, "documents")
                doc = _document_from_row(doc_rows[0] if doc_rows else None, job_id)
                translation_rows = _rows(con, "translation_runs")
                run_experiment_ids = sorted({
                    str(row.get("experiment_id") or "")
                    for row in translation_rows
                    if row.get("experiment_id")
                })
                if not item.get("experiment_id") and len(run_experiment_ids) == 1:
                    item["experiment_id"] = run_experiment_ids[0]
                    item["experiment_id_source"] = "translation_runs"
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

        glossary_rows = _rows(con, "glossary_entries", "source_term")
        glossary = [_glossary_to_runtime(row) for row in glossary_rows]
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

        run_memory = _run_context_memory(
            translation_rows,
            _rows(con, "memory_packs", "pack_id"),
            glossary_rows,
            block_by_id,
        )

        counts = {
            "blocks": len(blocks),
            "chapters": len(chapters),
            "runtime_glossary": len(glossary),
            "runtime_entities": len(entities),
            "runtime_relations": len(relations),
            "translation_rows": len(translation_rows),
            "run_context_glossary": len(run_memory["glossary_entries"]),
            "run_scope_blocks": len(run_memory["scope"]["block_ids"]),
            "run_scope_chapters": len(run_memory["scope"]["chapter_ids"]),
            "eval_gold_glossary": len(gold_glossary),
            "eval_references": len(references),
        }

        runtime_memory = {
            "glossary_entries": glossary,
            "entities": entities,
            "entity_relations": relations,
            "summaries": [],
        }
        selected_run_memory = {
            "glossary_entries": run_memory["glossary_entries"],
            "entities": run_memory["entities"],
            "entity_relations": run_memory["entity_relations"],
            "summaries": run_memory["summaries"],
        }
        memory_scope = "selected_run" if experiment_id else "project"
        project_memory = selected_run_memory if experiment_id else runtime_memory

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
                "memory_scope": memory_scope,
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
            "runtime_memory": runtime_memory,
            "project_memory": project_memory,
            "run_memory": run_memory,
            "eval_only": {
                "gold_glossary": gold_glossary,
                "references": references,
            },
            "translations": dict(translations),
        }
