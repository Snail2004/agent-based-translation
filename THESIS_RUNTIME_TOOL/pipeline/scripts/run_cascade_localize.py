from __future__ import annotations

import argparse
import json
from pathlib import Path

from pipeline.eval.cascade_localize import (
    DEFAULT_CHAPTERS,
    build_residual_gold,
    run_cascade_localize,
    score_residual_gold,
    write_reports,
)
from pipeline.eval.region_align import parse_model_specs


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run EV-D2L-10 occurrence localization/adherence cascade."
    )
    parser.add_argument("--configs", default="S0,S1", help="Comma-separated configs, e.g. S0,S1.")
    parser.add_argument("--tier-max", type=int, default=2, choices=[2, 3])
    parser.add_argument("--t3-model", default="", help="Reserved. T3 is prompt-review gated.")
    parser.add_argument("--k", type=int, default=3, help="Reserved max re-narrow K; report only in tier 2.")
    parser.add_argument("--db", default="data/jobs/d2l_p1/memory.sqlite3")
    parser.add_argument("--experiment", default="d2l_p3")
    parser.add_argument("--chapters", default=",".join(DEFAULT_CHAPTERS))
    parser.add_argument("--embed-endpoint", default="http://localhost:1234/v1/embeddings")
    parser.add_argument(
        "--models",
        default="bge-m3=text-embedding-bge-m3@gpustack/bge-m3-GGUF:Q8_0",
        help="Use one model for production T1. Example: bge-m3=text-embedding-bge-m3@Q8_0.",
    )
    parser.add_argument("--cache-dir", default="data/eval/embed_cache/cascade")
    parser.add_argument("--margin-threshold", type=float, default=0.20)
    parser.add_argument("--out", default="data/reports/cascade_localize")
    parser.add_argument("--score", action="store_true", help="Score a completed residual gold CSV.")
    parser.add_argument("--gold", help="Gold CSV for --score.")
    args = parser.parse_args()

    if args.score:
        if not args.gold:
            parser.error("--score requires --gold")
        report = score_residual_gold(args.gold)
        out = Path(args.out).with_name(Path(args.out).name + "_gold_score.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"out": str(out), **report}, ensure_ascii=False, indent=2))
        return 0

    model_configs = parse_model_specs(args.models)
    if len(model_configs) != 1:
        parser.error("EV-D2L-10 production T1 accepts exactly one model; pass bge-m3 only.")
    if args.tier_max > 2:
        raise SystemExit(
            "Tier 3 GPT is gated by prompt review. Run tier-max 2 first; do not call GPT here."
        )
    report = run_cascade_localize(
        db_path=args.db,
        experiment_id=args.experiment,
        configs=[item.strip() for item in args.configs.split(",") if item.strip()],
        chapters=[item.strip() for item in args.chapters.split(",") if item.strip()],
        embed_endpoint=args.embed_endpoint,
        model_config=model_configs[0],
        cache_dir=args.cache_dir,
        margin_threshold=args.margin_threshold,
        tier_max=args.tier_max,
    )
    paths = write_reports(report, args.out)
    print(json.dumps({
        "out": [str(path) for path in paths],
        "frozen_db_sha256_first16": report["frozen_db_sha256_first16"],
        "frozen_db_matches_expected": report["frozen_db_matches_expected"],
        "configs": {
            config: {
                "denominator": item["denominator"],
                "t2_resolved": item["t2_resolved"],
                "t3_residual": item["t3_residual"],
                "masquerade_suspect_count": item["masquerade_suspect_count"],
                "llm_calls": item["llm_calls"],
            }
            for config, item in report["reports"].items()
        },
        "tier_max": args.tier_max,
        "k_reserved": args.k,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
