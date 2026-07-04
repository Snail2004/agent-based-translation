from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import threading
import time
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from google import genai
from google.genai import types

from pipeline.scripts.probe_sf_bt import (
    SqliteCache,
    _cache_key,
    _parse_json_object,
    _sha256_file,
    _sha256_text,
    _write_json,
)
from pipeline.scripts.score_sf_bt import (
    DEFAULT_GEMINI_INPUT_PER_MILLION,
    DEFAULT_GEMINI_OUTPUT_PER_MILLION,
    SEED,
    _gemini_base_url,
    _gemini_cost,
    _gemini_finish_reason,
    _gemini_usage,
    _load_gemini_key,
)


DEFAULT_WORKDB = Path("data/jobs/exp_s0s1_full/memory.sqlite3")
DEFAULT_PLANTED = Path("data/reports/exp_s0s1_builderv2_v1/pj_planted_set_v1.json")
DEFAULT_OUT = Path("data/reports/exp_s0s1_builderv2_v1/pj_probe_v1.json")
DEFAULT_CACHE = Path("data/reports/exp_s0s1_builderv2_v1/pj_probe_cache.sqlite3")
DEFAULT_MODEL = "gemini-2.5-flash"
PROMPT_VERSION_PJ = "pj_judge_v1"
EXPECTED_PROMPT_SHA256 = "d47dbb171a30133f921063f0dad8f724256dc21d528d18724556b1d6d4f82bc2"
STYLE_TAGS = {"grammar", "naturalness", "word_choice"}
ALLOWED_TAGS = {
    "grammar",
    "naturalness",
    "word_choice",
    "terminology",
    "meaning",
    "omission_addition",
    "formatting",
}
TAG_ORDER = [
    "grammar",
    "naturalness",
    "word_choice",
    "terminology",
    "meaning",
    "omission_addition",
    "formatting",
]
VERDICTS = {"X", "Y", "TIE"}


