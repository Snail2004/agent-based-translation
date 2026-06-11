import csv
import io
from pathlib import Path
from typing import Any

from config import DATASET_FILES
from services.audit_log import log_event
from services.dataset_io import atomic_write_text, read_json, read_jsonl, write_json_atomic, write_jsonl_atomic
from services.mutations import MutationError, _find_block


REFERENCE_SOURCES = {"human", "ai_assisted_verified"}
REFERENCE_STATUSES = {"reviewed", "locked"}
CSV_FIELDS = [
    "reference_id",
    "block_id",
    "source_text",
    "draft_vi",
    "final_reference_vi",
    "method",
    "ai_model",
    "prompt_id",
    "translated_by",
    "reviewed_by",
    "status",
    "notes",
]


def _document_path(project_path: Path) -> Path:
    return project_path / "canonical" / DATASET_FILES["document"]


def _reference_path(project_path: Path) -> Path:
    return project_path / "canonical" / DATASET_FILES["manual_reference_subset"]


def _drafts_path(project_path: Path) -> Path:
    return project_path / "working" / "drafts.json"


def _read_document(project_path: Path) -> dict[str, Any]:
    return read_json(_document_path(project_path))


def _read_drafts(project_path: Path) -> dict[str, Any]:
    path = _drafts_path(project_path)
    if not path.exists():
        return {"references": {}}
    data = read_json(path)
    data.setdefault("references", {})
    return data


def _write_drafts(project_path: Path, drafts: dict[str, Any]) -> None:
    write_json_atomic(_drafts_path(project_path), drafts)
    write_review_csv(project_path, drafts)


def _next_ref_id(rows: list[dict[str, Any]], drafts: dict[str, Any]) -> str:
    used = {str(row.get("reference_id")) for row in rows}
    used.update(drafts.get("references", {}).keys())
    index = 1
    while True:
        candidate = f"ref_{index:03d}"
        if candidate not in used:
            return candidate
        index += 1


def _default_stratum(block: dict[str, Any]) -> str:
    if block.get("is_chapter_opening"):
        return "chapter_opening"
    if block.get("block_type") == "dialogue":
        return "dialogue"
    if (block.get("annotations") or {}).get("term_occurrences"):
        return "term_heavy"
    return "narrative"


def write_review_csv(project_path: Path, drafts: dict[str, Any] | None = None) -> None:
    drafts = drafts or _read_drafts(project_path)
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    for row in drafts.get("references", {}).values():
        writer.writerow({
            "reference_id": row.get("reference_id", ""),
            "block_id": row.get("block_id", ""),
            "source_text": row.get("source_text", ""),
            "draft_vi": row.get("draft_vi", row.get("reference_vi", "")),
            "final_reference_vi": row.get("reference_vi", "") if row.get("status") in REFERENCE_STATUSES else "",
            "method": row.get("source", ""),
            "ai_model": row.get("ai_model", ""),
            "prompt_id": row.get("prompt_id", ""),
            "translated_by": row.get("translated_by", ""),
            "reviewed_by": row.get("reviewed_by", ""),
            "status": row.get("status", "draft"),
            "notes": row.get("notes", ""),
        })
    atomic_write_text(project_path / "working" / "translation_review_log.csv", out.getvalue())


def save_reference_draft(project_path: Path, payload: dict[str, Any], user: str = "local") -> dict[str, Any]:
    document = _read_document(project_path)
    block_id = str(payload.get("block_id") or "")
    _, block = _find_block(document, block_id)
    refs = read_jsonl(_reference_path(project_path))
    drafts = _read_drafts(project_path)
    reference_id = payload.get("reference_id") or block.get("reference_translation_id") or _next_ref_id(refs, drafts)
    source_text = payload.get("source_text") or block.get("source_text") or block.get("clean_text")
    draft = drafts["references"].get(reference_id, {})
    draft.update({
        "reference_id": reference_id,
        "doc_id": document.get("doc_id"),
        "block_id": block_id,
        "source_text": source_text,
        "draft_vi": payload.get("draft_vi", payload.get("reference_vi", draft.get("draft_vi", ""))),
        "reference_vi": payload.get("reference_vi", draft.get("reference_vi", "")),
        "source": payload.get("source", draft.get("source", "")),
        "status": "draft",
        "stratum": payload.get("stratum", draft.get("stratum", _default_stratum(block))),
        "translated_by": payload.get("translated_by", draft.get("translated_by", user)),
        "reviewed_by": payload.get("reviewed_by", draft.get("reviewed_by", "")),
        "ai_model": payload.get("ai_model", draft.get("ai_model")),
        "prompt_id": payload.get("prompt_id", draft.get("prompt_id")),
        "ai_used_at": payload.get("ai_used_at", draft.get("ai_used_at")),
        "notes": payload.get("notes", draft.get("notes")),
    })
    drafts["references"][reference_id] = draft
    _write_drafts(project_path, drafts)
    log_event(project_path, "reference_draft", {"reference_id": reference_id, "block_id": block_id}, user)
    return draft


