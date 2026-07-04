from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import math
import re
import sqlite3
import statistics
import time
from collections import Counter
from pathlib import Path
from typing import Any

import requests


DEFAULT_WORKDB = Path("data/jobs/exp_s0s1_full/memory.sqlite3")
DEFAULT_COMET_PROBE = Path("data/reports/comet_qe_probe_wmt22_cometkiwi_da_utf8_wide.json")
DEFAULT_OUT = Path("data/reports/exp_s0s1_builderv2_v1/sf_bt_phase1_probe.json")
DEFAULT_CACHE = Path("data/reports/exp_s0s1_builderv2_v1/sf_bt_phase1_cache.sqlite3")
DEFAULT_ENDPOINT = "http://127.0.0.1:1234"
DEFAULT_EMBED_ENDPOINT = "http://127.0.0.1:1234/v1/embeddings"
DEFAULT_BT_MODEL = "google/gemma-4-12b"
DEFAULT_JUDGE_MODEL = "google/gemma-4-12b"
DEFAULT_EMBED_MODEL = "text-embedding-bge-m3"
DEFAULT_EXPERIMENT = "exp_s0s1_builderv2_v1"
PROMPT_VERSION_BT = "bt_prompt_v1"
PROMPT_VERSION_JUDGE = "bt_judge_v1"
SEED = 20260612
ALLOWED_FLAGS = {
    "semantic_mismatch",
    "numeric_mismatch",
    "negation_mismatch",
    "coverage_mismatch",
    "untranslated_residue",
    "format_only",
}

BT_PROMPT_TEMPLATE = """You are a professional Vietnamese-to-English translator.
Translate the text below into English.
- Preserve Markdown structure, inline code, and LaTeX math ($...$, $$...$$) exactly as they appear.
- Do not add explanations, notes, headers, or anything besides the translation itself.
- Do not summarize, shorten, or expand the content.

TEXT:
<<<
{vi_block}
>>>
"""

