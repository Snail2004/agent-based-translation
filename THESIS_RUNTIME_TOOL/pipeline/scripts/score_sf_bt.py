from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from google import genai
from google.genai import types

from pipeline.scripts.probe_sf_bt import (
    ALLOWED_FLAGS,
    BT_PROMPT_TEMPLATE,
    JUDGE_PROMPT_TEMPLATE,
    PROMPT_VERSION_BT,
    PROMPT_VERSION_JUDGE,
    SEED,
    SqliteCache,
    _bt_hygiene,
    _cache_key,
    _chat_completion_cached,
    _cosine_for_pair,
    _estimated_tokens,
    _sha256_file,
    _sha256_text,
    _write_json,
)


DEFAULT_WORKDB = Path("data/jobs/exp_s0s1_full/memory.sqlite3")
DEFAULT_OUT = Path("data/reports/exp_s0s1_builderv2_v1/sf_bt_pilot_100.json")
DEFAULT_CACHE = Path("data/reports/exp_s0s1_builderv2_v1/sf_bt_pilot_cache.sqlite3")
DEFAULT_EXPERIMENT = "exp_s0s1_builderv2_v1"
DEFAULT_CHAPTERS = "d2l_multilayer_perceptrons,d2l_preliminaries"
DEFAULT_CONFIGS = "S0,S1"
DEFAULT_ENDPOINT = "http://127.0.0.1:1234"
DEFAULT_EMBED_ENDPOINT = "http://127.0.0.1:1234/v1/embeddings"
DEFAULT_BT_MODEL = "google/gemma-4-12b"
DEFAULT_EMBED_MODEL = "text-embedding-bge-m3"
DEFAULT_JUDGE_MODEL = "gemini-2.5-flash"
DEFAULT_GEMINI_INPUT_PER_MILLION = 0.72
DEFAULT_GEMINI_OUTPUT_PER_MILLION = 6.00


