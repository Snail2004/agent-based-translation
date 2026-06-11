import json
import os
from pathlib import Path
from typing import Any

from config import DATASET_FILES


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    with tmp.open("wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def write_jsonl_atomic(path: Path, rows: list[dict[str, Any]]) -> None:
    text = "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows)
    atomic_write_text(path, text)


def flatten_document(document: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    chapters: list[dict[str, Any]] = []
    blocks: list[dict[str, Any]] = []

    for chapter in document.get("chapters", []):
        chapter_blocks = chapter.get("blocks", [])
        chapter_id = chapter.get("chapter_id")
        chapters.append({
            "chapter_id": chapter_id,
            "order_index": chapter.get("order_index"),
            "title": chapter.get("title"),
            "block_count": len(chapter_blocks),
        })
        for block in chapter_blocks:
            item = dict(block)
            item["chapter_id"] = chapter_id
            item["chapter_title"] = chapter.get("title")
            blocks.append(item)

    return chapters, blocks


def default_review_state(document: dict[str, Any]) -> dict[str, Any]:
    _, blocks = flatten_document(document)
    return {
        "version": 1,
        "blocks": {
            block["block_id"]: {
                "reviewed": False,
                "reviewed_by": None,
                "reviewed_at": None,
                "needs_retag": False,
            }
            for block in blocks
            if block.get("block_id")
        },
        "references": {},
        "summaries": {},
    }


def read_review_state(project_path: Path, document: dict[str, Any]) -> dict[str, Any]:
    path = project_path / "working" / "review_state.json"
    if not path.exists():
        return default_review_state(document)
    return read_json(path)


def read_reference_drafts(project_path: Path) -> dict[str, Any]:
    path = project_path / "working" / "drafts.json"
    if not path.exists():
        return {"references": {}}
    return read_json(path)


def read_jobs(project_path: Path) -> list[dict[str, Any]]:
    jobs_dir = project_path / "working" / "jobs"
    if not jobs_dir.exists():
        return []
    jobs = []
    for path in sorted(jobs_dir.glob("*.json")):
        try:
            jobs.append(read_json(path))
        except (OSError, json.JSONDecodeError):
            continue
    return jobs


def read_dataset(project_path: Path) -> dict[str, Any]:
    from services.history import history_state

    canonical = project_path / "canonical"
    document = read_json(canonical / DATASET_FILES["document"])
    chapters, blocks = flatten_document(document)

    return {
        "document": document,
        "chapters": chapters,
        "blocks": blocks,
        "glossary": read_jsonl(canonical / DATASET_FILES["glossary"]),
        "entities": read_jsonl(canonical / DATASET_FILES["entities"]),
        "entity_relations": read_jsonl(canonical / DATASET_FILES["entity_relations"]),
        "summaries": read_jsonl(canonical / DATASET_FILES["chapter_summaries"]),
        "references": read_jsonl(canonical / DATASET_FILES["manual_reference_subset"]),
        "reference_drafts": read_reference_drafts(project_path),
        "jobs": read_jobs(project_path),
        "review_state": read_review_state(project_path, document),
        "history_state": history_state(project_path),
    }
