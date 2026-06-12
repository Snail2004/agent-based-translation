from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from pipeline.agents.llm_client import LLMClient
from pipeline.agents.llm_config import load_llm_config
from pipeline.prepass.runner import run_prepass


def main() -> int:
    parser = argparse.ArgumentParser(description="Run World Builder pre-pass.")
    parser.add_argument("--source", required=True, help="Stripped source document.json.")
    parser.add_argument("--chapters", nargs="+", required=True, help="Chapter suffixes.")
    parser.add_argument("--out", required=True, help="Output artifact directory.")
    parser.add_argument(
        "--config",
        default="pipeline/configs/llm_prepass.yaml",
        help="LLM config YAML.",
    )
    parser.add_argument(
        "--cache",
        default="data/jobs/prepass_cache.sqlite3",
        help="Replay cache SQLite path.",
    )
    args = parser.parse_args()

    _ensure_api_key()
    config = load_llm_config(args.config)
    client = LLMClient(config=config, cache_path=args.cache)
    report = run_prepass(args.source, args.chapters, client, args.out)
    print(json.dumps(report.to_json_dict(), ensure_ascii=False, indent=2))
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