def main() -> int:
    parser = argparse.ArgumentParser(description="Score SF-BT pilot on deterministic block samples.")
    parser.add_argument("--db", default=str(DEFAULT_WORKDB))
    parser.add_argument("--experiment", default=DEFAULT_EXPERIMENT)
    parser.add_argument("--chapters", default=DEFAULT_CHAPTERS)
    parser.add_argument("--configs", default=DEFAULT_CONFIGS)
    parser.add_argument("--sample-per-chapter", type=int, default=25)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--cache-db", default=str(DEFAULT_CACHE))
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--embed-endpoint", default=DEFAULT_EMBED_ENDPOINT)
    parser.add_argument("--bt-model", default=DEFAULT_BT_MODEL)
    parser.add_argument("--embed-model", default=DEFAULT_EMBED_MODEL)
    parser.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL)
    parser.add_argument("--bt-max-tokens", type=int, default=2048)
    parser.add_argument("--judge-max-tokens", type=int, default=512)
    parser.add_argument("--chat-timeout-sec", type=int, default=240)
    parser.add_argument("--embed-timeout-sec", type=int, default=180)
    parser.add_argument("--judge-timeout-sec", type=int, default=120)
    parser.add_argument("--judge-concurrency", type=int, default=8)
    parser.add_argument("--thinking-budget", type=int, default=0)
    parser.add_argument("--gemini-input-price", type=float, default=DEFAULT_GEMINI_INPUT_PER_MILLION)
    parser.add_argument("--gemini-output-price", type=float, default=DEFAULT_GEMINI_OUTPUT_PER_MILLION)
    args = parser.parse_args()

    started = time.perf_counter()
    db_path = Path(args.db)
    out_path = Path(args.out)
    cache = SqliteCache(Path(args.cache_db))
    chapters = [item.strip() for item in str(args.chapters).split(",") if item.strip()]
    configs = [item.strip() for item in str(args.configs).split(",") if item.strip()]
    if len(configs) != 2:
        raise SystemExit("SF-BT pilot expects exactly two configs, e.g. S0,S1.")

    db_sha_before = _sha256_file(db_path)
    sample_blocks, sampling_report = _sample_blocks(
        db_path,
        experiment=args.experiment,
        chapters=chapters,
        configs=configs,
        sample_per_chapter=args.sample_per_chapter,
    )
    items = _load_items(db_path, experiment=args.experiment, block_ids=[row["block_id"] for row in sample_blocks], configs=configs)
    if not items:
        raise SystemExit("No sample translation rows found.")

    report: dict[str, Any] = {
        "metric": "SF-BT",
        "phase": "pilot_100_block_arm" if args.sample_per_chapter == 25 else "pilot_custom",
        "status": "running",
        "out": str(out_path),
        "db": str(db_path),
        "db_sha256_before": db_sha_before,
        "experiment": args.experiment,
        "chapters": chapters,
        "configs": configs,
        "sample_per_chapter": args.sample_per_chapter,
        "sampling": sampling_report,
        "models": {
            "bt_model": args.bt_model,
            "embed_model": args.embed_model,
            "judge_model": args.judge_model,
            "endpoint": args.endpoint,
            "embed_endpoint": args.embed_endpoint,
            "judge_provider": "gemini",
        },
        "prompt_versions": {
            "bt": PROMPT_VERSION_BT,
            "judge": PROMPT_VERSION_JUDGE,
            "bt_prompt_sha256": _sha256_text(BT_PROMPT_TEMPLATE),
            "judge_prompt_sha256": _sha256_text(JUDGE_PROMPT_TEMPLATE),
        },
        "request_profile": {
            "bt": {
                "temperature": 0,
                "top_p": 1,
                "seed": SEED,
                "repeat_penalty": 1.0,
                "reasoning_effort": "none",
                "max_tokens": args.bt_max_tokens,
            },
            "judge": {
                "temperature": 0,
                "thinking_budget": args.thinking_budget,
                "response_mime_type": "application/json",
                "max_output_tokens": args.judge_max_tokens,
                "concurrency": args.judge_concurrency,
            },
        },
        "items": [],
    }
    _write_json(out_path, report)

    bt_started = time.perf_counter()
    for index, item in enumerate(items, 1):
        print(f"[BT {index}/{len(items)}] {item['chapter_id']} {item['config']} {item['block_id']}", flush=True)
        _run_bt_only(
            item,
            cache=cache,
            endpoint=args.endpoint,
            bt_model=args.bt_model,
            bt_max_tokens=args.bt_max_tokens,
            chat_timeout_sec=args.chat_timeout_sec,
        )
        report["items"].append(item)
        if index % 10 == 0 or index == len(items):
            _write_json(out_path, _partial(report, cache, started, db_path))
    bt_elapsed = time.perf_counter() - bt_started

    cosine_started = time.perf_counter()
    for index, item in enumerate(report["items"], 1):
        print(f"[COS {index}/{len(report['items'])}] {item['chapter_id']} {item['config']} {item['block_id']}", flush=True)
        _run_cosine_only(
            item,
            cache=cache,
            embed_endpoint=args.embed_endpoint,
            embed_model=args.embed_model,
            embed_timeout_sec=args.embed_timeout_sec,
        )
        if index % 10 == 0 or index == len(report["items"]):
            _write_json(out_path, _partial(report, cache, started, db_path))
    cosine_elapsed = time.perf_counter() - cosine_started

    judge_started = time.perf_counter()
    _run_gemini_judge(
        report["items"],
        cache=cache,
        model=args.judge_model,
        concurrency=args.judge_concurrency,
        timeout_sec=args.judge_timeout_sec,
        max_output_tokens=args.judge_max_tokens,
        thinking_budget=args.thinking_budget,
        input_price=args.gemini_input_price,
        output_price=args.gemini_output_price,
    )
    judge_elapsed = time.perf_counter() - judge_started

    report["status"] = "pilot_complete_stop_no_full_run"
    report["db_sha256_after"] = _sha256_file(db_path)
    report["cache"] = cache.stats()
    report["elapsed_sec"] = time.perf_counter() - started
    report["runtime_summary"] = _runtime_summary(
        report["items"],
        bt_elapsed=bt_elapsed,
        cosine_elapsed=cosine_elapsed,
        judge_elapsed=judge_elapsed,
    )
    report["hygiene_summary"] = _hygiene_summary(report["items"])
    report["too_long_summary"] = _too_long_summary(report["items"])
    report["aggregates"] = _aggregates(report["items"], configs=configs)
    report["latency_by_length"] = _latency_by_length(report["items"])
    report["cost_eta"] = _cost_eta(
        report["items"],
        full_block_arm_items=_full_block_arm_count(db_path, experiment=args.experiment, chapters=chapters, configs=configs),
        runtime_summary=report["runtime_summary"],
    )
    report["db_unchanged"] = report["db_sha256_before"] == report["db_sha256_after"]
    _write_json(out_path, report)
    print(json.dumps(_console_summary(report), ensure_ascii=False, indent=2))
    return 0


