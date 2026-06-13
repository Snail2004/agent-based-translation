from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from pipeline.ingest.document_loader import load_document
from pipeline.memory.freeze import freeze_memory, is_memory_frozen
from pipeline.memory.store_init import init_db
from pipeline.prepass.db_source import load_document_from_connection
from pipeline.prepass.schemas import is_plain_pronoun
from pipeline.prepass.span_resolver import (
    ResolvedSpans,
    resolve_spans,
    resolve_spans_for_document,
)


@dataclass(frozen=True)
class BuildReport:
    doc_id: str
    glossary: int
    entities: int
    mentions: int
    relations: int
    memory_items: int
    coverage: dict[str, list[str]]
    frozen_at: str | None

    def to_json_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_memory(
    db_path: str | Path,
    document_json_path: str | Path,
    prepass_dir: str | Path,
    *,
    freeze: bool = True,
) -> BuildReport:
    db = Path(db_path)
    document_path = Path(document_json_path)
    prepass_path = Path(prepass_dir)
    document = json.loads(document_path.read_text(encoding="utf-8"))
    doc_id = str(document["doc_id"])

    artifact_pairs = _load_artifacts(prepass_path, document)
    artifact_paths = [path for path, _artifact in artifact_pairs]
    artifacts = [artifact for _path, artifact in artifact_pairs]
    resolved = resolve_spans(document_path, artifact_paths)

    connection = init_db(db)
    try:
        if is_memory_frozen(connection):
            raise RuntimeError(
                "Memory DB is already frozen; rebuild into a new DB file."
            )

        if _doc_block_count(connection, doc_id) == 0:
            connection.close()
            load_document(db, document_path)
            connection = init_db(db)
            if is_memory_frozen(connection):
                raise RuntimeError(
                    "Memory DB is already frozen; rebuild into a new DB file."
                )

        block_order = _block_order(connection, doc_id)
        chapter_blocks = _chapter_blocks(document)

        _delete_prepass_memory(connection, doc_id)
        glossary_count = _persist_glossary(connection, doc_id, artifacts, resolved, block_order)
        entities = _merge_entities(artifacts)
        entity_count = _persist_entities(connection, doc_id, entities, resolved, block_order)
        mention_count = _persist_mentions(connection, doc_id, resolved, set(entities))
        relation_count = _persist_relations(
            connection,
            doc_id,
            artifacts,
            entities,
            block_order,
            chapter_blocks,
        )
        memory_item_count = _persist_memory_items(
            connection,
            doc_id,
            artifacts,
            chapter_blocks,
        )

        frozen_at = freeze_memory(connection) if freeze else None
        report = BuildReport(
            doc_id=doc_id,
            glossary=glossary_count,
            entities=entity_count,
            mentions=mention_count,
            relations=relation_count,
            memory_items=memory_item_count,
            coverage=resolved.coverage,
            frozen_at=frozen_at,
        )
        connection.commit()
        return report
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def build_memory_from_db(
    db_path: str | Path,
    prepass_dir: str | Path,
    *,
    doc_id: str = "d2l",
    freeze: bool = True,
) -> BuildReport:
    db = Path(db_path)
    prepass_path = Path(prepass_dir)
    connection = init_db(db)
    try:
        if is_memory_frozen(connection):
            raise RuntimeError(
                "Memory DB is already frozen; rebuild into a new DB file."
            )
        artifact_chapters = _artifact_chapter_ids(prepass_path)
        document = load_document_from_connection(
            connection,
            doc_id,
            artifact_chapters,
            translate_only=True,
        )
        report = _build_memory_for_document(
            connection,
            document,
            prepass_path,
            freeze=freeze,
        )
        connection.commit()
        return report
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _build_memory_for_document(
    connection: sqlite3.Connection,
    document: dict[str, Any],
    prepass_path: Path,
    *,
    freeze: bool,
) -> BuildReport:
    doc_id = str(document["doc_id"])
    artifact_pairs = _load_artifacts(prepass_path, document)
    artifact_paths = [path for path, _artifact in artifact_pairs]
    artifacts = [artifact for _path, artifact in artifact_pairs]
    resolved = resolve_spans_for_document(document, artifact_paths)

    block_order = _block_order(connection, doc_id)
    chapter_blocks = _chapter_blocks(document)

    _delete_prepass_memory(connection, doc_id)
    glossary_count = _persist_glossary(connection, doc_id, artifacts, resolved, block_order)
    entities = _merge_entities(artifacts)
    entity_count = _persist_entities(connection, doc_id, entities, resolved, block_order)
    mention_count = _persist_mentions(connection, doc_id, resolved, set(entities))
    relation_count = _persist_relations(
        connection,
        doc_id,
        artifacts,
        entities,
        block_order,
        chapter_blocks,
    )
    memory_item_count = _persist_memory_items(
        connection,
        doc_id,
        artifacts,
        chapter_blocks,
    )

    frozen_at = freeze_memory(connection) if freeze else None
    return BuildReport(
        doc_id=doc_id,
        glossary=glossary_count,
        entities=entity_count,
        mentions=mention_count,
        relations=relation_count,
        memory_items=memory_item_count,
        coverage=resolved.coverage,
        frozen_at=frozen_at,
    )


