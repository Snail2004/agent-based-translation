from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from pipeline.agents.llm_client import LLMClient
from pipeline.agents.llm_config import load_llm_config
from pipeline.eval.localizer_cascade import (
    LocalizationResultCache,
    localize_with_t2,
    preflight_dev,
    render_audit_html,
    score_dev_pilot,
    write_audit_csv,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="EV-D2L-07b cascade localizer DEV pilot")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preflight", action="store_true")
    mode.add_argument("--run-dev", action="store_true")
    parser.add_argument("--dev", action="store_true", help="Required DEV-only label")
    parser.add_argument("--db", default="data/jobs/d2l_p1/memory.sqlite3")
    parser.add_argument("--gold", default="data/eval/localizer_gold.csv")
    parser.add_argument("--config", default="pipeline/configs/llm_localizer.yaml")
    parser.add_argument("--api-cache", default="data/eval/localizer_cascade/api_cache.sqlite3")
    parser.add_argument("--result-cache", default="data/eval/localizer_cascade/result_cache.sqlite3")
    parser.add_argument("--preflight-out", default="data/reports/localizer_cascade_preflight.json")
    parser.add_argument("--out", default="data/reports/localizer_cascade_dev.json")
    parser.add_argument("--audit-csv", default="data/eval/localizer_cascade/audit_dev.csv")
    parser.add_argument("--audit-html", default="data/eval/localizer_cascade/audit_dev.html")
    parser.add_argument("--confirm-token", default="")
    args = parser.parse_args()
    if not args.dev:
        raise SystemExit("EV-07b only supports the explicitly labeled --dev dataset")

    config = load_llm_config(args.config)
    preflight, cases, entries = preflight_dev(gold_path=args.gold, db_path=args.db, config=config)
    _write_json(args.preflight_out, preflight)
    if args.preflight:
        print(json.dumps(_public_preflight(preflight), ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    if len(cases) != 8:
        raise SystemExit(f"Refusing DEV probe: expected exactly 8 legacy residuals, got {len(cases)}")
    if args.confirm_token != preflight["confirm_token"]:
        raise SystemExit("Cost gate closed: --confirm-token does not match current rendered prompts")
    _ensure_api_key()
    client = LLMClient(config, args.api_cache, max_retries=3)
    result_cache = LocalizationResultCache(args.result_cache)
    decisions = {}
    for case in cases:
        decisions[case.opaque_id] = localize_with_t2(
            case, entries.get(case.opaque_id), client=client, result_cache=result_cache
        )
    report = {
        "mode": "dev_pilot",
        "prompt_version": preflight["prompt_version"],
        "model": config.model,
        "preflight": _public_preflight(preflight),
        "metrics": score_dev_pilot(cases, decisions),
        "decisions": [
            {"row_id": case.row_id, "opaque_id": case.opaque_id, **decisions[case.opaque_id].__dict__}
            for case in cases
        ],
        "usage": {
            "prompt_tokens": sum(item.prompt_tokens for item in decisions.values()),
            "completion_tokens": sum(item.completion_tokens for item in decisions.values()),
            "cost_usd": round(sum(item.cost_usd for item in decisions.values()), 8),
            "result_cache_hits": sum(item.from_cache for item in decisions.values()),
            "api_cache_hits": sum(item.api_cache_hit for item in decisions.values()),
        },
    }
    _write_json(args.out, report)
    write_audit_csv(args.audit_csv, cases, decisions)
    html = Path(args.audit_html)
    html.parent.mkdir(parents=True, exist_ok=True)
    html.write_text(render_audit_html(cases, decisions), encoding="utf-8")
    print(json.dumps({
        "mode": "dev_pilot",
        "cases": len(cases),
        "metrics": report["metrics"],
        "usage": report["usage"],
        "out": args.out,
        "audit_html": args.audit_html,
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _public_preflight(report: dict) -> dict:
    return {key: value for key, value in report.items() if key != "prompts"}


def _write_json(path: str, payload: dict) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _ensure_api_key() -> None:
    if os.environ.get("OPENAI_API_KEY"):
        return
    repo_root = Path(__file__).resolve().parents[3]
    for filename in ("OPENAI-KEY-2.txt", "OPENAI-KEY-1.txt", "API-KEY.txt"):
        path = repo_root / filename
        if path.exists() and path.read_text(encoding="utf-8").strip():
            os.environ["OPENAI_API_KEY"] = path.read_text(encoding="utf-8").strip()
            return
    raise SystemExit("OPENAI_API_KEY is not set and no local key file was found")


if __name__ == "__main__":
    raise SystemExit(main())