def _sample_blocks(
    db_path: Path,
    *,
    experiment: str,
    chapters: list[str],
    configs: list[str],
    sample_per_chapter: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    by_chapter: dict[str, Any] = {}
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row
        for chapter in chapters:
            rows = con.execute(
                """
                SELECT
                    b.chapter_id,
                    b.block_id,
                    b.order_index,
                    COALESCE(NULLIF(b.original_text, ''), b.text) AS source_text,
                    COUNT(DISTINCT tr.config) AS config_count
                FROM blocks b
                JOIN translation_runs tr ON tr.block_id = b.block_id
                WHERE tr.experiment_id = ?
                  AND tr.stage = 'draft'
                  AND b.chapter_id = ?
                  AND tr.config IN ({})
                GROUP BY b.chapter_id, b.block_id, b.order_index, source_text
                HAVING config_count = ?
                ORDER BY b.order_index
                """.format(",".join("?" * len(configs))),
                [experiment, chapter, *configs, len(configs)],
            ).fetchall()
            if len(rows) < sample_per_chapter:
                raise ValueError(f"Chapter {chapter} has only {len(rows)} eligible blocks, need {sample_per_chapter}.")
            indices = _systematic_midpoint_indices(len(rows), sample_per_chapter)
            chapter_selected = []
            for sample_ordinal, idx in enumerate(indices, 1):
                row = rows[idx]
                item = {
                    "chapter_id": str(row["chapter_id"]),
                    "block_id": str(row["block_id"]),
                    "order_index": int(row["order_index"]),
                    "sample_ordinal": sample_ordinal,
                    "eligible_index_0based": idx,
                    "source_char_count": len(str(row["source_text"] or "")),
                    "source_token_count": len(str(row["source_text"] or "").split()),
                }
                selected.append(item)
                chapter_selected.append(item)
            by_chapter[chapter] = {
                "eligible_blocks": len(rows),
                "sample_count": len(chapter_selected),
                "formula": "idx_i = floor((i + 0.5) * N / k), i=0..k-1, over eligible translated blocks sorted by order_index",
                "selected": chapter_selected,
            }
    return selected, {
        "sample_unit": "block_id",
        "arm_unit": "block_id x config",
        "eligible_definition": "block has draft translation rows for every requested config",
        "sample_per_chapter": sample_per_chapter,
        "by_chapter": by_chapter,
        "selected_block_ids": [item["block_id"] for item in selected],
    }


def _systematic_midpoint_indices(total: int, sample_count: int) -> list[int]:
    indices = [min(total - 1, int((i + 0.5) * total / sample_count)) for i in range(sample_count)]
    if len(set(indices)) != len(indices):
        deduped: list[int] = []
        used: set[int] = set()
        for idx in indices:
            candidate = idx
            while candidate in used and candidate + 1 < total:
                candidate += 1
            while candidate in used and candidate - 1 >= 0:
                candidate -= 1
            used.add(candidate)
            deduped.append(candidate)
        indices = deduped
    return indices


def _load_items(
    db_path: Path,
    *,
    experiment: str,
    block_ids: list[str],
    configs: list[str],
) -> list[dict[str, Any]]:
    if not block_ids:
        return []
    block_placeholders = ",".join("?" * len(block_ids))
    config_placeholders = ",".join("?" * len(configs))
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            f"""
            SELECT
                b.chapter_id,
                b.block_id,
                b.order_index,
                b.block_type,
                COALESCE(NULLIF(b.original_text, ''), b.text) AS source_text,
                tr.config,
                tr.output_text
            FROM translation_runs tr
            JOIN blocks b ON b.block_id = tr.block_id
            WHERE tr.experiment_id = ?
              AND tr.stage = 'draft'
              AND b.block_id IN ({block_placeholders})
              AND tr.config IN ({config_placeholders})
            ORDER BY b.chapter_id, b.order_index, tr.config
            """,
            [experiment, *block_ids, *configs],
        ).fetchall()
    block_order = {block_id: idx for idx, block_id in enumerate(block_ids)}
    items = [
        {
            "item_id": f"{row['chapter_id']}::{row['block_id']}::{row['config']}",
            "chapter_id": str(row["chapter_id"]),
            "block_id": str(row["block_id"]),
            "order_index": int(row["order_index"]),
            "block_type": str(row["block_type"] or ""),
            "config": str(row["config"]),
            "source_text": str(row["source_text"] or ""),
            "output_text": str(row["output_text"] or ""),
            "source_char_count": len(str(row["source_text"] or "")),
            "source_token_count": len(str(row["source_text"] or "").split()),
            "short_block": len(str(row["source_text"] or "")) < 40 or len(str(row["source_text"] or "").split()) < 8,
        }
        for row in rows
    ]
    return sorted(items, key=lambda item: (block_order.get(item["block_id"], 10**9), configs.index(item["config"])))


def _run_bt_only(
    item: dict[str, Any],
    *,
    cache: SqliteCache,
    endpoint: str,
    bt_model: str,
    bt_max_tokens: int,
    chat_timeout_sec: int,
) -> None:
    bt_prompt = BT_PROMPT_TEMPLATE.format(vi_block=item["output_text"])
    request_profile = {
        "temperature": 0,
        "top_p": 1,
        "seed": SEED,
        "repeat_penalty": 1.0,
        "reasoning_effort": "none",
        "max_tokens": bt_max_tokens,
    }
    bt_call = _chat_completion_cached(
        cache=cache,
        endpoint=endpoint,
        model=bt_model,
        messages=[{"role": "user", "content": bt_prompt}],
        prompt_version=PROMPT_VERSION_BT,
        prompt_text=BT_PROMPT_TEMPLATE,
        request_params=request_profile,
        timeout_sec=chat_timeout_sec,
    )
    bt_text = str(bt_call["content"])
    item["bt"] = {
        "text": bt_text,
        "finish_reason": bt_call.get("finish_reason", ""),
        "model_echo": bt_call.get("model_echo", ""),
        "usage": bt_call.get("usage", {}),
        "latency_sec": bt_call.get("latency_sec", 0.0),
        "from_cache": bt_call.get("from_cache", False),
        "content_prefix": bt_text[:160],
    }
    item["hygiene"] = _bt_hygiene(
        src=item["source_text"],
        vi=item["output_text"],
        bt=bt_text,
        finish_reason=str(bt_call.get("finish_reason") or ""),
    )
    item["too_long_for_llm"] = _estimated_tokens(JUDGE_PROMPT_TEMPLATE.format(first=item["source_text"], second=bt_text)) > 7000


