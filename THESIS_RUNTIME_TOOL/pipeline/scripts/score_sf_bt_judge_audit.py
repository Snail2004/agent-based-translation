from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import time
from pathlib import Path
from typing import Any

from pipeline.scripts.probe_sf_bt import (
    ALLOWED_FLAGS,
    JUDGE_PROMPT_TEMPLATE,
    PROMPT_VERSION_JUDGE,
    SEED,
    SqliteCache,
    _cache_key,
    _chat_completion_cached,
    _parse_json_object,
    _sha256_text,
    _write_json,
)


DEFAULT_INPUT = Path("data/reports/exp_s0s1_builderv2_v1/sf_bt_mlp_full_stage1.json")
DEFAULT_OUT = Path("data/reports/exp_s0s1_builderv2_v1/sf_bt_mlp_gemma_judge_audit.json")
DEFAULT_CACHE = Path("data/reports/exp_s0s1_builderv2_v1/sf_bt_mlp_gemma_judge_audit_cache.sqlite3")
DEFAULT_ENDPOINT = "http://127.0.0.1:1234"
DEFAULT_MODEL = "google/gemma-4-12b"


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit Gemini SF-BT judge scores with local Gemma on fixed MLP subsets.")
    parser.add_argument("--input", default=str(DEFAULT_INPUT))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--cache-db", default=str(DEFAULT_CACHE))
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--sample-size", type=int, default=100)
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument("--timeout-sec", type=int, default=240)
    args = parser.parse_args()

    input_path = Path(args.input)
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    items = list(payload.get("items") or [])
    if not items:
        raise SystemExit(f"No items in {input_path}")

    systematic = _select_systematic(items, args.sample_size)
    negative = _select_negative_delta_items(items)
    audit_items = _dedupe_items(systematic + negative)

    report: dict[str, Any] = {
        "metric": "SF-BT",
        "phase": "judge_audit_gemma_local",
        "status": "running",
        "input": str(input_path),
        "input_sha256": _sha256_file(input_path),
        "out": str(Path(args.out)),
        "models": {
            "baseline_judge": "gemini-2.5-flash",
            "audit_judge": args.model,
            "endpoint": args.endpoint,
        },
        "prompt": {
            "version": PROMPT_VERSION_JUDGE,
            "sha256": _sha256_text(JUDGE_PROMPT_TEMPLATE),
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
            "systematic": {
                "source": "950 MLP block-arm items from sf_bt_mlp_full_stage1.json",
                "formula": "idx_i = floor((i + 0.5) * N / k), i=0..k-1, over artifact item order",
                "requested_count": args.sample_size,
                "selected_count": len(systematic),
                "item_ids": [item["item_id"] for item in systematic],
            },
            "negative_pairs": _negative_pair_selection_metadata(items),
            "audit_unique_items": len(audit_items),
        },
        "items": [],
    }
    _write_json(Path(args.out), report)

    cache = SqliteCache(Path(args.cache_db))
    started = time.perf_counter()
    for index, item in enumerate(audit_items, 1):
        print(f"[{index}/{len(audit_items)}] {item['item_id']}", flush=True)
        row = _audit_item(
            item,
            group_membership={
                "systematic_100": item["item_id"] in {entry["item_id"] for entry in systematic},
                "llm_negative_pair_items": item["item_id"] in {entry["item_id"] for entry in negative},
            },
            cache=cache,
            endpoint=args.endpoint,
            model=args.model,
            max_tokens=args.max_tokens,
            timeout_sec=args.timeout_sec,
        )
        report["items"].append(row)
        if index % 10 == 0 or index == len(audit_items):
            partial = dict(report)
            partial["status"] = "running_partial"
            partial["elapsed_sec"] = time.perf_counter() - started
            partial["cache"] = cache.stats()
            _write_json(Path(args.out), partial)

    report["status"] = "audit_complete_stop_no_commit"
    report["elapsed_sec"] = time.perf_counter() - started
    report["cache"] = cache.stats()
    report["summary"] = {
        "systematic_100": _group_summary(report["items"], "systematic_100"),
        "llm_negative_pair_items": _group_summary(report["items"], "llm_negative_pair_items"),
    }
    report["disagreements_abs_ge_25"] = {
        "systematic_100": _disagreements(report["items"], "systematic_100", threshold=25.0),
        "llm_negative_pair_items": _disagreements(report["items"], "llm_negative_pair_items", threshold=25.0),
    }
    _write_json(Path(args.out), report)
    print(json.dumps(_console_summary(report), ensure_ascii=False, indent=2))
    return 0