def _canonical_reference_row(project_path: Path, reference_id: str) -> dict[str, Any] | None:
    for row in read_jsonl(_reference_path(project_path)):
        if row.get("reference_id") == reference_id:
            return row
    return None


def _write_canonical_reference(project_path: Path, row: dict[str, Any]) -> None:
    rows = read_jsonl(_reference_path(project_path))
    for idx, existing in enumerate(rows):
        if existing.get("reference_id") == row.get("reference_id"):
            rows[idx] = row
            break
    else:
        rows.append(row)
    write_jsonl_atomic(_reference_path(project_path), rows)


def _set_block_reference(project_path: Path, block_id: str, reference_id: str) -> None:
    document = _read_document(project_path)
    _, block = _find_block(document, block_id)
    block["reference_translation_id"] = reference_id
    write_json_atomic(_document_path(project_path), document)


def review_reference(project_path: Path, reference_id: str, payload: dict[str, Any], user: str = "local") -> dict[str, Any]:
    drafts = _read_drafts(project_path)
    draft = dict(drafts.get("references", {}).get(reference_id) or _canonical_reference_row(project_path, reference_id) or {})
    if not draft:
        raise MutationError("missing_reference", f"Reference not found: {reference_id}", 404)
    draft.update({k: v for k, v in payload.items() if k != "user"})
    reference_vi = draft.get("reference_vi") or draft.get("draft_vi")
    source = draft.get("source")
    if not reference_vi:
        raise MutationError("missing_reference_vi", "reference_vi is required before review.")
    if source not in REFERENCE_SOURCES:
        raise MutationError("invalid_reference_source", "source must be human or ai_assisted_verified.")
    if draft.get("ai_model") and source != "ai_assisted_verified":
        raise MutationError("ai_source_mismatch", "AI-touched references must use source=ai_assisted_verified.")
    draft["reference_vi"] = reference_vi
    draft["status"] = "reviewed"
    draft["reviewed_by"] = draft.get("reviewed_by") or payload.get("reviewed_by") or user
    draft["translated_by"] = draft.get("translated_by") or user
    row = {
        key: draft.get(key)
        for key in (
            "reference_id",
            "doc_id",
            "block_id",
            "source_text",
            "reference_vi",
            "source",
            "status",
            "stratum",
            "translated_by",
            "reviewed_by",
            "ai_model",
            "prompt_id",
            "ai_used_at",
            "notes",
        )
        if draft.get(key) is not None
    }
    _write_canonical_reference(project_path, row)
    _set_block_reference(project_path, row["block_id"], reference_id)
    drafts.setdefault("references", {})[reference_id] = draft
    _write_drafts(project_path, drafts)
    log_event(project_path, "reference_review", {"reference_id": reference_id}, user)
    return row


def lock_reference(project_path: Path, reference_id: str, user: str = "local") -> dict[str, Any]:
    rows = read_jsonl(_reference_path(project_path))
    for row in rows:
        if row.get("reference_id") == reference_id:
            if row.get("status") != "reviewed":
                raise MutationError("reference_not_reviewed", "Only reviewed references can be locked.")
            row["status"] = "locked"
            write_jsonl_atomic(_reference_path(project_path), rows)
            drafts = _read_drafts(project_path)
            if reference_id in drafts.get("references", {}):
                drafts["references"][reference_id]["status"] = "locked"
                _write_drafts(project_path, drafts)
            log_event(project_path, "reference_lock", {"reference_id": reference_id}, user)
            return row
    raise MutationError("missing_reference", f"Reference not found: {reference_id}", 404)


def draft_references(project_path: Path) -> list[dict[str, Any]]:
    drafts = _read_drafts(project_path)
    return [
        row for row in drafts.get("references", {}).values()
        if row.get("status", "draft") == "draft"
    ]
