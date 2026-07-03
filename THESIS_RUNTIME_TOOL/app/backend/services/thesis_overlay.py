from __future__ import annotations

import re
import sys
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

TOOL_ROOT = Path(__file__).resolve().parents[3]
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))

from pipeline.eval.surface_match import SurfaceOwner, allocate_spans, find_spans, normalize_surface
from pipeline.eval.cascade_localize import _locate_quote_span_in_region

from config import THESIS_REPORTS_ROOT
from services.thesis_readmodel import (
    ThesisReadModelError,
    _block_to_readmodel,
    _connect_readonly,
    _db_path,
    _entity_to_runtime,
    _glossary_to_runtime,
    _rows,
    _translation_to_readmodel,
)
from services.thesis_scores import load_scores, resolve_experiment_artifact_path, resolve_experiment_id_for_job



def load_registry_overlay(
    job_id: str,
    *,
    experiment_id: str | None = None,
    stage: str | None = None,
    block_id: str | None = None,
    chapter_id: str | None = None,
    cascade_report: str | Path | None = None,
    jobs_root: Path | None = None,
    reports_root: Path | None = None,
    prefer_materialized: bool = True,
) -> dict[str, Any]:
    """Build runtime registry overlay spans.

    This is a read-only composer over A01 DatasetReadModel and D01 ScoreReadModel.
    It never recomputes metrics; score status/forms are copied from reports only.
    """

    effective_experiment_id = experiment_id or resolve_experiment_id_for_job(
        job_id,
        reports_root=reports_root or THESIS_REPORTS_ROOT,
    )

    if prefer_materialized and effective_experiment_id:
        overlay_report = resolve_experiment_artifact_path(
            effective_experiment_id,
            "overlay",
            reports_root=reports_root or THESIS_REPORTS_ROOT,
        )
        if overlay_report:
            return _load_materialized_overlay(overlay_report)

    blocks, glossary, entities = _load_overlay_inputs(
        job_id,
        experiment_id=effective_experiment_id,
        stage=stage,
        block_id=block_id,
        chapter_id=chapter_id,
        jobs_root=jobs_root,
    )
    score_status = "loaded"
    try:
        scores = load_scores(
            job_id,
            experiment_id=effective_experiment_id,
            reports_root=reports_root or THESIS_REPORTS_ROOT,
        )
    except ThesisReadModelError as exc:
        scores = {"drift": []}
        score_status = f"unavailable:{exc.code}"

    source = _build_source_overlay(job_id, blocks, glossary, entities, jobs_root=jobs_root)
    score_index = _score_index(scores.get("drift") or [])
    target = _build_target_overlay(blocks, glossary, entities, score_index, source)
    cascade_status = "not_requested"
    cascade_audit: dict[str, Any] = {}
    if not cascade_report and effective_experiment_id:
        cascade_report = resolve_experiment_artifact_path(
            effective_experiment_id,
            "cascade",
            reports_root=reports_root or THESIS_REPORTS_ROOT,
        )
    if cascade_report:
        cascade_result = _merge_cascade_marks(
            target,
            blocks,
            glossary,
            cascade_report,
        )
        cascade_status = str(cascade_result.get("status") or "unknown")
        cascade_audit = dict(cascade_result.get("audit") or {})

    return {
        "meta": {
            "source": "thesis_registry_overlay",
            "job_id": job_id,
            "read_only": True,
            "score_status": score_status,
            "cascade_status": cascade_status,
            "cascade_audit": cascade_audit,
            "selected": {
                "experiment_id": effective_experiment_id,
                "stage": stage,
                "block_id": block_id,
                "chapter_id": chapter_id,
                "cascade_report": str(cascade_report) if cascade_report else None,
            },
            "note": (
                "Char spans are display-only. Status/forms_used are read from "
                "score reports when per-item detail exists; otherwise spans are neutral."
            ),
        },
        "source": source,
        "target_by_config": target,
    }