def _select_systematic(items: list[dict[str, Any]], sample_size: int) -> list[dict[str, Any]]:
    if sample_size <= 0 or sample_size > len(items):
        raise ValueError(f"Invalid sample size {sample_size} for {len(items)} items.")
    indices = [math.floor((i + 0.5) * len(items) / sample_size) for i in range(sample_size)]
    return [items[index] for index in indices]


def _select_negative_delta_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_block: dict[str, dict[str, dict[str, Any]]] = {}
    for item in items:
        by_block.setdefault(str(item["block_id"]), {})[str(item["config"])] = item
    selected: list[dict[str, Any]] = []
    for _block_id, configs in sorted(by_block.items(), key=lambda row: min(int(item.get("order_index") or 0) for item in row[1].values())):
        if "S0" not in configs or "S1" not in configs:
            continue
        s0 = _gemini_score(configs["S0"])
        s1 = _gemini_score(configs["S1"])
        if s0 is None or s1 is None:
            continue
        if s1 < s0:
            selected.extend([configs["S0"], configs["S1"]])
    return selected


def _negative_pair_selection_metadata(items: list[dict[str, Any]]) -> dict[str, Any]:
    selected = _select_negative_delta_items(items)
    pairs = []
    for i in range(0, len(selected), 2):
        s0 = selected[i]
        s1 = selected[i + 1]
        pairs.append({
            "block_id": s0["block_id"],
            "s0_item_id": s0["item_id"],
            "s1_item_id": s1["item_id"],
            "gemini_s0": _gemini_score(s0),
            "gemini_s1": _gemini_score(s1),
            "delta_s1_minus_s0": (_gemini_score(s1) or 0.0) - (_gemini_score(s0) or 0.0),
        })
    return {
        "definition": "block pairs where Gemini bt_llm_score.score_mean(S1) < score_mean(S0); include both arms",
        "pair_count": len(pairs),
        "item_count": len(selected),
        "pairs": pairs,
    }


def _dedupe_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    output = []
    for item in items:
        item_id = item["item_id"]
        if item_id in seen:
            continue
        seen.add(item_id)
        output.append(item)
    return output


def _audit_item(
    item: dict[str, Any],
    *,
    group_membership: dict[str, bool],
    cache: SqliteCache,
    endpoint: str,
    model: str,
    max_tokens: int,
    timeout_sec: int,
) -> dict[str, Any]:
    source = str(item["source_text"])
    bt_text = str((item.get("bt") or {}).get("text") or "")
    gemma = {
        "ab": _judge_pair(cache=cache, endpoint=endpoint, model=model, first=source, second=bt_text, direction="ab", max_tokens=max_tokens, timeout_sec=timeout_sec),
        "ba": _judge_pair(cache=cache, endpoint=endpoint, model=model, first=bt_text, second=source, direction="ba", max_tokens=max_tokens, timeout_sec=timeout_sec),
    }
    scores = [
        gemma[direction].get("score")
        for direction in ("ab", "ba")
        if isinstance(gemma[direction].get("score"), (int, float))
    ]
    gemma["score_mean"] = statistics.mean(scores) if scores else None
    ab_score = gemma["ab"].get("score")
    ba_score = gemma["ba"].get("score")
    gemma["score_delta_abs"] = (
        abs(float(ab_score) - float(ba_score))
        if isinstance(ab_score, (int, float)) and isinstance(ba_score, (int, float))
        else None
    )
    gemini = item.get("bt_llm_score") or {}
    return {
        "item_id": item["item_id"],
        "chapter_id": item.get("chapter_id"),
        "block_id": item.get("block_id"),
        "order_index": item.get("order_index"),
        "config": item.get("config"),
        "source_token_count": item.get("source_token_count"),
        "short_block": item.get("short_block"),
        "groups": group_membership,
        "gemini": {
            "score_mean": gemini.get("score_mean"),
            "score_delta_abs": gemini.get("score_delta_abs"),
            "ab": _compact_judge(gemini.get("ab") or {}),
            "ba": _compact_judge(gemini.get("ba") or {}),
        },
        "gemma": {
            "score_mean": gemma.get("score_mean"),
            "score_delta_abs": gemma.get("score_delta_abs"),
            "ab": _compact_judge(gemma.get("ab") or {}),
            "ba": _compact_judge(gemma.get("ba") or {}),
        },
        "score_diff_gemma_minus_gemini": (
            float(gemma["score_mean"]) - float(gemini["score_mean"])
            if isinstance(gemma.get("score_mean"), (int, float)) and isinstance(gemini.get("score_mean"), (int, float))
            else None
        ),
        "source_prefix": source[:220],
        "bt_prefix": bt_text[:220],
    }


