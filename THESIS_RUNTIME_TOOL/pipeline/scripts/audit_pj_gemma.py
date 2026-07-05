from __future__ import annotations

import argparse
import json
import math
import time
from collections import Counter
from pathlib import Path
from typing import Any

from pipeline.scripts.probe_pj import (
    PJ_PROMPT_TEMPLATE,
    PROMPT_VERSION_PJ,
    _aggregate_case,
    _assert_prompt_hash,
    _validate_pj_json,
)
from pipeline.scripts.probe_sf_bt import (
    SEED,
    SqliteCache,
    _chat_completion_cached,
    _sha256_file,
    _sha256_text,
    _write_json,
)
from pipeline.scripts.score_pj import _verdict_to_arm


DEFAULT_INPUT = Path("data/reports/exp_s0s1_builderv2_v1/pj_full_339.json")
DEFAULT_OUT = Path("data/reports/exp_s0s1_builderv2_v1/pj_gemma_audit.json")
DEFAULT_CACHE = Path("data/reports/exp_s0s1_builderv2_v1/pj_gemma_audit_cache.sqlite3")
DEFAULT_ENDPOINT = "http://127.0.0.1:1234"
DEFAULT_MODEL = "google/gemma-4-12b"


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit PJ Gemini verdicts with local Gemma on fixed MLP subsets.")
    parser.add_argument("--input", default=str(DEFAULT_INPUT))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--cache-db", default=str(DEFAULT_CACHE))
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--systematic-size", type=int, default=50)
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument("--timeout-sec", type=int, default=240)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()

    _assert_prompt_hash()
    input_path = Path(args.input)
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    cases = list(payload.get("cases") or [])
    if not cases:
        raise SystemExit(f"No judged cases in {input_path}")

    systematic = _select_systematic(cases, args.systematic_size)
    s1_loss = _select_s1_loss_overall(cases)
    if len(s1_loss) != 102:
        raise SystemExit(f"Expected 102 S1-loss-overall cases from full report, got {len(s1_loss)}")
    audit_cases = _dedupe_cases([*systematic, *s1_loss])

    report: dict[str, Any] = {
        "metric": "PJ",
        "phase": "gemma_local_audit",
        "status": "validate_only" if args.validate_only else "running",
        "input": str(input_path),
        "input_sha256": _sha256_file(input_path),
        "out": str(Path(args.out)),
        "baseline": {
            "judge": str(payload.get("model") or "gemini-2.5-flash"),
            "prompt_version": str(payload.get("prompt_version") or ""),
            "prompt_sha256": str(payload.get("prompt_sha256") or ""),
        },
        "audit_judge": {
            "model": args.model,
            "endpoint": args.endpoint,
            "prompt_version": PROMPT_VERSION_PJ,
            "prompt_sha256": _sha256_text(PJ_PROMPT_TEMPLATE),
        },
        "request_profile": {
            "temperature": 0,
            "top_p": 1,
            "seed": SEED,
            "repeat_penalty": 1.0,
            "reasoning_effort": "none",
            "max_tokens": args.max_tokens,
        },
        "selection": {
            "systematic_50": {
                "source": "339 judged non-identical PJ pairs from pj_full_339.json, sorted by artifact order",
                "formula": "idx_i = floor((i + 0.5) * N / k), i=0..k-1",
                "N": len(cases),
                "k": args.systematic_size,
                "indices_0based": _systematic_indices(len(cases), args.systematic_size),
                "case_ids": [case["id"] for case in systematic],
            },
            "s1_loss_overall": {
                "definition": "Gemini final overall winner maps to S0; therefore S1 loses overall.",
                "count": len(s1_loss),
                "case_ids": [case["id"] for case in s1_loss],
            },
            "audit_unique_cases": len(audit_cases),
        },
        "thresholds": {
            "reversal_rate_primary_max": 0.10,
            "reversal_definition": "Gemma final overall winner is the opposite arm from Gemini final overall winner; tie-vs-win is not counted as reversal.",
            "primary_denominator": "cases in the group with a decisive Gemini overall winner and valid Gemma aggregate.",
        },
        "cases": [],
    }
    _write_json(Path(args.out), report)

    if args.validate_only:
        print(json.dumps(_console_summary(report), ensure_ascii=False, indent=2))
        return 0

    started = time.perf_counter()
    cache = SqliteCache(Path(args.cache_db))
    systematic_ids = {case["id"] for case in systematic}
    s1_loss_ids = {case["id"] for case in s1_loss}

    for index, case in enumerate(audit_cases, 1):
        print(f"[PJ-Gemma {index}/{len(audit_cases)}] {case['id']}", flush=True)
        row = _audit_case(
            case,
            groups={
                "systematic_50": case["id"] in systematic_ids,
                "s1_loss_overall": case["id"] in s1_loss_ids,
            },
            cache=cache,
            endpoint=args.endpoint,
            model=args.model,
            max_tokens=args.max_tokens,
            timeout_sec=args.timeout_sec,
        )
        report["cases"].append(row)
        if index % 10 == 0 or index == len(audit_cases):
            partial = dict(report)
            partial["status"] = "running_partial"
            partial["elapsed_sec"] = time.perf_counter() - started
            partial["cache"] = cache.stats()
            partial["summary"] = _summary(partial["cases"])
            _write_json(Path(args.out), partial)

    report["status"] = "audit_complete_stop_for_review"
    report["elapsed_sec"] = time.perf_counter() - started
    report["cache"] = cache.stats()
    report["summary"] = _summary(report["cases"])
    report["reversals"] = {
        "systematic_50": _reversal_rows(report["cases"], "systematic_50"),
        "s1_loss_overall": _reversal_rows(report["cases"], "s1_loss_overall"),
    }
    report["validation_errors"] = _validation_error_counts(report["cases"])
    _write_json(Path(args.out), report)
    print(json.dumps(_console_summary(report), ensure_ascii=False, indent=2))
    return 0


