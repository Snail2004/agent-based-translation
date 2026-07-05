from __future__ import annotations

import argparse
import json
import math
import statistics
import time
from collections import Counter
from pathlib import Path
from typing import Any

from pipeline.scripts.probe_pj import (
    DEFAULT_CACHE,
    DEFAULT_MODEL,
    DEFAULT_WORKDB,
    PROMPT_VERSION_PJ,
    TAG_ORDER,
    _assert_prompt_hash,
    _aggregate_case,
    _normalize_auto_tie_text,
    _run_probe_calls,
    _total_cost,
)
from pipeline.scripts.probe_sf_bt import SqliteCache, _sha256_file, _sha256_text, _write_json
from pipeline.scripts.score_sf_bt import (
    DEFAULT_GEMINI_INPUT_PER_MILLION,
    DEFAULT_GEMINI_OUTPUT_PER_MILLION,
    SEED,
)
from pipeline.scripts.probe_pj import PJ_PROMPT_TEMPLATE


DEFAULT_OUT = Path("data/reports/exp_s0s1_builderv2_v1/pj_pilot_50.json")
DEFAULT_EXPERIMENT = "exp_s0s1_builderv2_v1"
DEFAULT_CHAPTER = "d2l_multilayer_perceptrons"
DEFAULT_EXPECTED_WORKDB_SHA256 = "92229381172FE30C2A10E2C467232AB992B7EA8ECB40415A1652AB7DE8C13F42"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run PJ pilot on real S0/S1 block pairs.")
    parser.add_argument("--db", default=str(DEFAULT_WORKDB))
    parser.add_argument("--experiment", default=DEFAULT_EXPERIMENT)
    parser.add_argument("--chapter", default=DEFAULT_CHAPTER)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--cache-db", default=str(DEFAULT_CACHE))
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--sample-size", type=int, default=50)
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--max-output-tokens", type=int, default=512)
    parser.add_argument("--thinking-budget", type=int, default=0)
    parser.add_argument("--timeout-sec", type=int, default=120)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--gemini-input-price", type=float, default=DEFAULT_GEMINI_INPUT_PER_MILLION)
    parser.add_argument("--gemini-output-price", type=float, default=DEFAULT_GEMINI_OUTPUT_PER_MILLION)
    parser.add_argument("--expected-db-sha256", default=DEFAULT_EXPECTED_WORKDB_SHA256)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()

    _assert_prompt_hash()
    started = time.perf_counter()
    db_path = Path(args.db)
    out_path = Path(args.out)
    db_sha_before = _sha256_file(db_path).upper()
    expected_sha = str(args.expected_db_sha256 or "").upper()
    if expected_sha and db_sha_before != expected_sha:
        raise SystemExit(f"Workdb hash mismatch before run: {db_sha_before} != {expected_sha}")

    pairs = _load_pairs(db_path, experiment=args.experiment, chapter=args.chapter)
    identical_pairs, different_pairs = _split_pairs(pairs)
    sampled_different, sampling_report = _sample_different_pairs(different_pairs, sample_size=args.sample_size)
    cases = [_case_from_pair(pair, seed=args.seed) for pair in sampled_different]
    auto_tie_cases = [_auto_tie_case(pair) for pair in identical_pairs]

    report: dict[str, Any] = {
        "metric": "PJ",
        "phase": "pilot_50_real_pairs",
        "status": "validate_only" if args.validate_only else "running",
        "db": str(db_path),
        "db_sha256_before": db_sha_before,
        "expected_db_sha256": expected_sha,
        "experiment": args.experiment,
        "chapter": args.chapter,
        "model": args.model,
        "prompt_version": PROMPT_VERSION_PJ,
        "prompt_sha256": _sha256_text(PJ_PROMPT_TEMPLATE),
        "request_profile": {
            "temperature": 0,
            "thinking_budget": args.thinking_budget,
            "response_mime_type": "application/json",
            "max_output_tokens": args.max_output_tokens,
            "concurrency": args.concurrency,
            "seed_for_arm_assignment": args.seed,
        },
        "pair_counts": {
            "total_pairs": len(pairs),
            "auto_tie_identical_pairs": len(identical_pairs),
            "different_pairs": len(different_pairs),
            "sampled_different_pairs": len(sampled_different),
        },
        "normalization": "NFC + CRLF/CR->LF + strip trailing whitespace per line",
        "sampling": sampling_report,
        "cases": cases,
        "auto_tie_cases": auto_tie_cases,
    }
    _write_json(out_path, report)

    if args.validate_only:
        print(json.dumps(_console_summary(report), ensure_ascii=False, indent=2))
        return 0

    cache = SqliteCache(Path(args.cache_db))
    _run_probe_calls(
        cases,
        cache=cache,
        model=args.model,
        timeout_sec=args.timeout_sec,
        max_output_tokens=args.max_output_tokens,
        thinking_budget=args.thinking_budget,
        input_price=args.gemini_input_price,
        output_price=args.gemini_output_price,
        concurrency=args.concurrency,
    )

    report["status"] = "pilot_complete_stop_for_review"
    report["db_sha256_after"] = _sha256_file(db_path).upper()
    report["db_unchanged"] = report["db_sha256_before"] == report["db_sha256_after"]
    report["cache"] = cache.stats()
    report["elapsed_sec"] = time.perf_counter() - started
    report["cases"] = cases
    report["auto_tie_cases"] = auto_tie_cases
    report["cost_usd"] = _total_cost(cases)
    report["aggregates"] = _pilot_aggregates(cases=cases, auto_tie_cases=auto_tie_cases)
    report["latency"] = _latency_summary(cases)
    report["cost_projection"] = _cost_projection(
        cases=cases,
        total_different_pairs=len(different_pairs),
        elapsed_sec=report["elapsed_sec"],
    )
    _write_json(out_path, report)
    print(json.dumps(_console_summary(report), ensure_ascii=False, indent=2))
    return 0


