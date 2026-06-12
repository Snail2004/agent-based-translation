from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from pipeline.agents.llm_client import LLMClient
from pipeline.agents.llm_config import load_llm_config
from pipeline.memory.store_init import migrate_db
from pipeline.translate.runner import translate_windows
from pipeline.translate.windower import build_windows


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run S0 translation over specified chapters."
    )
    parser.add_argument(
        "--db",
        required=True,
        help="Path to the frozen memory SQLite DB.",
    )
    parser.add_argument(
        "--chapters",
        nargs="+",
        required=True,
        help="Chapter suffixes to translate (e.g. ch02 ch03).",
    )
    parser.add_argument(
        "--config",
        default="S0",
        help="Config name (default: S0).",
    )
    parser.add_argument(
        "--experiment",
        required=True,
        help="Experiment ID for this run.",
    )
    parser.add_argument(
        "--source",
        help="Accepted for command compatibility; S0 windowing reads source blocks from DB.",
    )
    parser.add_argument(
        "--config-file",
        default="pipeline/configs/llm_translate.yaml",
        help="LLM config YAML for translation.",
    )
    parser.add_argument(
        "--cache",
        default="data/jobs/translate_cache.sqlite3",
        help="Replay cache SQLite path.",
    )
    parser.add_argument(
        "--report",
        help="Write JSON report to this path.",
    )
    args = parser.parse_args()

    _ensure_api_key()
    llm_config = load_llm_config(args.config_file)
    client = LLMClient(config=llm_config, cache_path=args.cache)

    # Migrate DB (adds window_id column if missing)
    db = migrate_db(args.db)

    # Resolve doc_id from DB
    row = db.execute("SELECT doc_id FROM documents LIMIT 1").fetchone()
    if not row:
        raise SystemExit("No document found in DB")
    doc_id = str(row["doc_id"])

    # Build windows
    windows = build_windows(db, doc_id, args.chapters)

    print(
        f"Experiment: {args.experiment}  Config: {args.config}  "
        f"DB: {args.db}  Chapters: {args.chapters}"
    )
    print(f"Windows planned: {len(windows)}")

    # Run translation
    report = translate_windows(
        db, windows, client, experiment_id=args.experiment, config=args.config
    )
    db.close()

    # Print summary
    print(f"\n=== Translate Report ===")
    print(f"Windows total:     {report.windows_total}")
    print(f"  translated:      {report.windows_translated}")
    print(f"  failed:         {report.windows_failed}")
    print(f"  skipped:        {report.windows_skipped}")
    print(f"Blocks translated: {report.blocks_translated}")
    print(f"Blocks failed:    {report.blocks_failed}")
    print(f"JSON fail rate:   {report.json_fail_rate:.4f}")
    print(f"\n=== Usage ===")
    usage = report.total_usage
    print(f"  prompt_tokens:      {usage['prompt_tokens']}")
    print(f"  completion_tokens:  {usage['completion_tokens']}")
    print(f"  total_cost_usd:    ${usage['cost_usd']:.6f}")
    print(f"  incremental_cost:   ${usage['incremental_cost_usd']:.6f}")
    print(f"  calls:             {usage['calls']}")
    print(f"  cache_hits:        {usage['cache_hits']}")
    print(f"Model: {report.model}  Seed: {report.seed}")

    if args.report:
        out_path = Path(args.report)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(report.to_json_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"\nReport written: {out_path}")

    return 0


def _ensure_api_key() -> None:
    if os.environ.get("OPENAI_API_KEY"):
        return
    repo_root = Path(__file__).resolve().parents[3]
    key_path = repo_root / "API-KEY.txt"
    if key_path.exists():
        key = key_path.read_text(encoding="utf-8").strip()
        if key:
            os.environ["OPENAI_API_KEY"] = key
            return
    raise SystemExit("OPENAI_API_KEY is not set and API-KEY.txt is missing or empty")


if __name__ == "__main__":
    raise SystemExit(main())
