from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from pipeline.prepass.db_source import load_document_from_connection


@dataclass(frozen=True)
class BuilderGoldReport:
    doc_id: str
    chapters: list[str]
    gold_terms_present: int
    builder_terms: int
    matched_terms: int
    agreement_terms: int
    recall: float
    agreement: float
    missing_terms: list[dict[str, str]]
    conflicts: list[dict[str, str]]
    extra_terms: list[dict[str, str]]

    def to_json_dict(self) -> dict[str, Any]:
        return asdict(self)


def score_builder_vs_gold(
    conn: sqlite3.Connection,
    *,
    doc_id: str,
    chapters: list[str],
) -> BuilderGoldReport:
    document = load_document_from_connection(conn, doc_id, chapters, translate_only=True)
    resolved_chapters = [str(chapter["chapter_id"]) for chapter in document["chapters"]]
    source_text = "\n\n".join(
        str(block.get("source_text") or block.get("clean_text") or "")
        for chapter in document["chapters"]
        for block in chapter.get("blocks") or []
    )
    gold_rows = conn.execute(
        """
        SELECT source_term, target_term
        FROM eval_glossary_gold
        WHERE doc_id = ?
        ORDER BY source_term, target_term
        """,
        (doc_id,),
    ).fetchall()
    present_gold: dict[str, dict[str, Any]] = {}
    for row in gold_rows:
        source = str(row["source_term"] or "")
        target = str(row["target_term"] or "")
        if not _count_matches(source_text, source):
            continue
        key = _normalize_source(source)
        entry = present_gold.setdefault(
            key,
            {"source_term": source, "targets": set(), "target_display": []},
        )
        normalized_target = _normalize_vi(target)
        entry["targets"].add(normalized_target)
        if target not in entry["target_display"]:
            entry["target_display"].append(target)

    builder_rows = conn.execute(
        """
        SELECT source_term, target_term, allowed_variants_json
        FROM glossary_entries
        WHERE doc_id = ?
        ORDER BY source_term
        """,
        (doc_id,),
    ).fetchall()
    builder_terms: dict[str, dict[str, Any]] = {}
    for row in builder_rows:
        source = str(row["source_term"] or "")
        target = str(row["target_term"] or "")
        variants = _json_list(row["allowed_variants_json"])
        builder_terms[_normalize_source(source)] = {
            "source_term": source,
            "target_term": target,
            "accepted_targets": {
                _normalize_vi(item)
                for item in [target, *[str(value) for value in variants]]
                if str(item).strip()
            },
        }

    conflicts: list[dict[str, str]] = []
    missing_terms: list[dict[str, str]] = []
    matched = 0
    agreed = 0
    for key, gold in sorted(present_gold.items(), key=lambda item: item[1]["source_term"].casefold()):
        builder = builder_terms.get(key)
        if builder is None:
            missing_terms.append(
                {
                    "source_term": gold["source_term"],
                    "gold_target": " | ".join(gold["target_display"]),
                }
            )
            continue
        matched += 1
        if builder["accepted_targets"] & gold["targets"]:
            agreed += 1
        else:
            conflicts.append(
                {
                    "source_term": gold["source_term"],
                    "builder_target": builder["target_term"],
                    "gold_target": " | ".join(gold["target_display"]),
                }
            )

    extra_terms = [
        {
            "source_term": item["source_term"],
            "builder_target": item["target_term"],
        }
        for key, item in sorted(
            builder_terms.items(),
            key=lambda pair: pair[1]["source_term"].casefold(),
        )
        if key not in present_gold
    ]
    gold_count = len(present_gold)
    return BuilderGoldReport(
        doc_id=doc_id,
        chapters=resolved_chapters,
        gold_terms_present=gold_count,
        builder_terms=len(builder_terms),
        matched_terms=matched,
        agreement_terms=agreed,
        recall=round(matched / gold_count, 6) if gold_count else 0.0,
        agreement=round(agreed / matched, 6) if matched else 0.0,
        missing_terms=missing_terms,
        conflicts=conflicts,
        extra_terms=extra_terms,
    )


def write_builder_vs_gold_report(
    db_path: str | Path,
    *,
    doc_id: str,
    chapters: list[str],
    out_path: str | Path,
) -> BuilderGoldReport:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        report = score_builder_vs_gold(conn, doc_id=doc_id, chapters=chapters)
    finally:
        conn.close()
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report.to_json_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


def _count_matches(text: str, needle: str) -> int:
    normalized_text = _normalize_source(text)
    normalized_needle = _normalize_source(needle)
    if not normalized_needle:
        return 0
    pattern = rf"(?<!\w){re.escape(normalized_needle)}(?!\w)"
    return len(re.findall(pattern, normalized_text, flags=re.UNICODE))


def _normalize_source(text: str) -> str:
    return (
        unicodedata.normalize("NFC", str(text))
        .replace("’", "'")
        .replace("‘", "'")
        .replace("`", "'")
        .casefold()
    )


def _normalize_vi(text: str) -> str:
    value = re.sub(r"\s+", " ", str(text).strip()).casefold()
    value = value.replace("đ", "d")
    decomposed = unicodedata.normalize("NFD", value)
    without_marks = "".join(
        char for char in decomposed if unicodedata.category(char) != "Mn"
    )
    return unicodedata.normalize("NFC", without_marks)


def _json_list(value: Any) -> list[Any]:
    try:
        parsed = json.loads(str(value or "[]"))
    except Exception:
        return []
    return parsed if isinstance(parsed, list) else []