def _load_pairs(db_path: Path, *, experiment: str, chapter: str) -> list[dict[str, Any]]:
    import sqlite3

    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            """
            SELECT
                b.chapter_id,
                b.block_id,
                b.order_index,
                b.block_type,
                COALESCE(NULLIF(b.original_text, ''), b.text) AS source_text,
                MAX(CASE WHEN tr.config = 'S0' THEN tr.output_text END) AS s0_text,
                MAX(CASE WHEN tr.config = 'S1' THEN tr.output_text END) AS s1_text,
                COUNT(DISTINCT tr.config) AS config_count
            FROM blocks b
            JOIN translation_runs tr ON tr.block_id = b.block_id
            WHERE tr.experiment_id = ?
              AND tr.stage = 'draft'
              AND b.chapter_id = ?
              AND tr.config IN ('S0', 'S1')
            GROUP BY b.chapter_id, b.block_id, b.order_index, b.block_type, source_text
            HAVING config_count = 2
            ORDER BY b.order_index
            """,
            (experiment, chapter),
        ).fetchall()
    pairs: list[dict[str, Any]] = []
    for row in rows:
        source_text = str(row["source_text"] or "")
        pairs.append(
            {
                "chapter_id": str(row["chapter_id"]),
                "block_id": str(row["block_id"]),
                "order_index": int(row["order_index"]),
                "block_type": str(row["block_type"] or ""),
                "source": source_text,
                "S0": str(row["s0_text"] or ""),
                "S1": str(row["s1_text"] or ""),
                "short_block": len(source_text) < 40 or len(source_text.split()) < 8,
                "source_char_count": len(source_text),
                "source_token_count": len(source_text.split()),
            }
        )
    return pairs


