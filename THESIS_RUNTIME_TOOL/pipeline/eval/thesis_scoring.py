from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from pathlib import Path
from typing import Any

from pipeline.eval.consistency import score_consistency
from pipeline.prepass.span_resolver import resolve_spans


METRIC_VERSION = "consistency_v1_thesis_scoring"


def normalize_apostrophe(text: str) -> str:
    """Normalize apostrophe variants before adapter-level matching."""

    return (
        text.replace("\u2019", "'")
        .replace("\u2018", "'")
        .replace("\u02bc", "'")
    )


def score_thesis_translations(
    *,
    db_path: str | Path,
    experiment_id: str,
    config: str,
    prepass_dir: str | Path,
    source_document_path: str | Path,
    ruler_note: str = "",
    ruler: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Score thesis translations with the same ruler used for oracle scoring."""

    db = Path(db_path)
    if not db.exists():
        raise FileNotFoundError(f"DB not found: {db}")

    if ruler is None:
        ruler = build_ruler_from_db_and_spans(
            db_path=db,
            prepass_dir=prepass_dir,
            source_document_path=source_document_path,
        )

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        translations = _load_translations(conn, experiment_id, config)
    finally:
        conn.close()

    normalized_translations = {
        block_id: normalize_apostrophe(text)
        for block_id, text in translations.items()
    }
    source_label = f"thesis_{experiment_id}_{config}"
    if ruler_note:
        source_label = f"{source_label}:{ruler_note}"

    return score_consistency(
        project=f"thesis_{experiment_id}_{config}",
        terms=ruler["terms"],
        entities=ruler["entities"],
        term_occurrences_by_block=ruler["term_occurrences_by_block"],
        entity_mentions_by_block=ruler["entity_mentions_by_block"],
        translations_by_block=normalized_translations,
        block_chapters=ruler["block_chapters"],
        source=source_label,
    )


def score_oracle_on_same_ruler(
    *,
    oracle_project_path: str | Path,
    ruler_block_chapters: dict[str, str],
    ruler_terms: dict[str, dict],
    ruler_entities: dict[str, dict],
    ruler_term_occurrences: dict[str, list],
    ruler_entity_mentions: dict[str, list],
) -> dict[str, Any]:
    """Score AI-LAB preview translations with an already-built thesis ruler."""

    from pipeline.eval.loaders import load_oracle_project

    oracle = load_oracle_project(oracle_project_path)
    translations = {
        block_id: normalize_apostrophe(text)
        for block_id, text in oracle.translations_by_block.items()
        if block_id in ruler_block_chapters
    }
    scored_blocks = set(translations)
    return score_consistency(
        project="oracle_preview_same_ruler",
        terms=ruler_terms,
        entities=ruler_entities,
        term_occurrences_by_block={
            block_id: list(items)
            for block_id, items in ruler_term_occurrences.items()
            if block_id in scored_blocks
        },
        entity_mentions_by_block={
            block_id: list(items)
            for block_id, items in ruler_entity_mentions.items()
            if block_id in scored_blocks
        },
        translations_by_block=translations,
        block_chapters={
            block_id: chapter
            for block_id, chapter in ruler_block_chapters.items()
            if block_id in scored_blocks
        },
        source="oracle_preview",
    )


def build_ruler_from_db_and_spans(
    db_path: str | Path,
    prepass_dir: str | Path,
    source_document_path: str | Path,
) -> dict[str, Any]:
    """Build the shared registry + occurrence ruler for thesis/oracle scoring.

    Entity mentions still come from span_resolver. Term occurrences are scanned
    at this adapter layer with apostrophe normalization on both artifact terms
    and source block text, then keyed by glossary_id.
    """

    conn = sqlite3.connect(Path(db_path))
    conn.row_factory = sqlite3.Row
    try:
        terms = _load_terms(conn)
        entities = _load_entities(conn)
    finally:
        conn.close()

    artifact_paths = _artifact_paths(prepass_dir)
    resolved = resolve_spans(source_document_path, artifact_paths)

    term_occurrences_by_block = _build_term_occurrences(
        terms=terms,
        artifact_terms=_artifact_source_terms(artifact_paths),
        source_blocks=_source_blocks_for_artifacts(source_document_path, artifact_paths),
    )

    entity_mentions_by_block: dict[str, list[dict[str, str]]] = {}
    for mention in resolved.entity_mentions:
        entity_mentions_by_block.setdefault(mention.block_id, []).append(
            {"entity_id": mention.entity_id, "surface": mention.surface}
        )

    block_chapters = {
        block_id: _block_chapter(block_id)
        for block_id in set(term_occurrences_by_block) | set(entity_mentions_by_block)
    }

    return {
        "terms": terms,
        "entities": entities,
        "term_occurrences_by_block": term_occurrences_by_block,
        "entity_mentions_by_block": entity_mentions_by_block,
        "block_chapters": block_chapters,
    }


def _load_terms(conn: sqlite3.Connection) -> dict[str, dict]:
    rows = conn.execute(
        """
        SELECT glossary_id, source_term, target_term, term_type,
               do_not_translate, allowed_variants_json, forbidden_variants_json
        FROM glossary_entries
        """
    ).fetchall()

    terms: dict[str, dict] = {}
    for row in rows:
        glossary_id = str(row["glossary_id"])
        source_term = str(row["source_term"] or "")
        terms[glossary_id] = {
            "source_term": source_term,
            "expected_target": str(row["target_term"] or ""),
            "allowed_variants": _json_list(row["allowed_variants_json"]),
            "forbidden_variants": _json_list(row["forbidden_variants_json"]),
            "do_not_translate": bool(int(row["do_not_translate"] or 0)),
            "_normalized_source": _normalize_for_match(source_term),
        }
    return terms


def _load_entities(conn: sqlite3.Connection) -> dict[str, dict]:
    rows = conn.execute(
        """
        SELECT entity_id, canonical_source, canonical_target,
               aliases_source_json, aliases_target_json
        FROM entities
        """
    ).fetchall()

    entities: dict[str, dict] = {}
    for row in rows:
        canonical_source = str(row["canonical_source"] or "")
        canonical_target = str(row["canonical_target"] or canonical_source)
        entities[str(row["entity_id"])] = {
            "canonical_source": canonical_source,
            "canonical_target": canonical_target,
            "aliases_source": _json_list(row["aliases_source_json"]),
            "aliases_target": _json_list(row["aliases_target_json"]),
        }
    return entities


def _load_translations(
    conn: sqlite3.Connection,
    experiment_id: str,
    config: str,
) -> dict[str, str]:
    rows = conn.execute(
        """
        SELECT block_id, output_text
        FROM translation_runs
        WHERE experiment_id = ? AND config = ? AND stage = 'draft'
        """,
        (experiment_id, config),
    ).fetchall()
    return {str(row["block_id"]): str(row["output_text"] or "") for row in rows}


def _block_chapter(block_id: str) -> str:
    match = re.search(r"(?:^|_)ch(\d+)", block_id, re.IGNORECASE)
    if match:
        return f"ch{int(match.group(1)):02d}"
    return "unknown"


def _artifact_paths(prepass_dir: str | Path) -> list[Path]:
    return [
        path
        for path in sorted(Path(prepass_dir).glob("*.json"))
        if path.name != "run_report.json"
    ]


def _artifact_source_terms(artifact_paths: list[Path]) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for path in artifact_paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        for item in data.get("glossary_candidates") or []:
            if not isinstance(item, dict):
                continue
            source_term = str(item.get("source_term") or "").strip()
            key = _normalize_for_match(source_term)
            if source_term and key not in seen:
                seen.add(key)
                terms.append(source_term)
    return terms


def _source_blocks_for_artifacts(
    source_document_path: str | Path,
    artifact_paths: list[Path],
) -> dict[str, str]:
    artifact_chapters = {
        str(json.loads(path.read_text(encoding="utf-8")).get("chapter_id") or "")
        for path in artifact_paths
    }
    document = json.loads(Path(source_document_path).read_text(encoding="utf-8"))

    blocks: dict[str, str] = {}
    for chapter in document.get("chapters") or []:
        if str(chapter.get("chapter_id") or "") not in artifact_chapters:
            continue
        for block in chapter.get("blocks") or []:
            block_id = str(block.get("block_id") or "")
            if not block_id:
                continue
            text = str(block.get("clean_text") or block.get("source_text") or "")
            blocks[block_id] = unicodedata.normalize("NFC", text)
    return blocks


def _build_term_occurrences(
    *,
    terms: dict[str, dict[str, Any]],
    artifact_terms: list[str],
    source_blocks: dict[str, str],
) -> dict[str, list[str]]:
    term_id_by_source: dict[str, str] = {}
    for term_id in sorted(terms):
        term = terms[term_id]
        key = str(term.get("_normalized_source") or _normalize_for_match(str(term.get("source_term") or "")))
        term_id_by_source.setdefault(key, term_id)

    occurrences: dict[str, list[str]] = {}
    for source_term in artifact_terms:
        term_id = term_id_by_source.get(_normalize_for_match(source_term))
        if not term_id:
            continue
        for block_id, text in source_blocks.items():
            count = _count_source_matches(text, source_term)
            if count:
                occurrences.setdefault(block_id, []).extend([term_id] * count)
    return occurrences


def _count_source_matches(text: str, source_term: str) -> int:
    normalized_text = _normalize_for_match(text)
    normalized_term = _normalize_for_match(source_term)
    if not normalized_term:
        return 0
    pattern = rf"(?<!\w){re.escape(normalized_term)}(?!\w)"
    return len(re.findall(pattern, normalized_text, flags=re.UNICODE))


def _normalize_for_match(text: str) -> str:
    return unicodedata.normalize("NFC", normalize_apostrophe(text)).casefold()


def _json_list(value: Any) -> list[Any]:
    try:
        parsed = json.loads(str(value or "[]"))
    except Exception:
        return []
    return parsed if isinstance(parsed, list) else []
