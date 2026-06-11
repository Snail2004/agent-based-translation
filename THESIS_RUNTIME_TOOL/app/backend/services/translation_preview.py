from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import DATASET_FILES
from services.dataset_io import flatten_document, read_json, read_jsonl, write_json_atomic
from services.workspace import ensure_working_files


TRANSLATION_PREVIEW_KIND = "translation_preview"
DEFAULT_SKILL_VERSION = "dataset-translation-preview@1"


class TranslationPreviewError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400, **details: Any):
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _canonical(project_path: Path) -> Path:
    return project_path / "canonical"


def _document_path(project_path: Path) -> Path:
    return _canonical(project_path) / DATASET_FILES["document"]


def _jsonl_path(project_path: Path, key: str) -> Path:
    return _canonical(project_path) / DATASET_FILES[key]


def _preview_dir(project_path: Path) -> Path:
    return project_path / "working" / "translation_preview"


def _runs_dir(project_path: Path) -> Path:
    return _preview_dir(project_path) / "runs"


def _input_dir(project_path: Path) -> Path:
    return _preview_dir(project_path) / "input"


def _agent_outputs_dir(project_path: Path) -> Path:
    return _preview_dir(project_path) / "agent_outputs"


def _index_path(project_path: Path) -> Path:
    return _preview_dir(project_path) / "index.json"


def _input_path(project_path: Path, chapter_id: str) -> Path:
    safe = _safe_id(chapter_id) or "chapter"
    return _input_dir(project_path) / f"{safe}_input.json"


def _agent_output_path(project_path: Path, chapter_id: str) -> Path:
    safe = _safe_id(chapter_id) or "chapter"
    return _agent_outputs_dir(project_path) / f"{safe}_preview.json"


def _run_path(project_path: Path, run_id: str) -> Path:
    safe = _safe_id(run_id)
    if safe != run_id:
        raise TranslationPreviewError("invalid_run_id", "run_id may only contain letters, numbers, dot, dash, or underscore.")
    return _runs_dir(project_path) / f"{safe}.json"


def _read_document(project_path: Path) -> dict[str, Any]:
    path = _document_path(project_path)
    if not path.exists():
        raise TranslationPreviewError("missing_document", "Project has not been extracted yet.", 404)
    return read_json(path)


def _read_index(project_path: Path) -> dict[str, Any]:
    path = _index_path(project_path)
    if not path.exists():
        return {"version": 1, "runs": []}
    index = read_json(path)
    if not isinstance(index, dict):
        return {"version": 1, "runs": []}
    runs = index.get("runs")
    if not isinstance(runs, list):
        index["runs"] = []
    index.setdefault("version", 1)
    return index


def _write_index(project_path: Path, index: dict[str, Any]) -> None:
    write_json_atomic(_index_path(project_path), index)


def _safe_id(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip())
    return safe.strip("_")


def _chapter_short(doc_id: str, chapter_id: str) -> str:
    value = str(chapter_id or "")
    prefix = f"{doc_id}_"
    if value.startswith(prefix):
        value = value[len(prefix):]
    return _safe_id(value) or "chapter"


def _next_run_id(project_path: Path, doc_id: str, chapter_id: str) -> str:
    index = _read_index(project_path)
    used = {str(row.get("run_id")) for row in index.get("runs", []) if row.get("run_id")}
    short = _chapter_short(doc_id, chapter_id)
    number = 1
    while True:
        run_id = f"tpreview_{short}_{number:03d}"
        if run_id not in used and not _run_path(project_path, run_id).exists():
            return run_id
        number += 1


def _context_ids(project_path: Path, document: dict[str, Any]) -> set[str]:
    chapter_ids = {
        str(chapter.get("chapter_id"))
        for chapter in document.get("chapters", [])
        if chapter.get("chapter_id")
    }
    entity_ids = {
        str(row.get("entity_id"))
        for row in read_jsonl(_jsonl_path(project_path, "entities"))
        if row.get("entity_id")
    }
    term_ids = {
        str(row.get("term_id"))
        for row in read_jsonl(_jsonl_path(project_path, "glossary"))
        if row.get("term_id")
    }
    relation_ids = {
        str(row.get("relation_id"))
        for row in read_jsonl(_jsonl_path(project_path, "entity_relations"))
        if row.get("relation_id")
    }
    return chapter_ids | entity_ids | term_ids | relation_ids