def _load_artifacts(
    prepass_dir: Path,
    document: dict[str, Any],
) -> list[tuple[Path, dict[str, Any]]]:
    chapter_order = {
        str(chapter.get("chapter_id") or ""): index
        for index, chapter in enumerate(document.get("chapters") or [])
    }
    pairs: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(prepass_dir.glob("*.json")):
        if path.name == "run_report.json":
            continue
        artifact = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(artifact, dict) and artifact.get("chapter_id"):
            pairs.append((path, artifact))
    if not pairs:
        raise ValueError(f"No prepass chapter artifacts found in {prepass_dir}")
    pairs.sort(
        key=lambda item: (
            chapter_order.get(str(item[1].get("chapter_id") or ""), 10**9),
            str(item[1].get("chapter_id") or ""),
        )
    )
    return pairs


def _artifact_chapter_ids(prepass_dir: Path) -> list[str]:
    chapter_ids: list[str] = []
    for path in sorted(prepass_dir.glob("*.json")):
        if path.name == "run_report.json":
            continue
        artifact = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(artifact, dict) and artifact.get("chapter_id"):
            chapter_ids.append(str(artifact["chapter_id"]))
    if not chapter_ids:
        raise ValueError(f"No prepass chapter artifacts found in {prepass_dir}")
    return chapter_ids


def _doc_block_count(connection: sqlite3.Connection, doc_id: str) -> int:
    row = connection.execute(
        "SELECT COUNT(*) AS count FROM blocks WHERE doc_id = ?",
        (doc_id,),
    ).fetchone()
    return int(row["count"] if isinstance(row, sqlite3.Row) else row[0])


def _block_order(connection: sqlite3.Connection, doc_id: str) -> dict[str, int]:
    return {
        str(row["block_id"]): int(row["order_index"])
        for row in connection.execute(
            "SELECT block_id, order_index FROM blocks WHERE doc_id = ?",
            (doc_id,),
        )
    }


