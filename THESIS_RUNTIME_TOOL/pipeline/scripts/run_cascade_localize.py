from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from pipeline.agents.llm_client import LLMClient
from pipeline.agents.llm_config import load_llm_config
from pipeline.eval.cascade_localize import (
    DEFAULT_CHAPTERS,
    build_residual_gold,
    run_cascade_localize,
    run_t3_pilot,
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
    parser.add_argument("--t3-model", default="", help="Use 'gpt' only for the capped Part-A T3 pilot.")
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
    parser.add_argument("--gold-reuse", nargs="*", default=[], help="Reusable human gold CSVs for T3 pilot.")
    parser.add_argument("--only-reused-labeled", action="store_true", help="T3 pilot guard: use only rows with reused human labels.")
    parser.add_argument("--locate-only", action="store_true", help="Use REWORK-2 locate-only T3; LLM locates and code scores.")
    parser.add_argument("--limit", type=int, default=0, help="Hard cap for T3 pilot calls.")
    parser.add_argument("--llm-config", default="pipeline/configs/llm_adjudicator.yaml")
    parser.add_argument("--llm-cache", default="data/eval/cascade_t3_llm_cache.sqlite3")
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
        if args.t3_model != "gpt":
            raise SystemExit("--tier-max 3 requires --t3-model gpt")
        if not args.locate_only:
            raise SystemExit("--tier-max 3 now requires --locate-only (REWORK-2 locate-only schema)")
        if not args.only_reused_labeled:
            raise SystemExit("--tier-max 3 requires --only-reused-labeled")
        if args.limit <= 0:
            raise SystemExit("--tier-max 3 requires a positive --limit")
        if not args.gold_reuse:
            raise SystemExit("--tier-max 3 requires --gold-reuse CSV paths")
        key_source = _ensure_openai_key()
        config = load_llm_config(args.llm_config)
        client = LLMClient(config=config, cache_path=args.llm_cache)
        report = run_t3_pilot(
            db_path=args.db,
            experiment_id=args.experiment,
            configs=[item.strip() for item in args.configs.split(",") if item.strip()],
            chapters=[item.strip() for item in args.chapters.split(",") if item.strip()],
            embed_endpoint=args.embed_endpoint,
            model_config=model_configs[0],
            cache_dir=args.cache_dir,
            margin_threshold=args.margin_threshold,
            gold_reuse_paths=args.gold_reuse,
            llm_client=client,
            limit=args.limit,
            locate_only=args.locate_only,
        )
        report["llm"]["api_key_source"] = key_source
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({
            "out": str(out),
            "attempted": report["attempted"],
            "correct": report["correct"],
            "accuracy": report["accuracy"],
            "fresh_calls": report["llm"]["fresh_calls"],
            "cache_hits": report["llm"]["cache_hits"],
            "prompt_tokens_fresh": report["llm"]["prompt_tokens_fresh"],
            "completion_tokens_fresh": report["llm"]["completion_tokens_fresh"],
            "cost_usd_fresh": report["llm"]["cost_usd_fresh"],
            "adherence_counts": report.get("adherence_counts"),
            "adherence_by_config": report.get("adherence_by_config"),
            "off_glossary_pct": report.get("off_glossary_pct"),
            "usage_today_after": report["llm"]["usage_today_after"],
        }, ensure_ascii=False, indent=2))
        return 0

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


def _ensure_openai_key() -> str:
    if os.getenv("OPENAI_API_KEY"):
        return "env:OPENAI_API_KEY"
    candidates = [
        Path("OPENAI-KEY-2.txt"),
        Path("OPENAI-KEY-1.txt"),
        Path("../OPENAI-KEY-2.txt"),
        Path("../OPENAI-KEY-1.txt"),
        Path("THESIS_RUNTIME_TOOL/OPENAI-KEY-2.txt"),
        Path("THESIS_RUNTIME_TOOL/OPENAI-KEY-1.txt"),
    ]
    for path in candidates:
        if not path.exists():
            continue
        key = path.read_text(encoding="utf-8").strip()
        if key:
            os.environ["OPENAI_API_KEY"] = key
            return f"file:{path.name}"
    raise RuntimeError("OPENAI_API_KEY is not set and no OPENAI-KEY-*.txt fallback was found")


if __name__ == "__main__":
    raise SystemExit(main())
