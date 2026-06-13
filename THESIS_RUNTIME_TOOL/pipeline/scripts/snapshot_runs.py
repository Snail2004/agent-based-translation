"""Xuất snapshot BỀN VỮNG (tracked trong git) cho một job dịch.

DB `memory.sqlite3` bị .gitignore → bản dịch S0/S1, registry (thước TAR) và
evaluation_runs CHỈ sống trên ổ đĩa. Khi nâng cấp hệ thống hoặc sửa/thêm thước đo
(vd COMET ở EV-03, lọc block judge F1) ta cần chấm LẠI CHÍNH bản dịch cũ mà KHÔNG
dịch lại (tốn tiền + model trôi). Snapshot gói đủ để tái-đánh-giá từ git:
  - translations: văn bản S0/S1 mỗi block + provenance (model/seed/prompt/fingerprint)
  - sources: EN gốc + text mỗi block (cho metric tham chiếu COMET/BLEU chạy lại)
  - registry: glossary_entries (+ allowed/forbidden variants) + entities (thước TAR)
  - evaluations: toàn bộ điểm đã đo (kèm metric_version)
  - manifest: db path, ngày, đếm dòng

Chỉ SELECT (không ghi bảng memory). Tất định, 0 gọi mạng.

  python -m pipeline.scripts.snapshot_runs \
    --db data/jobs/treasure_island_p2/memory.sqlite3 \
    --out data/reports/treasure_island_p2_snapshot.json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def _rows(con: sqlite3.Connection, sql: str, args: tuple = ()) -> list[dict]:
    cur = con.execute(sql, args)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def build_snapshot(db_path: str) -> dict:
    con = sqlite3.connect(db_path)
    translations = _rows(
        con,
        """
        SELECT t.run_id, t.block_id, b.chapter_id, t.window_id, t.config, t.stage,
               t.output_text, t.model, t.prompt_version, t.temperature, t.seed,
               t.system_fingerprint, t.cost, t.created_at
        FROM translation_runs t
        LEFT JOIN blocks b ON b.block_id = t.block_id
        ORDER BY t.config, b.chapter_id, t.block_id
        """,
    )
    sources = _rows(
        con,
        """
        SELECT DISTINCT b.block_id, b.chapter_id, b.order_index, b.block_type,
               b.original_text, b.text
        FROM blocks b
        WHERE b.block_id IN (SELECT DISTINCT block_id FROM translation_runs)
        ORDER BY b.chapter_id, b.order_index
        """,
    )
    glossary = _rows(
        con,
        """
        SELECT glossary_id, source_term, target_term, term_type, scope, chapter_id,
               do_not_translate, case_sensitive, allowed_variants_json,
               forbidden_variants_json, occurrences_count, status
        FROM glossary_entries ORDER BY glossary_id
        """,
    )
    entities = _rows(
        con,
        """
        SELECT entity_id, canonical_source, canonical_target, entity_type, role,
               importance, aliases_source_json, aliases_target_json,
               preferred_vietnamese_forms_json, status
        FROM entities ORDER BY entity_id
        """,
    )
    evaluations = _rows(
        con,
        """
        SELECT eval_id, run_id, scope, scope_id, metric_name, metric_value,
               metric_version, ablation_label, judge_model, judge_rationale
        FROM evaluation_runs ORDER BY metric_name, scope, scope_id
        """,
    )
    metric_versions = sorted(
        {r["metric_version"] for r in evaluations if r.get("metric_version")}
    )
    return {
        "manifest": {
            "db_path": db_path,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "purpose": "durable re-evaluation snapshot (DB is gitignored)",
            "counts": {
                "translations": len(translations),
                "sources": len(sources),
                "glossary_entries": len(glossary),
                "entities": len(entities),
                "evaluation_runs": len(evaluations),
            },
            "metric_versions_present": metric_versions,
        },
        "translations": translations,
        "sources": sources,
        "registry": {"glossary": glossary, "entities": entities},
        "evaluations": evaluations,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    snap = build_snapshot(args.db)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
    c = snap["manifest"]["counts"]
    print(f"Snapshot written: {out}")
    print(
        f"  translations={c['translations']} sources={c['sources']} "
        f"glossary={c['glossary_entries']} entities={c['entities']} "
        f"evaluations={c['evaluation_runs']}"
    )
    print(f"  metric_versions={snap['manifest']['metric_versions_present']}")


if __name__ == "__main__":
    main()
