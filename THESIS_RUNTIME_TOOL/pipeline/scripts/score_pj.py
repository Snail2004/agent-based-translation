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
    _map_call_verdict,
    _normalize_auto_tie_text,
    _pj_cache_key,
    _run_probe_calls,
    _total_cost,
)
from pipeline.scripts.probe_sf_bt import SqliteCache, _sha256_file, _sha256_text, _write_json
from pipeline.scripts.score_sf_bt import (
    DEFAULT_GEMINI_INPUT_PER_MILLION,
    DEFAULT_GEMINI_OUTPUT_PER_MILLION,
    SEED,
    _gemini_cost,
)
from pipeline.scripts.probe_pj import PJ_PROMPT_TEMPLATE


DEFAULT_OUT = Path("data/reports/exp_s0s1_builderv2_v1/pj_pilot_50.json")
DEFAULT_FULL_OUT = Path("data/reports/exp_s0s1_builderv2_v1/pj_full_339.json")
DEFAULT_ESTIMATE_PILOT_REPORT = Path("data/reports/exp_s0s1_builderv2_v1/pj_pilot_50.json")
DEFAULT_EXPERIMENT = "exp_s0s1_builderv2_v1"
DEFAULT_CHAPTER = "d2l_multilayer_perceptrons"
DEFAULT_EXPECTED_WORKDB_SHA256 = ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Run PJ pilot on real S0/S1 block pairs.")
    parser.add_argument("--db", default=str(DEFAULT_WORKDB))
    parser.add_argument("--experiment", default=DEFAULT_EXPERIMENT)
    parser.add_argument("--chapter", default=DEFAULT_CHAPTER)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--cache-db", default=str(DEFAULT_CACHE))
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--sample-size", type=int, default=50)
    parser.add_argument("--full", action="store_true", help="Judge every non-identical S0/S1 pair instead of a systematic sample.")
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--max-output-tokens", type=int, default=512)
    parser.add_argument("--thinking-budget", type=int, default=0)
    parser.add_argument("--timeout-sec", type=int, default=120)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--gemini-input-price", type=float, default=DEFAULT_GEMINI_INPUT_PER_MILLION)
    parser.add_argument("--gemini-output-price", type=float, default=DEFAULT_GEMINI_OUTPUT_PER_MILLION)
    parser.add_argument("--expected-db-sha256", default=DEFAULT_EXPECTED_WORKDB_SHA256)
    parser.add_argument("--estimate-only", action="store_true", help="Write a cost estimate and exit without API calls.")
    parser.add_argument("--confirm-usd", type=float, default=None, help="Required for non-estimate runs; abort if estimated cap exceeds this amount.")
    parser.add_argument("--estimate-cap-multiplier", type=float, default=1.25)
    parser.add_argument("--estimate-pilot-report", default=str(DEFAULT_ESTIMATE_PILOT_REPORT))
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
    judged_different, sampling_report = _select_different_pairs(
        different_pairs,
        sample_size=args.sample_size,
        full=bool(args.full),
    )
    cases = [_case_from_pair(pair, seed=args.seed) for pair in judged_different]
    auto_tie_cases = [_auto_tie_case(pair) for pair in identical_pairs]
    phase = "full" if args.full else f"pilot_{len(judged_different)}_real_pairs"
    if args.full and args.out == str(DEFAULT_OUT):
        out_path = DEFAULT_FULL_OUT

    report: dict[str, Any] = {
        "metric": "PJ",
        "phase": phase,
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
            "judged_different_pairs": len(judged_different),
            "sampled_different_pairs": len(judged_different) if not args.full else None,
        },
        "normalization": "NFC + CRLF/CR->LF + strip trailing whitespace per line",
        "sampling": sampling_report,
        "cases": cases,
        "auto_tie_cases": auto_tie_cases,
    }
    cache = SqliteCache(Path(args.cache_db))
    estimate = _estimate_pj_cost(
        cases,
        cache=cache,
        model=args.model,
        max_output_tokens=args.max_output_tokens,
        thinking_budget=args.thinking_budget,
        input_price=args.gemini_input_price,
        output_price=args.gemini_output_price,
        cap_multiplier=args.estimate_cap_multiplier,
        pilot_report=Path(args.estimate_pilot_report),
    )
    report["estimate"] = estimate
    _write_json(out_path, report)

    if args.validate_only or args.estimate_only:
        report["status"] = "estimate_only" if args.estimate_only else report["status"]
        _write_json(out_path, report)
        print(json.dumps(_console_summary(report), ensure_ascii=False, indent=2))
        return 0
    if args.confirm_usd is None:
        raise SystemExit("--confirm-usd is required for PJ runs that may call Gemini; use --estimate-only first.")
    if estimate["estimated_cost_cap_usd"] > args.confirm_usd:
        raise SystemExit(
            f"PJ estimate cap ${estimate['estimated_cost_cap_usd']:.4f} exceeds --confirm-usd ${args.confirm_usd:.4f}."
        )

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

    report["status"] = "full_complete_stop_for_review" if args.full else "pilot_complete_stop_for_review"
    report["db_sha256_after"] = _sha256_file(db_path).upper()
    report["db_unchanged"] = report["db_sha256_before"] == report["db_sha256_after"]
    report["cache"] = cache.stats()
    report["elapsed_sec"] = time.perf_counter() - started
    report["cases"] = cases
    report["auto_tie_cases"] = auto_tie_cases
    report["cost_usd"] = _total_cost(cases)
    report["aggregates"] = _pj_aggregates(cases=cases, auto_tie_cases=auto_tie_cases, full=bool(args.full))
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


