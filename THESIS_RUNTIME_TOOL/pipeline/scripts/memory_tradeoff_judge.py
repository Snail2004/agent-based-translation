from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path

from pipeline.agents.judge_client import JudgeClient
from pipeline.agents.llm_config import load_llm_config
from pipeline.eval.memory_tradeoff import (
    JSON_FORMAT,
    DEFAULT_JUDGE_MODEL,
    confirm_token,
    parse_judge_payload,
    preview_judge_calls,
    read_worksheet_jsonl,
    render_judge_prompt,
    write_gemini_jsonl,
)


DEFAULT_CONFIG = Path(__file__).parents[1] / "configs" / "judge_gemini.yaml"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Gemini blind judge for memory tradeoff worksheet.")
    parser.add_argument("--worksheet", required=True)
    parser.add_argument("--out", default="data/eval/memory_tradeoff/judgments_gemini.jsonl")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--cache", default="data/cache/memory_tradeoff_judge.sqlite3")
    parser.add_argument("--model", default=None, help="Override judge model; default reads config.")
    parser.add_argument("--preview-only", action="store_true")
    parser.add_argument("--confirm-token")
    args = parser.parse_args()

    rows = read_worksheet_jsonl(args.worksheet)
    config = load_llm_config(args.config)
    if args.model:
        config = replace(config, model=args.model)
    elif config.model == "gemini-2.5-flash":
        # Keep existing EV-02 config, but make the stable model id explicit in
        # preview/report. Google currently exposes the stable 2.5 Flash id
        # without a -NNN suffix.
        config = replace(config, model=DEFAULT_JUDGE_MODEL)

    preview = preview_judge_calls(rows, config, model=config.model)
    if args.preview_only:
        print(json.dumps(preview, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    expected = confirm_token(
        str(preview["judge_model"]),
        int(preview["calls"]),
        int(preview["estimated_prompt_tokens"]),
        int(preview["estimated_max_output_tokens"]),
    )
    if args.confirm_token != expected:
        raise SystemExit(
            "Refusing Gemini judge calls without matching confirm token. "
            f"Run --preview-only first and pass --confirm-token {expected}"
        )

    client = JudgeClient(config, args.cache)
    output_rows = []
    for row in rows:
        for orientation in ("forward", "reverse"):
            messages = render_judge_prompt(row, orientation=orientation)
            result = client.call(
                messages,
                response_format=JSON_FORMAT,
                tag=f"memory_tradeoff:{row.item_id}:{orientation}",
            )
            if result.json_error:
                judgment = parse_judge_payload(result.text)
                judgment["json_error"] = result.json_error
            else:
                judgment = parse_judge_payload(result.parsed_json or result.text)
            output_rows.append(
                {
                    "item_id": row.item_id,
                    "orientation": orientation,
                    "judge_model": result.model,
                    "from_cache": result.from_cache,
                    "cache_key": result.cache_key,
                    "usage": {
                        "prompt_tokens": result.usage.prompt_tokens,
                        "completion_tokens": result.usage.completion_tokens,
                        "cached_tokens": result.usage.cached_tokens,
                    },
                    "cost_usd": result.cost_usd,
                    "judgment": judgment,
                }
            )
    write_gemini_jsonl(args.out, output_rows)
    print(
        json.dumps(
            {
                **preview,
                "out": args.out,
                "rows": len(output_rows),
                "cache": args.cache,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
