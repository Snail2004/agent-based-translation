import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import DATASET_FILES, SCHEMA_DIR, VALIDATOR_SCRIPT
from services.audit_log import log_event
from services.dataset_io import read_json, read_jsonl, write_json_atomic, write_jsonl_atomic
from services.history import record_history


ANNOTATION_PROMPT_ID = "dataset-annotation-drafter-v1"
ENTITY_TYPE_ALIASES = {
    "character": "person",
    "location": "place",
    "organisation": "org",
    "organization": "org",
    "named_object": "concept",
    "object": "concept",
    "artifact": "concept",
    "artefact": "concept",
    "product": "concept",
    "term": "concept",
}
ENTITY_TYPES = {"person", "place", "org", "concept"}


class AnnotationError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400, **details: Any):
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _canonical(project_path: Path) -> Path:
    return project_path / "canonical"


def _working_annotation(project_path: Path) -> Path:
    return project_path / "working" / "annotation"


def _document_path(project_path: Path) -> Path:
    return _canonical(project_path) / DATASET_FILES["document"]


def _jsonl_path(project_path: Path, key: str) -> Path:
    return _canonical(project_path) / DATASET_FILES[key]


def _read_document(project_path: Path) -> dict[str, Any]:
    path = _document_path(project_path)
    if not path.exists():
        raise AnnotationError("missing_document", "Project has not been extracted yet.", 404)
    return read_json(path)


def _write_document(project_path: Path, document: dict[str, Any]) -> None:
    write_json_atomic(_document_path(project_path), document)


def _find_chapter(document: dict[str, Any], chapter_id: str) -> dict[str, Any]:
    for chapter in document.get("chapters", []):
        if chapter.get("chapter_id") == chapter_id:
            return chapter
    raise AnnotationError("missing_chapter", f"Chapter not found: {chapter_id}", 404)


def _chapter_blocks(document: dict[str, Any], chapter_id: str) -> list[dict[str, Any]]:
    return list(_find_chapter(document, chapter_id).get("blocks", []))


def _block_map(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        block.get("block_id"): block
        for chapter in document.get("chapters", [])
        for block in chapter.get("blocks", [])
        if block.get("block_id")
    }


def _next_id(rows: list[dict[str, Any]], field: str, prefix: str) -> str:
    used = {str(row.get(field)) for row in rows}
    index = 1
    while True:
        candidate = f"{prefix}_{index:03d}"
        if candidate not in used:
            return candidate
        index += 1


def _stable_hash(data: Any) -> str:
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _block_text_hash(blocks: list[dict[str, Any]]) -> str:
    payload = [{"block_id": block.get("block_id"), "clean_text": block.get("clean_text") or ""} for block in blocks]
    return _stable_hash(payload)


def _norm(value: Any) -> str:
    return str(value or "").casefold().strip()


def _entity_type(value: Any) -> str:
    raw = str(value or "concept").strip().casefold()
    normalized = ENTITY_TYPE_ALIASES.get(raw, raw)
    return normalized if normalized in ENTITY_TYPES else "concept"


def _aliases(row: dict[str, Any]) -> set[str]:
    values = {row.get("canonical_source"), row.get("canonical_target")}
    values.update(row.get("aliases_source") or [])
    values.update(row.get("aliases_target") or [])
    return {_norm(value) for value in values if _norm(value)}


def _annotation_path(project_path: Path, chapter_id: str, suffix: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in chapter_id)
    return _working_annotation(project_path) / f"{safe}_{suffix}.json"


def _candidate_path(project_path: Path, chapter_id: str) -> Path:
    return _annotation_path(project_path, chapter_id, "candidate")


def _resolved_path(project_path: Path, chapter_id: str) -> Path:
    return _annotation_path(project_path, chapter_id, "resolved")


def _input_path(project_path: Path, chapter_id: str) -> Path:
    return _annotation_path(project_path, chapter_id, "input")


def _provenance_path(project_path: Path, chapter_id: str) -> Path:
    return _annotation_path(project_path, chapter_id, "provenance")


def _all_matches(text: str, surface: str) -> list[int]:
    if not surface:
        return []
    starts: list[int] = []
    start = 0
    while True:
        found = text.find(surface, start)
        if found == -1:
            return starts
        starts.append(found)
        start = found + max(1, len(surface))


def _snippet(text: str, start: int, end: int) -> str:
    left = max(0, start - 32)
    right = min(len(text), end + 32)
    return text[left:right].replace("\n", " ")


