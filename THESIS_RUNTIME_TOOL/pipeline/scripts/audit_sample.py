from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from pipeline.eval.surface_match import SurfaceOwner, allocate_spans, find_spans


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a human-label audit sample for D_surface.")
    parser.add_argument("--report", required=True, help="D2L score report JSON.")
    parser.add_argument("--db", default="data/jobs/d2l_p1/memory.sqlite3", help="Frozen memory SQLite DB.")
    parser.add_argument("--tier", default="hard", help="Constraint tier to sample.")
    parser.add_argument("--n", type=int, default=30, help="Rows per config.")
    parser.add_argument("--configs", nargs="+", default=["S0", "S1"], help="Configs to sample.")
    parser.add_argument("--out", required=True, help="CSV output path.")
    args = parser.parse_args()

    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    rows = build_audit_rows(
        report=report,
        db_path=Path(args.db),
        tier=args.tier,
        configs=args.configs,
        rows_per_config=args.n,
    )
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "config",
        "source_term",
        "target_term",
        "status",
        "constraint_strength",
        "block_id",
        "sentence_EN",
        "sentence_VI",
        "predicted_form",
        "human_label",
        "note",
    ]
    with out_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Audit rows: {len(rows)}")
    print(f"Report written: {out_path}")
    return 0


def build_audit_rows(
    *,
    report: dict[str, Any],
    db_path: Path,
    tier: str,
    configs: list[str],
    rows_per_config: int,
) -> list[dict[str, str]]:
    experiment_id = str(report.get("experiment_id") or "d2l_p3")
    chapters = [str(item) for item in report.get("chapters") or []]
    rows: list[dict[str, str]] = []
    with sqlite3.connect(db_path) as con:
        con.row_factory = sqlite3.Row
        blocks = _load_blocks(con, chapters)
        translations = {
            config: _load_translations(con, experiment_id, config)
            for config in configs
        }
    block_order = [block["block_id"] for block in blocks]
    block_by_id = {block["block_id"]: block for block in blocks}
    for config in configs:
        terms = _terms_for_config(report, config, tier)
        count = 0
        for term in terms:
            source_term = str(term.get("source_term") or "")
            if not source_term:
                continue
            for block_id in _source_blocks_for_term(blocks, source_term):
                output = translations.get(config, {}).get(block_id, "")
                predicted = _predicted_form(output, term)
                rows.append({
                    "config": config,
                    "source_term": source_term,
                    "target_term": str(term.get("target_term") or ""),
                    "status": str(term.get("status") or ""),
                    "constraint_strength": str(term.get("constraint_strength") or ""),
                    "block_id": block_id,
                    "sentence_EN": _sentence_around(block_by_id[block_id]["text"], source_term, language="en"),
                    "sentence_VI": _sentence_around(output, predicted or str(term.get("target_term") or ""), language="vi"),
                    "predicted_form": predicted,
                    "human_label": "",
                    "note": "",
                })
                count += 1
                if count >= rows_per_config:
                    break
            if count >= rows_per_config:
                break
        # Keep deterministic shape even when the report has fewer candidates.
        rows.sort(key=lambda item: (configs.index(item["config"]), block_order.index(item["block_id"]) if item["block_id"] in block_order else 10**9, item["source_term"]))
    return rows


def _load_blocks(con: sqlite3.Connection, chapters: list[str]) -> list[dict[str, str]]:
    params: list[Any] = ["d2l"]
    chapter_sql = ""
    if chapters:
        chapter_sql = f"AND chapter_id IN ({','.join('?' * len(chapters))})"
        params.extend(chapters)
    return [
        {
            "block_id": str(row["block_id"]),
            "chapter_id": str(row["chapter_id"] or ""),
            "text": str(row["original_text"] or row["text"] or ""),
        }
        for row in con.execute(
            f"""
            SELECT block_id, chapter_id, order_index, text, original_text
            FROM blocks
            WHERE doc_id = ? {chapter_sql}
              AND block_type IN ('heading', 'prose')
            ORDER BY order_index
            """,
            params,
        )
    ]


def _load_translations(con: sqlite3.Connection, experiment_id: str, config: str) -> dict[str, str]:
    return {
        str(row["block_id"]): str(row["output_text"] or "")
        for row in con.execute(
            """
            SELECT block_id, output_text
            FROM translation_runs
            WHERE experiment_id = ? AND config = ? AND stage = 'draft'
            """,
            (experiment_id, config),
        )
    }


def _terms_for_config(report: dict[str, Any], config: str, tier: str) -> list[dict[str, Any]]:
    terms = (report.get("D_registry_consistency") or {}).get(config, {}).get("terms_all") or []
    priority = {"drift": 0, "undetected": 1, "consistent": 2}
    return sorted(
        [term for term in terms if str(term.get("constraint_strength") or "") == tier],
        key=lambda term: (
            priority.get(str(term.get("status") or ""), 9),
            str(term.get("source_term") or "").casefold(),
        ),
    )


def _source_blocks_for_term(blocks: list[dict[str, str]], source_term: str) -> list[str]:
    return [
        block["block_id"]
        for block in blocks
        if find_spans(block["text"], source_term, language="en")
    ]


def _predicted_form(output: str, term: dict[str, Any]) -> str:
    forms = list((term.get("forms_used") or {}).keys())
    owners = [SurfaceOwner(form, form) for form in forms]
    allocated = allocate_spans(output, owners, language="vi")
    candidates = [
        span
        for form in forms
        for span in allocated.get(form, [])
    ]
    if not candidates:
        return ""
    return sorted(candidates, key=lambda span: (span.start, -(span.end - span.start)))[0].surface


def _sentence_around(text: str, needle: str, *, language: str) -> str:
    value = str(text or "")
    if not value:
        return ""
    spans = find_spans(value, needle, language=language) if needle else []
    if not spans:
        return value[:500]
    start, end, _ = spans[0]
    left = max(value.rfind(".", 0, start), value.rfind("\n", 0, start))
    right_candidates = [pos for pos in [value.find(".", end), value.find("\n", end)] if pos >= 0]
    right = min(right_candidates) if right_candidates else min(len(value), end + 240)
    sentence = value[left + 1:right + 1].strip()
    return re.sub(r"\s+", " ", sentence)[:500]


if __name__ == "__main__":
    raise SystemExit(main())