PJ_PROMPT_TEMPLATE = """You are a strict, impartial evaluator of Vietnamese translations of English technical text (a machine learning textbook).

You are given one English source segment and two candidate Vietnamese translations, labeled X and Y. The labels and their order are arbitrary and carry no information about which candidate is better.

Compare the two candidates and output ONLY a JSON object with exactly these keys:

{"overall_verdict": "X" | "Y" | "TIE", "style_verdict": "X" | "Y" | "TIE", "tags": [...], "note": "..."}

Definitions:

1. overall_verdict — which candidate is the better Vietnamese translation of the source OVERALL, considering accuracy of meaning, completeness, technical terminology, grammar, and naturalness together. If the candidates are equally good, or the differences are too trivial to justify a preference, use "TIE".

2. style_verdict — which candidate reads better as Vietnamese PROSE, judged ONLY on grammar, fluency, naturalness, and choice of ordinary (non-technical) words. For this verdict you MUST ignore differences in technical term choices and differences in meaning relative to the source. If the only differences between X and Y are technical terms or meaning, style_verdict MUST be "TIE".

3. tags — 1 to 3 items, most important first, naming the kinds of difference that drove your verdicts, chosen ONLY from this list:
- "grammar": grammatical errors, broken or incomplete sentences.
- "naturalness": stiff, word-by-word, or un-Vietnamese phrasing.
- "word_choice": weaker choice of ordinary (non-technical) words.
- "terminology": difference in how technical terms are rendered.
- "meaning": the candidates differ in meaning relative to the English source.
- "omission_addition": content missing from or added to one candidate relative to the source.
- "formatting": markdown, code, math, numbers, or URLs damaged in one candidate.
If both verdicts are "TIE", tags may be an empty list [].

4. note — at most 25 words, in English, pointing to the decisive difference, or "no meaningful difference".

Rules:
- Judge as a knowledgeable Vietnamese reader of machine-learning texts.
- Do not reward a candidate for staying closer to English wording; good Vietnamese matters, literal similarity does not.
- Code, LaTeX math, URLs, and numbers must be preserved exactly; damage there counts against a candidate (tag "formatting").
- The segment may be a heading, list item, or code caption; judge it as such, and prefer "TIE" when it is too short to show a real quality difference.
- Use "TIE" whenever you cannot confidently prefer one candidate; never invent a preference.
- Output the JSON object only. No other text.

English source:
<<<SRC
{source}
SRC>>>

Candidate X:
<<<X
{candidate_x}
X>>>

Candidate Y:
<<<Y
{candidate_y}
Y>>>"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Run PJ planted-set probe after human review.")
    parser.add_argument("--db", default=str(DEFAULT_WORKDB))
    parser.add_argument("--planted", default=str(DEFAULT_PLANTED))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--cache-db", default=str(DEFAULT_CACHE))
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--max-output-tokens", type=int, default=512)
    parser.add_argument("--thinking-budget", type=int, default=0)
    parser.add_argument("--timeout-sec", type=int, default=120)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--gemini-input-price", type=float, default=DEFAULT_GEMINI_INPUT_PER_MILLION)
    parser.add_argument("--gemini-output-price", type=float, default=DEFAULT_GEMINI_OUTPUT_PER_MILLION)
    parser.add_argument("--validate-only", action="store_true", help="Validate planted set and prompt hash without API calls.")
    args = parser.parse_args()

    _assert_prompt_hash()
    db_path = Path(args.db)
    db_sha_before = _sha256_file(db_path)
    planted = _load_planted(Path(args.planted))
    _validate_planted(planted)
    _validate_planted_sources(db_path, planted["cases"])
    if args.validate_only:
        print(json.dumps({"status": "valid", "cases": len(planted["cases"]), "prompt_sha256": _sha256_text(PJ_PROMPT_TEMPLATE)}, ensure_ascii=False, indent=2))
        return 0

    started = time.perf_counter()
    cache = SqliteCache(Path(args.cache_db))
    report: dict[str, Any] = {
        "metric": "PJ",
        "phase": "planted_probe",
        "status": "running",
        "db": str(db_path),
        "db_sha256_before": db_sha_before,
        "planted_path": str(Path(args.planted)),
        "model": args.model,
        "prompt_version": PROMPT_VERSION_PJ,
        "prompt_sha256": _sha256_text(PJ_PROMPT_TEMPLATE),
        "request_profile": {
            "temperature": 0,
            "thinking_budget": args.thinking_budget,
            "response_mime_type": "application/json",
            "max_output_tokens": args.max_output_tokens,
            "concurrency": args.concurrency,
        },
        "cases": [],
    }
    _write_json(Path(args.out), report)

    _run_probe_calls(
        planted["cases"],
        cache=cache,
        model=args.model,
        timeout_sec=args.timeout_sec,
        max_output_tokens=args.max_output_tokens,
        thinking_budget=args.thinking_budget,
        input_price=args.gemini_input_price,
        output_price=args.gemini_output_price,
        concurrency=args.concurrency,
    )
    report["cases"] = planted["cases"]
    report["status"] = "probe_complete_stop_for_review"
    report["db_sha256_after"] = _sha256_file(db_path)
    report["db_unchanged"] = report["db_sha256_before"] == report["db_sha256_after"]
    report["cache"] = cache.stats()
    report["elapsed_sec"] = time.perf_counter() - started
    report["aggregates"] = _probe_aggregates(planted["cases"])
    report["cost_usd"] = _total_cost(planted["cases"])
    _write_json(Path(args.out), report)
    print(json.dumps(_console_summary(report), ensure_ascii=False, indent=2))
    return 0


def _assert_prompt_hash() -> None:
    actual = _sha256_text(PJ_PROMPT_TEMPLATE.replace("\r\n", "\n"))
    if actual != EXPECTED_PROMPT_SHA256:
        raise RuntimeError(f"pj_judge_v1 hash mismatch: {actual} != {EXPECTED_PROMPT_SHA256}")


def _load_planted(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("cases"), list):
        raise ValueError("Planted file must be an object with a cases list.")
    return payload


def _validate_planted(payload: dict[str, Any]) -> None:
    cases = payload["cases"]
    if len(cases) != 20:
        raise ValueError(f"Expected 20 planted cases, got {len(cases)}.")
    category_counts = Counter(str(case.get("category")) for case in cases)
    expected_categories = {"P-IDENT": 5, "P-GRAM": 5, "P-MEAN": 5, "P-TERM": 5}
    if dict(category_counts) != expected_categories:
        raise ValueError(f"Unexpected planted category counts: {dict(category_counts)}")
    for case in cases:
        for field in ("id", "category", "source_block_id", "source", "candidate_a", "candidate_b", "expected"):
            if field not in case:
                raise ValueError(f"{case.get('id', '<unknown>')} missing {field}")
        expected = case["expected"]
        if expected.get("overall_final") not in {"A", "B", "TIE"}:
            raise ValueError(f"{case['id']} invalid expected overall_final")
        if expected.get("style_final") not in {"A", "B", "TIE"}:
            raise ValueError(f"{case['id']} invalid expected style_final")
        primary_tag = expected.get("primary_tag")
        if primary_tag is not None and primary_tag not in ALLOWED_TAGS:
            raise ValueError(f"{case['id']} invalid expected primary_tag")


def _validate_planted_sources(db_path: Path, cases: list[dict[str, Any]]) -> None:
    block_ids = [str(case["source_block_id"]) for case in cases]
    placeholders = ",".join("?" * len(block_ids))
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as con:
        rows = con.execute(
            f"""
            SELECT block_id
            FROM blocks
            WHERE chapter_id = 'd2l_multilayer_perceptrons'
              AND block_id IN ({placeholders})
            """,
            block_ids,
        ).fetchall()
    found = {str(row[0]) for row in rows}
    missing = sorted(set(block_ids) - found)
    if missing:
        raise ValueError(f"Planted cases reference non-MLP or missing block ids: {missing}")


def _run_probe_calls(
    cases: list[dict[str, Any]],
    *,
    cache: SqliteCache,
    model: str,
    timeout_sec: int,
    max_output_tokens: int,
    thinking_budget: int,
    input_price: float,
    output_price: float,
    concurrency: int,
) -> None:
    tasks: list[tuple[str, str, str, str, str]] = []
    for case in cases:
        case["calls"] = {}
        tasks.append((case["id"], "ab", str(case["candidate_a"]), str(case["candidate_b"]), "A_as_X"))
        tasks.append((case["id"], "ba", str(case["candidate_b"]), str(case["candidate_a"]), "B_as_X"))

    case_by_id = {case["id"]: case for case in cases}
    api_tasks: list[tuple[str, str, str, str, str, str]] = []
    for case_id, direction, candidate_x, candidate_y, order_note in tasks:
        case = case_by_id[case_id]
        cache_key = _pj_cache_key(
            model=model,
            direction=direction,
            source=str(case["source"]),
            candidate_x=candidate_x,
            candidate_y=candidate_y,
            max_output_tokens=max_output_tokens,
            thinking_budget=thinking_budget,
        )
        cached = cache.get(cache_key)
        if cached is not None:
            cached["from_cache"] = True
            case["calls"][direction] = cached
        else:
            api_tasks.append((cache_key, case_id, direction, candidate_x, candidate_y, order_note))

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

        def call_api(task: tuple[str, str, str, str, str, str]) -> tuple[str, str, str, dict[str, Any]]:
            cache_key, case_id, direction, candidate_x, candidate_y, order_note = task
            case = case_by_id[case_id]
            result = _call_pj_with_retry(
                get_client=get_client,
                model=model,
                source=str(case["source"]),
                candidate_x=candidate_x,
                candidate_y=candidate_y,
                direction=direction,
                order_note=order_note,
                timeout_sec=timeout_sec,
                max_output_tokens=max_output_tokens,
                thinking_budget=thinking_budget,
                input_price=input_price,
                output_price=output_price,
            )
            result["cache_key"] = cache_key
            return cache_key, case_id, direction, result

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [pool.submit(call_api, task) for task in api_tasks]
            for index, future in enumerate(as_completed(futures), 1):
                cache_key, case_id, direction, result = future.result()
                case_by_id[case_id]["calls"][direction] = result
                if not result.get("validation_error") and not result.get("transport_error"):
                    cache.put(cache_key, result)
                if index % 10 == 0 or index == len(futures):
                    print(f"[PJ {index}/{len(futures)}] completed", flush=True)

    for case in cases:
        case["final"] = _aggregate_case(case)


def _call_pj_with_retry(
    *,
    get_client: Any,
    model: str,
    source: str,
    candidate_x: str,
    candidate_y: str,
    direction: str,
    order_note: str,
    timeout_sec: int,
    max_output_tokens: int,
    thinking_budget: int,
    input_price: float,
    output_price: float,
) -> dict[str, Any]:
    attempts = []
    result: dict[str, Any] = {}
    prompt = _render_prompt(source=source, candidate_x=candidate_x, candidate_y=candidate_y)
    for attempt in (1, 2):
        started = time.perf_counter()
        try:
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
            parsed, validation_error = _validate_pj_json(text)
            result = {
                "direction": direction,
                "order_note": order_note,
                **parsed,
                "validation_error": validation_error,
                "transport_error": "",
                "raw_content": text,
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


def _render_prompt(*, source: str, candidate_x: str, candidate_y: str) -> str:
    # `.format` is intentionally not used because the prompt contains JSON braces.
    return (
        PJ_PROMPT_TEMPLATE.replace("{source}", source)
        .replace("{candidate_x}", candidate_x)
        .replace("{candidate_y}", candidate_y)
    )


def _pj_cache_key(
    *,
    model: str,
    direction: str,
    source: str,
    candidate_x: str,
    candidate_y: str,
    max_output_tokens: int,
    thinking_budget: int,
) -> str:
    return _cache_key(
        {
            "kind": "pj_judge",
            "provider": "gemini",
            "base_url_policy": "env_or_shopaikey_if_sk",
            "model": model,
            "direction": direction,
            "prompt_version": PROMPT_VERSION_PJ,
            "prompt_sha256": _sha256_text(PJ_PROMPT_TEMPLATE),
            "input_sha256": _sha256_text(
                json.dumps(
                    {"source": source, "candidate_x": candidate_x, "candidate_y": candidate_y},
                    ensure_ascii=False,
                    sort_keys=True,
                )
            ),
            "temperature": 0,
            "max_output_tokens": max_output_tokens,
            "thinking_budget": thinking_budget,
            "response_mime_type": "application/json",
        }
    )


def _validate_pj_json(text: str) -> tuple[dict[str, Any], str]:
    parsed = _parse_json_object(text)
    empty = {"overall_verdict": None, "style_verdict": None, "tags": [], "note": ""}
    if parsed is None:
        return empty, "json_parse_fail"
    if not isinstance(parsed, dict):
        return empty, "json_not_object"
    overall = parsed.get("overall_verdict")
    style = parsed.get("style_verdict")
    if overall not in VERDICTS or style not in VERDICTS:
        return empty, "invalid_verdict"
    raw_tags = parsed.get("tags")
    if not isinstance(raw_tags, list):
        return empty, "tags_not_list"
    tags = [str(tag) for tag in raw_tags]
    if len(tags) > 3:
        return empty, "too_many_tags"
    if any(tag not in ALLOWED_TAGS for tag in tags):
        return empty, "invalid_tag"
    if not tags and not (overall == "TIE" and style == "TIE"):
        return empty, "empty_tags_for_non_tie"
    note = str(parsed.get("note") or "")
    return {"overall_verdict": str(overall), "style_verdict": str(style), "tags": tags, "note": note}, ""


def _aggregate_case(case: dict[str, Any]) -> dict[str, Any]:
    calls = case.get("calls") or {}
    tags_final = _merge_tags(calls)
    overall_final, overall_flags = _aggregate_verdict(calls, verdict_key="overall_verdict")
    style_final, style_flags = _aggregate_verdict(calls, verdict_key="style_verdict")
    flags = [*overall_flags, *style_flags]
    if style_final != "TIE" and not (set(tags_final) & STYLE_TAGS):
        style_final = "TIE"
        flags.append("style_unsupported_by_tags")
    expected = case.get("expected") or {}
    return {
        "overall_final": overall_final,
        "style_final": style_final,
        "tags_final": tags_final,
        "flags": flags,
        "expected_overall_match": overall_final == expected.get("overall_final"),
        "expected_style_match": style_final == expected.get("style_final"),
        "expected_primary_tag_match": expected.get("primary_tag") is None or expected.get("primary_tag") in tags_final,
    }


def _aggregate_verdict(calls: dict[str, Any], *, verdict_key: str) -> tuple[str, list[str]]:
    mapped = []
    flags = []
    for direction in ("ab", "ba"):
        call = calls.get(direction) or {}
        verdict = call.get(verdict_key)
        if call.get("validation_error") or call.get("transport_error") or verdict not in VERDICTS:
            return "TIE", [f"{verdict_key}_missing_or_invalid"]
        mapped.append(_map_call_verdict(str(verdict), direction=direction))
    if mapped[0] == mapped[1]:
        return mapped[0], []
    flags.append("overall_order_inconsistent" if verdict_key == "overall_verdict" else "style_order_inconsistent")
    return "TIE", flags


def _map_call_verdict(verdict: str, *, direction: str) -> str:
    if verdict == "TIE":
        return "TIE"
    if direction == "ab":
        return "A" if verdict == "X" else "B"
    if direction == "ba":
        return "B" if verdict == "X" else "A"
    raise ValueError(f"unknown direction {direction}")


def _merge_tags(calls: dict[str, Any]) -> list[str]:
    tags = {tag for call in calls.values() for tag in (call.get("tags") or []) if tag in ALLOWED_TAGS}
    return [tag for tag in TAG_ORDER if tag in tags]


def _probe_aggregates(cases: list[dict[str, Any]]) -> dict[str, Any]:
    by_category: dict[str, list[dict[str, Any]]] = {}
    for case in cases:
        by_category.setdefault(str(case["category"]), []).append(case)
    parse_fail_count = sum(
        1
        for case in cases
        for call in (case.get("calls") or {}).values()
        if call.get("validation_error") or call.get("transport_error")
    )
    p_ident = by_category.get("P-IDENT", [])
    ident_call_ties = sum(
        1
        for case in p_ident
        for call in (case.get("calls") or {}).values()
        if call.get("overall_verdict") == "TIE"
    )
    p_gram = by_category.get("P-GRAM", [])
    p_mean = by_category.get("P-MEAN", [])
    p_term = by_category.get("P-TERM", [])
    return {
        "counts_by_category": {category: len(rows) for category, rows in sorted(by_category.items())},
        "parse_or_transport_fail_calls": parse_fail_count,
        "P-IDENT": {"overall_tie_calls": ident_call_ties, "pass": ident_call_ties == 10},
        "P-GRAM": {
            "overall_correct_pairs": _expected_overall_count(p_gram),
            "grammar_tag_pairs": _tag_count(p_gram, "grammar"),
            "pass": _expected_overall_count(p_gram) >= 4 and _tag_count(p_gram, "grammar") >= 4,
        },
        "P-MEAN": {
            "overall_correct_pairs": _expected_overall_count(p_mean),
            "meaning_tag_pairs": _tag_count(p_mean, "meaning"),
            "pass": _expected_overall_count(p_mean) >= 4 and _tag_count(p_mean, "meaning") >= 4,
        },
        "P-TERM": {
            "style_tie_pairs": sum(1 for case in p_term if (case.get("final") or {}).get("style_final") == "TIE"),
            "terminology_tag_pairs": _tag_count(p_term, "terminology"),
            "pass": sum(1 for case in p_term if (case.get("final") or {}).get("style_final") == "TIE") >= 4
            and _tag_count(p_term, "terminology") >= 4,
        },
        "order_inconsistent_P_GRAM_P_MEAN": sum(
            1
            for case in [*p_gram, *p_mean]
            if "overall_order_inconsistent" in ((case.get("final") or {}).get("flags") or [])
        ),
        "thresholds": {
            "P_IDENT_overall_TIE_call_level": "10/10",
            "P_GRAM_overall_correct_pairs": ">=4/5",
            "P_GRAM_grammar_tag_pairs": ">=4/5",
            "P_MEAN_overall_correct_pairs": ">=4/5",
            "P_MEAN_meaning_tag_pairs": ">=4/5",
            "P_TERM_style_TIE_pairs": ">=4/5",
            "P_TERM_terminology_tag_pairs": ">=4/5",
            "P_GRAM_P_MEAN_overall_order_inconsistent": "<=1/10",
            "parse_fail_after_retry": "<=1/40 calls",
        },
    }


def _expected_overall_count(cases: list[dict[str, Any]]) -> int:
    return sum(1 for case in cases if (case.get("final") or {}).get("overall_final") == (case.get("expected") or {}).get("overall_final"))


def _tag_count(cases: list[dict[str, Any]], tag: str) -> int:
    return sum(1 for case in cases if tag in ((case.get("final") or {}).get("tags_final") or []))


def _total_cost(cases: list[dict[str, Any]]) -> float:
    return sum(float(call.get("cost_usd") or 0.0) for case in cases for call in (case.get("calls") or {}).values())


def _console_summary(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": report["status"],
        "cases": len(report["cases"]),
        "db_unchanged": report.get("db_unchanged"),
        "cost_usd": report.get("cost_usd"),
        "cache": report.get("cache"),
        "aggregates": report.get("aggregates"),
    }


def _normalize_auto_tie_text(text: str) -> str:
    normalized = unicodedata.normalize("NFC", str(text)).replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in normalized.split("\n"))


if __name__ == "__main__":
    raise SystemExit(main())