def _judge_pair(
    *,
    cache: SqliteCache,
    endpoint: str,
    model: str,
    first: str,
    second: str,
    direction: str,
    max_tokens: int,
    timeout_sec: int,
) -> dict[str, Any]:
    prompt = JUDGE_PROMPT_TEMPLATE.format(first=first, second=second)
    request_profile = {
        "temperature": 0,
        "top_p": 1,
        "seed": SEED,
        "repeat_penalty": 1.0,
        "reasoning_effort": "none",
        "max_tokens": max_tokens,
    }
    last_result = None
    for attempt in (1, 2):
        call = _chat_completion_cached(
            cache=cache,
            endpoint=endpoint,
            model=model,
            messages=[{"role": "user", "content": prompt}],
            prompt_version=f"gemma_audit:{PROMPT_VERSION_JUDGE}:{direction}" + ("" if attempt == 1 else ":retry1"),
            prompt_text=JUDGE_PROMPT_TEMPLATE,
            request_params=request_profile,
            timeout_sec=timeout_sec,
        )
        result = _parse_judge_call(call, direction=direction, attempt=attempt)
        last_result = result
        if not result.get("validation_error"):
            return result
    return last_result or {
        "direction": direction,
        "score": None,
        "flags": [],
        "note": "",
        "validation_error": "unknown_error",
        "raw_content_prefix": "",
        "finish_reason": "",
        "model_echo": "",
        "usage": {},
        "latency_sec": 0.0,
        "from_cache": False,
        "attempt": 0,
    }


def _parse_judge_call(call: dict[str, Any], *, direction: str, attempt: int) -> dict[str, Any]:
    parsed = _parse_json_object(call["content"])
    validation_error = ""
    score = None
    flags: list[str] = []
    note = ""
    if parsed is None:
        validation_error = "json_parse_fail"
    else:
        try:
            score = float(parsed["score"])
            if score < 0 or score > 100:
                validation_error = "score_out_of_range"
            raw_flags = parsed.get("flags") or []
            if not isinstance(raw_flags, list) or any(str(flag) not in ALLOWED_FLAGS for flag in raw_flags):
                validation_error = validation_error or "invalid_flags"
            flags = [str(flag) for flag in raw_flags if str(flag) in ALLOWED_FLAGS]
            note = str(parsed.get("note") or "")
        except Exception as exc:  # noqa: BLE001
            validation_error = f"schema_error:{type(exc).__name__}"
    return {
        "direction": direction,
        "score": score,
        "flags": flags,
        "note": note,
        "validation_error": validation_error,
        "raw_content_prefix": str(call["content"])[:240],
        "finish_reason": call.get("finish_reason", ""),
        "model_echo": call.get("model_echo", ""),
        "usage": call.get("usage", {}),
        "latency_sec": call.get("latency_sec", 0.0),
        "from_cache": call.get("from_cache", False),
        "attempt": attempt,
    }


