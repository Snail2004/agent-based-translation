from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from pipeline.eval.memory_tradeoff import (
    DEFAULT_CHAPTERS,
    DEFAULT_SEED,
    build_judge_worksheet,
    build_override_set,
    render_worksheet_html,
    validate_dummy_not_real,
    write_key_json,
    write_override_csv,
    write_worksheet_jsonl,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the blind memory-tradeoff worksheet.")
    parser.add_argument("--db", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--out-dir", default="data/eval/memory_tradeoff")
    parser.add_argument("--chapters", nargs="+", default=list(DEFAULT_CHAPTERS))
    parser.add_argument("--experiment", default="d2l_p3")
    parser.add_argument("--profile", default="technical_d2l_v1")
    parser.add_argument("--doc-id", default="d2l")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args()

    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    items = build_override_set(
        report,
        args.db,
        chapters=args.chapters,
        experiment_id=args.experiment,
        profile_name=args.profile,
        doc_id=args.doc_id,
    )
    rows, key_rows = build_judge_worksheet(items, seed=args.seed)
    validate_dummy_not_real(rows)

    out_dir = Path(args.out_dir)
    key_dir = out_dir / "KEY"
    out_dir.mkdir(parents=True, exist_ok=True)
    key_dir.mkdir(parents=True, exist_ok=True)

    write_override_csv(out_dir / "override_set.csv", items)
    write_worksheet_jsonl(out_dir / "worksheet.jsonl", rows)
    (out_dir / "worksheet.html").write_text(render_worksheet_html(rows), encoding="utf-8")
    write_key_json(key_dir / "worksheet_KEY.json", key_rows, seed=args.seed)
    (key_dir / "README.md").write_text(
        "DO NOT OPEN until judgments_human.json and judgments_gemini.jsonl are complete.\n"
        "Opening worksheet_KEY.json reveals S0/S1 provenance and breaks the blind.\n",
        encoding="utf-8",
    )

    unresolved = [item for item in items if not item.rep_resolved]
    summary = {
        "override_items": len(items),
        "resolved_items": len(rows),
        "unresolved_items": len(unresolved),
        "tier_breakdown": dict(Counter(item.tier for item in items)),
        "resolved_tier_breakdown": dict(Counter(row.tier for row in rows)),
        "human_suggested": sum(1 for row in rows if row.human_suggested),
        "seed": args.seed,
        "out_dir": str(out_dir),
        "artifacts": [
            "override_set.csv",
            "worksheet.jsonl",
            "worksheet.html",
            "KEY/worksheet_KEY.json",
            "KEY/README.md",
        ],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