def _systematic_indices(n: int, k: int) -> list[int]:
    if k <= 0 or k > n:
        raise ValueError(f"Invalid systematic sample size {k} for {n} cases.")
    return [math.floor((i + 0.5) * n / k) for i in range(k)]


def _select_systematic(cases: list[dict[str, Any]], sample_size: int) -> list[dict[str, Any]]:
    return [cases[index] for index in _systematic_indices(len(cases), sample_size)]


def _select_s1_loss_overall(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for case in cases:
        final = case.get("final") or {}
        if _verdict_to_arm(case, str(final.get("overall_final") or "TIE")) == "S0":
            output.append(case)
    return output


def _dedupe_cases(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    output = []
    for case in cases:
        case_id = case["id"]
        if case_id in seen:
            continue
        seen.add(case_id)
        output.append(case)
    return output


def _audit_case(
    case: dict[str, Any],
    *,
    groups: dict[str, bool],
    cache: SqliteCache,
    endpoint: str,
    model: str,
    max_tokens: int,
    timeout_sec: int,
) -> dict[str, Any]:
    gemma_case = {
        key: case.get(key)
        for key in (
            "id",
            "category",
            "source_block_id",
            "block_id",
            "chapter_id",
            "order_index",
            "block_type",
            "source",
            "candidate_a",
            "candidate_b",
            "candidate_a_arm",
            "candidate_b_arm",
            "short_block",
            "source_char_count",
            "source_token_count",
            "arm_assignment_seed",
        )
    }
    gemma_case["calls"] = {
        "ab": _judge_direction(
            case,
            direction="ab",
            cache=cache,
            endpoint=endpoint,
            model=model,
            max_tokens=max_tokens,
            timeout_sec=timeout_sec,
        ),
        "ba": _judge_direction(
            case,
            direction="ba",
            cache=cache,
            endpoint=endpoint,
            model=model,
            max_tokens=max_tokens,
            timeout_sec=timeout_sec,
        ),
    }
    gemma_case["final"] = _aggregate_case(gemma_case)

    gemini_final = case.get("final") or {}
    gemini_overall_arm = _verdict_to_arm(case, str(gemini_final.get("overall_final") or "TIE"))
    gemma_overall_arm = _verdict_to_arm(gemma_case, str((gemma_case.get("final") or {}).get("overall_final") or "TIE"))
    gemma_invalid = _gemma_invalid(gemma_case)
    reversal = (
        not gemma_invalid
        and gemini_overall_arm in {"S0", "S1"}
        and gemma_overall_arm in {"S0", "S1"}
        and gemma_overall_arm != gemini_overall_arm
    )
    tie_vs_win = (
        not gemma_invalid
        and {gemini_overall_arm, gemma_overall_arm} & {"S0", "S1"}
        and "TIE" in {gemini_overall_arm, gemma_overall_arm}
    )

    return {
        "id": case["id"],
        "block_id": case.get("block_id"),
        "order_index": case.get("order_index"),
        "short_block": bool(case.get("short_block")),
        "groups": groups,
        "candidate_a_arm": case.get("candidate_a_arm"),
        "candidate_b_arm": case.get("candidate_b_arm"),
        "gemini": {
            "overall_final": gemini_final.get("overall_final"),
            "style_final": gemini_final.get("style_final"),
            "tags_final": gemini_final.get("tags_final") or [],
            "flags": gemini_final.get("flags") or [],
            "overall_arm": gemini_overall_arm,
            "style_arm": _verdict_to_arm(case, str(gemini_final.get("style_final") or "TIE")),
            "ab": _compact_call((case.get("calls") or {}).get("ab") or {}),
            "ba": _compact_call((case.get("calls") or {}).get("ba") or {}),
        },
        "gemma": {
            "overall_final": gemma_case["final"].get("overall_final"),
            "style_final": gemma_case["final"].get("style_final"),
            "tags_final": gemma_case["final"].get("tags_final") or [],
            "flags": gemma_case["final"].get("flags") or [],
            "overall_arm": gemma_overall_arm,
            "style_arm": _verdict_to_arm(gemma_case, str(gemma_case["final"].get("style_final") or "TIE")),
            "invalid_for_reversal": gemma_invalid,
            "ab": _compact_call(gemma_case["calls"]["ab"]),
            "ba": _compact_call(gemma_case["calls"]["ba"]),
        },
        "reversal_overall": reversal,
        "tie_vs_win_overall": bool(tie_vs_win),
        "source_prefix": str(case.get("source") or "")[:220],
        "candidate_a_prefix": str(case.get("candidate_a") or "")[:220],
        "candidate_b_prefix": str(case.get("candidate_b") or "")[:220],
    }


def _judge_direction(
    case: dict[str, Any],
    *,
    direction: str,
    cache: SqliteCache,
    endpoint: str,
    model: str,
    max_tokens: int,
    timeout_sec: int,
) -> dict[str, Any]:
    if direction == "ab":
        candidate_x = str(case.get("candidate_a") or "")
        candidate_y = str(case.get("candidate_b") or "")
        order_note = "X=A, Y=B"
    elif direction == "ba":
        candidate_x = str(case.get("candidate_b") or "")
        candidate_y = str(case.get("candidate_a") or "")
        order_note = "X=B, Y=A"
    else:
        raise ValueError(f"Unknown direction {direction}")

    prompt = PJ_PROMPT_TEMPLATE.replace("{source}", str(case.get("source") or ""))
    prompt = prompt.replace("{candidate_x}", candidate_x)
    prompt = prompt.replace("{candidate_y}", candidate_y)
    request_profile = {
        "temperature": 0,
        "top_p": 1,
        "seed": SEED,
        "repeat_penalty": 1.0,
        "reasoning_effort": "none",
        "max_tokens": max_tokens,
    }
    last_result: dict[str, Any] | None = None
    for attempt in (1, 2):
        try:
            call = _chat_completion_cached(
                cache=cache,
                endpoint=endpoint,
                model=model,
                messages=[{"role": "user", "content": prompt}],
                prompt_version=f"pj_gemma_audit:{PROMPT_VERSION_PJ}:{direction}" + ("" if attempt == 1 else ":retry1"),
                prompt_text=PJ_PROMPT_TEMPLATE,
                request_params=request_profile,
                timeout_sec=timeout_sec,
            )
            result = _parse_pj_call(call, direction=direction, order_note=order_note, attempt=attempt)
        except Exception as exc:  # noqa: BLE001
            result = {
                "direction": direction,
                "order_note": order_note,
                "overall_verdict": None,
                "style_verdict": None,
                "tags": [],
                "note": "",
                "validation_error": "",
                "transport_error": f"{type(exc).__name__}: {exc}",
                "raw_content": "",
                "raw_content_prefix": "",
                "finish_reason": "",
                "model_echo": "",
                "usage": {},
                "latency_sec": 0.0,
                "from_cache": False,
                "attempt": attempt,
            }
        last_result = result
        if not result.get("validation_error") and not result.get("transport_error"):
            return result
    return last_result or {
        "direction": direction,
        "order_note": order_note,
        "overall_verdict": None,
        "style_verdict": None,
        "tags": [],
        "note": "",
        "validation_error": "unknown_error",
        "transport_error": "",
        "raw_content": "",
        "raw_content_prefix": "",
        "finish_reason": "",
        "model_echo": "",
        "usage": {},
        "latency_sec": 0.0,
        "from_cache": False,
        "attempt": 0,
    }


def _parse_pj_call(call: dict[str, Any], *, direction: str, order_note: str, attempt: int) -> dict[str, Any]:
    parsed, error = _validate_pj_json(str(call.get("content") or ""))
    return {
        "direction": direction,
        "order_note": order_note,
        "overall_verdict": parsed.get("overall_verdict"),
        "style_verdict": parsed.get("style_verdict"),
        "tags": parsed.get("tags") or [],
        "note": parsed.get("note") or "",
        "validation_error": error,
        "transport_error": "",
        "raw_content": str(call.get("content") or ""),
        "raw_content_prefix": str(call.get("content") or "")[:240],
        "finish_reason": call.get("finish_reason", ""),
        "model_echo": call.get("model_echo", ""),
        "usage": call.get("usage", {}),
        "latency_sec": call.get("latency_sec", 0.0),
        "from_cache": bool(call.get("from_cache")),
        "attempt": attempt,
    }


def _gemma_invalid(case: dict[str, Any]) -> bool:
    calls = case.get("calls") or {}
    return any(
        (calls.get(direction) or {}).get("validation_error")
        or (calls.get(direction) or {}).get("transport_error")
        for direction in ("ab", "ba")
    )


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "systematic_50": _group_summary(rows, "systematic_50"),
        "s1_loss_overall": _group_summary(rows, "s1_loss_overall"),
    }


def _group_summary(rows: list[dict[str, Any]], group_name: str) -> dict[str, Any]:
    selected = [row for row in rows if row.get("groups", {}).get(group_name)]
    gemini_decisive = [row for row in selected if row["gemini"]["overall_arm"] in {"S0", "S1"}]
    valid = [row for row in selected if not row["gemma"]["invalid_for_reversal"]]
    evaluable = [row for row in gemini_decisive if not row["gemma"]["invalid_for_reversal"]]
    reversals = [row for row in evaluable if row.get("reversal_overall")]
    tie_vs_win = [row for row in valid if row.get("tie_vs_win_overall")]
    same_winner = [
        row
        for row in evaluable
        if row["gemma"]["overall_arm"] in {"S0", "S1"}
        and row["gemma"]["overall_arm"] == row["gemini"]["overall_arm"]
    ]
    gemma_overall_arms = Counter(row["gemma"]["overall_arm"] for row in valid)
    gemini_overall_arms = Counter(row["gemini"]["overall_arm"] for row in selected)
    primary_denominator = len(evaluable)
    primary_rate = len(reversals) / primary_denominator if primary_denominator else 0.0
    return {
        "n_cases": len(selected),
        "n_gemini_decisive": len(gemini_decisive),
        "n_gemma_valid": len(valid),
        "n_evaluable_primary": primary_denominator,
        "same_winner_count": len(same_winner),
        "tie_vs_win_count": len(tie_vs_win),
        "reversal_count": len(reversals),
        "reversal_rate_primary": primary_rate,
        "reversal_rate_over_all_cases": len(reversals) / len(selected) if selected else 0.0,
        "pass_reversal_rate_le_10pct": primary_rate <= 0.10,
        "gemini_overall_arm_counts": dict(gemini_overall_arms),
        "gemma_overall_arm_counts_valid": dict(gemma_overall_arms),
        "validation_errors": _validation_error_counts(selected),
    }


def _reversal_rows(rows: list[dict[str, Any]], group_name: str) -> list[dict[str, Any]]:
    output = []
    for row in rows:
        if not row.get("groups", {}).get(group_name) or not row.get("reversal_overall"):
            continue
        output.append({
            "id": row["id"],
            "block_id": row["block_id"],
            "order_index": row["order_index"],
            "gemini_overall_arm": row["gemini"]["overall_arm"],
            "gemma_overall_arm": row["gemma"]["overall_arm"],
            "gemini_tags": row["gemini"]["tags_final"],
            "gemma_tags": row["gemma"]["tags_final"],
            "gemini_notes": {
                "ab": row["gemini"]["ab"]["note"],
                "ba": row["gemini"]["ba"]["note"],
            },
            "gemma_notes": {
                "ab": row["gemma"]["ab"]["note"],
                "ba": row["gemma"]["ba"]["note"],
            },
            "source_prefix": row["source_prefix"],
            "candidate_a_prefix": row["candidate_a_prefix"],
            "candidate_b_prefix": row["candidate_b_prefix"],
        })
    return output


def _validation_error_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for row in rows:
        for judge_name in ("gemini", "gemma"):
            for direction in ("ab", "ba"):
                call = (row.get(judge_name) or {}).get(direction) or {}
                validation_error = str(call.get("validation_error") or "")
                transport_error = str(call.get("transport_error") or "")
                if validation_error:
                    counts[f"{judge_name}:validation:{validation_error}"] += 1
                if transport_error:
                    counts[f"{judge_name}:transport"] += 1
    return dict(counts)


def _compact_call(call: dict[str, Any]) -> dict[str, Any]:
    return {
        "overall_verdict": call.get("overall_verdict"),
        "style_verdict": call.get("style_verdict"),
        "tags": call.get("tags") or [],
        "note": call.get("note") or "",
        "validation_error": call.get("validation_error") or "",
        "transport_error": call.get("transport_error") or "",
        "raw_content": call.get("raw_content") or "",
        "raw_content_prefix": call.get("raw_content_prefix") or "",
        "finish_reason": call.get("finish_reason") or "",
        "model_echo": call.get("model_echo") or call.get("model_version") or "",
        "usage": call.get("usage") or {},
        "latency_sec": call.get("latency_sec") or 0.0,
        "from_cache": bool(call.get("from_cache")),
    }


def _console_summary(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": report["status"],
        "out": report["out"],
        "input": report["input"],
        "selection": {
            "systematic_50": report["selection"]["systematic_50"]["k"],
            "s1_loss_overall": report["selection"]["s1_loss_overall"]["count"],
            "audit_unique_cases": report["selection"]["audit_unique_cases"],
        },
        "cache": report.get("cache"),
        "elapsed_sec": report.get("elapsed_sec"),
        "summary": report.get("summary"),
    }


if __name__ == "__main__":
    raise SystemExit(main())