def _select_different_pairs(
    pairs: list[dict[str, Any]],
    *,
    sample_size: int,
    full: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if full:
        return pairs, {
            "population": "all different S0/S1 MLP block pairs sorted by order_index",
            "mode": "full",
            "N": len(pairs),
            "k": len(pairs),
            "indices_0based": list(range(len(pairs))),
            "selected_block_ids": [pair["block_id"] for pair in pairs],
        }
    return _sample_different_pairs(pairs, sample_size=sample_size)


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


def _estimate_pj_cost(
    cases: list[dict[str, Any]],
    *,
    cache: SqliteCache,
    model: str,
    max_output_tokens: int,
    thinking_budget: int,
    input_price: float,
    output_price: float,
    cap_multiplier: float,
    pilot_report: Path,
) -> dict[str, Any]:
    pilot = _pj_p90_from_report(pilot_report, input_price=input_price, output_price=output_price)
    logical_calls = len(cases) * 2
    cache_hits = 0
    for case in cases:
        for direction, candidate_x, candidate_y in (
            ("ab", str(case["candidate_a"]), str(case["candidate_b"])),
            ("ba", str(case["candidate_b"]), str(case["candidate_a"])),
        ):
            key = _pj_cache_key(
                model=model,
                direction=direction,
                source=str(case["source"]),
                candidate_x=candidate_x,
                candidate_y=candidate_y,
                max_output_tokens=max_output_tokens,
                thinking_budget=thinking_budget,
            )
            if cache.get(key) is not None:
                cache_hits += 1
    fresh_calls = max(0, logical_calls - cache_hits)
    estimated_cost = fresh_calls * pilot["p90_cost_usd_per_call"]
    return {
        "judged_pairs": len(cases),
        "auto_tie_pairs": 0,
        "logical_calls": logical_calls,
        "cache_hits": cache_hits,
        "fresh_calls": fresh_calls,
        "pilot_report": str(pilot_report),
        "p90_prompt_tokens_per_call": pilot["p90_prompt_tokens"],
        "p90_completion_tokens_per_call": pilot["p90_completion_tokens"],
        "p90_cost_usd_per_call": pilot["p90_cost_usd_per_call"],
        "estimated_cost_usd": round(estimated_cost, 12),
        "cap_multiplier": cap_multiplier,
        "estimated_cost_cap_usd": round(estimated_cost * cap_multiplier, 12),
    }


def _pj_p90_from_report(path: Path, *, input_price: float, output_price: float) -> dict[str, Any]:
    prompt_tokens: list[int] = []
    completion_tokens: list[int] = []
    costs: list[float] = []
    if path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
        for case in payload.get("cases") or []:
            for call in (case.get("calls") or {}).values():
                usage = call.get("usage") or {}
                prompt = int(usage.get("prompt_tokens") or usage.get("prompt_token_count") or 0)
                completion = int(usage.get("completion_tokens") or usage.get("candidates_token_count") or 0)
                if prompt or completion:
                    prompt_tokens.append(prompt)
                    completion_tokens.append(completion)
                    costs.append(_gemini_cost({"prompt_tokens": prompt, "completion_tokens": completion}, input_price=input_price, output_price=output_price))
    p90_prompt = int(round(_quantile(sorted(prompt_tokens), 0.90))) if prompt_tokens else 1200
    p90_completion = int(round(_quantile(sorted(completion_tokens), 0.90))) if completion_tokens else 512
    p90_cost = _quantile(sorted(costs), 0.90) if costs else _gemini_cost(
        {"prompt_tokens": p90_prompt, "completion_tokens": p90_completion},
        input_price=input_price,
        output_price=output_price,
    )
    return {
        "p90_prompt_tokens": p90_prompt,
        "p90_completion_tokens": p90_completion,
        "p90_cost_usd_per_call": p90_cost,
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


def _pj_aggregates(*, cases: list[dict[str, Any]], auto_tie_cases: list[dict[str, Any]], full: bool) -> dict[str, Any]:
    judged_label = f"judged_full_{len(cases)}" if full else f"judged_sample_{len(cases)}"
    combined_label = "full_plus_all_auto_ties" if full else "sample_plus_all_auto_ties"
    return {
        judged_label: _aggregate_scope(cases),
        f"{judged_label}_without_short_block": _aggregate_scope([case for case in cases if not case.get("short_block")]),
        combined_label: _aggregate_scope([*cases, *auto_tie_cases]),
        f"{combined_label}_without_short_block": _aggregate_scope(
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
        "n_effective": {
            "overall": n - overall.get("TIE", 0),
            "style": n - style.get("TIE", 0),
        },
        "tag_counts": {tag: tag_counts.get(tag, 0) for tag in TAG_ORDER if tag_counts.get(tag, 0)},
        "flag_counts": dict(flag_counts),
        "order_inconsistent": {
            "overall_count": flag_counts.get("overall_order_inconsistent", 0),
            "style_count": flag_counts.get("style_order_inconsistent", 0),
            "overall_rate": flag_counts.get("overall_order_inconsistent", 0) / n if n else 0.0,
            "style_rate": flag_counts.get("style_order_inconsistent", 0) / n if n else 0.0,
        },
        "order_consistency_breakdown": {
            "overall": _order_consistency_breakdown(cases, verdict_key="overall_verdict"),
            "style": _order_consistency_breakdown(cases, verdict_key="style_verdict"),
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


def _order_consistency_breakdown(cases: list[dict[str, Any]], *, verdict_key: str) -> dict[str, Any]:
    counts = Counter()
    for case in cases:
        if case.get("category") == "AUTO-TIE":
            counts["consistent"] += 1
            counts["consistent_tie"] += 1
            continue
        calls = case.get("calls") or {}
        mapped: list[str] = []
        invalid = False
        for direction in ("ab", "ba"):
            call = calls.get(direction) or {}
            verdict = call.get(verdict_key)
            if call.get("validation_error") or call.get("transport_error") or verdict not in {"X", "Y", "TIE"}:
                invalid = True
                break
            mapped.append(_map_call_verdict(str(verdict), direction=direction))
        if invalid or len(mapped) != 2:
            counts["invalid"] += 1
        elif mapped[0] == mapped[1]:
            counts["consistent"] += 1
            if mapped[0] == "TIE":
                counts["consistent_tie"] += 1
            else:
                counts["consistent_win"] += 1
        elif "TIE" in mapped:
            counts["soft_tie_vs_win"] += 1
        else:
            counts["hard_x_vs_y"] += 1
    n = len(cases)
    return {
        "n": n,
        "consistent": counts.get("consistent", 0),
        "consistent_tie": counts.get("consistent_tie", 0),
        "consistent_win": counts.get("consistent_win", 0),
        "soft_tie_vs_win": counts.get("soft_tie_vs_win", 0),
        "hard_x_vs_y": counts.get("hard_x_vs_y", 0),
        "invalid": counts.get("invalid", 0),
        "consistent_rate": counts.get("consistent", 0) / n if n else 0.0,
        "soft_rate": counts.get("soft_tie_vs_win", 0) / n if n else 0.0,
        "hard_rate": counts.get("hard_x_vs_y", 0) / n if n else 0.0,
    }


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
        "estimate": report.get("estimate"),
        "cache": report.get("cache"),
        "cost_usd": report.get("cost_usd"),
        "aggregates": report.get("aggregates"),
        "cost_projection": report.get("cost_projection"),
    }


if __name__ == "__main__":
    raise SystemExit(main())