def _run_cosine_only(
    item: dict[str, Any],
    *,
    cache: SqliteCache,
    embed_endpoint: str,
    embed_model: str,
    embed_timeout_sec: int,
) -> None:
    bt_text = str((item.get("bt") or {}).get("text") or "")
    item["bt_bge_cosine"] = _cosine_for_pair(
        cache=cache,
        endpoint=embed_endpoint,
        model=embed_model,
        left=item["source_text"],
        right=bt_text,
        timeout_sec=embed_timeout_sec,
    )
    item["direct_bge_en_vi"] = _cosine_for_pair(
        cache=cache,
        endpoint=embed_endpoint,
        model=embed_model,
        left=item["source_text"],
        right=item["output_text"],
        timeout_sec=embed_timeout_sec,
    )


def _run_gemini_judge(
    items: list[dict[str, Any]],
    *,
    cache: SqliteCache,
    model: str,
    concurrency: int,
    timeout_sec: int,
    max_output_tokens: int,
    thinking_budget: int,
    input_price: float,
    output_price: float,
) -> None:
    tasks = []
    for item in items:
        item["bt_llm_score"] = {}
        if item.get("too_long_for_llm"):
            item["bt_llm_score"]["skipped"] = "too_long_for_llm"
            continue
        source = str(item["source_text"])
        bt_text = str((item.get("bt") or {}).get("text") or "")
        tasks.append((item["item_id"], "ab", source, bt_text))
        tasks.append((item["item_id"], "ba", bt_text, source))
    item_by_id = {item["item_id"]: item for item in items}
    api_tasks = []
    for task in tasks:
        item_id, direction, first, second = task
        key = _judge_cache_key(
            model=model,
            direction=direction,
            first=first,
            second=second,
            max_output_tokens=max_output_tokens,
            thinking_budget=thinking_budget,
        )
        cached = cache.get(key)
        if cached is not None:
            cached["from_cache"] = True
            item_by_id[item_id]["bt_llm_score"][direction] = cached
        else:
            api_tasks.append((key, *task))
    if api_tasks:
        key = _load_gemini_key()
        base_url = _gemini_base_url(key)
        thread_local = threading.local()

        def get_client() -> Any:
            client = getattr(thread_local, "client", None)
            if client is None:
                http_options = (
                    types.HttpOptions(baseUrl=base_url, timeout=timeout_sec * 1000)
                    if base_url
                    else types.HttpOptions(timeout=timeout_sec * 1000)
                )
                client = genai.Client(api_key=key, http_options=http_options)
                thread_local.client = client
            return client

        def call_api(task: tuple[str, str, str, str, str]) -> tuple[str, str, str, dict[str, Any]]:
            cache_key, item_id, direction, first, second = task
            result = _call_gemini_judge_with_retry(
                get_client=get_client,
                model=model,
                first=first,
                second=second,
                direction=direction,
                timeout_sec=timeout_sec,
                max_output_tokens=max_output_tokens,
                thinking_budget=thinking_budget,
                input_price=input_price,
                output_price=output_price,
            )
            result["cache_key"] = cache_key
            return cache_key, item_id, direction, result

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [pool.submit(call_api, task) for task in api_tasks]
            for index, future in enumerate(as_completed(futures), 1):
                cache_key, item_id, direction, result = future.result()
                item_by_id[item_id]["bt_llm_score"][direction] = result
                if not result.get("validation_error") and not result.get("transport_error"):
                    cache.put(cache_key, result)
                if index % 20 == 0 or index == len(futures):
                    print(f"[JUDGE {index}/{len(futures)}] completed", flush=True)
    for item in items:
        judge = item.get("bt_llm_score") or {}
        scores = [
            judge.get(direction, {}).get("score")
            for direction in ("ab", "ba")
            if isinstance(judge.get(direction, {}).get("score"), (int, float))
        ]
        judge["score_mean"] = statistics.mean(scores) if scores else None
        ab = judge.get("ab", {}).get("score")
        ba = judge.get("ba", {}).get("score")
        judge["score_delta_abs"] = abs(float(ab) - float(ba)) if isinstance(ab, (int, float)) and isinstance(ba, (int, float)) else None