def _load_materialized_overlay(path: str | Path) -> dict[str, Any]:
    overlay_path = Path(path)
    if not overlay_path.exists():
        raise ThesisReadModelError(
            "materialized_overlay_missing",
            f"Materialized overlay does not exist: {overlay_path}",
            404,
        )
    try:
        payload = json.loads(overlay_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ThesisReadModelError(
            "materialized_overlay_invalid_json",
            f"Could not read materialized overlay JSON: {type(exc).__name__}",
            500,
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("target_by_config"), dict):
        raise ThesisReadModelError(
            "materialized_overlay_invalid",
            "Materialized overlay is missing target_by_config.",
            500,
        )
    meta = payload.setdefault("meta", {})
    if isinstance(meta, dict):
        meta["materialized_loaded_from"] = str(overlay_path)
    return payload


def _load_overlay_inputs(
    job_id: str,
    *,
    experiment_id: str | None,
    stage: str | None,
    block_id: str | None,
    chapter_id: str | None,
    jobs_root: Path | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    with _connect_readonly(_db_path(job_id, jobs_root)) as con:
        block_rows = _scoped_block_rows(con, block_id=block_id, chapter_id=chapter_id)
        blocks = [_block_to_readmodel(row) for row in block_rows]
        block_by_id = {str(block.get("block_id") or ""): block for block in blocks}

        glossary = [_glossary_to_runtime(row) for row in _rows(con, "glossary_entries", "source_term")]
        entities = [_entity_to_runtime(row) for row in _rows(con, "entities", "entity_id")]

        if block_by_id:
            for row in _scoped_translation_rows(
                con,
                list(block_by_id),
                experiment_id=experiment_id,
                stage=stage,
            ):
                item = _translation_to_readmodel(row)
                key = row.get("config") or row.get("stage") or "translation"
                block = block_by_id.get(str(row.get("block_id") or ""))
                if block is not None:
                    block.setdefault("translations", {})[key] = item

    return blocks, glossary, entities


def _scoped_block_rows(
    con,
    *,
    block_id: str | None,
    chapter_id: str | None,
) -> list[dict[str, Any]]:
    if block_id:
        rows = con.execute(
            "SELECT * FROM blocks WHERE block_id=? ORDER BY order_index",
            (block_id,),
        ).fetchall()
    elif chapter_id:
        rows = con.execute(
            "SELECT * FROM blocks WHERE chapter_id=? ORDER BY order_index",
            (chapter_id,),
        ).fetchall()
    else:
        rows = con.execute("SELECT * FROM blocks ORDER BY order_index").fetchall()
    return [dict(row) for row in rows]


def _scoped_translation_rows(
    con,
    block_ids: list[str],
    *,
    experiment_id: str | None,
    stage: str | None,
) -> list[dict[str, Any]]:
    if not block_ids:
        return []
    placeholders = ",".join("?" for _ in block_ids)
    params: list[Any] = list(block_ids)
    where = [f"block_id IN ({placeholders})"]
    if experiment_id:
        where.append("experiment_id=?")
        params.append(experiment_id)
    if stage:
        where.append("stage=?")
        params.append(stage)
    sql = "SELECT * FROM translation_runs WHERE " + " AND ".join(where)
    sql += " ORDER BY config, stage, window_id, block_id"
    return [dict(row) for row in con.execute(sql, params).fetchall()]


def _build_source_overlay(
    job_id: str,
    blocks: list[dict[str, Any]],
    glossary: list[dict[str, Any]],
    entities: list[dict[str, Any]],
    *,
    jobs_root: Path | None,
) -> dict[str, Any]:
    block_texts = {
        str(block.get("block_id")): str(block.get("clean_text") or block.get("source_text") or "")
        for block in blocks
    }
    block_order = [str(block.get("block_id")) for block in blocks]
    block_chapters = {
        str(block.get("block_id")): str(block.get("chapter_id") or "")
        for block in blocks
    }
    token_index = _build_token_index(block_texts)

    glossary_candidates: list[dict[str, Any]] = []
    owners_by_block: dict[str, list[SurfaceOwner]] = defaultdict(list)
    term_by_id: dict[str, dict[str, Any]] = {}
    for term in glossary:
        term_id = str(term.get("term_id") or term.get("glossary_id") or "")
        source_term = str(term.get("source_term") or "").strip()
        if not term_id or not source_term:
            continue
        term_by_id[term_id] = term
        for block_id in _candidate_block_ids(source_term, block_order, token_index):
            if not _in_scope(term, block_id, block_chapters.get(block_id, "")):
                continue
            owners_by_block[block_id].append(
                SurfaceOwner(term_id, source_term, _case_sensitive(term))
            )
    for block_id in block_order:
        text = block_texts.get(block_id, "")
        allocated = allocate_spans(text, owners_by_block.get(block_id, []), language="en")
        for term_id, spans in allocated.items():
            term = term_by_id.get(term_id) or {}
            source_term = str(term.get("source_term") or "").strip()
            for span in spans:
                glossary_candidates.append({
                    "id": term_id,
                    "block_id": block_id,
                    "span": [span.start, span.end],
                    "surface": span.surface,
                    "source_term": source_term,
                    "provenance": "runtime_memory",
                })

    source_glossary = _group_selected_by_id(glossary_candidates)
    source_entities = _source_entity_mentions(
        job_id,
        entities,
        block_texts,
        jobs_root=jobs_root,
    )
    return {
        "glossary_by_id": source_glossary,
        "entities_by_id": source_entities,
    }


def _source_entity_mentions(
    job_id: str,
    entities: list[dict[str, Any]],
    block_texts: dict[str, str],
    *,
    jobs_root: Path | None,
) -> dict[str, dict[str, Any]]:
    entity_ids = {str(entity.get("entity_id") or "") for entity in entities}
    mentions_by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)

    try:
        with _connect_readonly(_db_path(job_id, jobs_root)) as con:
            for row in _rows(con, "mentions", "block_id, char_start, char_end"):
                entity_id = str(row.get("entity_id") or "")
                block_id = str(row.get("block_id") or "")
                if entity_id not in entity_ids or block_id not in block_texts:
                    continue
                start = int(row.get("char_start") or 0)
                end = int(row.get("char_end") or start)
                if end <= start:
                    continue
                mentions_by_id[entity_id].append({
                    "id": entity_id,
                    "mention_id": row.get("mention_id"),
                    "block_id": block_id,
                    "span": [start, end],
                    "surface": row.get("surface") or block_texts[block_id][start:end],
                    "provenance": "runtime_memory",
                })
    except ThesisReadModelError:
        raise

    if mentions_by_id:
        return {
            entity_id: {
                "mentions": sorted(items, key=lambda item: (item["block_id"], item["span"][0], item["span"][1])),
                "source": "mentions",
            }
            for entity_id, items in sorted(mentions_by_id.items())
        }

    # Fallback for older DBs without a mentions table.
    candidates: list[dict[str, Any]] = []
    for entity in entities:
        entity_id = str(entity.get("entity_id") or "")
        surfaces = _entity_source_surfaces(entity)
        for surface in surfaces:
            for block_id, text in block_texts.items():
                for start, end, matched in _find_matches(text, surface, language="en"):
                    candidates.append({
                        "id": entity_id,
                        "block_id": block_id,
                        "span": [start, end],
                        "surface": matched,
                        "provenance": "runtime_memory",
                    })
    grouped = _group_selected_by_id(candidates)
    return {
        entity_id: {"mentions": value["occurrences"], "source": "surface_scan"}
        for entity_id, value in grouped.items()
    }


def _build_target_overlay(
    blocks: list[dict[str, Any]],
    glossary: list[dict[str, Any]],
    entities: list[dict[str, Any]],
    score_index: dict[str, Any],
    source_overlay: dict[str, Any],
) -> dict[str, Any]:
    source_glossary_blocks = _source_block_map(source_overlay, "glossary_by_id")
    source_entity_blocks = _source_block_map(source_overlay, "entities_by_id")
    configs = sorted({
        str(config)
        for block in blocks
        for config in (block.get("translations") or {})
    })
    result: dict[str, Any] = {}
    for config in configs:
        glossary_candidates: list[dict[str, Any]] = []
        entity_candidates: list[dict[str, Any]] = []
        for block in blocks:
            block_id = str(block.get("block_id") or "")
            row = (block.get("translations") or {}).get(config) or {}
            target_text = str(row.get("target_text") or row.get("output_text") or "")
            if not block_id or not target_text:
                continue

            glossary_owners: list[SurfaceOwner] = []
            glossary_owner_details: dict[str, dict[str, Any]] = {}
            for term in glossary:
                term_id = str(term.get("term_id") or term.get("glossary_id") or "")
                if block_id not in source_glossary_blocks.get(term_id, set()):
                    continue
                detail = _lookup_glossary_detail(score_index, config, term)
                forms, forms_source, scored = _target_forms_for_term(term, detail)
                for form in forms:
                    owner_id = f"glossary:{term_id}\u241f{form}"
                    glossary_owners.append(SurfaceOwner(owner_id, form, _case_sensitive(term)))
                    glossary_owner_details[owner_id] = {
                        "term_id": term_id,
                        "form": form,
                        "detail": detail,
                        "forms_source": forms_source,
                        "scored": scored,
                    }
            for owner_id, spans in allocate_spans(target_text, glossary_owners, language="vi").items():
                owner = glossary_owner_details[owner_id]
                for span in spans:
                    glossary_candidates.append(_target_candidate(
                        item_id=owner["term_id"],
                        block_id=block_id,
                        config=config,
                        start=span.start,
                        end=span.end,
                        surface=span.surface,
                        matched_form=owner["form"],
                        detail=owner["detail"],
                        forms_source=owner["forms_source"],
                        scored=owner["scored"],
                        kind="glossary",
                    ))

            entity_owners: list[SurfaceOwner] = []
            entity_owner_details: dict[str, dict[str, Any]] = {}
            for entity in entities:
                entity_id = str(entity.get("entity_id") or "")
                if block_id not in source_entity_blocks.get(entity_id, set()):
                    continue
                detail = _lookup_entity_detail(score_index, config, entity)
                forms, forms_source, scored = _target_forms_for_entity(entity, detail)
                for form in forms:
                    owner_id = f"entity:{entity_id}\u241f{form}"
                    entity_owners.append(SurfaceOwner(owner_id, form, False))
                    entity_owner_details[owner_id] = {
                        "entity_id": entity_id,
                        "form": form,
                        "detail": detail,
                        "forms_source": forms_source,
                        "scored": scored,
                    }
            for owner_id, spans in allocate_spans(target_text, entity_owners, language="vi").items():
                owner = entity_owner_details[owner_id]
                for span in spans:
                    entity_candidates.append(_target_candidate(
                        item_id=owner["entity_id"],
                        block_id=block_id,
                        config=config,
                        start=span.start,
                        end=span.end,
                        surface=span.surface,
                        matched_form=owner["form"],
                        detail=owner["detail"],
                        forms_source=owner["forms_source"],
                        scored=owner["scored"],
                        kind="entity",
                    ))

        result[config] = {
            "glossary_by_id": _group_selected_by_id(glossary_candidates),
            "entities_by_id": {
                item_id: {"mentions": value["occurrences"], "source": "target_scan"}
                for item_id, value in _group_selected_by_id(entity_candidates).items()
            },
        }
    return result


def _source_block_map(source_overlay: dict[str, Any], bucket_name: str) -> dict[str, set[str]]:
    return {
        item_id: _source_blocks_for(source_overlay, bucket_name, item_id)
        for item_id in ((source_overlay or {}).get(bucket_name) or {})
    }


def _source_blocks_for(source_overlay: dict[str, Any], bucket_name: str, item_id: str) -> set[str]:
    bucket = (source_overlay or {}).get(bucket_name) or {}
    row = bucket.get(str(item_id)) or {}
    spans = row.get("occurrences") or row.get("mentions") or []
    return {str(item.get("block_id") or "") for item in spans if item.get("block_id")}


def _target_candidate(
    *,
    item_id: str,
    block_id: str,
    config: str,
    start: int,
    end: int,
    surface: str,
    matched_form: str,
    detail: dict[str, Any] | None,
    forms_source: str,
    scored: bool,
    kind: str,
) -> dict[str, Any]:
    forms_used = dict((detail or {}).get("forms_used") or {})
    raw_status = (detail or {}).get("status") or "unscored"
    constraint_strength = (detail or {}).get("constraint_strength")
    return {
        "id": item_id,
        "block_id": block_id,
        "config": config,
        "span": [start, end],
        "surface": surface,
        "matched_form": matched_form,
        "status": raw_status,
        "display_status": _display_status(raw_status, constraint_strength),
        "constraint_strength": constraint_strength,
        "forms_used": forms_used,
        "forms_source": forms_source,
        "scored": bool(scored),
        "kind": kind,
        "mark_source": "surface_form",
        "located_by": "block_detect",
        "provenance": "translation_runs+score_report" if scored else "translation_runs+runtime_memory",
    }


def _merge_cascade_marks(
    target_by_config: dict[str, Any],
    blocks: list[dict[str, Any]],
    glossary: list[dict[str, Any]],
    cascade_report: str | Path,
) -> dict[str, Any]:
    path = Path(cascade_report)
    if not path.exists():
        return {"status": "unavailable:not_found", "audit": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"status": f"unavailable:invalid_json:{type(exc).__name__}", "audit": {}}

    block_targets = _translated_text_by_config_block(blocks)
    glossary_lookup = _glossary_lookup_by_source(glossary)
    cascade_marks: list[dict[str, Any]] = []
    occ_sources: dict[str, str] = {}
    audit = {
        "loaded": 0,
        "skipped": 0,
        "skipped_by_reason": {},
        "deduped_surface_form": 0,
        "cross_term_overlaps": 0,
        "by_mark_source": {},
        "by_located_by": {},
        "notes": {
            "gpt_fallback": "Counts rendered overlay marks only; fallback calls that concluded not_rendered have no span to display.",
        },
    }

    for decision in _cascade_decisions(payload):
        mark = _cascade_mark_from_decision(decision, block_targets, glossary_lookup)
        if mark is None:
            _increment_cascade_skip(audit, str(decision.get("_cascade_skip_reason") or "unlocatable"))
            continue
        occ_id = str(mark.get("occ_id") or "")
        mark_source = str(mark.get("mark_source") or "")
        if occ_id:
            previous = occ_sources.get(occ_id)
            if previous and previous != mark_source:
                raise ThesisReadModelError(
                    "cascade_tier_collision",
                    f"Cascade report contains conflicting tiers for occurrence {occ_id}.",
                    500,
                )
            occ_sources[occ_id] = mark_source
        cascade_marks.append(mark)
        audit["loaded"] += 1
        audit["by_mark_source"][mark_source] = audit["by_mark_source"].get(mark_source, 0) + 1
        located_by = str(mark.get("located_by") or "")
        audit["by_located_by"][located_by] = audit["by_located_by"].get(located_by, 0) + 1

    _flag_cross_term_cascade_overlaps(cascade_marks, audit)

    added = 0
    for mark in cascade_marks:
        config = str(mark.get("config") or "")
        term_id = str(mark.get("id") or "")
        if not config or not term_id:
            _increment_cascade_skip(audit, "missing_mark_key")
            continue
        config_bucket = target_by_config.setdefault(config, {"glossary_by_id": {}, "entities_by_id": {}})
        glossary_bucket = config_bucket.setdefault("glossary_by_id", {})
        row = glossary_bucket.setdefault(term_id, {"occurrences": [], "source": "cascade_overlay"})
        occurrences = list(row.setdefault("occurrences", []))
        kept: list[dict[str, Any]] = []
        for existing in occurrences:
            if _cascade_replaces_surface_form(existing, mark, term_id):
                audit["deduped_surface_form"] += 1
                continue
            kept.append(existing)
        kept.append(mark)
        row["occurrences"] = kept
        row["occurrences"] = sorted(
            row["occurrences"],
            key=lambda item: (str(item.get("block_id") or ""), int((item.get("span") or [0, 0])[0]), int((item.get("span") or [0, 0])[1])),
        )
        added += 1
    return {"status": f"loaded:{added}:skipped:{audit['skipped']}", "audit": audit}


def _increment_cascade_skip(audit: dict[str, Any], reason: str) -> None:
    reason = reason or "unknown"
    audit["skipped"] += 1
    skipped_by_reason = audit.setdefault("skipped_by_reason", {})
    skipped_by_reason[reason] = skipped_by_reason.get(reason, 0) + 1


def _cascade_replaces_surface_form(existing: dict[str, Any], mark: dict[str, Any], bucket_term_id: str) -> bool:
    if existing.get("mark_source") != "surface_form":
        return False
    existing_id = str(existing.get("id") or bucket_term_id or "")
    if existing_id != str(mark.get("id") or ""):
        return False
    if str(existing.get("block_id") or "") != str(mark.get("block_id") or ""):
        return False
    if str(existing.get("config") or "") != str(mark.get("config") or ""):
        return False
    return _spans_overlap(existing.get("span"), mark.get("span"))


def _spans_overlap(left: Any, right: Any) -> bool:
    try:
        a0, a1 = int(left[0]), int(left[1])
        b0, b1 = int(right[0]), int(right[1])
    except Exception:
        return False
    return a0 < b1 and b0 < a1


def _locate_markdown_clean_quote(target_text: str, quote: str) -> tuple[int, int] | None:
    if not target_text or not quote:
        return None
    clean_chars: list[str] = []
    index_map: list[int] = []
    for idx, char in enumerate(target_text):
        if char in {"*", "_", "`"}:
            continue
        clean_chars.append(char)
        index_map.append(idx)
    clean_text = "".join(clean_chars)
    start = clean_text.find(quote)
    if start < 0:
        return None
    end = start + len(quote)
    if end <= start or end > len(index_map):
        return None
    raw_start = index_map[start]
    raw_end = index_map[end - 1] + 1
    while raw_start > 0 and target_text[raw_start - 1] in {"*", "_", "`"}:
        raw_start -= 1
    while raw_end < len(target_text) and target_text[raw_end] in {"*", "_", "`"}:
        raw_end += 1
    return raw_start, raw_end


def _flag_cross_term_cascade_overlaps(marks: list[dict[str, Any]], audit: dict[str, Any]) -> None:
    by_block: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for mark in marks:
        by_block[(str(mark.get("config") or ""), str(mark.get("block_id") or ""))].append(mark)
    for rows in by_block.values():
        rows = sorted(rows, key=lambda item: (int((item.get("span") or [0, 0])[0]), int((item.get("span") or [0, 0])[1])))
        for idx, left in enumerate(rows):
            for right in rows[idx + 1:]:
                if int((right.get("span") or [0, 0])[0]) >= int((left.get("span") or [0, 0])[1]):
                    break
                if str(left.get("id") or "") == str(right.get("id") or ""):
                    continue
                if _spans_overlap(left.get("span"), right.get("span")):
                    left["cross_term_overlap"] = True
                    right["cross_term_overlap"] = True
                    audit["cross_term_overlaps"] += 1


def _cascade_decisions(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        result: list[dict[str, Any]] = []
        if isinstance(payload.get("decisions"), list):
            result.extend(item for item in payload["decisions"] if isinstance(item, dict))
        reports = payload.get("reports")
        if isinstance(reports, dict):
            for report in reports.values():
                if isinstance(report, dict) and isinstance(report.get("decisions"), list):
                    result.extend(item for item in report["decisions"] if isinstance(item, dict))
        return result
    return []


def _cascade_mark_from_decision(
    decision: dict[str, Any],
    block_targets: dict[tuple[str, str], str],
    glossary_lookup: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    config = str(decision.get("config") or "")
    block_id = str(decision.get("block_id") or "")
    source_term = str(decision.get("source_term") or "")
    if not config or not block_id or not source_term:
        return _skip_cascade_decision(decision, "missing_required_field")
    target_text = block_targets.get((config, block_id), "")
    if not target_text:
        return _skip_cascade_decision(decision, "missing_target_text")

    span: tuple[int, int] | None = None
    mark_source = ""
    located_by = ""
    resolved_by = str(decision.get("resolved_by") or "")
    is_t3 = resolved_by.startswith("t3") or bool(decision.get("target_quote"))
    is_fallback = bool(decision.get("t3_fallback_cache_key") or decision.get("t3_fallback_usage"))
    if is_t3:
        if _has_int_span(decision.get("target_start"), decision.get("target_end")):
            start = int(decision["target_start"])
            end = int(decision["target_end"])
            if 0 <= start < end <= len(target_text):
                span = (start, end)
        else:
            quote = str(decision.get("target_quote_clean") or decision.get("target_quote") or "").strip()
            left_context = str(decision.get("left_context") or "")
            if quote:
                located = _locate_quote_span_in_region(target_text, quote, left_context)
                if located[0] is not None and located[1] is not None:
                    span = (int(located[0]), int(located[1]))
                else:
                    fallback = _locate_markdown_clean_quote(target_text, quote)
                    if fallback is not None:
                        span = fallback
                        decision["clean_text_fallback"] = True
        if span is not None:
            mark_source = "cascade_t3_llm"
            located_by = "ai_locate_fallback" if is_fallback else "ai_locate_local"
    elif _has_int_span(decision.get("target_start"), decision.get("target_end")):
        start = int(decision["target_start"])
        end = int(decision["target_end"])
        if 0 <= start < end <= len(target_text):
            span = (start, end)
            mark_source = "cascade_t2"
            located_by = "code_exact"
    if span is None or not mark_source:
        reason = "not_rendered" if str(decision.get("decision") or "") == "not_rendered" else "unlocatable"
        return _skip_cascade_decision(decision, reason)

    term = glossary_lookup.get(_norm_key(source_term)) or {}
    term_id = str(term.get("term_id") or term.get("glossary_id") or decision.get("term_id") or source_term)
    surface = target_text[span[0]:span[1]]
    return {
        "id": term_id,
        "block_id": block_id,
        "config": config,
        "span": [span[0], span[1]],
        "surface": surface,
        "matched_form": str(decision.get("matched_form_rank") or decision.get("accepted_form") or ""),
        "status": str(decision.get("decision") or "localized"),
        "display_status": "cascade",
        "constraint_strength": None,
        "forms_used": {},
        "forms_source": "cascade_report",
        "scored": False,
        "kind": "glossary",
        "mark_source": mark_source,
        "located_by": located_by,
        "provenance": "cascade_report",
        "source_term": source_term,
        "occ_id": decision.get("occ_id"),
        "masquerade_suspect": bool(decision.get("masquerade_suspect")),
        "clean_text_fallback": bool(decision.get("clean_text_fallback")),
        "gpt_fallback": bool(decision.get("t3_fallback_cache_key") or decision.get("t3_fallback_usage")),
    }


def _skip_cascade_decision(decision: dict[str, Any], reason: str) -> None:
    decision["_cascade_skip_reason"] = reason
    return None


def _has_int_span(start: Any, end: Any) -> bool:
    try:
        return start is not None and end is not None and int(end) > int(start)
    except (TypeError, ValueError):
        return False


def _translated_text_by_config_block(blocks: list[dict[str, Any]]) -> dict[tuple[str, str], str]:
    result: dict[tuple[str, str], str] = {}
    for block in blocks:
        block_id = str(block.get("block_id") or "")
        for config, row in (block.get("translations") or {}).items():
            target_text = str((row or {}).get("target_text") or (row or {}).get("output_text") or "")
            if block_id and target_text:
                result[(str(config), block_id)] = target_text
    return result


def _glossary_lookup_by_source(glossary: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for term in glossary:
        source = _norm_key(term.get("source_term") or "")
        if source and source not in result:
            result[source] = term
    return result


def _score_index(drift: list[dict[str, Any]]) -> dict[str, Any]:
    glossary: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    entities: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for item in drift:
        config = str(item.get("config") or "")
        if not config:
            continue
        category = str(item.get("drift_category") or "")
        if category == "entity-name" or item.get("target_term_kind") == "entity_id":
            entity_id = str(item.get("target_term") or "")
            if entity_id:
                entities[config][entity_id] = item
            source_key = _norm_key(item.get("source_term") or "")
            if source_key:
                entities[config][source_key] = item
        else:
            source_key = _norm_key(item.get("source_term") or "")
            if source_key:
                glossary[config][source_key] = item
    return {"glossary": glossary, "entities": entities}


def _lookup_glossary_detail(score_index: dict[str, Any], config: str, term: dict[str, Any]) -> dict[str, Any] | None:
    source_key = _norm_key(term.get("source_term") or "")
    return (score_index.get("glossary") or {}).get(config, {}).get(source_key)


def _lookup_entity_detail(score_index: dict[str, Any], config: str, entity: dict[str, Any]) -> dict[str, Any] | None:
    lookup = (score_index.get("entities") or {}).get(config, {})
    entity_id = str(entity.get("entity_id") or "")
    if entity_id in lookup:
        return lookup[entity_id]
    return lookup.get(_norm_key(entity.get("canonical_source") or ""))


def _target_forms_for_term(term: dict[str, Any], detail: dict[str, Any] | None) -> tuple[list[str], str, bool]:
    if detail is not None:
        return _dedupe_forms((detail.get("forms_used") or {}).keys()), "score_report.forms_used", True
    return _dedupe_forms([
        term.get("target_term"),
        term.get("expected_target"),
        *(term.get("allowed_variants") or []),
    ]), "runtime_memory.fallback", False


def _target_forms_for_entity(entity: dict[str, Any], detail: dict[str, Any] | None) -> tuple[list[str], str, bool]:
    if detail is not None:
        return _dedupe_forms((detail.get("forms_used") or {}).keys()), "score_report.forms_used", True
    return _dedupe_forms([
        entity.get("canonical_target"),
        *(entity.get("aliases_target") or []),
        *(entity.get("preferred_vietnamese_forms") or []),
    ]), "runtime_memory.fallback", False


def _entity_source_surfaces(entity: dict[str, Any]) -> list[str]:
    return _dedupe_forms([
        entity.get("canonical_source"),
        *(entity.get("aliases_source") or []),
    ])


def _build_token_index(block_texts: dict[str, str]) -> dict[str, set[str]]:
    index: dict[str, set[str]] = defaultdict(set)
    for block_id, text in block_texts.items():
        for token in set(_tokens(text)):
            index[token].add(block_id)
    return index


def _candidate_block_ids(
    needle: str,
    block_order: list[str],
    token_index: dict[str, set[str]],
) -> list[str]:
    tokens = _tokens(needle)
    if not tokens:
        return block_order
    candidate_sets = [token_index.get(token, set()) for token in tokens]
    if not candidate_sets or any(not items for items in candidate_sets):
        return []
    candidates = set.intersection(*candidate_sets)
    return [block_id for block_id in block_order if block_id in candidates]


def _tokens(text: str) -> list[str]:
    normalized = _norm_key(text)
    return re.findall(r"\w+", normalized, flags=re.UNICODE)


def _group_selected_by_id(candidates: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    selected = _select_non_overlapping(candidates)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in selected:
        item_id = str(item.get("id") or "")
        if item_id:
            grouped[item_id].append(_public_span(item))
    return {
        item_id: {
            "occurrences": sorted(items, key=lambda item: (item["block_id"], item["span"][0], item["span"][1])),
            "source": "overlay_scan",
        }
        for item_id, items in sorted(grouped.items())
    }


def _public_span(item: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in item.items()
        if key not in {"id"}
    }


def _select_non_overlapping(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_block: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in candidates:
        by_block[(str(item.get("config") or ""), str(item.get("block_id") or ""))].append(item)

    selected: list[dict[str, Any]] = []
    for items in by_block.values():
        cur = -1
        for item in sorted(
            items,
            key=lambda value: (
                int((value.get("span") or [0, 0])[0]),
                -(int((value.get("span") or [0, 0])[1]) - int((value.get("span") or [0, 0])[0])),
                str(value.get("id") or ""),
            ),
        ):
            start, end = item.get("span") or [0, 0]
            if int(start) < cur:
                continue
            selected.append(item)
            cur = int(end)
    return selected


def _in_scope(term: dict[str, Any], block_id: str, chapter_id: str) -> bool:
    term_chapter = str(term.get("chapter_id") or "").strip()
    if term_chapter and term_chapter != chapter_id:
        return False
    evidence = [str(item) for item in term.get("evidence_span_ids") or []]
    block_scope = [item for item in evidence if item == block_id]
    if block_scope:
        return True
    return True


def _find_matches(text: str, needle: str, *, language: str = "en") -> list[tuple[int, int, str]]:
    return find_spans(text, needle, language=language)


def _display_status(status: str, constraint_strength: str | None) -> str:
    if status == "drift" and constraint_strength not in {None, "", "hard"}:
        return "diagnostic"
    return status


def _norm_key(text: str) -> str:
    return normalize_surface(text).casefold().strip()


def _case_sensitive(row: dict[str, Any]) -> bool:
    return bool(int(row.get("case_sensitive") or 0))


def _dedupe_forms(values: Any) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        text = str(value or "").strip()
        key = _norm_key(text)
        if not text or key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result