def _relation_ids(project_path: Path) -> set[str]:
    return {
        str(row.get("relation_id"))
        for row in read_jsonl(_jsonl_path(project_path, "entity_relations"))
        if row.get("relation_id")
    }


def _list_value(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _clean_dict(item: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item.items() if value is not None}


def translation_input_paths(project_path: Path, chapter_id: str) -> dict[str, str]:
    return {
        "project_root": str(project_path.resolve()),
        "input": str(_input_path(project_path, chapter_id).resolve()),
        "preview_output_suggested": str(_agent_output_path(project_path, chapter_id).resolve()),
        "runs_dir": str(_runs_dir(project_path).resolve()),
    }


def build_translation_input(project_path: Path, doc_id: str, chapter_id: str) -> dict[str, Any]:
    ensure_working_files(project_path)
    document = _read_document(project_path)
    if document.get("doc_id") != doc_id:
        raise TranslationPreviewError("doc_id_mismatch", "Project document doc_id does not match request.")

    chapters, blocks = flatten_document(document)
    chapter_ids = {str(chapter.get("chapter_id")) for chapter in chapters if chapter.get("chapter_id")}
    if chapter_id not in chapter_ids:
        raise TranslationPreviewError("missing_chapter", f"Chapter not found: {chapter_id}", 404)

    bundle_blocks: list[dict[str, Any]] = []
    for block in blocks:
        if str(block.get("chapter_id")) != chapter_id:
            continue
        discourse = block.get("discourse") if isinstance(block.get("discourse"), dict) else {}
        clean_discourse = _clean_dict({
            "speaker_entity_id": _optional_text(discourse.get("speaker_entity_id")),
            "addressee_entity_id": _optional_text(discourse.get("addressee_entity_id")),
        })
        item = {
            "block_id": block.get("block_id"),
            "block_type": block.get("block_type"),
            "clean_text": block.get("clean_text") or "",
        }
        if clean_discourse:
            item["discourse"] = clean_discourse
        bundle_blocks.append(item)

    known_entities = [
        _clean_dict({
            "entity_id": row.get("entity_id"),
            "canonical_source": row.get("canonical_source") or "",
            "canonical_target": row.get("canonical_target") or "",
            "aliases_target": _list_value(row.get("aliases_target")),
            "pronoun_policy": row.get("pronoun_policy") or "",
        })
        for row in read_jsonl(_jsonl_path(project_path, "entities"))
        if row.get("entity_id")
    ]

    known_terms = [
        _clean_dict({
            "term_id": row.get("term_id"),
            "source_term": row.get("source_term") or "",
            "expected_target": row.get("expected_target") or "",
            "allowed_variants": _list_value(row.get("allowed_variants")),
            "forbidden_variants": _list_value(row.get("forbidden_variants")),
        })
        for row in read_jsonl(_jsonl_path(project_path, "glossary"))
        if row.get("term_id")
    ]

    known_relations = [
        {
            "relation_id": row.get("relation_id"),
            "source_entity_id": row.get("source_entity_id"),
            "target_entity_id": row.get("target_entity_id"),
            "relation_type": row.get("relation_type") or "",
            "address_policy": row.get("address_policy") if isinstance(row.get("address_policy"), dict) else {},
            "state_label": row.get("state_label"),
            "valid_from_block_id": row.get("valid_from_block_id"),
            "valid_to_block_id": row.get("valid_to_block_id"),
        }
        for row in read_jsonl(_jsonl_path(project_path, "entity_relations"))
        if row.get("relation_id")
    ]

    chapter_summary: dict[str, Any] = {
        "summary_source": "",
        "emotional_tone": "",
        "motifs": [],
        "key_events": [],
        "open_threads": [],
    }
    for row in read_jsonl(_jsonl_path(project_path, "chapter_summaries")):
        if row.get("chapter_id") == chapter_id:
            chapter_summary = _clean_dict({
                "summary_source": row.get("summary_source") or "",
                "emotional_tone": row.get("emotional_tone") or "",
                "motifs": _list_value(row.get("motifs")),
                "key_events": _list_value(row.get("key_events")),
                "open_threads": _list_value(row.get("open_threads")),
                "setting": row.get("setting"),
                "characters_present": _list_value(row.get("characters_present")),
                "translation_notes": row.get("translation_notes"),
            })
            break

    payload = {
        "doc_id": doc_id,
        "chapter_id": chapter_id,
        "blocks": bundle_blocks,
        "known_entities": known_entities,
        "known_terms": known_terms,
        "known_relations": known_relations,
        "chapter_summary": chapter_summary,
        "paths": translation_input_paths(project_path, chapter_id),
    }
    _input_dir(project_path).mkdir(parents=True, exist_ok=True)
    _agent_outputs_dir(project_path).mkdir(parents=True, exist_ok=True)
    write_json_atomic(_input_path(project_path, chapter_id), payload)
    return payload


def load_translation_input(project_path: Path, doc_id: str, chapter_id: str) -> dict[str, Any]:
    document = _read_document(project_path)
    if document.get("doc_id") != doc_id:
        raise TranslationPreviewError("doc_id_mismatch", "Project document doc_id does not match request.")

    chapters, _blocks = flatten_document(document)
    chapter_ids = {str(chapter.get("chapter_id")) for chapter in chapters if chapter.get("chapter_id")}
    if chapter_id not in chapter_ids:
        raise TranslationPreviewError("missing_chapter", f"Chapter not found: {chapter_id}", 404)

    path = _input_path(project_path, chapter_id)
    if not path.exists():
        raise TranslationPreviewError(
            "missing_translation_preview_input",
            f"No saved translation preview input found for {chapter_id}. Build input first.",
            404,
            path=str(path.resolve()),
        )
    payload = read_json(path)
    if payload.get("doc_id") != doc_id or payload.get("chapter_id") != chapter_id:
        raise TranslationPreviewError(
            "stale_translation_preview_input",
            "Saved translation preview input does not match the current project/chapter. Rebuild input.",
            409,
            path=str(path.resolve()),
        )
    payload["paths"] = {**translation_input_paths(project_path, chapter_id), **(payload.get("paths") or {})}
    return payload


def _warning(code: str, message: str, **extra: Any) -> dict[str, Any]:
    item = {"code": code, "message": message}
    item.update(extra)
    return item


def _normalise_preview(project_path: Path, doc_id: str, preview: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not isinstance(preview, dict):
        raise TranslationPreviewError("invalid_preview", "Translation preview JSON object is required.")

    document = _read_document(project_path)
    if document.get("doc_id") != doc_id:
        raise TranslationPreviewError("doc_id_mismatch", "Project document doc_id does not match request.")
    if preview.get("doc_id") != doc_id:
        raise TranslationPreviewError("preview_doc_id_mismatch", "Preview doc_id does not match project doc_id.")

    chapter_id = str(preview.get("chapter_id") or "")
    chapters, blocks = flatten_document(document)
    chapter_ids = {str(chapter.get("chapter_id")) for chapter in chapters if chapter.get("chapter_id")}
    if chapter_id not in chapter_ids:
        raise TranslationPreviewError("missing_chapter", f"Chapter not found: {chapter_id}", 404)

    block_chapter = {
        str(block.get("block_id")): str(block.get("chapter_id"))
        for block in blocks
        if block.get("block_id")
    }
    allowed_context = _context_ids(project_path, document)
    allowed_relations = _relation_ids(project_path)
    warnings: list[dict[str, Any]] = []

    preview_blocks = preview.get("blocks")
    if not isinstance(preview_blocks, list):
        raise TranslationPreviewError("invalid_blocks", "Preview blocks must be a list.")

    normalised_blocks: list[dict[str, Any]] = []
    for index, raw in enumerate(preview_blocks):
        if not isinstance(raw, dict):
            raise TranslationPreviewError("invalid_block", "Each preview block must be an object.", location=f"blocks[{index}]")
        block_id = str(raw.get("block_id") or "")
        if block_id not in block_chapter:
            raise TranslationPreviewError("unknown_block_id", f"Preview block_id not found: {block_id}", block_id=block_id)
        if block_chapter[block_id] != chapter_id:
            raise TranslationPreviewError(
                "block_chapter_mismatch",
                f"Preview block_id does not belong to chapter {chapter_id}: {block_id}",
                block_id=block_id,
            )

        used_context = raw.get("used_context") if isinstance(raw.get("used_context"), list) else []
        clean_used_context: list[str] = []
        for context_id in used_context:
            context_id = str(context_id)
            clean_used_context.append(context_id)
            if context_id not in allowed_context:
                warnings.append(_warning(
                    "unknown_used_context",
                    f"used_context id is not known in this dataset: {context_id}",
                    block_id=block_id,
                    context_id=context_id,
                ))

        address = raw.get("address_applied") if isinstance(raw.get("address_applied"), dict) else None
        clean_address = address
        if address and address.get("relation_id"):
            relation_id = str(address.get("relation_id"))
            if relation_id not in allowed_relations:
                warnings.append(_warning(
                    "unknown_relation_id",
                    f"address_applied relation_id is not known in this dataset: {relation_id}",
                    block_id=block_id,
                    relation_id=relation_id,
                ))

        normalised_blocks.append({
            "block_id": block_id,
            "target_text": str(raw.get("target_text") or ""),
            "mentions": raw.get("mentions") if isinstance(raw.get("mentions"), list) else [],
            "address_applied": clean_address,
            "used_context": clean_used_context,
            "notes": str(raw.get("notes") or ""),
        })

    run_id = str(preview.get("run_id") or "").strip() or _next_run_id(project_path, doc_id, chapter_id)
    run_id = _safe_id(run_id)
    if not run_id:
        raise TranslationPreviewError("invalid_run_id", "Unable to create a run_id for translation preview.")

    run = {
        "run_id": run_id,
        "doc_id": doc_id,
        "chapter_id": chapter_id,
        "kind": TRANSLATION_PREVIEW_KIND,
        "model": preview.get("model"),
        "skill_version": preview.get("skill_version") or DEFAULT_SKILL_VERSION,
        "prompt_version": preview.get("prompt_version"),
        "created_at": preview.get("created_at") or _now_iso(),
        "blocks": normalised_blocks,
        "warnings": warnings,
    }
    return run, warnings


def _index_entry(run: dict[str, Any]) -> dict[str, Any]:
    mention_count = sum(len(block.get("mentions") or []) for block in run.get("blocks", []))
    used_context_count = sum(len(block.get("used_context") or []) for block in run.get("blocks", []))
    return {
        "run_id": run.get("run_id"),
        "doc_id": run.get("doc_id"),
        "chapter_id": run.get("chapter_id"),
        "created_at": run.get("created_at"),
        "model": run.get("model"),
        "skill_version": run.get("skill_version"),
        "prompt_version": run.get("prompt_version"),
        "block_count": len(run.get("blocks", [])),
        "mention_count": mention_count,
        "used_context_count": used_context_count,
        "warning_count": len(run.get("warnings", [])),
    }


def import_translation_preview(project_path: Path, doc_id: str, preview: dict[str, Any]) -> dict[str, Any]:
    ensure_working_files(project_path)
    run, warnings = _normalise_preview(project_path, doc_id, preview)

    runs_dir = _runs_dir(project_path)
    runs_dir.mkdir(parents=True, exist_ok=True)
    write_json_atomic(_run_path(project_path, run["run_id"]), run)

    index = _read_index(project_path)
    entry = _index_entry(run)
    rows = [row for row in index.get("runs", []) if row.get("run_id") != run["run_id"]]
    rows.append(entry)
    rows.sort(key=lambda row: str(row.get("created_at") or ""))
    index["runs"] = rows
    _write_index(project_path, index)

    return {
        "run": run,
        "index_entry": entry,
        "warnings": warnings,
        "paths": {
            "run": str(_run_path(project_path, run["run_id"]).resolve()),
            "index": str(_index_path(project_path).resolve()),
        },
    }


def import_agent_translation_preview(project_path: Path, doc_id: str, chapter_id: str) -> dict[str, Any]:
    path = _agent_output_path(project_path, chapter_id)
    if not path.exists():
        raise TranslationPreviewError(
            "missing_agent_translation_preview",
            f"No agent preview output found for {chapter_id}.",
            404,
            path=str(path.resolve()),
        )
    preview = read_json(path)
    if not isinstance(preview, dict):
        raise TranslationPreviewError("invalid_agent_translation_preview", "Agent preview output must be one JSON object.", 400, path=str(path.resolve()))
    result = import_translation_preview(project_path, doc_id, preview)
    result["paths"] = {
        **result.get("paths", {}),
        "agent_output": str(path.resolve()),
    }
    return result


def list_translation_previews(project_path: Path) -> dict[str, Any]:
    index = _read_index(project_path)
    return {
        "runs": index.get("runs", []),
        "paths": {
            "index": str(_index_path(project_path).resolve()),
            "runs_dir": str(_runs_dir(project_path).resolve()),
        },
    }


def load_translation_preview(project_path: Path, run_id: str) -> dict[str, Any]:
    path = _run_path(project_path, run_id)
    if not path.exists():
        raise TranslationPreviewError("missing_translation_preview", f"Translation preview run not found: {run_id}", 404)
    return {
        "run": read_json(path),
        "path": str(path.resolve()),
    }
