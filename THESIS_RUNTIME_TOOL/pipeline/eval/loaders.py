from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class OracleConsistencyInput:
    project: str
    terms: dict[str, dict[str, Any]]
    entities: dict[str, dict[str, Any]]
    term_occurrences_by_block: dict[str, list[Any]]
    entity_mentions_by_block: dict[str, list[Any]]
    translations_by_block: dict[str, str]
    block_chapters: dict[str, str]


def load_oracle_project(project_path: str | Path) -> OracleConsistencyInput:
    """Read an AI-LAB oracle project without modifying any source files."""

    root = Path(project_path)
    canonical = root / "canonical"
    document_path = canonical / "document.json"
    terms = _load_jsonl_by_id(canonical / "glossary.jsonl", "term_id")
    entities = _load_jsonl_by_id(canonical / "entities.jsonl", "entity_id")
    document = json.loads(document_path.read_text(encoding="utf-8"))

    term_occurrences_by_block: dict[str, list[Any]] = {}
    entity_mentions_by_block: dict[str, list[Any]] = {}
    block_chapters: dict[str, str] = {}
    for chapter in document.get("chapters") or []:
        chapter_label = _chapter_label(chapter)
        for block in chapter.get("blocks") or []:
            block_id = str(block["block_id"])
            annotations = block.get("annotations") or {}
            block_chapters[block_id] = chapter_label
            term_occurrences_by_block[block_id] = list(
                annotations.get("term_occurrences") or []
            )
            entity_mentions_by_block[block_id] = list(
                annotations.get("entity_mentions") or []
            )

    translations_by_block = _load_preview_translations(
        root / "working" / "translation_preview" / "agent_outputs"
    )
    return OracleConsistencyInput(
        project=str(document.get("doc_id") or root.name),
        terms=terms,
        entities=entities,
        term_occurrences_by_block=term_occurrences_by_block,
        entity_mentions_by_block=entity_mentions_by_block,
        translations_by_block=translations_by_block,
        block_chapters=block_chapters,
    )


def _load_jsonl_by_id(path: Path, key: str) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        records[str(record[key])] = record
    return records


def _load_preview_translations(path: Path) -> dict[str, str]:
    translations: dict[str, str] = {}
    for preview_path in sorted(path.glob("*_preview.json")):
        data = json.loads(preview_path.read_text(encoding="utf-8"))
        for block in data.get("blocks") or []:
            translations[str(block["block_id"])] = str(block.get("target_text") or "")
    return translations


def _chapter_label(chapter: dict[str, Any]) -> str:
    chapter_id = str(chapter.get("chapter_id") or "")
    match = re.search(r"_ch(\d+)$", chapter_id)
    if match:
        return f"ch{int(match.group(1)):02d}"
    order = chapter.get("order_index")
    if isinstance(order, int):
        return f"ch{order + 1:02d}"
    return chapter_id or "unknown"