def _judge_cache_key(
    *,
    model: str,
    direction: str,
    first: str,
    second: str,
    max_output_tokens: int,
    thinking_budget: int,
) -> str:
    return _cache_key(
        {
            "kind": "gemini_judge",
            "provider": "gemini",
            "base_url_policy": "env_or_shopaikey_if_sk",
            "model": model,
            "direction": direction,
            "prompt_version": PROMPT_VERSION_JUDGE,
            "prompt_sha256": _sha256_text(JUDGE_PROMPT_TEMPLATE),
            "input_sha256": _sha256_text(json.dumps({"first": first, "second": second}, ensure_ascii=False, sort_keys=True)),
            "temperature": 0,
            "max_output_tokens": max_output_tokens,
            "thinking_budget": thinking_budget,
            "response_mime_type": "application/json",
        }
    )


def _call_gemini_judge_with_retry(
    *,
    get_client: Any,
    model: str,
    first: str,
    second: str,
    direction: str,
    timeout_sec: int,
    max_output_tokens: int,
    thinking_budget: int,
    input_price: float,
    output_price: float,
) -> dict[str, Any]:
    attempts = []
    for attempt in (1, 2):
        started = time.perf_counter()
        try:
            prompt = JUDGE_PROMPT_TEMPLATE.format(first=first, second=second)
            response = get_client().models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0,
                    max_output_tokens=max_output_tokens,
                    response_mime_type="application/json",
                    thinking_config=types.ThinkingConfig(thinking_budget=thinking_budget),
                ),
            )
            latency = time.perf_counter() - started
            text = str(getattr(response, "text", "") or "")
            usage = _gemini_usage(response)
            model_version = str(getattr(response, "model_version", "") or "")
            finish_reason = _gemini_finish_reason(response)
            score, flags, note, validation_error = _validate_judge_json(text)
            result = {
                "direction": direction,
                "score": score,
                "flags": flags,
                "note": note,
                "validation_error": validation_error,
                "transport_error": "",
                "raw_content_prefix": text[:240],
                "finish_reason": finish_reason,
                "model_echo": model,
                "model_version": model_version,
                "usage": usage,
                "latency_sec": latency,
                "from_cache": False,
                "cost_usd": _gemini_cost(usage, input_price=input_price, output_price=output_price),
                "attempt": attempt,
                "attempts": attempts,
            }
            attempts.append(
                {
                    "attempt": attempt,
                    "latency_sec": latency,
                    "validation_error": validation_error,
                    "transport_error": "",
                    "finish_reason": finish_reason,
                    "usage": usage,
                }
            )
            if not validation_error:
                result["attempts"] = attempts
                return result
        except Exception as exc:  # noqa: BLE001
            latency = time.perf_counter() - started
            attempts.append(
                {
                    "attempt": attempt,
                    "latency_sec": latency,
                    "validation_error": "",
                    "transport_error": f"{type(exc).__name__}: {exc}",
                }
            )
            result = {
                "direction": direction,
                "score": None,
                "flags": [],
                "note": "",
                "validation_error": "",
                "transport_error": f"{type(exc).__name__}: {exc}",
                "raw_content_prefix": "",
                "finish_reason": "",
                "model_echo": model,
                "model_version": "",
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "thoughts_tokens": 0},
                "latency_sec": latency,
                "from_cache": False,
                "cost_usd": 0.0,
                "attempt": attempt,
                "attempts": attempts,
            }
        if attempt == 1:
            time.sleep(0.5)
    result["attempts"] = attempts
    return result


def _validate_judge_json(text: str) -> tuple[float | None, list[str], str, str]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None, [], "", "json_parse_fail"
    if not isinstance(parsed, dict):
        return None, [], "", "json_not_object"
    try:
        score = float(parsed["score"])
        error = ""
        if score < 0 or score > 100:
            error = "score_out_of_range"
        raw_flags = parsed.get("flags") or []
        if not isinstance(raw_flags, list) or any(str(flag) not in ALLOWED_FLAGS for flag in raw_flags):
            error = error or "invalid_flags"
        flags = [str(flag) for flag in raw_flags if str(flag) in ALLOWED_FLAGS]
        note = str(parsed.get("note") or "")
        return score, flags, note, error
    except Exception as exc:  # noqa: BLE001
        return None, [], "", f"schema_error:{type(exc).__name__}"


def _gemini_usage(response: Any) -> dict[str, int]:
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return {"prompt_tokens": 0, "completion_tokens": 0, "thoughts_tokens": 0}
    thoughts = getattr(usage, "thoughts_token_count", None)
    return {
        "prompt_tokens": int(getattr(usage, "prompt_token_count", 0) or 0),
        "completion_tokens": int(getattr(usage, "candidates_token_count", 0) or 0),
        "thoughts_tokens": int(thoughts or 0),
    }