JUDGE_PROMPT_TEMPLATE = """You compare two English passages, A and B, and judge how close they are IN MEANING.
Ignore differences in style, word choice, sentence order, formatting, and phrasing
when the meaning is unchanged. Judge only: facts, claims, numbers, logical relations,
negations, and coverage (does one passage clearly state more or less than the other?).

Score bands:
100 = same meaning; differences are purely stylistic
 75 = minor drift; one small detail differs or became vague
 50 = noticeable drift; a fact, number, or relation differs
 25 = substantial mismatch; key claims differ or a chunk of content is absent in one passage
  0 = different or contradictory content

Flags (all that apply, else empty):
semantic_mismatch | numeric_mismatch | negation_mismatch | coverage_mismatch |
untranslated_residue | format_only

Return JSON only: {{"score": <0-100>, "flags": [...], "note": "<one short sentence>"}}

PASSAGE A:
{first}

PASSAGE B:
{second}
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Run SF-BT Phase-1 probe: BT -> bge cosine -> LLM judge.")
    parser.add_argument("--db", default=str(DEFAULT_WORKDB))
    parser.add_argument("--comet-probe", default=str(DEFAULT_COMET_PROBE))
    parser.add_argument("--experiment", default=DEFAULT_EXPERIMENT)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--cache-db", default=str(DEFAULT_CACHE))
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--embed-endpoint", default=DEFAULT_EMBED_ENDPOINT)
    parser.add_argument("--bt-model", default=DEFAULT_BT_MODEL)
    parser.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL)
    parser.add_argument("--embed-model", default=DEFAULT_EMBED_MODEL)
    parser.add_argument("--chat-timeout-sec", type=int, default=240)
    parser.add_argument("--embed-timeout-sec", type=int, default=180)
    parser.add_argument("--bt-max-tokens", type=int, default=2048)
    parser.add_argument("--judge-max-tokens", type=int, default=512)
    parser.add_argument("--limit", type=int, default=0, help="Debug cap over cases; 0 means all.")
    args = parser.parse_args()

    workdb = Path(args.db)
    db_sha_before = _sha256_file(workdb) if workdb.exists() else None
    cache = SqliteCache(Path(args.cache_db))
    started = time.perf_counter()

    comet_cases, comet_pairs = _load_comet_probe(Path(args.comet_probe))
    real_cases = _load_real_diagnostic_cases(workdb, experiment=args.experiment) if workdb.exists() else []
    cases = comet_cases + real_cases
    if args.limit > 0:
        allowed_ids = {case["id"] for case in cases[: args.limit]}
        cases = cases[: args.limit]
        comet_pairs = [pair for pair in comet_pairs if pair["good"] in allowed_ids and pair["bad"] in allowed_ids]

    request_profile = {
        "temperature": 0,
        "top_p": 1,
        "seed": SEED,
        "repeat_penalty": 1.0,
        "reasoning_effort": "none",
    }
    report: dict[str, Any] = {
        "metric": "SF-BT",
        "phase": "phase1_probe",
        "status": "probe_only_not_full_metric",
        "created_at_unix": int(time.time()),
        "db": str(workdb),
        "db_sha256_before": db_sha_before,
        "experiment": args.experiment,
        "models": {
            "bt_model": args.bt_model,
            "judge_model": args.judge_model,
            "embed_model": args.embed_model,
            "endpoint": args.endpoint,
            "embed_endpoint": args.embed_endpoint,
        },
        "prompt_versions": {
            "bt": PROMPT_VERSION_BT,
            "judge": PROMPT_VERSION_JUDGE,
            "bt_prompt_sha256": _sha256_text(BT_PROMPT_TEMPLATE),
            "judge_prompt_sha256": _sha256_text(JUDGE_PROMPT_TEMPLATE),
        },
        "request_profile": request_profile,
        "input_counts": {
            "comet_wide_cases": len(comet_cases),
            "comet_wide_pairs": len(comet_pairs),
            "real_diagnostic_cases": len(real_cases),
            "total_cases": len(cases),
        },
        "cases": [],
    }

    model_listing = _safe_get_models(args.endpoint)
    report["lmstudio_models"] = model_listing

    for index, case in enumerate(cases, 1):
        print(f"[{index}/{len(cases)}] {case['id']}", flush=True)
        case_report = _run_case(
            case,
            cache=cache,
            endpoint=args.endpoint,
            embed_endpoint=args.embed_endpoint,
            bt_model=args.bt_model,
            judge_model=args.judge_model,
            embed_model=args.embed_model,
            chat_timeout_sec=args.chat_timeout_sec,
            embed_timeout_sec=args.embed_timeout_sec,
            bt_max_tokens=args.bt_max_tokens,
            judge_max_tokens=args.judge_max_tokens,
            request_profile=request_profile,
        )
        report["cases"].append(case_report)
        _write_json(Path(args.out), _partial_report(report, cache, started, workdb))

    report["pairwise"] = _pairwise_report(report["cases"], comet_pairs)
    report["order_sensitivity"] = _order_sensitivity(report["cases"])
    report["hygiene_summary"] = _hygiene_summary(report["cases"])
    report["score_band_distribution"] = _score_band_distribution(report["cases"])
    report["preamble_summary"] = _preamble_summary(report["cases"])
    report["runtime_summary"] = _runtime_summary(report["cases"])
    report["cache"] = cache.stats()
    report["elapsed_sec"] = time.perf_counter() - started
    report["db_sha256_after"] = _sha256_file(workdb) if workdb.exists() else None
    _write_json(Path(args.out), report)
    print(json.dumps(_console_summary(report), ensure_ascii=False, indent=2))
    return 0


def _run_case(
    case: dict[str, Any],
    *,
    cache: "SqliteCache",
    endpoint: str,
    embed_endpoint: str,
    bt_model: str,
    judge_model: str,
    embed_model: str,
    chat_timeout_sec: int,
    embed_timeout_sec: int,
    bt_max_tokens: int,
    judge_max_tokens: int,
    request_profile: dict[str, Any],
) -> dict[str, Any]:
    src = str(case["src"])
    vi = str(case["mt"])
    bt_prompt = BT_PROMPT_TEMPLATE.format(vi_block=vi)
    bt_call = _chat_completion_cached(
        cache=cache,
        endpoint=endpoint,
        model=bt_model,
        messages=[{"role": "user", "content": bt_prompt}],
        prompt_version=PROMPT_VERSION_BT,
        prompt_text=BT_PROMPT_TEMPLATE,
        request_params={**request_profile, "max_tokens": bt_max_tokens},
        timeout_sec=chat_timeout_sec,
    )
    bt_text = bt_call["content"]
    hygiene = _bt_hygiene(src=src, vi=vi, bt=bt_text, finish_reason=str(bt_call.get("finish_reason") or ""))

    en_bt_cos = _cosine_for_pair(
        cache=cache,
        endpoint=embed_endpoint,
        model=embed_model,
        left=src,
        right=bt_text,
        timeout_sec=embed_timeout_sec,
    )
    direct_en_vi_cos = _cosine_for_pair(
        cache=cache,
        endpoint=embed_endpoint,
        model=embed_model,
        left=src,
        right=vi,
        timeout_sec=embed_timeout_sec,
    )

    judge = {}
    too_long = _estimated_tokens(JUDGE_PROMPT_TEMPLATE.format(first=src, second=bt_text)) > 7000
    if too_long:
        judge["skipped"] = "too_long_for_llm"
    else:
        judge["ab"] = _judge_pair(
            cache=cache,
            endpoint=endpoint,
            model=judge_model,
            first=src,
            second=bt_text,
            direction="ab",
            request_profile=request_profile,
            max_tokens=judge_max_tokens,
            timeout_sec=chat_timeout_sec,
        )
        judge["ba"] = _judge_pair(
            cache=cache,
            endpoint=endpoint,
            model=judge_model,
            first=bt_text,
            second=src,
            direction="ba",
            request_profile=request_profile,
            max_tokens=judge_max_tokens,
            timeout_sec=chat_timeout_sec,
        )
        scores = [
            item["score"]
            for item in [judge["ab"], judge["ba"]]
            if isinstance(item.get("score"), (int, float))
        ]
        judge["score_mean"] = statistics.mean(scores) if scores else None
        judge["score_delta_abs"] = abs(judge["ab"].get("score", 0) - judge["ba"].get("score", 0))

    return {
        "id": case["id"],
        "source": case.get("source", "comet_wide"),
        "expected": case.get("expected", ""),
        "note": case.get("note", ""),
        "src": src,
        "mt": vi,
        "bt": {
            "text": bt_text,
            "finish_reason": bt_call.get("finish_reason", ""),
            "model_echo": bt_call.get("model_echo", ""),
            "usage": bt_call.get("usage", {}),
            "latency_sec": bt_call.get("latency_sec", 0.0),
            "from_cache": bt_call.get("from_cache", False),
            "content_prefix": bt_text[:160],
        },
        "hygiene": hygiene,
        "bt_bge_cosine": en_bt_cos,
        "direct_bge_en_vi": direct_en_vi_cos,
        "bt_llm_score": judge,
        "short_block": len(src) < 40 or len(src.split()) < 8,
        "too_long_for_llm": too_long,
    }


def _load_comet_probe(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = []
    for item in payload.get("cases") or []:
        cases.append({
            "id": str(item["id"]),
            "src": str(item["src"]),
            "mt": str(item["mt"]),
            "expected": str(item.get("expected") or ""),
            "note": str(item.get("note") or ""),
            "source": "comet_wide",
        })
    pairs = [
        {"good": str(item["good"]), "bad": str(item["bad"])}
        for item in payload.get("pair_results") or []
    ]
    if not cases or not pairs:
        raise ValueError(f"Probe artifact missing cases/pairs: {path}")
    return cases, pairs


def _load_real_diagnostic_cases(db_path: Path, *, experiment: str) -> list[dict[str, Any]]:
    queries = [
        ("real_vector_s0_expected_blind", "vector", "S0", "expected_blind", "real term-style case: vector/vecto"),
        ("real_vector_s1_expected_blind", "vector", "S1", "expected_blind", "real term-style case: vector/vecto"),
        ("real_regularization_s1_expected_blind", "regularization", "S1", "expected_blind", "real term-style case: regularization"),
        ("real_negation_s0", " not ", "S0", "diagnostic_real", "real negation candidate"),
        ("real_numeric_s1", "0.1", "S1", "diagnostic_real", "real numeric/math candidate"),
    ]
    cases: list[dict[str, Any]] = []
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row
        for case_id, needle, config, expected, note in queries:
            row = con.execute(
                """
                SELECT
                    b.chapter_id,
                    b.block_id,
                    b.order_index,
                    COALESCE(NULLIF(b.original_text, ''), b.text) AS src,
                    tr.output_text AS mt
                FROM translation_runs tr
                JOIN blocks b ON b.block_id = tr.block_id
                WHERE tr.experiment_id = ?
                  AND tr.stage = 'draft'
                  AND tr.config = ?
                  AND (
                    lower(COALESCE(NULLIF(b.original_text, ''), b.text)) LIKE ?
                    OR lower(tr.output_text) LIKE ?
                  )
                ORDER BY b.chapter_id, b.order_index
                LIMIT 1
                """,
                (experiment, config, f"%{needle.lower()}%", f"%{needle.lower()}%"),
            ).fetchone()
            if row is None:
                continue
            cases.append({
                "id": case_id,
                "src": str(row["src"] or ""),
                "mt": str(row["mt"] or ""),
                "expected": expected,
                "note": note,
                "source": "workdb_diagnostic",
                "chapter_id": str(row["chapter_id"]),
                "block_id": str(row["block_id"]),
                "config": config,
            })
    return cases


def _judge_pair(
    *,
    cache: "SqliteCache",
    endpoint: str,
    model: str,
    first: str,
    second: str,
    direction: str,
    request_profile: dict[str, Any],
    max_tokens: int,
    timeout_sec: int,
) -> dict[str, Any]:
    prompt = JUDGE_PROMPT_TEMPLATE.format(first=first, second=second)
    call = _chat_completion_cached(
        cache=cache,
        endpoint=endpoint,
        model=model,
        messages=[{"role": "user", "content": prompt}],
        prompt_version=f"{PROMPT_VERSION_JUDGE}:{direction}",
        prompt_text=JUDGE_PROMPT_TEMPLATE,
        request_params={**request_profile, "max_tokens": max_tokens},
        timeout_sec=timeout_sec,
    )
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
        "raw_content_prefix": call["content"][:240],
        "finish_reason": call.get("finish_reason", ""),
        "model_echo": call.get("model_echo", ""),
        "usage": call.get("usage", {}),
        "latency_sec": call.get("latency_sec", 0.0),
        "from_cache": call.get("from_cache", False),
    }


def _chat_completion_cached(
    *,
    cache: "SqliteCache",
    endpoint: str,
    model: str,
    messages: list[dict[str, str]],
    prompt_version: str,
    prompt_text: str,
    request_params: dict[str, Any],
    timeout_sec: int,
) -> dict[str, Any]:
    key = _cache_key({
        "kind": "chat",
        "endpoint": endpoint,
        "model": model,
        "messages": messages,
        "prompt_version": prompt_version,
        "prompt_sha256": _sha256_text(prompt_text),
        "request_params": request_params,
    })
    cached = cache.get(key)
    if cached is not None:
        cached["from_cache"] = True
        return cached
    payload = {
        "model": model,
        "messages": messages,
        "temperature": request_params.get("temperature", 0),
        "top_p": request_params.get("top_p", 1),
        "seed": request_params.get("seed", SEED),
        "max_tokens": request_params["max_tokens"],
        "repeat_penalty": request_params.get("repeat_penalty", 1.0),
        "reasoning_effort": request_params.get("reasoning_effort", "none"),
    }
    started = time.perf_counter()
    response = requests.post(f"{endpoint.rstrip('/')}/v1/chat/completions", json=payload, timeout=timeout_sec)
    latency = time.perf_counter() - started
    response.raise_for_status()
    body = response.json()
    choice = body["choices"][0]
    message = choice.get("message") or {}
    result = {
        "content": str(message.get("content") or ""),
        "reasoning_content": str(message.get("reasoning_content") or ""),
        "finish_reason": str(choice.get("finish_reason") or ""),
        "model_echo": str(body.get("model") or ""),
        "usage": body.get("usage") or {},
        "latency_sec": latency,
        "from_cache": False,
    }
    cache.put(key, result)
    return result


def _cosine_for_pair(
    *,
    cache: "SqliteCache",
    endpoint: str,
    model: str,
    left: str,
    right: str,
    timeout_sec: int,
) -> dict[str, Any]:
    started = time.perf_counter()
    left_vec = _embedding_cached(cache, endpoint=endpoint, model=model, text=left, timeout_sec=timeout_sec)
    right_vec = _embedding_cached(cache, endpoint=endpoint, model=model, text=right, timeout_sec=timeout_sec)
    return {
        "score": _cosine(left_vec["embedding"], right_vec["embedding"]),
        "model": model,
        "left_from_cache": left_vec["from_cache"],
        "right_from_cache": right_vec["from_cache"],
        "latency_sec": time.perf_counter() - started,
    }


def _embedding_cached(
    cache: "SqliteCache",
    *,
    endpoint: str,
    model: str,
    text: str,
    timeout_sec: int,
) -> dict[str, Any]:
    normalized = _normalize_text(text)
    key = _cache_key({
        "kind": "embedding",
        "endpoint": endpoint,
        "model": model,
        "input_sha256": _sha256_text(normalized),
        "input": normalized,
    })
    cached = cache.get(key)
    if cached is not None:
        cached["from_cache"] = True
        return cached
    started = time.perf_counter()
    response = requests.post(endpoint, json={"model": model, "input": normalized}, timeout=timeout_sec)
    latency = time.perf_counter() - started
    response.raise_for_status()
    body = response.json()
    returned_model = str(body.get("model") or model)
    vector = [float(value) for value in body["data"][0]["embedding"]]
    result = {
        "embedding": vector,
        "model_echo": returned_model,
        "latency_sec": latency,
        "from_cache": False,
    }
    cache.put(key, result)
    return result


def _bt_hygiene(*, src: str, vi: str, bt: str, finish_reason: str) -> dict[str, Any]:
    flags: list[str] = []
    if not bt.strip():
        flags.append("empty_bt")
    if finish_reason == "length":
        flags.append("bt_truncated")
    vi_chars = _vietnamese_diacritic_count(bt)
    if vi_chars >= 8 and vi_chars / max(len(bt), 1) > 0.02:
        flags.append("vietnamese_residue")
    if _math_token_count(vi) != _math_token_count(bt):
        flags.append("math_token_count_mismatch")
    if _code_tick_count(vi) != _code_tick_count(bt):
        flags.append("code_tick_count_mismatch")
    source_copy_ratio = difflib.SequenceMatcher(None, _normalize_text(src).casefold(), _normalize_text(vi).casefold()).ratio()
    if len(src) >= 40 and source_copy_ratio >= 0.95:
        flags.append("input_matches_source")
    ratio = len(bt.strip()) / max(len(vi.strip()), 1)
    if ratio < 0.35 or ratio > 2.8:
        flags.append("length_ratio_abnormal")
    preamble = bool(re.match(r"^\s*(here is|sure[,:\s]|the translation)", bt, flags=re.I))
    if preamble:
        flags.append("preamble_detected")
    return {
        "flags": flags,
        "vietnamese_diacritic_chars": vi_chars,
        "length_ratio_bt_over_vi": ratio,
        "src_char_count": len(src),
        "src_token_count": len(src.split()),
        "preamble_detected": preamble,
        "input_source_copy_ratio": source_copy_ratio,
    }


def _pairwise_report(cases: list[dict[str, Any]], pairs: list[dict[str, str]]) -> dict[str, Any]:
    by_id = {case["id"]: case for case in cases}
    components = ["bt_bge_cosine", "bt_llm_score", "direct_bge_en_vi"]
    results: dict[str, Any] = {}
    pair_rows: list[dict[str, Any]] = []
    for pair in pairs:
        good = by_id.get(pair["good"])
        bad = by_id.get(pair["bad"])
        if not good or not bad:
            continue
        row = {"good": pair["good"], "bad": pair["bad"], "components": {}}
        for component in components:
            good_score = _component_score(good, component)
            bad_score = _component_score(bad, component)
            ok = good_score is not None and bad_score is not None and good_score > bad_score
            row["components"][component] = {
                "good_score": good_score,
                "bad_score": bad_score,
                "ok": ok,
                "margin": (good_score - bad_score) if good_score is not None and bad_score is not None else None,
            }
        pair_rows.append(row)
    for component in components:
        valid = [row for row in pair_rows if row["components"][component]["good_score"] is not None and row["components"][component]["bad_score"] is not None]
        ok_count = sum(1 for row in valid if row["components"][component]["ok"])
        results[component] = {
            "ok": ok_count,
            "total": len(valid),
            "accuracy": ok_count / len(valid) if valid else None,
            "failures": [
                {
                    "good": row["good"],
                    "bad": row["bad"],
                    **row["components"][component],
                }
                for row in valid
                if not row["components"][component]["ok"]
            ],
        }
    results["pairs"] = pair_rows
    return results


def _component_score(case: dict[str, Any], component: str) -> float | None:
    if component == "bt_llm_score":
        value = (case.get("bt_llm_score") or {}).get("score_mean")
        return float(value) if value is not None else None
    value = (case.get(component) or {}).get("score")
    return float(value) if value is not None else None


def _order_sensitivity(cases: list[dict[str, Any]]) -> dict[str, Any]:
    rows = []
    for case in cases:
        if case.get("expected") == "expected_blind":
            continue
        judge = case.get("bt_llm_score") or {}
        if "ab" not in judge or "ba" not in judge:
            continue
        ab = judge["ab"].get("score")
        ba = judge["ba"].get("score")
        if ab is None or ba is None:
            continue
        rows.append({"id": case["id"], "ab": ab, "ba": ba, "abs_delta": abs(float(ab) - float(ba))})
    deltas = [row["abs_delta"] for row in rows]
    mean_abs = statistics.mean(deltas) if deltas else 0.0
    max_abs = max(deltas) if deltas else 0.0
    return {
        "evaluated_cases": len(rows),
        "mean_abs": mean_abs,
        "max_abs": max_abs,
        "full_run_policy": "one_direction" if mean_abs <= 3 and max_abs <= 10 else "two_direction",
        "threshold": {"mean_abs_max": 3, "max_abs_max": 10},
        "top_deltas": sorted(rows, key=lambda item: item["abs_delta"], reverse=True)[:10],
    }


def _hygiene_summary(cases: list[dict[str, Any]]) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    for case in cases:
        counts.update((case.get("hygiene") or {}).get("flags") or [])
    return {
        "flag_counts": dict(sorted(counts.items())),
        "cases_with_flags": [
            {"id": case["id"], "flags": (case.get("hygiene") or {}).get("flags") or []}
            for case in cases
            if (case.get("hygiene") or {}).get("flags")
        ],
    }


def _score_band_distribution(cases: list[dict[str, Any]]) -> dict[str, Any]:
    bins = Counter()
    for case in cases:
        score = _component_score(case, "bt_llm_score")
        if score is None:
            bins["missing"] += 1
        elif score >= 90:
            bins["90_100"] += 1
        elif score >= 70:
            bins["70_89"] += 1
        elif score >= 40:
            bins["40_69"] += 1
        elif score > 0:
            bins["1_39"] += 1
        else:
            bins["0"] += 1
    return dict(sorted(bins.items()))


def _preamble_summary(cases: list[dict[str, Any]]) -> dict[str, Any]:
    preamble = [
        {"id": case["id"], "prefix": (case.get("bt") or {}).get("content_prefix", "")}
        for case in cases
        if (case.get("hygiene") or {}).get("preamble_detected")
    ]
    return {"count": len(preamble), "cases": preamble[:10]}


def _runtime_summary(cases: list[dict[str, Any]]) -> dict[str, Any]:
    bt_latencies = [float((case.get("bt") or {}).get("latency_sec") or 0.0) for case in cases]
    judge_latencies = [
        float((case.get("bt_llm_score") or {}).get(direction, {}).get("latency_sec") or 0.0)
        for case in cases
        for direction in ("ab", "ba")
        if direction in (case.get("bt_llm_score") or {})
    ]
    bt_tokens = [int((case.get("bt") or {}).get("usage", {}).get("total_tokens") or 0) for case in cases]
    judge_tokens = [
        int((case.get("bt_llm_score") or {}).get(direction, {}).get("usage", {}).get("total_tokens") or 0)
        for case in cases
        for direction in ("ab", "ba")
        if direction in (case.get("bt_llm_score") or {})
    ]
    return {
        "bt_calls": len(bt_latencies),
        "judge_calls": len(judge_latencies),
        "bt_latency_sum_sec": sum(bt_latencies),
        "judge_latency_sum_sec": sum(judge_latencies),
        "bt_latency_median_sec": statistics.median(bt_latencies) if bt_latencies else 0.0,
        "judge_latency_median_sec": statistics.median(judge_latencies) if judge_latencies else 0.0,
        "bt_tokens_total": sum(bt_tokens),
        "judge_tokens_total": sum(judge_tokens),
        "bt_finish_reason_counts": dict(Counter((case.get("bt") or {}).get("finish_reason") or "" for case in cases)),
    }


def _partial_report(report: dict[str, Any], cache: "SqliteCache", started: float, workdb: Path) -> dict[str, Any]:
    partial = dict(report)
    partial["status"] = "running_partial"
    partial["cache"] = cache.stats()
    partial["elapsed_sec"] = time.perf_counter() - started
    partial["db_sha256_after"] = _sha256_file(workdb) if workdb.exists() else None
    return partial


def _console_summary(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "out": str(DEFAULT_OUT),
        "cases": report["input_counts"],
        "pairwise": {
            key: {k: value[k] for k in ("ok", "total", "accuracy")}
            for key, value in (report.get("pairwise") or {}).items()
            if key != "pairs"
        },
        "order_sensitivity": report.get("order_sensitivity"),
        "hygiene_flags": (report.get("hygiene_summary") or {}).get("flag_counts"),
        "elapsed_sec": report.get("elapsed_sec"),
        "db_unchanged": report.get("db_sha256_before") == report.get("db_sha256_after"),
    }


def _safe_get_models(endpoint: str) -> dict[str, Any]:
    try:
        response = requests.get(f"{endpoint.rstrip('/')}/v1/models", timeout=10)
        response.raise_for_status()
        return response.json()
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def _parse_json_object(text: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if not na or not nb:
        return 0.0
    return dot / (na * nb)


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text)).strip()


def _estimated_tokens(text: str) -> int:
    return max(1, int(len(text) / 4))


def _vietnamese_diacritic_count(text: str) -> int:
    return len(re.findall(r"[ăâđêôơưáàảãạấầẩẫậắằẳẵặéèẻẽẹếềểễệíìỉĩịóòỏõọốồổỗộớờởỡợúùủũụứừửữựýỳỷỹỵ]", text, flags=re.I))


def _math_token_count(text: str) -> int:
    return text.count("$")


def _code_tick_count(text: str) -> int:
    return text.count("`")


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _cache_key(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


class SqliteCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(path)
        self.con.execute(
            "CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at REAL NOT NULL)"
        )
        self.con.commit()
        self.hits = 0
        self.misses = 0
        self.writes = 0

    def get(self, key: str) -> dict[str, Any] | None:
        row = self.con.execute("SELECT payload FROM cache WHERE key = ?", (key,)).fetchone()
        if row is None:
            self.misses += 1
            return None
        self.hits += 1
        return json.loads(str(row[0]))

    def put(self, key: str, payload: dict[str, Any]) -> None:
        self.con.execute(
            "INSERT OR REPLACE INTO cache(key, payload, created_at) VALUES (?, ?, ?)",
            (key, json.dumps(payload, ensure_ascii=False, separators=(",", ":")), time.time()),
        )
        self.con.commit()
        self.writes += 1

    def stats(self) -> dict[str, Any]:
        count = self.con.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
        return {
            "path": str(self.path),
            "entries": int(count),
            "hits": self.hits,
            "misses": self.misses,
            "writes": self.writes,
        }


if __name__ == "__main__":
    raise SystemExit(main())