def _split_pairs(pairs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    identical: list[dict[str, Any]] = []
    different: list[dict[str, Any]] = []
    for pair in pairs:
        if _normalize_auto_tie_text(pair["S0"]) == _normalize_auto_tie_text(pair["S1"]):
            identical.append(pair)
        else:
            different.append(pair)
    return identical, different


def _sample_different_pairs(pairs: list[dict[str, Any]], *, sample_size: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if len(pairs) < sample_size:
        raise ValueError(f"Only {len(pairs)} different pairs available, need {sample_size}.")
    indices = [math.floor((i + 0.5) * len(pairs) / sample_size) for i in range(sample_size)]
    selected = [pairs[index] for index in indices]
    return selected, {
        "population": "different S0/S1 MLP block pairs sorted by order_index",
        "formula": "idx_i = floor((i + 0.5) * N / k), i=0..k-1",
        "N": len(pairs),
        "k": sample_size,
        "indices_0based": indices,
        "selected_block_ids": [pair["block_id"] for pair in selected],
    }


def _case_from_pair(pair: dict[str, Any], *, seed: int) -> dict[str, Any]:
    parity = int(_sha256_text(f"{seed}:{pair['block_id']}")[-1], 16) % 2
    if parity == 0:
        arm_a, arm_b = "S0", "S1"
    else:
        arm_a, arm_b = "S1", "S0"
    return {
        "id": f"PJ_MLP_{pair['block_id']}",
        "category": "REAL-PAIR",
        "source_block_id": pair["block_id"],
        "block_id": pair["block_id"],
        "chapter_id": pair["chapter_id"],
        "order_index": pair["order_index"],
        "block_type": pair["block_type"],
        "source": pair["source"],
        "candidate_a": pair[arm_a],
        "candidate_b": pair[arm_b],
        "candidate_a_arm": arm_a,
        "candidate_b_arm": arm_b,
        "short_block": pair["short_block"],
        "source_char_count": pair["source_char_count"],
        "source_token_count": pair["source_token_count"],
        "arm_assignment_seed": seed,
    }


def _auto_tie_case(pair: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f"PJ_MLP_{pair['block_id']}_auto_tie",
        "category": "AUTO-TIE",
        "source_block_id": pair["block_id"],
        "block_id": pair["block_id"],
        "chapter_id": pair["chapter_id"],
        "order_index": pair["order_index"],
        "block_type": pair["block_type"],
        "short_block": pair["short_block"],
        "source_char_count": pair["source_char_count"],
        "source_token_count": pair["source_token_count"],
        "auto_tie_reason": "normalized S0 output equals normalized S1 output",
        "final": {
            "overall_final": "TIE",
            "style_final": "TIE",
            "tags_final": [],
            "flags": ["auto_tie_identical_normalized"],
        },
    }


def _pilot_aggregates(*, cases: list[dict[str, Any]], auto_tie_cases: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "judged_sample_50": _aggregate_scope(cases),
        "judged_sample_50_without_short_block": _aggregate_scope([case for case in cases if not case.get("short_block")]),
        "sample_plus_all_auto_ties": _aggregate_scope([*cases, *auto_tie_cases]),
        "sample_plus_all_auto_ties_without_short_block": _aggregate_scope(
            [case for case in [*cases, *auto_tie_cases] if not case.get("short_block")]
        ),
    }


def _aggregate_scope(cases: list[dict[str, Any]]) -> dict[str, Any]:
    overall = Counter()
    style = Counter()
    overall_arm = Counter()
    style_arm = Counter()
    tag_counts = Counter()
    flag_counts = Counter()
    for case in cases:
        final = case.get("final") or {}
        overall_final = str(final.get("overall_final") or "TIE")
        style_final = str(final.get("style_final") or "TIE")
        overall[overall_final] += 1
        style[style_final] += 1
        overall_arm[_verdict_to_arm(case, overall_final)] += 1
        style_arm[_verdict_to_arm(case, style_final)] += 1
        tag_counts.update(final.get("tags_final") or [])
        flag_counts.update(final.get("flags") or [])
    n = len(cases)
    return {
        "n": n,
        "overall_verdict_counts": dict(overall),
        "style_verdict_counts": dict(style),
        "overall_tie_rate": overall.get("TIE", 0) / n if n else 0.0,
        "style_tie_rate": style.get("TIE", 0) / n if n else 0.0,
        "overall_win_loss_by_arm": dict(overall_arm),
        "style_win_loss_by_arm": dict(style_arm),
        "tag_counts": {tag: tag_counts.get(tag, 0) for tag in TAG_ORDER if tag_counts.get(tag, 0)},
        "flag_counts": dict(flag_counts),
        "order_inconsistent": {
            "overall_count": flag_counts.get("overall_order_inconsistent", 0),
            "style_count": flag_counts.get("style_order_inconsistent", 0),
            "overall_rate": flag_counts.get("overall_order_inconsistent", 0) / n if n else 0.0,
            "style_rate": flag_counts.get("style_order_inconsistent", 0) / n if n else 0.0,
        },
        "style_unsupported_count": flag_counts.get("style_unsupported_by_tags", 0),
        "style_unsupported_rate": flag_counts.get("style_unsupported_by_tags", 0) / n if n else 0.0,
    }


def _verdict_to_arm(case: dict[str, Any], verdict: str) -> str:
    if verdict == "A":
        return str(case.get("candidate_a_arm") or "A")
    if verdict == "B":
        return str(case.get("candidate_b_arm") or "B")
    return "TIE"


def _latency_summary(cases: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [call for case in cases for call in (case.get("calls") or {}).values()]
    latencies = [float(call.get("latency_sec") or 0.0) for call in rows]
    usage = Counter()
    for call in rows:
        for key, value in (call.get("usage") or {}).items():
            usage[key] += int(value or 0)
    return {
        "call_count": len(rows),
        "p50_sec": statistics.median(latencies) if latencies else 0.0,
        "p90_sec": _p90(latencies),
        "max_sec": max(latencies) if latencies else 0.0,
        "sum_sec": sum(latencies),
        "usage": dict(usage),
        "model_versions": dict(Counter(str(call.get("model_version") or "") for call in rows)),
        "finish_reason_counts": dict(Counter(str(call.get("finish_reason") or "") for call in rows)),
        "validation_error_count": sum(1 for call in rows if call.get("validation_error")),
        "transport_error_count": sum(1 for call in rows if call.get("transport_error")),
    }


def _cost_projection(*, cases: list[dict[str, Any]], total_different_pairs: int, elapsed_sec: float) -> dict[str, Any]:
    judged_pairs = max(len(cases), 1)
    judged_calls = judged_pairs * 2
    total_calls = total_different_pairs * 2
    scale = total_different_pairs / judged_pairs
    cost = _total_cost(cases)
    cache_miss_calls = sum(1 for case in cases for call in (case.get("calls") or {}).values() if not call.get("from_cache"))
    return {
        "pilot_judged_pairs": judged_pairs,
        "pilot_logical_calls": judged_calls,
        "pilot_cache_miss_calls": cache_miss_calls,
        "full_different_pairs": total_different_pairs,
        "full_logical_calls": total_calls,
        "scale_factor": scale,
        "pilot_cost_usd": cost,
        "full_cost_est_usd_same_cache_profile": cost * scale,
        "full_elapsed_est_sec_same_cache_profile": elapsed_sec * scale,
    }


def _p90(values: list[float]) -> float:
    if not values:
        return 0.0
    if len(values) < 10:
        return max(values)
    return statistics.quantiles(values, n=10)[8]


def _console_summary(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": report["status"],
        "phase": report["phase"],
        "db_unchanged": report.get("db_unchanged"),
        "pair_counts": report.get("pair_counts"),
        "cache": report.get("cache"),
        "cost_usd": report.get("cost_usd"),
        "aggregates": report.get("aggregates"),
        "cost_projection": report.get("cost_projection"),
    }


if __name__ == "__main__":
    raise SystemExit(main())