def _resolve_surface(block: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    text = block.get("clean_text") or ""
    surface = str(raw.get("surface") or "")
    if not surface:
        return {"status": "unresolved", "message": "Missing surface text.", "matches": []}

    matches = _all_matches(text, surface)
    if not matches:
        return {
            "status": "unresolved",
            "message": "Surface text was not found in clean_text.",
            "surface": surface,
            "matches": [],
        }

    left_context = str(raw.get("left_context") or "")
    right_context = str(raw.get("right_context") or "")
    context_matches: list[int] = []
    for start in matches:
        end = start + len(surface)
        left_ok = not left_context or text[:start].endswith(left_context)
        right_ok = not right_context or text[end:].startswith(right_context)
        if left_ok and right_ok:
            context_matches.append(start)

    chosen = context_matches or matches
    if len(chosen) != 1:
        return {
            "status": "ambiguous",
            "message": "Surface resolved to multiple possible spans.",
            "surface": surface,
            "matches": [
                {"span": [start, start + len(surface)], "snippet": _snippet(text, start, start + len(surface))}
                for start in chosen[:8]
            ],
        }

    start = chosen[0]
    end = start + len(surface)
    selected = text[start:end]
    if selected != surface:
        return {
            "status": "unresolved",
            "message": "Resolved span does not match surface.",
            "surface": surface,
            "span": [start, end],
        }
    return {
        "status": "ok",
        "surface": surface,
        "span": [start, end],
        "left_context": left_context,
        "right_context": right_context,
    }


def _span_intersects(a: list[int], b: list[int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def _ensure_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def build_annotation_input(project_path: Path, doc_id: str, chapter_id: str) -> dict[str, Any]:
    document = _read_document(project_path)
    if document.get("doc_id") != doc_id:
        raise AnnotationError("doc_id_mismatch", "Project document doc_id does not match request.")
    chapter = _find_chapter(document, chapter_id)
    glossary = read_jsonl(_jsonl_path(project_path, "glossary"))
    entities = read_jsonl(_jsonl_path(project_path, "entities"))
    relations = read_jsonl(_jsonl_path(project_path, "entity_relations"))

    payload = {
        "doc_id": doc_id,
        "chapter_id": chapter_id,
        "chapter_title": chapter.get("title") or "",
        "blocks": [
            {
                "block_id": block.get("block_id"),
                "clean_text": block.get("clean_text") or "",
                "block_type": block.get("block_type"),
                "discourse": block.get("discourse") or {},
                "annotations": block.get("annotations") or {},
            }
            for block in chapter.get("blocks", [])
        ],
        "known_entities": [
            {
                "entity_id": row.get("entity_id"),
                "canonical_source": row.get("canonical_source"),
                "canonical_target": row.get("canonical_target"),
                "entity_type": row.get("entity_type"),
                "aliases_source": row.get("aliases_source", []),
                "aliases_target": row.get("aliases_target", []),
                "pronoun_policy": row.get("pronoun_policy", ""),
            }
            for row in entities
        ],
        "known_terms": [
            {
                "term_id": row.get("term_id"),
                "source_term": row.get("source_term"),
                "expected_target": row.get("expected_target"),
                "allowed_variants": row.get("allowed_variants", []),
                "forbidden_variants": row.get("forbidden_variants", []),
                "status": row.get("status"),
            }
            for row in glossary
        ],
        "known_relations": [
            {
                "relation_id": row.get("relation_id"),
                "source_entity_id": row.get("source_entity_id"),
                "target_entity_id": row.get("target_entity_id"),
                "relation_type": row.get("relation_type"),
                "state_label": row.get("state_label"),
                "valid_from_block_id": row.get("valid_from_block_id"),
                "valid_to_block_id": row.get("valid_to_block_id"),
                "address_policy": row.get("address_policy") or {},
            }
            for row in relations
        ],
        "contract": {
            "skill": "dataset-annotation-drafter",
            "prompt_id": ANNOTATION_PROMPT_ID,
            "spans": "Do not emit offsets. Backend resolves surface + context.",
        },
        "paths": {
            "project_root": str(project_path.resolve()),
            "input": str(_input_path(project_path, chapter_id).resolve()),
            "candidate": str(_candidate_path(project_path, chapter_id).resolve()),
            "resolved": str(_resolved_path(project_path, chapter_id).resolve()),
        },
    }
    write_json_atomic(_input_path(project_path, chapter_id), payload)
    log_event(project_path, "annotation_input_built", {"chapter_id": chapter_id, "blocks": len(payload["blocks"])}, "system")
    return payload


def load_annotation_candidate(project_path: Path, doc_id: str, chapter_id: str) -> dict[str, Any]:
    document = _read_document(project_path)
    if document.get("doc_id") != doc_id:
        raise AnnotationError("doc_id_mismatch", "Project document doc_id does not match request.")
    _find_chapter(document, chapter_id)
    path = _candidate_path(project_path, chapter_id)
    if not path.exists():
        raise AnnotationError(
            "missing_annotation_candidate",
            f"No agent-written AnnotationCandidate found for {chapter_id}.",
            404,
            path=str(path.resolve()),
        )
    candidate = read_json(path)
    if not isinstance(candidate, dict):
        raise AnnotationError("invalid_candidate", "Annotation candidate file must contain one JSON object.", 422)
    return {
        "doc_id": doc_id,
        "chapter_id": chapter_id,
        "candidate": candidate,
        "path": str(path.resolve()),
    }


def _resolve_entity_candidate(
    row: dict[str, Any],
    *,
    doc_id: str,
    block_by_id: dict[str, dict[str, Any]],
    existing_entities: list[dict[str, Any]],
    next_entity_id: str,
) -> dict[str, Any]:
    key = str(row.get("entity_key") or row.get("existing_entity_id") or "").strip()
    existing_id = row.get("existing_entity_id")
    known = {item.get("entity_id"): item for item in existing_entities}
    matched_existing = None
    if existing_id:
        matched_existing = known.get(existing_id)
    if matched_existing is None:
        candidate_names = {
            _norm(row.get("canonical_source")),
            *(_norm(alias) for alias in row.get("aliases_source", []) or []),
        }
        for item in existing_entities:
            if candidate_names & _aliases(item):
                matched_existing = item
                existing_id = item.get("entity_id")
                break

    entity_id = str(existing_id or next_entity_id)
    mentions = []
    for raw in _ensure_list(row.get("mentions")):
        block_id = str(raw.get("block_id") or "")
        block = block_by_id.get(block_id)
        if not block:
            mentions.append({"status": "unresolved", "block_id": block_id, "message": "Block not found.", "raw": raw})
            continue
        resolved = _resolve_surface(block, raw)
        resolved.update({"block_id": block_id, "raw": raw})
        mentions.append(resolved)

    status = "ok" if mentions and all(item.get("status") == "ok" for item in mentions) else "needs_review"
    if not key:
        status = "needs_review"
    return {
        "status": status,
        "entity_key": key,
        "entity_id": entity_id,
        "existing_entity_id": existing_id,
        "is_new": not bool(existing_id),
        "doc_id": doc_id,
        "canonical_source": row.get("canonical_source") or row.get("suggested_canonical_source") or row.get("canonical_target") or key,
        "canonical_target": row.get("canonical_target") or row.get("suggested_canonical_target") or row.get("canonical_source") or key,
        "entity_type": _entity_type(row.get("entity_type")),
        "gender": row.get("gender", None),
        "aliases_source": row.get("aliases_source", []),
        "aliases_target": row.get("aliases_target", []),
        "pronoun_policy": row.get("pronoun_policy", ""),
        "mentions": mentions,
        "reason": row.get("reason", ""),
        "confidence": row.get("confidence", 0.8),
    }


def _resolve_glossary_candidate(
    row: dict[str, Any],
    *,
    doc_id: str,
    block_by_id: dict[str, dict[str, Any]],
    existing_terms: list[dict[str, Any]],
    next_term_id: str,
    entity_spans: list[dict[str, Any]],
) -> dict[str, Any]:
    key = str(row.get("term_key") or row.get("existing_term_id") or row.get("source_term") or "").strip()
    existing_id = row.get("existing_term_id")
    known = {item.get("term_id"): item for item in existing_terms}
    if existing_id and existing_id not in known:
        existing_id = None
    if not existing_id:
        source_norm = _norm(row.get("source_term"))
        for item in existing_terms:
            if source_norm and source_norm == _norm(item.get("source_term")):
                existing_id = item.get("term_id")
                break

    term_id = str(existing_id or next_term_id)
    occurrences = []
    for raw in _ensure_list(row.get("occurrences")):
        block_id = str(raw.get("block_id") or "")
        block = block_by_id.get(block_id)
        if not block:
            occurrences.append({"status": "unresolved", "block_id": block_id, "message": "Block not found.", "raw": raw})
            continue
        resolved = _resolve_surface(block, raw)
        resolved.update({"block_id": block_id, "raw": raw})
        if resolved.get("status") == "ok":
            for entity_span in entity_spans:
                if entity_span["block_id"] == block_id and _span_intersects(resolved["span"], entity_span["span"]):
                    resolved["status"] = "conflict_dual_tag"
                    resolved["message"] = f"Glossary occurrence overlaps entity {entity_span['entity_id']}."
                    break
        occurrences.append(resolved)

    status = "ok" if occurrences and all(item.get("status") == "ok" for item in occurrences) else "needs_review"
    return {
        "status": status,
        "term_key": key,
        "term_id": term_id,
        "existing_term_id": existing_id,
        "is_new": not bool(existing_id),
        "doc_id": doc_id,
        "source_term": row.get("source_term") or key,
        "expected_target": row.get("expected_target") or row.get("suggested_expected_target") or row.get("source_term") or key,
        "allowed_variants": row.get("allowed_variants") or row.get("suggested_allowed_variants") or [],
        "forbidden_variants": row.get("forbidden_variants") or row.get("suggested_forbidden_variants") or [],
        "domain": row.get("domain", ""),
        "chapter_scope": row.get("chapter_scope", "global"),
        "glossary_status": "candidate",
        "occurrences": occurrences,
        "reason": row.get("reason", ""),
        "confidence": row.get("confidence", 0.8),
    }


def _clean_address_pair(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    pair: dict[str, Any] = {}
    if "self_term" in source:
        pair["self_term"] = source.get("self_term")
    if "address_term" in source:
        pair["address_term"] = source.get("address_term")
    return pair


def _clean_address_policy(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    policy: dict[str, Any] = {}
    for key in ("source_to_target", "target_to_source"):
        pair = _clean_address_pair(source.get(key))
        if pair:
            policy[key] = pair
    return policy


def _resolve_relation_candidate(
    row: dict[str, Any],
    *,
    doc_id: str,
    block_by_id: dict[str, dict[str, Any]],
    existing_relations: list[dict[str, Any]],
    entity_key_map: dict[str, str],
    next_relation_id: str,
) -> dict[str, Any]:
    existing_id = str(row.get("existing_relation_id") or "").strip()
    relation_by_id = {item.get("relation_id"): item for item in existing_relations}
    messages = []
    matched_existing = relation_by_id.get(existing_id) if existing_id else None
    if existing_id and matched_existing is None:
        messages.append(f"Unknown existing_relation_id: {existing_id}")

    source_ref = str(row.get("source_ref") or row.get("source_entity_id") or "").strip()
    target_ref = str(row.get("target_ref") or row.get("target_entity_id") or "").strip()
    source_id = entity_key_map.get(source_ref) if source_ref else None
    target_id = entity_key_map.get(target_ref) if target_ref else None
    if not source_ref:
        messages.append("Missing source_ref.")
    elif not source_id:
        messages.append(f"Unresolved source_ref: {source_ref}")
    if not target_ref:
        messages.append("Missing target_ref.")
    elif not target_id:
        messages.append(f"Unresolved target_ref: {target_ref}")
    if source_id and target_id and source_id == target_id:
        messages.append("source_ref and target_ref resolve to the same entity.")

    relation_type = str(row.get("relation_type") or "").strip()
    if not relation_type:
        messages.append("Missing relation_type.")

    state_label = str(row.get("state_label") or "").strip() or None
    valid_from = str(row.get("valid_from_block_id") or "").strip() or None
    valid_to = str(row.get("valid_to_block_id") or "").strip() or None
    trigger_event_id = str(row.get("trigger_event_id") or "").strip() or None
    if valid_from and str(valid_from) not in block_by_id:
        messages.append(f"valid_from_block_id not found: {valid_from}")
    if valid_to and str(valid_to) not in block_by_id:
        messages.append(f"valid_to_block_id not found: {valid_to}")

    evidence = []
    for raw in _ensure_list(row.get("evidence")):
        block_id = str(raw.get("block_id") or "")
        if not block_id or block_id not in block_by_id:
            messages.append(f"Evidence block not found: {block_id or '<missing>'}")
            continue
        item = {"block_id": block_id}
        if raw.get("surface"):
            item["surface"] = str(raw.get("surface"))
        evidence.append(item)
    if not evidence:
        messages.append("At least one evidence block is required.")

    # Guard against silent duplicate relations: if the agent did not target an
    # existing relation via existing_relation_id, but the same directed pair +
    # phase signature already exists, flag it so the candidate lands as
    # needs_review (which _accepted() refuses to apply) instead of creating a
    # duplicate edge. The human can add existing_relation_id to update the
    # existing relation, or change the phase to keep them genuinely separate.
    duplicate_of = None
    if matched_existing is None and source_id and target_id:
        phase_sig = (state_label, valid_from, valid_to)
        for existing in existing_relations:
            if (
                existing.get("source_entity_id") == source_id
                and existing.get("target_entity_id") == target_id
                and (
                    existing.get("state_label") or None,
                    existing.get("valid_from_block_id") or None,
                    existing.get("valid_to_block_id") or None,
                ) == phase_sig
            ):
                duplicate_of = existing.get("relation_id")
                break
        if duplicate_of:
            messages.append(
                f"Possible duplicate of existing relation {duplicate_of} "
                f"(same source/target/phase). Set existing_relation_id=\"{duplicate_of}\" "
                "to update it, or change the phase (state_label/valid_from/valid_to) to keep it separate."
            )

    relation_id = existing_id if matched_existing else next_relation_id
    return {
        "status": "ok" if not messages else "needs_review",
        "relation_id": relation_id,
        "existing_relation_id": existing_id or None,
        "duplicate_of": duplicate_of,
        "is_new": matched_existing is None,
        "relation_key": row.get("relation_key"),
        "doc_id": doc_id,
        "source_ref": source_ref,
        "source_entity_id": source_id,
        "target_ref": target_ref,
        "target_entity_id": target_id,
        "relation_type": relation_type,
        "state_label": state_label,
        "valid_from_block_id": valid_from,
        "valid_to_block_id": valid_to,
        "trigger_event_id": trigger_event_id,
        "address_policy": _clean_address_policy(row.get("suggested_address_policy") or row.get("address_policy")),
        "evidence": evidence,
        "notes": row.get("notes") or row.get("reason"),
        "confidence": row.get("confidence", 0.8),
        "messages": messages,
    }


def resolve_annotation_candidate(project_path: Path, doc_id: str, candidate: dict[str, Any], user: str = "local") -> dict[str, Any]:
    if not isinstance(candidate, dict):
        raise AnnotationError("invalid_candidate", "Annotation candidate JSON object is required.")
    document = _read_document(project_path)
    if document.get("doc_id") != doc_id:
        raise AnnotationError("doc_id_mismatch", "Project document doc_id does not match request.")
    if candidate.get("doc_id") and candidate.get("doc_id") != doc_id:
        raise AnnotationError("candidate_doc_id_mismatch", "Candidate doc_id does not match project doc_id.")

    chapter_id = str(candidate.get("chapter_id") or "")
    blocks = _chapter_blocks(document, chapter_id)
    block_by_id = _block_map(document)
    existing_entities = read_jsonl(_jsonl_path(project_path, "entities"))
    existing_terms = read_jsonl(_jsonl_path(project_path, "glossary"))
    existing_relations = read_jsonl(_jsonl_path(project_path, "entity_relations"))
    next_entity_id = _next_id(existing_entities, "entity_id", "e")
    next_term_id = _next_id(existing_terms, "term_id", "g")
    next_relation_id = _next_id(existing_relations, "relation_id", "rel")

    resolved_entities = []
    entity_key_map: dict[str, str] = {
        str(entity.get("entity_id")): str(entity.get("entity_id"))
        for entity in existing_entities
        if entity.get("entity_id")
    }
    next_entity_index = int(next_entity_id.split("_")[-1])
    for row in _ensure_list(candidate.get("entity_candidates")):
        entity_id = f"e_{next_entity_index:03d}"
        resolved = _resolve_entity_candidate(
            row,
            doc_id=doc_id,
            block_by_id=block_by_id,
            existing_entities=existing_entities,
            next_entity_id=entity_id,
        )
        resolved_entities.append(resolved)
        if resolved.get("entity_key"):
            entity_key_map[resolved["entity_key"]] = resolved["entity_id"]
        entity_key_map[resolved["entity_id"]] = resolved["entity_id"]
        if resolved.get("is_new"):
            next_entity_index += 1

    entity_spans = [
        {"entity_id": entity["entity_id"], "block_id": mention["block_id"], "span": mention["span"]}
        for entity in resolved_entities
        for mention in entity.get("mentions", [])
        if mention.get("status") == "ok" and mention.get("span")
    ]

    resolved_glossary = []
    next_term_index = int(next_term_id.split("_")[-1])
    for row in _ensure_list(candidate.get("glossary_candidates")):
        term_id = f"g_{next_term_index:03d}"
        resolved = _resolve_glossary_candidate(
            row,
            doc_id=doc_id,
            block_by_id=block_by_id,
            existing_terms=existing_terms,
            next_term_id=term_id,
            entity_spans=entity_spans,
        )
        resolved_glossary.append(resolved)
        if resolved.get("is_new"):
            next_term_index += 1

    resolved_discourse = []
    for row in _ensure_list(candidate.get("discourse_candidates")):
        block_id = str(row.get("block_id") or "")
        speaker_ref = row.get("speaker_ref")
        addressee_ref = row.get("addressee_ref")
        speaker_id = entity_key_map.get(str(speaker_ref)) if speaker_ref else None
        addressee_id = entity_key_map.get(str(addressee_ref)) if addressee_ref else None
        messages = []
        if block_id not in block_by_id:
            messages.append("Block not found.")
        if speaker_ref and not speaker_id:
            messages.append(f"Unresolved speaker_ref: {speaker_ref}")
        if addressee_ref and not addressee_id:
            messages.append(f"Unresolved addressee_ref: {addressee_ref}")
        resolved_discourse.append({
            "status": "ok" if not messages else "needs_review",
            "block_id": block_id,
            "speaker_ref": speaker_ref,
            "speaker_entity_id": speaker_id,
            "addressee_ref": addressee_ref,
            "addressee_entity_id": addressee_id,
            "reason": row.get("reason", ""),
            "confidence": row.get("confidence", 0.8),
            "messages": messages,
        })

    resolved_relations = []
    next_relation_index = int(next_relation_id.split("_")[-1])
    for row in _ensure_list(candidate.get("relation_candidates")):
        relation_id = f"rel_{next_relation_index:03d}"
        resolved = _resolve_relation_candidate(
            row,
            doc_id=doc_id,
            block_by_id=block_by_id,
            existing_relations=existing_relations,
            entity_key_map=entity_key_map,
            next_relation_id=relation_id,
        )
        resolved_relations.append(resolved)
        if resolved.get("is_new"):
            next_relation_index += 1

    summary = candidate.get("summary_candidate") if isinstance(candidate.get("summary_candidate"), dict) else None
    resolved_summary = None
    if summary:
        characters = []
        messages = []
        for ref in _ensure_list(summary.get("characters_present_refs")):
            entity_id = entity_key_map.get(str(ref))
            if not entity_id:
                messages.append(f"Unresolved characters_present ref: {ref}")
                continue
            entity = next((item for item in resolved_entities if item.get("entity_id") == entity_id), None)
            if entity and entity.get("entity_type") != "person":
                messages.append(f"characters_present should reference person entities only: {ref}")
                continue
            characters.append(entity_id)
        resolved_summary = {
            "status": "ok" if not messages and summary.get("summary_source") else "needs_review",
            "chapter_id": chapter_id,
            "summary_source": summary.get("summary_source", ""),
            "source": "ai_assisted_verified",
            "characters_present": characters,
            "key_events": summary.get("key_events", []),
            "setting": summary.get("setting"),
            "emotional_tone": summary.get("emotional_tone"),
            "motifs": summary.get("motifs", []),
            "summary_target": summary.get("summary_target"),
            "open_threads": summary.get("open_threads", []),
            "translation_notes": summary.get("translation_notes"),
            "confidence": summary.get("confidence", candidate.get("confidence", 0.8)),
            "messages": messages,
        }

    meta = {
        "doc_id": doc_id,
        "chapter_id": chapter_id,
        "prompt_id": ANNOTATION_PROMPT_ID,
        "candidate_hash": _stable_hash(candidate),
        "block_text_hash": _block_text_hash(blocks),
        "resolved_at": _now_iso(),
        "resolved_by": user,
    }
    result = {
        "meta": meta,
        "entities": resolved_entities,
        "glossary": resolved_glossary,
        "discourse": resolved_discourse,
        "relations": resolved_relations,
        "summary": resolved_summary,
        "warnings": candidate.get("warnings", []),
        "paths": {
            "candidate": str(_candidate_path(project_path, chapter_id).resolve()),
            "resolved": str(_resolved_path(project_path, chapter_id).resolve()),
        },
    }
    write_json_atomic(_candidate_path(project_path, chapter_id), candidate)
    write_json_atomic(_resolved_path(project_path, chapter_id), result)
    log_event(project_path, "annotation_candidate_resolved", {
        "chapter_id": chapter_id,
        "entities": len(resolved_entities),
        "glossary": len(resolved_glossary),
        "relations": len(resolved_relations),
    }, user)
    return result


def _accepted(item: dict[str, Any], accept_all: bool, decisions: dict[str, Any], key: str) -> bool:
    if item.get("status") != "ok":
        return False
    if accept_all:
        return True
    return bool(decisions.get(key))


def _validate_full_dataset(
    project_path: Path,
    document: dict[str, Any],
    glossary: list[dict[str, Any]],
    entities: list[dict[str, Any]],
    summaries: list[dict[str, Any]],
    relations: list[dict[str, Any]],
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        for key, filename in DATASET_FILES.items():
            src = _canonical(project_path) / filename
            dst = tmp_path / filename
            if src.exists():
                shutil.copyfile(src, dst)
            elif key != "manual_reference_subset":
                dst.write_text("", encoding="utf-8")
        write_json_atomic(tmp_path / DATASET_FILES["document"], document)
        write_jsonl_atomic(tmp_path / DATASET_FILES["glossary"], glossary)
        write_jsonl_atomic(tmp_path / DATASET_FILES["entities"], entities)
        write_jsonl_atomic(tmp_path / DATASET_FILES["chapter_summaries"], summaries)
        write_jsonl_atomic(tmp_path / DATASET_FILES["entity_relations"], relations)
        (tmp_path / DATASET_FILES["manual_reference_subset"]).touch(exist_ok=True)
        proc = subprocess.run(
            [
                sys.executable,
                str(VALIDATOR_SCRIPT),
                "--dataset",
                str(tmp_path),
                "--schema",
                str(SCHEMA_DIR),
                "--json",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        try:
            payload = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            payload = {"errors": [{"message": proc.stderr or proc.stdout or "Validator failed."}]}
        if proc.returncode != 0:
            raise AnnotationError(
                "validation_failed",
                "Resolved annotation candidate would fail dataset validation.",
                422,
                validation=payload,
            )


def _apply_resolved(
    project_path: Path,
    doc_id: str,
    resolved: dict[str, Any],
    accept_all: bool,
    decisions: dict[str, Any],
    user: str,
) -> dict[str, Any]:
    document = _read_document(project_path)
    block_by_id = _block_map(document)
    glossary = read_jsonl(_jsonl_path(project_path, "glossary"))
    entities = read_jsonl(_jsonl_path(project_path, "entities"))
    relations = read_jsonl(_jsonl_path(project_path, "entity_relations"))
    summaries = read_jsonl(_jsonl_path(project_path, "chapter_summaries"))
    entity_by_id = {row.get("entity_id"): row for row in entities}
    term_by_id = {row.get("term_id"): row for row in glossary}
    relation_by_id = {row.get("relation_id"): row for row in relations}
    counts = {
        "entities": 0,
        "entity_mentions": 0,
        "glossary": 0,
        "occurrences": 0,
        "discourse": 0,
        "relations": 0,
        "summary": 0,
    }

    for item in resolved.get("entities", []):
        if not _accepted(item, accept_all, decisions, f"entity:{item.get('entity_id')}"):
            continue
        mentions = [
            {"block_id": mention["block_id"], "surface": mention["surface"], "span": mention["span"]}
            for mention in item.get("mentions", [])
            if mention.get("status") == "ok"
        ]
        if not mentions:
            continue
        row = entity_by_id.get(item.get("entity_id"))
        if row is None:
            row = {
                "entity_id": item.get("entity_id"),
                "doc_id": doc_id,
                "canonical_source": item.get("canonical_source") or item.get("entity_key"),
                "canonical_target": item.get("canonical_target") or item.get("canonical_source") or item.get("entity_key"),
                "entity_type": _entity_type(item.get("entity_type")),
                "gender": item.get("gender", None),
                "aliases_source": item.get("aliases_source", []),
                "aliases_target": item.get("aliases_target", []),
                "pronoun_policy": item.get("pronoun_policy", ""),
                "mentions": [],
                "annotated_by": user,
                "confidence": item.get("confidence", 0.8),
            }
            entities.append(row)
            entity_by_id[row["entity_id"]] = row
            counts["entities"] += 1
        existing_mentions = {
            (m.get("block_id"), tuple(m.get("span", [])), m.get("surface"))
            for m in row.setdefault("mentions", [])
        }
        for mention in mentions:
            block = block_by_id.get(mention["block_id"])
            if block is None:
                raise AnnotationError("missing_block", f"Block not found at apply: {mention['block_id']}")
            start, end = mention["span"]
            if (block.get("clean_text") or "")[start:end] != mention["surface"]:
                raise AnnotationError("span_drift", f"Span drifted for entity {item.get('entity_id')}.")
            key = (mention["block_id"], tuple(mention["span"]), mention["surface"])
            if key not in existing_mentions:
                row["mentions"].append(mention)
                counts["entity_mentions"] += 1
            refs = block.setdefault("annotations", {}).setdefault("entity_mentions", [])
            if row["entity_id"] not in refs:
                refs.append(row["entity_id"])

    for item in resolved.get("glossary", []):
        if not _accepted(item, accept_all, decisions, f"glossary:{item.get('term_id')}"):
            continue
        occurrences = [
            {"block_id": occ["block_id"], "span": occ["span"]}
            for occ in item.get("occurrences", [])
            if occ.get("status") == "ok"
        ]
        if not occurrences:
            continue
        row = term_by_id.get(item.get("term_id"))
        if row is None:
            row = {
                "term_id": item.get("term_id"),
                "doc_id": doc_id,
                "source_term": item.get("source_term"),
                "expected_target": item.get("expected_target") or item.get("source_term"),
                "allowed_variants": item.get("allowed_variants", []),
                "forbidden_variants": item.get("forbidden_variants", []),
                "domain": item.get("domain", ""),
                "chapter_scope": item.get("chapter_scope", "global"),
                "status": "candidate",
                "occurrences": [],
                "annotated_by": user,
                "confidence": item.get("confidence", 0.8),
            }
            glossary.append(row)
            term_by_id[row["term_id"]] = row
            counts["glossary"] += 1
        existing_occurrences = {(occ.get("block_id"), tuple(occ.get("span", []))) for occ in row.setdefault("occurrences", [])}
        for occ in occurrences:
            block = block_by_id.get(occ["block_id"])
            if block is None:
                raise AnnotationError("missing_block", f"Block not found at apply: {occ['block_id']}")
            start, end = occ["span"]
            if (block.get("clean_text") or "")[start:end] != row["source_term"]:
                raise AnnotationError("span_drift", f"Span drifted for glossary term {item.get('term_id')}.")
            key = (occ["block_id"], tuple(occ["span"]))
            if key not in existing_occurrences:
                row["occurrences"].append(occ)
                counts["occurrences"] += 1
            refs = block.setdefault("annotations", {}).setdefault("term_occurrences", [])
            if row["term_id"] not in refs:
                refs.append(row["term_id"])

    for item in resolved.get("discourse", []):
        if not _accepted(item, accept_all, decisions, f"discourse:{item.get('block_id')}"):
            continue
        block = block_by_id.get(item.get("block_id"))
        if block is None:
            continue
        discourse = block.setdefault("discourse", {})
        if item.get("speaker_entity_id"):
            discourse["speaker_entity_id"] = item["speaker_entity_id"]
        if item.get("addressee_entity_id"):
            discourse["addressee_entity_id"] = item["addressee_entity_id"]
        counts["discourse"] += 1

    for item in resolved.get("relations", []):
        if not _accepted(item, accept_all, decisions, f"relation:{item.get('relation_id')}"):
            continue
        source_id = item.get("source_entity_id")
        target_id = item.get("target_entity_id")
        if source_id not in entity_by_id:
            raise AnnotationError("missing_entity", f"Relation source entity not found at apply: {source_id}")
        if target_id not in entity_by_id:
            raise AnnotationError("missing_entity", f"Relation target entity not found at apply: {target_id}")
        relation_id = item.get("relation_id")
        row = relation_by_id.get(relation_id)
        if row is None:
            row = {"relation_id": relation_id, "doc_id": doc_id}
            relations.append(row)
            relation_by_id[relation_id] = row
        row.update({
            "doc_id": doc_id,
            "source_entity_id": source_id,
            "target_entity_id": target_id,
            "relation_type": item.get("relation_type"),
            "evidence": item.get("evidence", []),
        })
        for key in (
            "state_label",
            "valid_from_block_id",
            "valid_to_block_id",
            "trigger_event_id",
            "notes",
        ):
            if key in item:
                row[key] = item[key]
            elif key in row:
                row.pop(key, None)
        if item.get("address_policy"):
            row["address_policy"] = item["address_policy"]
        else:
            row.pop("address_policy", None)
        if "confidence" in item:
            row["confidence"] = item["confidence"]
        counts["relations"] += 1

    summary = resolved.get("summary")
    if summary and _accepted(summary, accept_all, decisions, "summary"):
        row = next((item for item in summaries if item.get("chapter_id") == summary.get("chapter_id")), None)
        if row is None:
            row = {"doc_id": doc_id, "chapter_id": summary.get("chapter_id")}
            summaries.append(row)
        for key in (
            "summary_source",
            "source",
            "characters_present",
            "key_events",
            "setting",
            "emotional_tone",
            "motifs",
            "summary_target",
            "open_threads",
            "translation_notes",
            "confidence",
        ):
            if key in summary:
                row[key] = summary[key]
        counts["summary"] = 1

    _validate_full_dataset(project_path, document, glossary, entities, summaries, relations)
    _write_document(project_path, document)
    write_jsonl_atomic(_jsonl_path(project_path, "entities"), entities)
    write_jsonl_atomic(_jsonl_path(project_path, "glossary"), glossary)
    write_jsonl_atomic(_jsonl_path(project_path, "entity_relations"), relations)
    write_jsonl_atomic(_jsonl_path(project_path, "chapter_summaries"), summaries)
    return counts


def apply_annotation_candidate(project_path: Path, doc_id: str, payload: dict[str, Any], user: str = "local") -> dict[str, Any]:
    if not payload.get("approved"):
        raise AnnotationError("approval_required", "Applying annotation candidates requires approved=true.")
    chapter_id = str(payload.get("chapter_id") or "")
    resolved_path = _resolved_path(project_path, chapter_id)
    if not resolved_path.exists():
        raise AnnotationError("missing_resolved_candidate", "Resolve the annotation candidate before applying.", 404)
    resolved = read_json(resolved_path)
    document = _read_document(project_path)
    blocks = _chapter_blocks(document, chapter_id)
    current_hash = _block_text_hash(blocks)
    expected_hash = resolved.get("meta", {}).get("block_text_hash")
    if current_hash != expected_hash:
        raise AnnotationError(
            "annotation_drift",
            "Chapter clean_text changed after resolve. Re-run resolve before apply.",
            409,
            expected=expected_hash,
            current=current_hash,
        )
    accept_all = bool(payload.get("accept_all_resolved", True))
    decisions = payload.get("decisions") if isinstance(payload.get("decisions"), dict) else {}

    def operation() -> dict[str, Any]:
        counts = _apply_resolved(project_path, doc_id, resolved, accept_all, decisions, user)
        provenance = {
            "doc_id": doc_id,
            "chapter_id": chapter_id,
            "prompt_id": ANNOTATION_PROMPT_ID,
            "candidate_hash": resolved.get("meta", {}).get("candidate_hash"),
            "block_text_hash": expected_hash,
            "applied_at": _now_iso(),
            "applied_by": user,
            "counts": counts,
            "accept_all_resolved": accept_all,
        }
        write_json_atomic(_provenance_path(project_path, chapter_id), provenance)
        return {"chapter_id": chapter_id, "counts": counts, "provenance": provenance}

    result = record_history(
        project_path,
        action="apply_annotation_candidate",
        label=f"Apply annotation candidate for {chapter_id}",
        target={"doc_id": doc_id, "chapter_id": chapter_id},
        user=user,
        operation=operation,
    )
    log_event(project_path, "annotation_candidate_applied", {"chapter_id": chapter_id, "counts": result["counts"]}, user)
    return result