def _compact_judge(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "score": row.get("score"),
        "flags": row.get("flags") or [],
        "note": row.get("note") or "",
        "validation_error": row.get("validation_error") or "",
        "finish_reason": row.get("finish_reason") or "",
        "model_echo": row.get("model_echo") or "",
        "latency_sec": row.get("latency_sec") or 0.0,
        "from_cache": bool(row.get("from_cache")),
    }


def _group_summary(rows: list[dict[str, Any]], group_name: str) -> dict[str, Any]:
    selected = [row for row in rows if row.get("groups", {}).get(group_name)]
    pairs = [
        (float(row["gemini"]["score_mean"]), float(row["gemma"]["score_mean"]))
        for row in selected
        if isinstance(row.get("gemini", {}).get("score_mean"), (int, float))
        and isinstance(row.get("gemma", {}).get("score_mean"), (int, float))
    ]
    diffs = [gemma - gemini for gemini, gemma in pairs]
    return {
        "n_items": len(selected),
        "n_valid_pairs": len(pairs),
        "pearson": _pearson([a for a, _b in pairs], [b for _a, b in pairs]),
        "mean_diff_gemma_minus_gemini": statistics.mean(diffs) if diffs else None,
        "median_diff_gemma_minus_gemini": statistics.median(diffs) if diffs else None,
        "mean_abs_diff": statistics.mean([abs(diff) for diff in diffs]) if diffs else None,
        "abs_diff_ge_25_count": sum(1 for diff in diffs if abs(diff) >= 25.0),
        "gemini_mean": statistics.mean([a for a, _b in pairs]) if pairs else None,
        "gemma_mean": statistics.mean([b for _a, b in pairs]) if pairs else None,
        "validation_errors": _validation_error_counts(selected),
    }


def _disagreements(rows: list[dict[str, Any]], group_name: str, *, threshold: float) -> list[dict[str, Any]]:
    output = []
    for row in rows:
        if not row.get("groups", {}).get(group_name):
            continue
        diff = row.get("score_diff_gemma_minus_gemini")
        if not isinstance(diff, (int, float)) or abs(diff) < threshold:
            continue
        output.append({
            "item_id": row["item_id"],
            "block_id": row["block_id"],
            "config": row["config"],
            "diff_gemma_minus_gemini": diff,
            "gemini_score": row["gemini"]["score_mean"],
            "gemma_score": row["gemma"]["score_mean"],
            "gemini_notes": {"ab": row["gemini"]["ab"]["note"], "ba": row["gemini"]["ba"]["note"]},
            "gemma_notes": {"ab": row["gemma"]["ab"]["note"], "ba": row["gemma"]["ba"]["note"]},
            "source_prefix": row["source_prefix"],
            "bt_prefix": row["bt_prefix"],
        })
    return sorted(output, key=lambda item: abs(float(item["diff_gemma_minus_gemini"])), reverse=True)


def _validation_error_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        for judge_name in ("gemini", "gemma"):
            for direction in ("ab", "ba"):
                error = row.get(judge_name, {}).get(direction, {}).get("validation_error") or ""
                if error:
                    key = f"{judge_name}:{error}"
                    counts[key] = counts.get(key, 0) + 1
    return counts


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 2 or len(xs) != len(ys):
        return None
    mean_x = statistics.mean(xs)
    mean_y = statistics.mean(ys)
    dx = [x - mean_x for x in xs]
    dy = [y - mean_y for y in ys]
    denom = math.sqrt(sum(x * x for x in dx) * sum(y * y for y in dy))
    if denom == 0:
        return None
    return sum(x * y for x, y in zip(dx, dy, strict=True)) / denom


def _gemini_score(item: dict[str, Any]) -> float | None:
    score = (item.get("bt_llm_score") or {}).get("score_mean")
    return float(score) if isinstance(score, (int, float)) else None


def _console_summary(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "out": report["out"],
        "status": report["status"],
        "elapsed_sec": report["elapsed_sec"],
        "cache": report["cache"],
        "summary": report["summary"],
        "disagreement_counts": {
            key: len(value)
            for key, value in report["disagreements_abs_ge_25"].items()
        },
    }


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
