from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


TOOL_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = TOOL_ROOT / "app" / "backend"
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.thesis_overlay import load_registry_overlay


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark thesis registry overlay read-model.")
    parser.add_argument("--job", required=True, help="Job id, e.g. d2l_p1.")
    parser.add_argument("--block", help="Optional block_id scope.")
    parser.add_argument("--chapter", help="Optional chapter_id scope.")
    parser.add_argument("--jobs-root", default="data/jobs", help="Jobs root.")
    parser.add_argument("--reports-root", default="data/reports", help="Reports root.")
    parser.add_argument("--block-budget", type=float, default=0.3, help="Seconds budget for block scope.")
    parser.add_argument("--chapter-budget", type=float, default=4.0, help="Seconds budget for chapter scope.")
    parser.add_argument("--cold", action="store_true", help="Measure cold call only; default asserts warm cached call.")
    args = parser.parse_args()

    cold_started = time.perf_counter()
    cold_overlay = load_registry_overlay(
        args.job,
        block_id=args.block,
        chapter_id=args.chapter,
        jobs_root=Path(args.jobs_root),
        reports_root=Path(args.reports_root),
    )
    cold_elapsed = time.perf_counter() - cold_started
    if args.cold:
        overlay = cold_overlay
        elapsed = cold_elapsed
        warm_elapsed = None
    else:
        warm_started = time.perf_counter()
        overlay = load_registry_overlay(
            args.job,
            block_id=args.block,
            chapter_id=args.chapter,
            jobs_root=Path(args.jobs_root),
            reports_root=Path(args.reports_root),
        )
        warm_elapsed = time.perf_counter() - warm_started
        elapsed = warm_elapsed
    source_count = sum(len(item.get("occurrences") or []) for item in (overlay.get("source", {}).get("glossary_by_id") or {}).values())
    target_count = 0
    for config_data in (overlay.get("target_by_config") or {}).values():
        target_count += sum(len(item.get("occurrences") or []) for item in (config_data.get("glossary_by_id") or {}).values())
    result = {
        "job": args.job,
        "block": args.block,
        "chapter": args.chapter,
        "elapsed_seconds": round(elapsed, 6),
        "cold_elapsed_seconds": round(cold_elapsed, 6),
        "warm_elapsed_seconds": None if warm_elapsed is None else round(warm_elapsed, 6),
        "source_glossary_spans": source_count,
        "target_glossary_spans": target_count,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))

    if args.block and elapsed > args.block_budget:
        raise SystemExit(f"Overlay block benchmark exceeded budget: {elapsed:.3f}s > {args.block_budget:.3f}s")
    if args.chapter and elapsed > args.chapter_budget:
        raise SystemExit(f"Overlay chapter benchmark exceeded budget: {elapsed:.3f}s > {args.chapter_budget:.3f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
