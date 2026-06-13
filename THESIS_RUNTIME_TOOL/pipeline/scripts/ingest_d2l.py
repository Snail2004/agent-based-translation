from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from pipeline.ingest.d2l_glossary import parse_glossary, store_glossary_gold
from pipeline.ingest.d2l_markdown_loader import DOC_ID, load_d2l_markdown
from pipeline.memory.store_init import init_db


@dataclass(frozen=True)
class ChapterCoverage:
    chapter_id: str
    chapter_slug: str
    order_index: int
    total_blocks: int
    prose_blocks: int
    prose_tokens_estimate: int
    glossary_terms_total: int
    terms_present_in_chapter: int
    term_occurrences_total: int
    term_density_per_1k_tokens: float
    has_agent_term: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest D2L EN origin markdown + eval-only glossary gold.")
    parser.add_argument("--src", required=True, help="Path to data/sources/d2l-vi.")
    parser.add_argument("--db", required=True, help="Output runtime memory.sqlite3 path.")
    parser.add_argument("--out", required=True, help="Coverage report JSON path.")
    parser.add_argument("--commit", help="Pinned D2L source commit. Defaults to git rev-parse.")
    args = parser.parse_args()

    source_root = Path(args.src)
    db_path = Path(args.db)
    out_path = Path(args.out)
    source_commit = args.commit or _git_commit(source_root)

    conn = init_db(db_path)
    try:
        markdown_report = load_d2l_markdown(conn, source_root, source_commit=source_commit)
        glossary_path = source_root / "glossary.md"
        glossary_entries = parse_glossary(glossary_path)
        glossary_count = store_glossary_gold(
            conn,
            DOC_ID,
            glossary_entries,
            source_path=glossary_path.relative_to(source_root),
            source_commit=source_commit,
        )
        coverage = build_coverage(conn, DOC_ID, glossary_entries)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    report = {
        "doc_id": DOC_ID,
        "source_root": str(source_root),
        "source_commit": source_commit,
        "markdown": markdown_report.to_json_dict(),
        "glossary_gold_entries": glossary_count,
        "coverage": [item.to_dict() for item in coverage],
        "top_chapters_by_density": [
            item.to_dict()
            for item in sorted(
                coverage,
                key=lambda value: (
                    -value.term_density_per_1k_tokens,
                    -value.term_occurrences_total,
                    value.order_index,
                ),
            )[:10]
        ],
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({
        "doc_id": DOC_ID,
        "chapters": markdown_report.chapters,
        "loaded_chapters": markdown_report.loaded_chapters,
        "sections": markdown_report.sections,
        "blocks": markdown_report.blocks,
        "prose_blocks": markdown_report.prose_blocks,
        "glossary_gold_entries": glossary_count,
        "report": str(out_path),
    }, ensure_ascii=False, indent=2))
    print("\nTop chapters by glossary term density:")
    for item in report["top_chapters_by_density"][:10]:
        print(
            f"{item['chapter_id']}: density={item['term_density_per_1k_tokens']:.2f} "
            f"occ={item['term_occurrences_total']} terms={item['terms_present_in_chapter']} "
            f"agent={item['has_agent_term']}"
        )
    return 0


def build_coverage(
    conn: sqlite3.Connection,
    doc_id: str,
    glossary_entries: list[Any],
) -> list[ChapterCoverage]:
    rows = conn.execute(
        """
        SELECT chapter_id, order_index, block_type, text, style_json
        FROM blocks
        WHERE doc_id = ?
        ORDER BY order_index
        """,
        (doc_id,),
    ).fetchall()
    chapters: dict[str, dict[str, Any]] = {}
    chapter_order: list[str] = []
    for row in rows:
        chapter_id = str(row["chapter_id"])
        if chapter_id not in chapters:
            style = _json_dict(row["style_json"])
            chapters[chapter_id] = {
                "chapter_id": chapter_id,
                "chapter_slug": str(style.get("chapter_slug") or chapter_id.removeprefix("d2l_")),
                "order_index": len(chapter_order),
                "total_blocks": 0,
                "prose_blocks": 0,
                "prose_texts": [],
            }
            chapter_order.append(chapter_id)
        item = chapters[chapter_id]
        item["total_blocks"] += 1
        if str(row["block_type"]) == "prose":
            item["prose_blocks"] += 1
            item["prose_texts"].append(str(row["text"] or ""))

    result: list[ChapterCoverage] = []
    source_terms = [str(entry.source_term) for entry in glossary_entries]
    total_terms = len(source_terms)
    for chapter_id in chapter_order:
        item = chapters[chapter_id]
        prose = "\n\n".join(item["prose_texts"])
        token_estimate = _estimate_tokens(prose)
        per_term_counts = {
            term: _count_matches(prose, term)
            for term in source_terms
        }
        present_counts = {term: count for term, count in per_term_counts.items() if count}
        occurrences = sum(present_counts.values())
        density = (occurrences / token_estimate * 1000.0) if token_estimate else 0.0
        result.append(
            ChapterCoverage(
                chapter_id=chapter_id,
                chapter_slug=item["chapter_slug"],
                order_index=item["order_index"],
                total_blocks=item["total_blocks"],
                prose_blocks=item["prose_blocks"],
                prose_tokens_estimate=token_estimate,
                glossary_terms_total=total_terms,
                terms_present_in_chapter=len(present_counts),
                term_occurrences_total=occurrences,
                term_density_per_1k_tokens=round(density, 4),
                has_agent_term=per_term_counts.get("agent", 0) > 0,
            )
        )
    return result


def _git_commit(source_root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(source_root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception:
        return "unknown"
    return result.stdout.strip()


def _count_matches(text: str, needle: str) -> int:
    normalized_text = _normalize_for_match(text)
    normalized_needle = _normalize_for_match(needle.strip())
    if not normalized_needle:
        return 0
    pattern = rf"(?<!\w){re.escape(normalized_needle)}(?!\w)"
    return len(re.findall(pattern, normalized_text, flags=re.UNICODE))


def _normalize_for_match(text: str) -> str:
    normalized = unicodedata.normalize("NFC", str(text))
    return (
        normalized.replace("’", "'")
        .replace("‘", "'")
        .replace("`", "'")
        .casefold()
    )


def _estimate_tokens(text: str) -> int:
    return max(0, len(str(text)) // 4)


def _json_dict(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


if __name__ == "__main__":
    raise SystemExit(main())
