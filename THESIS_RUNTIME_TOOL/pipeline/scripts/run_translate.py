from __future__ import annotations

import argparse
import json
import os
import sqlite3
import uuid
from pathlib import Path
from statistics import mean
from typing import Any

from pipeline.agents.llm_client import LLMClient, estimate_prompt_tokens
from pipeline.agents.llm_config import LLMConfig, load_llm_config
from pipeline.memory.store_init import migrate_db
from pipeline.retrieval.context_builder import (
    build_context_pack,
    load_notebook_terms,
    pack_policy_counts,
    pack_repair_queue,
    plan_anchors,
    registry_injection_stats,
)
from pipeline.translate.prompt import build_messages, prompt_version_for_config
from pipeline.translate.profiles import get_profile
from pipeline.translate.runner import TranslateReport, translate_windows
from pipeline.translate.run_events import EventSink
from pipeline.translate.windower import Window, build_windows


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run S0/S1 translation over specified chapters."
    )
    parser.add_argument("--db", required=True, help="Path to the frozen memory SQLite DB.")
    parser.add_argument(
        "--chapters",
        nargs="+",
        required=True,
        help="Chapter suffixes to translate (e.g. ch02 ch03 or D2L slugs).",
    )
    parser.add_argument("--profile", default="literary_v1", help="Document profile.")
    parser.add_argument(
        "--config",
        default=None,
        choices=["S0", "S1"],
        help="Legacy single config name.",
    )
    parser.add_argument(
        "--configs",
        nargs="+",
        choices=["S0", "S1"],
        help="One or more config names.",
    )
    parser.add_argument(
        "--experiment",
        default=None,
        help="Experiment ID. Defaults to d2l_p3 for technical_d2l_v1, else translate_run.",
    )
    parser.add_argument(
        "--source",
        help="Accepted for command compatibility; windowing reads source blocks from DB.",
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
    parser.add_argument("--report", help="Write JSON report to this path.")
    parser.add_argument(
        "--context-budget",
        type=int,
        default=500,
        help="S1 hard-constraints context budget in rough tokens.",
    )
    parser.add_argument(
        "--memory-notebook",
        help="Builder-v2 audited notebook JSON to drive S1 term injection.",
    )
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="Render prompts and estimate tokens without calling the API.",
    )
    parser.add_argument(
        "--event-log",
        help="Optional sidecar JSONL event log path for live observation.",
    )
    parser.add_argument(
        "--run-id",
        dest="attempt_id",
        help="Run/attempt id used inside the sidecar event log.",
    )
    parser.add_argument(
        "--attempt-id",
        dest="attempt_id",
        help="Alias for --run-id.",
    )
    args = parser.parse_args()

    profile = get_profile(args.profile)
    configs = _configs_from_args(args)
    experiment = args.experiment or profile.default_experiment_id
    llm_config = load_llm_config(args.config_file)
    db = _open_db(args.db, read_only=args.preflight_only)
    try:
        doc_id = _single_doc_id(db)
        windows = build_windows(
            db,
            doc_id,
            args.chapters,
            block_types=profile.translatable_block_types,
        )
        notebook_terms = load_notebook_terms(args.memory_notebook) if args.memory_notebook else None
        preflight = _preflight(
            db,
            doc_id,
            windows,
            configs,
            llm_config,
            profile_name=profile.name,
            context_budget_tokens=args.context_budget,
            notebook_terms=notebook_terms,
        )
        _print_preflight(args, experiment, doc_id, profile.name, preflight)
        _raise_if_preflight_unsafe(preflight, llm_config)
        if args.preflight_only:
            return 0
    finally:
        if args.preflight_only:
            db.close()

    _ensure_api_key()
    client = LLMClient(config=llm_config, cache_path=args.cache)
    event_sink = None
    if args.event_log:
        attempt_id = args.attempt_id or f"run_{uuid.uuid4().hex[:12]}"
        event_sink = EventSink(args.event_log, run_id=attempt_id, attempt_id=attempt_id)
    reports: dict[str, dict[str, Any]] = {}
    try:
        for config in configs:
            context_builder = (
                _notebook_context_builder(
                    notebook_terms,
                    profile_name=profile.name,
                    context_budget_tokens=args.context_budget,
                )
                if config.upper() == "S1" and notebook_terms is not None
                else None
            )
            report = translate_windows(
                db,
                windows,
                client,
                experiment_id=experiment,
                config=config,
                context_builder=context_builder,
                context_budget_tokens=args.context_budget,
                profile_name=profile.name,
                event_sink=event_sink,
            )
            reports[config] = report.to_json_dict()
            _print_translate_summary(report)
    finally:
        db.close()

    if args.report:
        out_path = Path(args.report)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(
                {
                    "experiment_id": experiment,
                    "profile": profile.name,
                    "chapters": args.chapters,
                    "memory_notebook": _memory_notebook_report(args.memory_notebook, notebook_terms),
                    "configs": reports,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"\nReport written: {out_path}")

    return 0


def _configs_from_args(args: argparse.Namespace) -> list[str]:
    if args.configs:
        return [str(item).upper() for item in args.configs]
    if args.config:
        return [str(args.config).upper()]
    return ["S0"]


def _open_db(path: str, *, read_only: bool) -> sqlite3.Connection:
    if read_only:
        db_path = Path(path).resolve()
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn
    return migrate_db(path)


def _single_doc_id(db: sqlite3.Connection) -> str:
    row = db.execute("SELECT doc_id FROM documents ORDER BY doc_id LIMIT 1").fetchone()
    if not row:
        raise SystemExit("No document found in DB")
    return str(row["doc_id"])


def _preflight(
    db: sqlite3.Connection,
    doc_id: str,
    windows: list[Window],
    configs: list[str],
    llm_config: LLMConfig,
    *,
    profile_name: str,
    context_budget_tokens: int,
    notebook_terms: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    resolved_chapters = _window_chapter_ids(db, windows)
    result: dict[str, Any] = {
        "resolved_chapters": resolved_chapters,
        "windows": len(windows),
        "blocks": sum(len(window.block_ids) for window in windows),
        "block_type_counts": _block_type_counts(db, doc_id, resolved_chapters),
        "translatable_block_type_counts": _window_block_type_counts(db, windows),
        "registry": registry_injection_stats(db, doc_id, profile_name=profile_name),
        "memory_notebook": {
            "pack_policy_counts": pack_policy_counts(notebook_terms),
            "repair_queue_count": len(pack_repair_queue(notebook_terms)),
        }
        if notebook_terms is not None
        else None,
        "configs": {},
    }
    for config in configs:
        estimates: list[int] = []
        context_terms: list[int] = []
        for window in windows:
            blocks = _fetch_window_blocks(db, window)
            context_pack = None
            if config.upper() == "S1":
                anchors = plan_anchors(
                    db,
                    blocks,
                    profile_name=profile_name,
                    term_rows=notebook_terms,
                )
                context_pack = build_context_pack(
                    db,
                    window,
                    anchors,
                    budget_tokens=context_budget_tokens,
                    term_rows=notebook_terms,
                )
                context_terms.append(_context_term_count(context_pack))
            messages = build_messages(
                blocks,
                prompt_version=prompt_version_for_config(config, profile_name),
                config=config,
                context_pack=context_pack,
                profile_name=profile_name,
            )
            estimates.append(
                estimate_prompt_tokens(messages, response_format={"type": "json_object"})
            )
        total_prompt = sum(estimates)
        upper_total = total_prompt + len(estimates) * llm_config.max_output_tokens
        result["configs"][config.upper()] = {
            "windows": len(estimates),
            "prompt_tokens_min": min(estimates) if estimates else 0,
            "prompt_tokens_avg": round(mean(estimates), 2) if estimates else 0,
            "prompt_tokens_max": max(estimates) if estimates else 0,
            "prompt_tokens_total_est": total_prompt,
            "upper_total_with_max_output": upper_total,
            "injected_terms_min": min(context_terms) if context_terms else 0,
            "injected_terms_avg": round(mean(context_terms), 2) if context_terms else 0,
            "injected_terms_max": max(context_terms) if context_terms else 0,
        }
    result["upper_total_all_configs"] = sum(
        item["upper_total_with_max_output"] for item in result["configs"].values()
    )
    return result


def _notebook_context_builder(
    notebook_terms: list[dict[str, Any]],
    *,
    profile_name: str,
    context_budget_tokens: int,
):
    def build(db: sqlite3.Connection, window: Window, blocks_for_prompt: list[dict[str, Any]]):
        anchors = plan_anchors(
            db,
            blocks_for_prompt,
            profile_name=profile_name,
            term_rows=notebook_terms,
        )
        return build_context_pack(
            db,
            window,
            anchors,
            budget_tokens=context_budget_tokens,
            term_rows=notebook_terms,
        )

    return build


def _context_term_count(context_pack: Any | None) -> int:
    if context_pack is None:
        return 0
    return (
        len(getattr(context_pack, "glossary_lines", []) or [])
        + len(getattr(context_pack, "preserve_lines", []) or [])
        + len(getattr(context_pack, "context_sensitive_lines", []) or [])
    )


def _memory_notebook_report(
    notebook_path: str | None,
    notebook_terms: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    if notebook_path is None or notebook_terms is None:
        return None
    return {
        "path": notebook_path,
        "pack_policy_counts": pack_policy_counts(notebook_terms),
        "repair_queue": pack_repair_queue(notebook_terms),
    }


def _window_chapter_ids(db: sqlite3.Connection, windows: list[Window]) -> list[str]:
    block_ids = [block_id for window in windows for block_id in window.block_ids]
    if not block_ids:
        return []
    placeholders = ",".join("?" * len(block_ids))
    rows = db.execute(
        f"""
        SELECT chapter_id, MIN(order_index) AS first_order
        FROM blocks
        WHERE block_id IN ({placeholders})
        GROUP BY chapter_id
        ORDER BY first_order
        """,
        block_ids,
    ).fetchall()
    return [str(row["chapter_id"]) for row in rows]


def _block_type_counts(
    db: sqlite3.Connection,
    doc_id: str,
    chapter_ids: list[str],
) -> dict[str, int]:
    if not chapter_ids:
        return {}
    placeholders = ",".join("?" * len(chapter_ids))
    rows = db.execute(
        f"""
        SELECT block_type, COUNT(*) AS count
        FROM blocks
        WHERE doc_id = ? AND chapter_id IN ({placeholders})
        GROUP BY block_type
        ORDER BY block_type
        """,
        [doc_id, *chapter_ids],
    ).fetchall()
    return {str(row["block_type"]): int(row["count"]) for row in rows}


def _window_block_type_counts(db: sqlite3.Connection, windows: list[Window]) -> dict[str, int]:
    block_ids = [block_id for window in windows for block_id in window.block_ids]
    if not block_ids:
        return {}
    placeholders = ",".join("?" * len(block_ids))
    rows = db.execute(
        f"""
        SELECT block_type, COUNT(*) AS count
        FROM blocks
        WHERE block_id IN ({placeholders})
        GROUP BY block_type
        ORDER BY block_type
        """,
        block_ids,
    ).fetchall()
    return {str(row["block_type"]): int(row["count"]) for row in rows}


def _fetch_window_blocks(db: sqlite3.Connection, window: Window) -> list[dict[str, Any]]:
    if not window.block_ids:
        return []
    placeholders = ",".join("?" * len(window.block_ids))
    rows = db.execute(
        f"""
        SELECT block_id, doc_id, chapter_id, order_index, block_type,
               text AS clean_text, original_text AS source_text
        FROM blocks
        WHERE block_id IN ({placeholders})
        ORDER BY order_index
        """,
        list(window.block_ids),
    ).fetchall()
    return [dict(row) for row in rows]


def _print_preflight(
    args: argparse.Namespace,
    experiment: str,
    doc_id: str,
    profile_name: str,
    preflight: dict[str, Any],
) -> None:
    print(
        f"Experiment: {experiment}  Profile: {profile_name}  DB: {args.db}  "
        f"Chapters: {args.chapters}  Doc: {doc_id}"
    )
    print("\n=== Preflight ===")
    print(f"Resolved chapters: {preflight['resolved_chapters']}")
    print(f"Windows planned: {preflight['windows']}")
    print(f"Blocks in windows: {preflight['blocks']}")
    print(f"All block types: {preflight['block_type_counts']}")
    print(f"Translatable block types: {preflight['translatable_block_type_counts']}")
    print(f"Registry injection stats: {preflight['registry']}")
    if preflight.get("memory_notebook"):
        notebook = preflight["memory_notebook"]
        print(f"Memory notebook policy: {notebook['pack_policy_counts']}")
        print(f"Memory notebook repair queue: {notebook['repair_queue_count']}")
    for config, item in preflight["configs"].items():
        print(f"\n[{config}]")
        print(
            "  prompt_tokens min/avg/max: "
            f"{item['prompt_tokens_min']} / {item['prompt_tokens_avg']} / {item['prompt_tokens_max']}"
        )
        print(f"  prompt_tokens total est: {item['prompt_tokens_total_est']}")
        print(f"  upper total with max output: {item['upper_total_with_max_output']}")
        if config == "S1":
            print(
                "  injected_terms min/avg/max: "
                f"{item['injected_terms_min']} / {item['injected_terms_avg']} / {item['injected_terms_max']}"
            )
    print(f"\nUpper total all configs: {preflight['upper_total_all_configs']}")


def _raise_if_preflight_unsafe(preflight: dict[str, Any], llm_config: LLMConfig) -> None:
    prompt_cap = llm_config.prompt_token_cap
    if prompt_cap is not None:
        for config, item in preflight["configs"].items():
            max_prompt = int(item["prompt_tokens_max"])
            if max_prompt > prompt_cap:
                raise SystemExit(
                    f"Preflight abort: {config} prompt max {max_prompt} > cap {prompt_cap}"
                )
    if preflight["upper_total_all_configs"] > llm_config.daily_token_cap:
        raise SystemExit(
            "Preflight abort: upper token estimate "
            f"{preflight['upper_total_all_configs']} > daily cap {llm_config.daily_token_cap}"
        )


def _print_translate_summary(report: TranslateReport) -> None:
    print(f"\n=== Translate Report: {report.config} ===")
    print(f"Windows total:      {report.windows_total}")
    print(f"  translated:       {report.windows_translated}")
    print(f"  failed:           {report.windows_failed}")
    print(f"  skipped:          {report.windows_skipped}")
    print(f"Blocks translated:  {report.blocks_translated}")
    print(f"Blocks failed:      {report.blocks_failed}")
    print(f"JSON fail rate:     {report.json_fail_rate:.4f}")
    usage = report.total_usage
    print("\n=== Usage ===")
    print(f"  prompt_tokens:      {usage['prompt_tokens']}")
    print(f"  completion_tokens:  {usage['completion_tokens']}")
    print(f"  total_cost_usd:     ${usage['cost_usd']:.6f}")
    print(f"  incremental_cost:   ${usage['incremental_cost_usd']:.6f}")
    print(f"  calls:              {usage['calls']}")
    print(f"  cache_hits:         {usage['cache_hits']}")
    print(f"Model: {report.model}  Seed: {report.seed}")
    if report.config.upper() == "S1":
        context_stats = report.context_stats
        print("\n=== Context ===")
        print(f"  windows_with_context: {context_stats['windows_with_context']}")
        print(f"  low_context_windows:  {context_stats['windows_low_context']}")
        print(f"  dropped_by_budget:    {context_stats['dropped_by_budget']}")


def _ensure_api_key() -> None:
    if os.environ.get("OPENAI_API_KEY"):
        return
    repo_root = Path(__file__).resolve().parents[3]
    for filename in ["OPENAI-KEY-2.txt", "OPENAI-KEY-1.txt", "API-KEY.txt"]:
        key_path = repo_root / filename
        if key_path.exists():
            key = key_path.read_text(encoding="utf-8").strip()
            if key:
                os.environ["OPENAI_API_KEY"] = key
                return
    raise SystemExit(
        "OPENAI_API_KEY is not set and OPENAI-KEY-2.txt / OPENAI-KEY-1.txt / API-KEY.txt are missing or empty"
    )


if __name__ == "__main__":
    raise SystemExit(main())
