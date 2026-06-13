from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from pipeline.agents.llm_client import LLMClient
from pipeline.agents.llm_client import estimate_prompt_tokens
from pipeline.agents.llm_config import LLMConfig, load_llm_config
from pipeline.prepass.db_source import load_document_from_db
from pipeline.prepass.prompt import build_messages
from pipeline.prepass.persist import build_memory_from_db
from pipeline.prepass.runner import (
    build_d2l_prepass_windows,
    run_prepass,
    run_prepass_document,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run World Builder pre-pass.")
    parser.add_argument("--source", help="Stripped source document.json.")
    parser.add_argument("--db", help="Runtime DB path; reads documents/blocks from DB.")
    parser.add_argument("--doc-id", help="Document id when --db contains multiple docs.")
    parser.add_argument("--chapters", nargs="+", required=True, help="Chapter suffixes.")
    parser.add_argument("--out", help="Output artifact directory.")
    parser.add_argument(
        "--mode",
        default="literary",
        choices=["literary", "d2l_terminology"],
        help="World Builder prompt/validation mode.",
    )
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
    parser.add_argument(
        "--freeze",
        action="store_true",
        help="Persist prepass memory into --db and freeze it. Requires --db.",
    )
    parser.add_argument(
        "--memory-report",
        default="data/reports/d2l_memory_build.json",
        help="Report path for --freeze memory build.",
    )
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="Estimate token usage and exit before any LLM call.",
    )
    parser.add_argument(
        "--skip-preflight",
        action="store_true",
        help="Skip preflight run-level quota estimate; per-call guard still applies.",
    )
    args = parser.parse_args()
    out_dir = args.out or _default_out_dir(args)

    config = load_llm_config(args.config)
    _ensure_api_key()
    client = LLMClient(config=config, cache_path=args.cache)
    memory_report = None
    preflight = None
    if args.db:
        document = load_document_from_db(args.db, args.chapters, doc_id=args.doc_id)
        preflight = _preflight_document(document, args.chapters, config, mode=args.mode)
        if args.preflight_only:
            print(json.dumps(preflight, ensure_ascii=False, indent=2))
            return 0
        if not args.skip_preflight:
            _enforce_preflight(preflight, client.get_usage_today(), config)
        report = run_prepass_document(
            document,
            args.chapters,
            client,
            out_dir,
            document_label=args.db,
            mode=args.mode,
        )
        if args.freeze:
            failed = [chapter.chapter_id for chapter in report.chapters if chapter.status != "passed"]
            if failed:
                print(json.dumps(report.to_json_dict(), ensure_ascii=False, indent=2))
                raise SystemExit(
                    "--freeze refused because prepass chapters failed: "
                    + ", ".join(failed)
                )
            memory_report = build_memory_from_db(
                args.db,
                out_dir,
                doc_id=args.doc_id or str(document.get("doc_id") or "d2l"),
                freeze=True,
            )
            memory_report_path = Path(args.memory_report)
            memory_report_path.parent.mkdir(parents=True, exist_ok=True)
            memory_report_path.write_text(
                json.dumps(memory_report.to_json_dict(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
    else:
        if args.freeze:
            raise SystemExit("--freeze requires --db")
        if not args.source:
            raise SystemExit("Either --source or --db is required")
        if args.out is None:
            raise SystemExit("--out is required when using --source")
        report = run_prepass(args.source, args.chapters, client, out_dir, mode=args.mode)
    payload = report.to_json_dict()
    if preflight is not None:
        payload = {
            "preflight": preflight,
            "prepass": payload,
        }
    if memory_report is not None:
        payload = {
            **({"preflight": preflight} if preflight is not None else {}),
            "prepass": report.to_json_dict(),
            "memory_build": memory_report.to_json_dict(),
        }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _default_out_dir(args: argparse.Namespace) -> str:
    if not args.db:
        return ""
    slug = "_".join(_slug_part(chapter) for chapter in args.chapters)
    if args.mode == "d2l_terminology" and args.chapters == ["deep_learning_computation"]:
        return "data/prepass/d2l_dev"
    if args.mode == "d2l_terminology":
        return "data/prepass/d2l_benchmark"
    return f"data/prepass/{args.mode}_{slug}"


def _slug_part(value: str) -> str:
    return "".join(char if char.isalnum() else "_" for char in str(value)).strip("_") or "chapter"


def _preflight_document(
    document: dict,
    chapter_ids: list[str],
    config: LLMConfig,
    *,
    mode: str,
) -> dict:
    response_format = {"type": "json_object"}
    chapters = _select_loaded_chapters(document, chapter_ids)
    chapter_reports = []
    total_calls = 0
    total_prompt_estimate = 0
    max_prompt_estimate = 0

    for chapter in chapters:
        chapter_id = str(chapter["chapter_id"])
        estimates = []
        if mode == "d2l_terminology":
            windows = build_d2l_prepass_windows(chapter)
            for window in windows:
                window_chapter = {**chapter, "blocks": window.blocks, "window_id": window.window_id}
                messages = build_messages(window_chapter, "", mode=mode)
                estimates.append(
                    {
                        "window_id": window.window_id,
                        "prompt_tokens_est": estimate_prompt_tokens(messages, response_format),
                        "source_tokens_est": window.est_src_tokens,
                    }
                )
        else:
            messages = build_messages(chapter, "(preflight registry omitted)", mode=mode)
            estimates.append(
                {
                    "window_id": chapter_id,
                    "prompt_tokens_est": estimate_prompt_tokens(messages, response_format),
                    "source_tokens_est": 0,
                }
            )

        prompt_total = sum(item["prompt_tokens_est"] for item in estimates)
        prompt_max = max((item["prompt_tokens_est"] for item in estimates), default=0)
        total_calls += len(estimates)
        total_prompt_estimate += prompt_total
        max_prompt_estimate = max(max_prompt_estimate, prompt_max)
        chapter_reports.append(
            {
                "chapter_id": chapter_id,
                "calls": len(estimates),
                "prompt_tokens_est": prompt_total,
                "max_prompt_tokens_est": prompt_max,
                "max_output_tokens_per_call": config.max_output_tokens,
                "total_tokens_upper_bound": prompt_total + len(estimates) * config.max_output_tokens,
            }
        )

    return {
        "mode": mode,
        "chapters_requested": chapter_ids,
        "chapters": chapter_reports,
        "calls": total_calls,
        "prompt_tokens_est": total_prompt_estimate,
        "max_prompt_tokens_est": max_prompt_estimate,
        "max_output_tokens_per_call": config.max_output_tokens,
        "total_tokens_upper_bound": total_prompt_estimate + total_calls * config.max_output_tokens,
        "prompt_token_cap": config.prompt_token_cap,
        "daily_token_cap": config.daily_token_cap,
    }


def _enforce_preflight(preflight: dict, usage_today: dict[str, int | str], config: LLMConfig) -> None:
    prompt_cap = config.prompt_token_cap
    if prompt_cap is not None and int(preflight["max_prompt_tokens_est"]) > prompt_cap:
        raise SystemExit(
            "Preflight refused: max estimated prompt tokens "
            f"{preflight['max_prompt_tokens_est']} > cap {prompt_cap}"
        )
    projected = int(usage_today["total_tokens"]) + int(preflight["total_tokens_upper_bound"])
    if projected > config.daily_token_cap:
        raise SystemExit(
            "Preflight refused: estimated UTC daily token cap would be exceeded: "
            f"{projected} > {config.daily_token_cap}"
        )


def _select_loaded_chapters(document: dict, chapter_ids: list[str]) -> list[dict]:
    chapters = document.get("chapters") or []
    selected = []
    for requested in chapter_ids:
        for chapter in chapters:
            chapter_id = str(chapter.get("chapter_id") or "")
            if chapter_id == requested or chapter_id.endswith(f"_{requested}"):
                selected.append(chapter)
                break
        else:
            raise ValueError(f"Chapter not found: {requested}")
    return selected


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
