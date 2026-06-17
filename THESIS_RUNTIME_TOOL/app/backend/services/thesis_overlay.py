from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any

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
from services.thesis_scores import load_scores


APOSTROPHE_TRANSLATION = str.maketrans({
    "\u2018": "'",
    "\u2019": "'",
    "\u201b": "'",
    "\u2032": "'",
    "`": "'",
    "\u00b4": "'",
})


def load_registry_overlay(
    job_id: str,
    *,
    experiment_id: str | None = None,
    stage: str | None = None,
    block_id: str | None = None,
    chapter_id: str | None = None,
    jobs_root: Path | None = None,
    reports_root: Path | None = None,
) -> dict[str, Any]:
    """Build runtime registry overlay spans.

    This is a read-only composer over A01 DatasetReadModel and D01 ScoreReadModel.
    It never recomputes metrics; score status/forms are copied from reports only.
    """

    blocks, glossary, entities = _load_overlay_inputs(
        job_id,
        experiment_id=experiment_id,
        stage=stage,
        block_id=block_id,
        chapter_id=chapter_id,
        jobs_root=jobs_root,
    )
    score_status = "loaded"
    try:
        scores = load_scores(job_id, reports_root=reports_root or THESIS_REPORTS_ROOT)
    except ThesisReadModelError as exc:
        scores = {"drift": []}
        score_status = f"unavailable:{exc.code}"

    source = _build_source_overlay(job_id, blocks, glossary, entities, jobs_root=jobs_root)
    score_index = _score_index(scores.get("drift") or [])
    target = _build_target_overlay(blocks, glossary, entities, score_index, source)

    return {
        "meta": {
            "source": "thesis_registry_overlay",
            "job_id": job_id,
            "read_only": True,
            "score_status": score_status,
            "selected": {
                "experiment_id": experiment_id,
                "stage": stage,
                "block_id": block_id,
                "chapter_id": chapter_id,
            },
            "note": (
                "Char spans are display-only. Status/forms_used are read from "
                "score reports when per-item detail exists; otherwise spans are neutral."
            ),
        },
        "source": source,
        "target_by_config": target,
    }


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
    for term in glossary:
        term_id = str(term.get("term_id") or term.get("glossary_id") or "")
        source_term = str(term.get("source_term") or "").strip()
        if not term_id or not source_term:
            continue
        for block_id in _candidate_block_ids(source_term, block_order, token_index):
            text = block_texts.get(block_id, "")
            if not _in_scope(term, block_id, block_chapters.get(block_id, "")):
                continue
            for start, end, surface in _find_matches(text, source_term):
                glossary_candidates.append({
                    "id": term_id,
                    "block_id": block_id,
                    "span": [start, end],
                    "surface": surface,
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
                for start, end, matched in _find_matches(text, surface):
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

            for term in glossary:
                term_id = str(term.get("term_id") or term.get("glossary_id") or "")
                if block_id not in source_glossary_blocks.get(term_id, set()):
                    continue
                detail = _lookup_glossary_detail(score_index, config, term)
                forms, forms_source, scored = _target_forms_for_term(term, detail)
                for form in forms:
                    for start, end, surface in _find_matches(target_text, form):
                        glossary_candidates.append(_target_candidate(
                            item_id=term_id,
                            block_id=block_id,
                            config=config,
                            start=start,
                            end=end,
                            surface=surface,
                            matched_form=form,
                            detail=detail,
                            forms_source=forms_source,
                            scored=scored,
                            kind="glossary",
                        ))

            for entity in entities:
                entity_id = str(entity.get("entity_id") or "")
                if block_id not in source_entity_blocks.get(entity_id, set()):
                    continue
                detail = _lookup_entity_detail(score_index, config, entity)
                forms, forms_source, scored = _target_forms_for_entity(entity, detail)
                for form in forms:
                    for start, end, surface in _find_matches(target_text, form):
                        entity_candidates.append(_target_candidate(
                            item_id=entity_id,
                            block_id=block_id,
                            config=config,
                            start=start,
                            end=end,
                            surface=surface,
                            matched_form=form,
                            detail=detail,
                            forms_source=forms_source,
                            scored=scored,
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
    return {
        "id": item_id,
        "block_id": block_id,
        "config": config,
        "span": [start, end],
        "surface": surface,
        "matched_form": matched_form,
        "status": (detail or {}).get("status") or "unscored",
        "constraint_strength": (detail or {}).get("constraint_strength"),
        "forms_used": forms_used,
        "forms_source": forms_source,
        "scored": bool(scored),
        "kind": kind,
        "provenance": "translation_runs+score_report" if scored else "translation_runs+runtime_memory",
    }


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


def _find_matches(text: str, needle: str) -> list[tuple[int, int, str]]:
    value = str(text or "")
    raw_needle = str(needle or "").strip()
    if not value or not raw_needle:
        return []
    normalized_text = _normalize_apostrophe(value)
    normalized_needle = _normalize_apostrophe(unicodedata.normalize("NFC", raw_needle))
    if not normalized_needle:
        return []
    prefix = r"(?<!\w)" if _is_word_char(normalized_needle[0]) else ""
    suffix = r"(?!\w)" if _is_word_char(normalized_needle[-1]) else ""
    pattern = prefix + re.escape(normalized_needle) + suffix
    return [
        (match.start(), match.end(), value[match.start():match.end()])
        for match in re.finditer(pattern, normalized_text, flags=re.IGNORECASE | re.UNICODE)
    ]


def _is_word_char(value: str) -> bool:
    return bool(re.match(r"\w", value, flags=re.UNICODE))


def _normalize_apostrophe(text: str) -> str:
    return str(text or "").translate(APOSTROPHE_TRANSLATION)


def _norm_key(text: str) -> str:
    return unicodedata.normalize("NFC", _normalize_apostrophe(str(text or ""))).casefold().strip()


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