def _gemini_finish_reason(response: Any) -> str:
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return ""
    return str(getattr(candidates[0], "finish_reason", "") or "")


def _gemini_cost(usage: dict[str, int], *, input_price: float, output_price: float) -> float:
    return round(
        usage.get("prompt_tokens", 0) / 1_000_000 * input_price
        + usage.get("completion_tokens", 0) / 1_000_000 * output_price,
        12,
    )


def _load_gemini_key() -> str:
    for env_name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        value = os.environ.get(env_name, "").strip()
        if value:
            return value
    for parent in [Path.cwd(), *Path.cwd().parents]:
        candidate = parent / "GEMINI-KEY.txt"
        if candidate.exists():
            value = candidate.read_text(encoding="utf-8").strip()
            if value:
                return value
    raise RuntimeError("Gemini key not found; set GEMINI_API_KEY/GOOGLE_API_KEY or create GEMINI-KEY.txt.")


def _gemini_base_url(api_key: str) -> str | None:
    value = os.environ.get("GEMINI_BASE_URL", "").strip()
    if value:
        return value.rstrip("/")
    for parent in [Path.cwd(), *Path.cwd().parents]:
        candidate = parent / "GEMINI-BASE-URL.txt"
        if candidate.exists():
            text = candidate.read_text(encoding="utf-8").strip()
            if text:
                return text.rstrip("/")
    if api_key.startswith("sk-"):
        return "https://api.shopaikey.com"
    return None


def _full_block_arm_count(db_path: Path, *, experiment: str, chapters: list[str], configs: list[str]) -> int:
    chapter_placeholders = ",".join("?" * len(chapters))
    config_placeholders = ",".join("?" * len(configs))
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as con:
        row = con.execute(
            f"""
            SELECT COUNT(*) AS n
            FROM translation_runs tr
            JOIN blocks b ON b.block_id = tr.block_id
            WHERE tr.experiment_id = ?
              AND tr.stage = 'draft'
              AND b.chapter_id IN ({chapter_placeholders})
              AND tr.config IN ({config_placeholders})
            """,
            [experiment, *chapters, *configs],
        ).fetchone()
    return int(row[0])


def _aggregates(items: list[dict[str, Any]], *, configs: list[str]) -> dict[str, Any]:
    return {
        "all": _aggregate_scope(items, configs=configs),
        "without_flags": _aggregate_scope([item for item in items if not _item_flags(item)], configs=configs),
        "by_flag_filter_note": "without_flags excludes short_block, too_long_for_llm, and any BT hygiene flag",
    }


def _aggregate_scope(items: list[dict[str, Any]], *, configs: list[str]) -> dict[str, Any]:
    components = {
        "SF-BT-cos": lambda item: _component_value(item, "bt_bge_cosine"),
        "SF-BT-llm": lambda item: (item.get("bt_llm_score") or {}).get("score_mean"),
        "direct_bge_en_vi": lambda item: _component_value(item, "direct_bge_en_vi"),
    }
    per_chapter_arm: dict[str, Any] = {}
    paired: dict[str, Any] = {}
    for component, getter in components.items():
        grouped: dict[tuple[str, str], list[float]] = defaultdict(list)
        for item in items:
            value = getter(item)
            if isinstance(value, (int, float)):
                grouped[(item["chapter_id"], item["config"])].append(float(value))
        per_chapter_arm[component] = {}
        for (chapter, config), scores in sorted(grouped.items()):
            per_chapter_arm[component].setdefault(chapter, {})[config] = _score_stats(scores)
        paired[component] = _paired_delta(items, getter=getter, configs=configs)
    return {"per_chapter_arm": per_chapter_arm, "paired_delta": paired}


def _component_value(item: dict[str, Any], key: str) -> float | None:
    value = (item.get(key) or {}).get("score")
    return float(value) if isinstance(value, (int, float)) else None