def _chapter_blocks(document: dict[str, Any]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for chapter in document.get("chapters") or []:
        chapter_id = str(chapter.get("chapter_id") or "")
        result[chapter_id] = [
            str(block.get("block_id") or "")
            for block in chapter.get("blocks") or []
            if block.get("block_id")
        ]
    return result


def _delete_prepass_memory(connection: sqlite3.Connection, doc_id: str) -> None:
    for table in [
        "mentions",
        "entity_relations",
        "glossary_entries",
        "memory_items",
        "entities",
    ]:
        connection.execute(f"DELETE FROM {table} WHERE doc_id = ?", (doc_id,))


def _persist_glossary(
    connection: sqlite3.Connection,
    doc_id: str,
    artifacts: list[dict[str, Any]],
    resolved: ResolvedSpans,
    block_order: dict[str, int],
) -> int:
    terms: dict[str, dict[str, Any]] = {}
    target_votes: dict[str, Counter[str]] = defaultdict(Counter)
    first_seen_targets: dict[str, list[str]] = defaultdict(list)
    allowed_by_key: dict[str, list[str]] = defaultdict(list)
    forbidden_by_key: dict[str, list[str]] = defaultdict(list)
    evidence_by_key: dict[str, list[str]] = defaultdict(list)
    for artifact in artifacts:
        for term in artifact.get("glossary_candidates") or []:
            if not isinstance(term, dict):
                continue
            source_term = str(term.get("source_term") or "").strip()
            if not source_term:
                continue
            key = source_term.casefold()
            target = _term_target(term, source_term)
            if target:
                target_votes[key][target] += 1
                if target not in first_seen_targets[key]:
                    first_seen_targets[key].append(target)
            allowed_by_key[key].extend(str(item) for item in term.get("allowed_variants") or [])
            forbidden_by_key[key].extend(str(item) for item in term.get("forbidden_variants") or [])
            evidence_by_key[key].extend(str(item) for item in term.get("evidence_span_ids") or [])
            if key not in terms:
                terms[key] = dict(term)
            else:
                existing_blocks = set(terms[key].get("block_ids") or [])
                existing_blocks.update(term.get("block_ids") or [])
                terms[key]["block_ids"] = sorted(existing_blocks)

    occurrences_by_term: dict[str, list[str]] = defaultdict(list)
    for occurrence in resolved.term_occurrences:
        occurrences_by_term[occurrence.source_term.casefold()].append(occurrence.block_id)

    count = 0
    used_glossary_ids: set[str] = set()
    for key, term in sorted(terms.items()):
        source_term = str(term.get("source_term") or "").strip()
        glossary_id = _unique_id(f"gl_{_slug(source_term)}", used_glossary_ids)
        target_term = _choose_target(
            target_votes.get(key, Counter()),
            first_seen_targets.get(key, []),
            fallback=_term_target(term, source_term),
        )
        occurrence_blocks = occurrences_by_term.get(key, [])
        last_block_id = _last_block_id(occurrence_blocks, block_order)
        allowed_variants = _clean_string_list(
            [
                target_term,
                *first_seen_targets.get(key, []),
                *allowed_by_key.get(key, []),
            ]
        )
        forbidden_variants = _clean_string_list(forbidden_by_key.get(key, []))
        evidence_span_ids = _clean_string_list(
            [
                *(term.get("block_ids") or []),
                *evidence_by_key.get(key, []),
            ]
        )
        connection.execute(
            """
            INSERT INTO glossary_entries (
              glossary_id, doc_id, source_term, target_term, term_type,
              do_not_translate, allowed_variants_json, forbidden_variants_json,
              evidence_span_ids_json, confidence, status, occurrences_count, last_block_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'approved', ?, ?)
            """,
            (
                glossary_id,
                doc_id,
                source_term,
                target_term,
                str(term.get("term_type") or term.get("category") or "other"),
                1 if bool(term.get("do_not_translate")) else 0,
                json.dumps(allowed_variants, ensure_ascii=False),
                json.dumps(forbidden_variants, ensure_ascii=False),
                json.dumps(evidence_span_ids, ensure_ascii=False),
                0.7,
                len(occurrence_blocks),
                last_block_id,
            ),
        )
        count += 1
    return count


def _term_target(term: dict[str, Any], fallback: str) -> str:
    return str(
        term.get("canonical_target")
        or term.get("proposed_target_vi")
        or fallback
    ).strip()


def _choose_target(votes: Counter[str], first_seen: list[str], fallback: str) -> str:
    if not votes:
        return fallback
    max_count = max(votes.values())
    candidates = {target for target, count in votes.items() if count == max_count}
    for target in first_seen:
        if target in candidates:
            return target
    return sorted(candidates, key=lambda item: item.casefold())[0]


def _merge_entities(artifacts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    entities: dict[str, dict[str, Any]] = {}
    for artifact in artifacts:
        for entity in artifact.get("entities") or []:
            if not isinstance(entity, dict):
                continue
            entity_id = str(entity.get("entity_id") or "").strip()
            if not entity_id:
                continue
            current = entities.setdefault(
                entity_id,
                {
                    "entity_id": entity_id,
                    "aliases_source": [],
                    "aliases_target_vi": [],
                },
            )
            for field in [
                "canonical_source",
                "entity_type",
                "proposed_target_vi",
            ]:
                value = str(entity.get(field) or "").strip()
                if value:
                    current[field] = value
            current["aliases_source"].extend(entity.get("aliases_source") or [])
            current["aliases_target_vi"].extend(entity.get("aliases_target_vi") or [])
    for entity in entities.values():
        entity["canonical_source"] = _clean_canonical_source(
            str(entity.get("canonical_source") or entity["entity_id"])
        )
        entity["aliases_source"] = _clean_source_aliases(entity.get("aliases_source") or [])
        entity["aliases_target_vi"] = _clean_string_list(entity.get("aliases_target_vi") or [])
    return entities


def _persist_entities(
    connection: sqlite3.Connection,
    doc_id: str,
    entities: dict[str, dict[str, Any]],
    resolved: ResolvedSpans,
    block_order: dict[str, int],
) -> int:
    mention_blocks: dict[str, list[str]] = defaultdict(list)
    for mention in resolved.entity_mentions:
        mention_blocks[mention.entity_id].append(mention.block_id)

    count = 0
    for entity_id, entity in sorted(entities.items()):
        blocks = mention_blocks.get(entity_id, [])
        canonical_source = str(entity.get("canonical_source") or entity_id)
        canonical_target = str(entity.get("proposed_target_vi") or canonical_source)
        connection.execute(
            """
            INSERT INTO entities (
              entity_id, doc_id, canonical_source, canonical_target,
              entity_type, first_block_id, latest_block_id,
              aliases_source_json, aliases_target_json, confidence, status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'approved')
            """,
            (
                entity_id,
                doc_id,
                canonical_source,
                canonical_target,
                str(entity.get("entity_type") or "other"),
                _first_block_id(blocks, block_order),
                _last_block_id(blocks, block_order),
                json.dumps(entity.get("aliases_source") or [], ensure_ascii=False),
                json.dumps(entity.get("aliases_target_vi") or [], ensure_ascii=False),
                0.7,
            ),
        )
        count += 1
    return count


def _persist_mentions(
    connection: sqlite3.Connection,
    doc_id: str,
    resolved: ResolvedSpans,
    known_entity_ids: set[str],
) -> int:
    counters: Counter[tuple[str, str]] = Counter()
    count = 0
    for mention in resolved.entity_mentions:
        if mention.entity_id not in known_entity_ids:
            continue
        key = (mention.block_id, mention.entity_id)
        counters[key] += 1
        mention_id = f"m_{mention.block_id}_{mention.entity_id}_{counters[key]:03d}"
        connection.execute(
            """
            INSERT INTO mentions (
              mention_id, doc_id, entity_id, block_id, surface,
              mention_type, char_start, char_end, confidence
            )
            VALUES (?, ?, ?, ?, ?, 'name', ?, ?, ?)
            """,
            (
                mention_id,
                doc_id,
                mention.entity_id,
                mention.block_id,
                mention.surface,
                mention.char_start,
                mention.char_end,
                0.75,
            ),
        )
        count += 1
    return count


def _persist_relations(
    connection: sqlite3.Connection,
    doc_id: str,
    artifacts: list[dict[str, Any]],
    entities: dict[str, dict[str, Any]],
    block_order: dict[str, int],
    chapter_blocks: dict[str, list[str]],
) -> int:
    states_by_pair: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for artifact in artifacts:
        chapter_id = str(artifact.get("chapter_id") or "")
        fallback_block = (chapter_blocks.get(chapter_id) or [None])[0]
        for relation in artifact.get("relations") or []:
            if not isinstance(relation, dict):
                continue
            original_a = str(relation.get("a") or "").strip()
            original_b = str(relation.get("b") or "").strip()
            if original_a not in entities or original_b not in entities:
                continue
            sorted_a, sorted_b = sorted((original_a, original_b))
            trigger = str(relation.get("trigger_block_id") or fallback_block or "")
            if trigger not in block_order:
                trigger = str(fallback_block or "")
            if not trigger:
                continue
            if (original_a, original_b) == (sorted_a, sorted_b):
                a_to_b = str(relation.get("address_a_to_b_vi") or "")
                b_to_a = str(relation.get("address_b_to_a_vi") or "")
            else:
                a_to_b = str(relation.get("address_b_to_a_vi") or "")
                b_to_a = str(relation.get("address_a_to_b_vi") or "")
            states_by_pair[(sorted_a, sorted_b)].append(
                {
                    "chapter_id": chapter_id,
                    "relation_type": str(relation.get("relation") or ""),
                    "state_label": str(relation.get("state_label") or ""),
                    "valid_from_block_id": trigger,
                    "address_policy": {"a_to_b": a_to_b, "b_to_a": b_to_a},
                    "notes": str(relation.get("notes") or ""),
                    "order": block_order[trigger],
                }
            )

    count = 0
    for (source_id, target_id), states in sorted(states_by_pair.items()):
        states.sort(key=lambda item: (item["order"], item["state_label"]))
        for index, state in enumerate(states):
            next_state = states[index + 1] if index + 1 < len(states) else None
            valid_to = None
            if next_state is not None:
                valid_to = _previous_block_id(
                    next_state["valid_from_block_id"],
                    state["valid_from_block_id"],
                    block_order,
                )
            relation_id = f"rel_{source_id}_{target_id}_{index + 1:03d}"
            connection.execute(
                """
                INSERT INTO entity_relations (
                  relation_id, doc_id, source_entity_id, target_entity_id,
                  relation_type, state_label, valid_from_block_id,
                  valid_to_block_id, trigger_event_id, address_policy_json,
                  evidence_json, confidence, notes
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)
                """,
                (
                    relation_id,
                    doc_id,
                    source_id,
                    target_id,
                    state["relation_type"],
                    state["state_label"],
                    state["valid_from_block_id"],
                    valid_to,
                    json.dumps(state["address_policy"], ensure_ascii=False, sort_keys=True),
                    json.dumps(
                        [
                            {
                                "chapter_id": state["chapter_id"],
                                "trigger_block_id": state["valid_from_block_id"],
                            }
                        ],
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    0.7,
                    state["notes"],
                ),
            )
            count += 1
    return count


def _persist_memory_items(
    connection: sqlite3.Connection,
    doc_id: str,
    artifacts: list[dict[str, Any]],
    chapter_blocks: dict[str, list[str]],
) -> int:
    count = 0
    for artifact in artifacts:
        chapter_id = str(artifact.get("chapter_id") or "")
        blocks = chapter_blocks.get(chapter_id) or []
        summary = str(artifact.get("chapter_summary_vi") or "").strip()
        if summary:
            connection.execute(
                """
                INSERT INTO memory_items (
                  memory_id, doc_id, memory_type, scope, chapter_id,
                  block_start, block_end, content, payload_json,
                  confidence, status, source_refs_json
                )
                VALUES (?, ?, 'chapter_summary', 'chapter', ?, ?, ?, ?, '{}', ?, 'approved', ?)
                """,
                (
                    f"mi_{_slug(chapter_id)}_summary",
                    doc_id,
                    chapter_id,
                    blocks[0] if blocks else None,
                    blocks[-1] if blocks else None,
                    summary,
                    0.7,
                    json.dumps(blocks, ensure_ascii=False),
                ),
            )
            count += 1

        for index, motif in enumerate(artifact.get("motifs") or [], start=1):
            if not isinstance(motif, dict):
                continue
            note = str(motif.get("note") or "").strip()
            block_ids = [str(item) for item in motif.get("block_ids") or []]
            if not note:
                continue
            connection.execute(
                """
                INSERT INTO memory_items (
                  memory_id, doc_id, memory_type, scope, chapter_id,
                  block_start, block_end, content, payload_json,
                  confidence, status, source_refs_json
                )
                VALUES (?, ?, 'motif', 'chapter', ?, ?, ?, ?, ?, ?, 'approved', ?)
                """,
                (
                    f"mi_{_slug(chapter_id)}_motif_{index:03d}",
                    doc_id,
                    chapter_id,
                    block_ids[0] if block_ids else None,
                    block_ids[-1] if block_ids else None,
                    note,
                    json.dumps({"block_ids": block_ids}, ensure_ascii=False, sort_keys=True),
                    0.65,
                    json.dumps(block_ids, ensure_ascii=False),
                ),
            )
            count += 1
    return count


def _first_block_id(block_ids: list[str], block_order: dict[str, int]) -> str | None:
    if not block_ids:
        return None
    return min(block_ids, key=lambda block_id: block_order.get(block_id, 10**9))


def _last_block_id(block_ids: list[str], block_order: dict[str, int]) -> str | None:
    if not block_ids:
        return None
    return max(block_ids, key=lambda block_id: block_order.get(block_id, -1))


def _previous_block_id(
    next_block_id: str,
    current_block_id: str,
    block_order: dict[str, int],
) -> str | None:
    reverse_order = {order: block_id for block_id, order in block_order.items()}
    next_order = block_order.get(next_block_id)
    current_order = block_order.get(current_block_id)
    if next_order is None or current_order is None:
        return None
    previous_order = next_order - 1
    if previous_order < current_order:
        return None
    return reverse_order.get(previous_order)


def _clean_canonical_source(value: str) -> str:
    return value.split("/", 1)[0].strip()


def _clean_source_aliases(values: list[Any]) -> list[str]:
    return [
        item
        for item in _clean_string_list(values)
        if not is_plain_pronoun(item)
    ]


def _clean_string_list(values: list[Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        item = str(value).strip()
        key = item.casefold()
        if item and key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")
    return slug or "item"


def _unique_id(base: str, used: set[str]) -> str:
    if base not in used:
        used.add(base)
        return base
    counter = 2
    while True:
        candidate = f"{base}_{counter:03d}"
        if candidate not in used:
            used.add(candidate)
            return candidate
        counter += 1
