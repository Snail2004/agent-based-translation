from __future__ import annotations

import argparse
import json
from pathlib import Path

from pipeline.eval.occ_align import (
    DEFAULT_EXPERIMENT_ID,
    build_gold_sample_rows,
    load_frozen_translations,
    load_occ_inputs,
    write_gold_sample_csv,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Create proposer-blind gold CSV for occurrence alignment pilot.")
    parser.add_argument("--chapter", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--experiment", default=DEFAULT_EXPERIMENT_ID)
    parser.add_argument("--profile", default="technical_d2l_v1")
    parser.add_argument("--term-policy-root")
    parser.add_argument("--cap-per-term", type=int, default=3)
    parser.add_argument("--max-rows", type=int, default=60)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    resolved_chapter, _blocks, occ_frame = load_occ_inputs(
        args.db,
        chapter=args.chapter,
        profile_name=args.profile,
        term_policy_root=args.term_policy_root,
    )
    translations = {
        config: load_frozen_translations(args.db, config=config, experiment_id=args.experiment)
        for config in ["S0", "S1"]
    }
    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    rows = build_gold_sample_rows(
        occ_frame,
        translations,
        report,
        cap_per_term=args.cap_per_term,
        seed=args.seed,
        max_rows=args.max_rows,
    )
    write_gold_sample_csv(
        args.out,
        rows,
        seed=args.seed,
        cap_per_term=args.cap_per_term,
        max_rows=args.max_rows,
    )
    print(
        json.dumps(
            {
                "chapter": resolved_chapter,
                "occurrences": len(occ_frame),
                "rows": len(rows),
                "seed": args.seed,
                "cap_per_term": args.cap_per_term,
                "max_rows": args.max_rows,
                "out": args.out,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