def _paired_delta(items: list[dict[str, Any]], *, getter: Any, configs: list[str]) -> dict[str, Any]:
    a, b = configs
    by_block: dict[tuple[str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    for item in items:
        by_block[(item["chapter_id"], item["block_id"])][item["config"]] = item
    pairs = []
    by_chapter: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for (chapter, block_id), row in sorted(by_block.items()):
        if a not in row or b not in row:
            continue
        score_a = getter(row[a])
        score_b = getter(row[b])
        if not isinstance(score_a, (int, float)) or not isinstance(score_b, (int, float)):
            continue
        pair = {
            "chapter_id": chapter,
            "block_id": block_id,
            f"{a}_score": float(score_a),
            f"{b}_score": float(score_b),
            "delta": float(score_b) - float(score_a),
        }
        pairs.append(pair)
        by_chapter[chapter].append(pair)
    return {
        "direction": f"{b}-{a}",
        "overall": _delta_stats(pairs),
        "by_chapter": {chapter: _delta_stats(rows) for chapter, rows in sorted(by_chapter.items())},
        "pairs": pairs,
    }


def _score_stats(scores: list[float]) -> dict[str, float | int]:
    ordered = sorted(scores)
    return {
        "n": len(scores),
        "mean": statistics.mean(scores) if scores else 0.0,
        "median": statistics.median(scores) if scores else 0.0,
        "p25": _quantile(ordered, 0.25),
        "p75": _quantile(ordered, 0.75),
        "min": min(scores) if scores else 0.0,
        "max": max(scores) if scores else 0.0,
    }


def _delta_stats(pairs: list[dict[str, Any]]) -> dict[str, Any]:
    deltas = [float(pair["delta"]) for pair in pairs]
    return {
        "n": len(deltas),
        "mean_delta": statistics.mean(deltas) if deltas else 0.0,
        "median_delta": statistics.median(deltas) if deltas else 0.0,
        "pct_s1_gt_s0": sum(1 for delta in deltas if delta > 0) / len(deltas) if deltas else 0.0,
        "pct_s0_gt_s1": sum(1 for delta in deltas if delta < 0) / len(deltas) if deltas else 0.0,
        "pct_abs_delta_lt_0_01": sum(1 for delta in deltas if abs(delta) < 0.01) / len(deltas) if deltas else 0.0,
    }


def _quantile(ordered: list[float], q: float) -> float:
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    frac = pos - lo
    return ordered[lo] * (1 - frac) + ordered[hi] * frac


def _item_flags(item: dict[str, Any]) -> list[str]:
    flags: list[str] = []
    if item.get("short_block"):
        flags.append("short_block")
    if item.get("too_long_for_llm"):
        flags.append("too_long_for_llm")
    flags.extend((item.get("hygiene") or {}).get("flags") or [])
    return sorted(set(flags))


def _hygiene_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    item_flags = []
    for item in items:
        flags = _item_flags(item)
        counts.update(flags)
        if flags:
            item_flags.append(
                {
                    "item_id": item["item_id"],
                    "chapter_id": item["chapter_id"],
                    "block_id": item["block_id"],
                    "config": item["config"],
                    "flags": flags,
                }
            )
    return {"flag_counts": dict(sorted(counts.items())), "items_with_flags": item_flags}


def _too_long_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [
        {"item_id": item["item_id"], "chapter_id": item["chapter_id"], "block_id": item["block_id"], "config": item["config"]}
        for item in items
        if item.get("too_long_for_llm")
    ]
    return {"count": len(rows), "items": rows}


def _runtime_summary(
    items: list[dict[str, Any]],
    *,
    bt_elapsed: float,
    cosine_elapsed: float,
    judge_elapsed: float,
) -> dict[str, Any]:
    bt_latencies = [float((item.get("bt") or {}).get("latency_sec") or 0.0) for item in items]
    cos_latencies = [
        float((item.get(name) or {}).get("latency_sec") or 0.0)
        for item in items
        for name in ("bt_bge_cosine", "direct_bge_en_vi")
    ]
    judge_rows = [
        (item.get("bt_llm_score") or {}).get(direction, {})
        for item in items
        for direction in ("ab", "ba")
        if direction in (item.get("bt_llm_score") or {})
    ]
    judge_latencies = [float(row.get("latency_sec") or 0.0) for row in judge_rows]
    judge_usage = Counter()
    for row in judge_rows:
        usage = row.get("usage") or {}
        for key in ("prompt_tokens", "completion_tokens", "thoughts_tokens"):
            judge_usage[key] += int(usage.get(key) or 0)
    return {
        "block_arm_items": len(items),
        "bt_calls": len(bt_latencies),
        "judge_calls": len(judge_latencies),
        "bt_wall_elapsed_sec": bt_elapsed,
        "cosine_wall_elapsed_sec": cosine_elapsed,
        "local_bt_plus_cos_wall_elapsed_sec": bt_elapsed + cosine_elapsed,
        "judge_wall_elapsed_sec": judge_elapsed,
        "bt_latency_sum_sec": sum(bt_latencies),
        "bt_latency_p50_sec": statistics.median(bt_latencies) if bt_latencies else 0.0,
        "bt_latency_p90_sec": _p90(bt_latencies),
        "bt_latency_max_sec": max(bt_latencies) if bt_latencies else 0.0,
        "cosine_latency_sum_sec": sum(cos_latencies),
        "judge_latency_sum_sec": sum(judge_latencies),
        "judge_latency_p50_sec": statistics.median(judge_latencies) if judge_latencies else 0.0,
        "judge_latency_p90_sec": _p90(judge_latencies),
        "judge_latency_max_sec": max(judge_latencies) if judge_latencies else 0.0,
        "judge_usage": dict(judge_usage),
        "judge_errors": _judge_error_counts(items),
        "bt_finish_reason_counts": dict(Counter((item.get("bt") or {}).get("finish_reason") or "" for item in items)),
        "judge_finish_reason_counts": dict(Counter(str(row.get("finish_reason") or "") for row in judge_rows)),
    }


def _judge_error_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    counts = Counter()
    for item in items:
        judge = item.get("bt_llm_score") or {}
        for direction in ("ab", "ba"):
            row = judge.get(direction) or {}
            if row.get("transport_error"):
                counts["transport_error"] += 1
            if row.get("validation_error"):
                counts["validation_error"] += 1
    return dict(counts)


def _latency_by_length(items: list[dict[str, Any]]) -> dict[str, Any]:
    buckets: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for item in items:
        bucket = _length_bucket(int(item.get("source_token_count") or 0))
        buckets[bucket]["bt"].append(float((item.get("bt") or {}).get("latency_sec") or 0.0))
        for name in ("bt_bge_cosine", "direct_bge_en_vi"):
            buckets[bucket]["cosine"].append(float((item.get(name) or {}).get("latency_sec") or 0.0))
        for direction in ("ab", "ba"):
            row = (item.get("bt_llm_score") or {}).get(direction)
            if row:
                buckets[bucket]["judge"].append(float(row.get("latency_sec") or 0.0))
    return {
        bucket: {kind: _latency_stats(values) for kind, values in sorted(parts.items())}
        for bucket, parts in sorted(buckets.items())
    }


def _length_bucket(tokens: int) -> str:
    if tokens <= 50:
        return "000_050"
    if tokens <= 100:
        return "051_100"
    if tokens <= 200:
        return "101_200"
    return "201_plus"


def _latency_stats(values: list[float]) -> dict[str, float | int]:
    return {
        "n": len(values),
        "p50": statistics.median(values) if values else 0.0,
        "p90": _p90(values),
        "max": max(values) if values else 0.0,
        "sum": sum(values),
    }


def _p90(values: list[float]) -> float:
    if not values:
        return 0.0
    if len(values) < 10:
        return max(values)
    return statistics.quantiles(values, n=10)[8]


def _cost_eta(items: list[dict[str, Any]], *, full_block_arm_items: int, runtime_summary: dict[str, Any]) -> dict[str, Any]:
    pilot_items = max(len(items), 1)
    scale = full_block_arm_items / pilot_items
    judge_rows = [
        (item.get("bt_llm_score") or {}).get(direction, {})
        for item in items
        for direction in ("ab", "ba")
        if direction in (item.get("bt_llm_score") or {})
    ]
    judge_cost = sum(float(row.get("cost_usd") or 0.0) for row in judge_rows)
    full_judge_cost = judge_cost * scale
    judge_calls = len(judge_rows)
    full_judge_calls = judge_calls * scale
    throughput = judge_calls / max(float(runtime_summary.get("judge_wall_elapsed_sec") or 0.0), 1e-9)
    judge_full_wall = full_judge_calls / throughput if throughput else 0.0
    local_full_wall = (
        float(runtime_summary.get("local_bt_plus_cos_wall_elapsed_sec") or 0.0)
        * scale
    )
    return {
        "pilot_block_arm_items": pilot_items,
        "full_block_arm_items": full_block_arm_items,
        "scale_factor": scale,
        "gemini_judge_cost_pilot_usd": judge_cost,
        "gemini_judge_cost_full_est_usd": full_judge_cost,
        "gemini_cost_warning_over_10_usd": full_judge_cost > 10.0,
        "judge_calls_pilot": judge_calls,
        "judge_calls_full_est": full_judge_calls,
        "judge_throughput_calls_per_min": throughput * 60.0,
        "judge_full_wall_est_sec": judge_full_wall,
        "local_bt_plus_cos_full_wall_est_sec": local_full_wall,
        "total_full_wall_est_sec_if_same_cache_profile": local_full_wall + judge_full_wall,
    }


def _partial(report: dict[str, Any], cache: SqliteCache, started: float, db_path: Path) -> dict[str, Any]:
    partial = dict(report)
    partial["status"] = "running_partial"
    partial["cache"] = cache.stats()
    partial["elapsed_sec"] = time.perf_counter() - started
    partial["db_sha256_after"] = _sha256_file(db_path)
    return partial


def _console_summary(report: dict[str, Any]) -> dict[str, Any]:
    aggr = report.get("aggregates", {}).get("all", {})
    return {
        "out": report.get("out", str(DEFAULT_OUT)),
        "status": report["status"],
        "db_unchanged": report["db_unchanged"],
        "items": len(report["items"]),
        "runtime_summary": report["runtime_summary"],
        "cost_eta": report["cost_eta"],
        "paired_delta_overall": {
            component: values.get("overall")
            for component, values in (aggr.get("paired_delta") or {}).items()
        },
        "hygiene_flags": report.get("hygiene_summary", {}).get("flag_counts"),
    }


if __name__ == "__main__":
    raise SystemExit(main())
