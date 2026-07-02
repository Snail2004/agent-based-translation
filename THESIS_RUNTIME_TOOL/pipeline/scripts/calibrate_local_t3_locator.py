from __future__ import annotations

import argparse
import json
import statistics
import time
from collections import Counter
from pathlib import Path
from typing import Any

import requests

from pipeline.eval.cascade_localize import (
    _score_locate_only_against_reused_gold,
    _target_region_text,
)
from pipeline.eval.llm_adjudicator import (
    RESULT_SCHEMA,
    AdjudicationInput,
    build_messages,
    validate_payload,
)


DEFAULT_ENDPOINT = "http://127.0.0.1:1234"
DEFAULT_MEASURE = Path("data/reports/cascade_gold_locate_measure.json")
DEFAULT_S0 = Path("data/reports/cascade_localize_S0.json")
DEFAULT_S1 = Path("data/reports/cascade_localize_S1.json")
DEFAULT_OUT = Path("data/reports/local_t3_locator_calibration.json")

MODEL_PROFILES = {
    "openai/gpt-oss-20b": {
        "reasoning_effort": "low",
        "context_length": 4096,
        "project_calls": 1724,
    },
    "qwen/qwen3.5-9b": {
        "reasoning_effort": "none",
        "context_length": 4096,
        "project_calls": 1724,
    },
    "google/gemma-4-12b": {
        "reasoning_effort": "none",
        "context_length": 4096,
        "project_calls": 1724,
    },
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Calibrate LM Studio local models on the EV-D2L T3 locate-only gold set."
    )
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--measure", default=str(DEFAULT_MEASURE))
    parser.add_argument("--cascade-s0", default=str(DEFAULT_S0))
    parser.add_argument("--cascade-s1", default=str(DEFAULT_S1))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument(
        "--models",
        default="openai/gpt-oss-20b,qwen/qwen3.5-9b,google/gemma-4-12b",
    )
    parser.add_argument("--limit", type=int, default=0, help="Debug cap; 0 means all 103 items.")
    parser.add_argument("--no-load", action="store_true", help="Do not call LM Studio load/unload endpoints.")
    parser.add_argument("--keep-loaded", action="store_true", help="Leave each model loaded after its run.")
    parser.add_argument("--timeout-sec", type=int, default=180)
    args = parser.parse_args()

    models = [item.strip() for item in args.models.split(",") if item.strip()]
    items, baseline = load_calibration_items(
        measure_path=Path(args.measure),
        cascade_paths=[Path(args.cascade_s0), Path(args.cascade_s1)],
    )
    if args.limit > 0:
        items = items[: args.limit]
    report = {
        "task": "BUILDER-V2 §35.11",
        "status": "DEV calibration only; not a thesis-final metric",
        "endpoint": args.endpoint,
        "out": str(Path(args.out)),
        "items": len(items),
        "baseline_gpt": baseline,
        "request_profile_common": common_request_profile(),
        "models": {},
    }
    for model in models:
        profile = MODEL_PROFILES.get(model, {"reasoning_effort": "none", "context_length": 4096, "project_calls": 1724})
        try:
            if not args.no_load:
                _load_model(args.endpoint, model, int(profile["context_length"]), timeout=args.timeout_sec)
            loaded_info = _model_info(args.endpoint, model)
            model_report = run_model(
                endpoint=args.endpoint,
                model=model,
                reasoning_effort=str(profile["reasoning_effort"]),
                items=items,
                timeout_sec=args.timeout_sec,
                projected_calls=int(profile.get("project_calls") or 1724),
            )
            model_report["loaded_model_info"] = loaded_info
            report["models"][model] = model_report
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        finally:
            if not args.no_load and not args.keep_loaded:
                _unload_loaded_instance_if_present(args.endpoint, model, timeout=args.timeout_sec)
    report["decision"] = _decision_summary(report["models"])
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(_console_summary(report), ensure_ascii=False, indent=2))
    return 0


def load_calibration_items(
    *,
    measure_path: Path,
    cascade_paths: list[Path],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    measure = json.loads(measure_path.read_text(encoding="utf-8"))
    decision_by_occ: dict[str, dict[str, Any]] = {}
    for path in cascade_paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        for decision in payload.get("decisions") or []:
            decision_by_occ[str(decision.get("occ_id") or "")] = decision
    items: list[dict[str, Any]] = []
    missing: list[str] = []
    for row in measure.get("t3_records") or []:
        occ_id = str(row.get("occ_id") or "")
        decision = decision_by_occ.get(occ_id)
        if not decision:
            missing.append(occ_id)
            continue
        items.append({
            "occ_id": occ_id,
            "config": row.get("config"),
            "source_term": row.get("source_term"),
            "gold_label": row.get("gold_label"),
            "gold_span": row.get("gold_span"),
            "baseline": row,
            "decision": decision,
        })
    if missing:
        raise ValueError(f"Missing cascade decisions for {len(missing)} t3_records: {missing[:5]}")
    baseline = dict(measure.get("t3_locate_scoring") or {})
    baseline["source"] = str(measure_path)
    return items, baseline


def run_model(
    *,
    endpoint: str,
    model: str,
    reasoning_effort: str,
    items: list[dict[str, Any]],
    timeout_sec: int,
    projected_calls: int,
) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    latencies: list[float] = []
    tokens_per_second: list[float] = []
    started = time.perf_counter()
    request_params = request_params_for_model(model=model, reasoning_effort=reasoning_effort)
    for index, item in enumerate(items, 1):
        decision = item["decision"]
        target_region = _target_region_text(decision)
        messages = build_messages(
            AdjudicationInput(
                occurrence_id=str(item["occ_id"]),
                source_term=str(item["source_term"]),
                occurrence_index=_occurrence_index_in_source_sentence(decision),
                source_sentence=str(decision.get("source_sentence") or ""),
                target_region=target_region,
            )
        )
        call_started = time.perf_counter()
        raw = _call_lmstudio(
            endpoint=endpoint,
            model=model,
            messages=messages,
            request_params=request_params,
            timeout_sec=timeout_sec,
        )
        latency = time.perf_counter() - call_started
        latencies.append(latency)
        content = raw["content"]
        parsed, parse_error = _parse_json_content(content)
        validation_error_type = ""
        if parse_error:
            validated = {
                "found": False,
                "target_quote": "",
                "left_context": "",
                "confidence": "low",
                "validation_error": "json_parse_fail",
            }
        else:
            validated = validate_payload(parsed, str(item["occ_id"]), target_region)
            if validated.get("validation_error"):
                validation_error_type = str(validated["validation_error"])
        scored = _score_locate_only_against_reused_gold(
            validated,
            {
                "gold_label": item.get("gold_label") or "",
                "gold_target_span": item.get("gold_span") or "",
            },
        )
        outcome = classify_outcome(validated, scored, parse_error)
        counts[outcome] += 1
        if validated.get("confidence") == "low":
            counts["confidence_low"] += 1
        usage = raw.get("usage") or {}
        total_tokens = int(usage.get("total_tokens") or 0)
        if latency > 0 and total_tokens:
            tokens_per_second.append(total_tokens / latency)
        records.append({
            "index": index,
            "occ_id": item["occ_id"],
            "config": item["config"],
            "source_term": item["source_term"],
            "gold_label": item["gold_label"],
            "gold_span": item["gold_span"],
            "target_quote": validated.get("target_quote", ""),
            "found": bool(validated.get("found")),
            "confidence": validated.get("confidence", "low"),
            "correct": bool(scored["correct"]),
            "outcome": outcome,
            "correct_reason": scored["reason"],
            "parse_error": parse_error,
            "validation_error": validation_error_type,
            "raw_content_prefix": content[:300],
            "raw_reasoning_prefix": str(raw.get("reasoning_content") or "")[:300],
            "finish_reason": raw.get("finish_reason", ""),
            "model_echo": raw.get("model_echo", ""),
            "usage": usage,
            "latency_sec": round(latency, 4),
        })
    elapsed = time.perf_counter() - started
    correct = counts["correct"]
    attempted = len(items)
    failures = [item for item in records if not item["correct"]]
    reject_or_parse = counts["quote_validation_fail"] + counts["json_parse_fail"]
    return {
        "model": model,
        "reasoning_effort": reasoning_effort,
        "request_params": request_params,
        "attempted": attempted,
        "correct": correct,
        "accuracy": round(correct / attempted, 6) if attempted else 0.0,
        "correct_count": counts["correct"],
        "valid_but_wrong": counts["valid_but_wrong"],
        "not_found_wrong": counts["not_found_wrong"],
        "quote_validation_fail": counts["quote_validation_fail"],
        "json_parse_fail": counts["json_parse_fail"],
        "confidence_low": counts["confidence_low"],
        "reject_or_parse_rate": round(reject_or_parse / attempted, 6) if attempted else 0.0,
        "median_latency_sec": round(statistics.median(latencies), 4) if latencies else 0.0,
        "median_tokens_per_sec": round(statistics.median(tokens_per_second), 4) if tokens_per_second else 0.0,
        "elapsed_sec": round(elapsed, 4),
        "projected_wall_time_for_1724_calls_sec": round((statistics.median(latencies) if latencies else 0.0) * projected_calls, 2),
        "failure_samples": [
            {
                "occ_id": item["occ_id"],
                "source_term": item["source_term"],
                "gold_span": item["gold_span"],
                "target_quote": item["target_quote"],
                "outcome": item["outcome"],
                "reason": item["correct_reason"],
                "raw_content_prefix": item["raw_content_prefix"],
            }
            for item in failures[:3]
        ],
        "records": records,
    }


def request_params_for_model(*, model: str, reasoning_effort: str) -> dict[str, Any]:
    return {
        "temperature": 0,
        "top_p": 1.0,
        "top_k": 0,
        "min_p": 0.0,
        "seed": 20260612,
        "repeat_penalty": 1.0,
        "max_tokens": 512,
        "stream": False,
        "response_format": RESULT_SCHEMA,
        "reasoning_effort": reasoning_effort,
    }


def common_request_profile() -> dict[str, Any]:
    return {
        "temperature": 0,
        "top_p": 1.0,
        "top_k": 0,
        "min_p": 0.0,
        "seed": 20260612,
        "repeat_penalty": 1.0,
        "max_tokens": 512,
        "response_format": "json_schema",
        "context_length_requested": 4096,
    }


def classify_outcome(
    validated: dict[str, Any],
    scored: dict[str, Any],
    parse_error: str,
) -> str:
    if parse_error:
        return "json_parse_fail"
    if str(validated.get("validation_error") or ""):
        return "quote_validation_fail"
    if bool(scored["correct"]):
        return "correct"
    if not bool(validated.get("found")):
        return "not_found_wrong"
    return "valid_but_wrong"


def _call_lmstudio(
    *,
    endpoint: str,
    model: str,
    messages: list[dict[str, str]],
    request_params: dict[str, Any],
    timeout_sec: int,
) -> dict[str, Any]:
    body = {
        "model": model,
        "messages": messages,
        **request_params,
    }
    response = requests.post(
        f"{endpoint.rstrip('/')}/v1/chat/completions",
        json=body,
        timeout=timeout_sec,
    )
    response.raise_for_status()
    payload = response.json()
    choice = (payload.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    return {
        "content": str(message.get("content") or ""),
        "reasoning_content": message.get("reasoning_content") or "",
        "finish_reason": choice.get("finish_reason") or "",
        "model_echo": payload.get("model") or "",
        "usage": payload.get("usage") or {},
    }


def _parse_json_content(content: str) -> tuple[Any | None, str]:
    if not str(content or "").strip():
        return None, "empty_content"
    try:
        return json.loads(content), ""
    except json.JSONDecodeError as exc:
        return None, f"json_decode_error:{exc.msg}"


def _load_model(endpoint: str, model: str, context_length: int, *, timeout: int) -> None:
    _unload_other_loaded_llms(endpoint, keep_model=model, timeout=timeout)
    existing = _loaded_instance(endpoint, model)
    if existing:
        actual_context = int((existing.get("config") or {}).get("context_length") or 0)
        if actual_context == context_length:
            return
        _unload_model(endpoint, str(existing["id"]), timeout=timeout)
    response = requests.post(
        f"{endpoint.rstrip('/')}/api/v1/models/load",
        json={"model": model, "context_length": context_length},
        timeout=timeout,
    )
    response.raise_for_status()


def _unload_other_loaded_llms(endpoint: str, *, keep_model: str, timeout: int) -> None:
    response = requests.get(f"{endpoint.rstrip('/')}/api/v1/models", timeout=30)
    response.raise_for_status()
    for item in response.json().get("models") or []:
        if item.get("type") != "llm":
            continue
        if item.get("key") == keep_model:
            continue
        for instance in item.get("loaded_instances") or []:
            instance_id = str(instance.get("id") or "")
            if not instance_id or instance_id == keep_model:
                continue
            _unload_model(endpoint, instance_id, timeout=timeout)


def _unload_loaded_instance_if_present(endpoint: str, model: str, *, timeout: int) -> None:
    existing = _loaded_instance(endpoint, model)
    if existing and existing.get("id"):
        _unload_model(endpoint, str(existing["id"]), timeout=timeout)


def _unload_model(endpoint: str, instance_id: str, *, timeout: int) -> None:
    response = requests.post(
        f"{endpoint.rstrip('/')}/api/v1/models/unload",
        json={"instance_id": instance_id},
        timeout=timeout,
    )
    response.raise_for_status()


def _model_info(endpoint: str, model: str) -> dict[str, Any]:
    response = requests.get(f"{endpoint.rstrip('/')}/api/v1/models", timeout=30)
    response.raise_for_status()
    for item in response.json().get("models") or []:
        if item.get("key") == model:
            return {
                "key": item.get("key"),
                "display_name": item.get("display_name"),
                "selected_variant": item.get("selected_variant"),
                "quantization": item.get("quantization"),
                "params_string": item.get("params_string"),
                "loaded_instances": item.get("loaded_instances") or [],
                "max_context_length": item.get("max_context_length"),
                "capabilities": item.get("capabilities"),
            }
    return {"key": model, "missing": True}


def _loaded_instance(endpoint: str, model: str) -> dict[str, Any] | None:
    info = _model_info(endpoint, model)
    instances = info.get("loaded_instances") or []
    for instance in instances:
        if instance.get("id") == model:
            return instance
    return instances[0] if instances else None


def _occurrence_index_in_source_sentence(decision: dict[str, Any]) -> int:
    source_sentence = str(decision.get("source_sentence") or "")
    source_term = str(decision.get("source_term") or "")
    source_start = int(decision.get("source_start") or 0)
    sentence_start = str(decision.get("source_text") or "").find(source_sentence)
    relative = source_start - sentence_start if sentence_start >= 0 else 0
    cursor = 0
    count = 0
    term_cf = source_term.casefold()
    sentence_cf = source_sentence.casefold()
    while True:
        index = sentence_cf.find(term_cf, cursor)
        if index < 0:
            break
        count += 1
        if index <= relative:
            cursor = index + max(1, len(term_cf))
            continue
        break
    return max(1, count)


def _decision_summary(models: dict[str, Any]) -> dict[str, Any]:
    passing = []
    for model, report in models.items():
        if report["attempted"] != 103:
            passing.append({
                "model": model,
                "status": "debug_limit_only",
                "correct": report["correct"],
                "accuracy": report["accuracy"],
                "median_tokens_per_sec": report["median_tokens_per_sec"],
                "projected_wall_time_for_1724_calls_sec": report["projected_wall_time_for_1724_calls_sec"],
            })
            continue
        reject_or_parse = report["quote_validation_fail"] + report["json_parse_fail"]
        attempted = report["attempted"]
        if report["correct"] >= 101 and (reject_or_parse / attempted if attempted else 1.0) < 0.05:
            status = "pass_primary"
        elif 98 <= report["correct"] <= 100 and (reject_or_parse / attempted if attempted else 1.0) < 0.05:
            status = "pass_with_gpt_fallback_and_audit"
        else:
            status = "fail_use_gpt"
        passing.append({
            "model": model,
            "status": status,
            "correct": report["correct"],
            "accuracy": report["accuracy"],
            "median_tokens_per_sec": report["median_tokens_per_sec"],
            "projected_wall_time_for_1724_calls_sec": report["projected_wall_time_for_1724_calls_sec"],
        })
    candidates = [item for item in passing if item["status"].startswith("pass")]
    winner = None
    if candidates:
        winner = sorted(candidates, key=lambda item: (-item["median_tokens_per_sec"], -item["correct"], item["model"]))[0]
    return {
        "model_statuses": passing,
        "recommended_primary": winner,
    }


def _console_summary(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "out": report.get("out"),
        "items": report["items"],
        "baseline_gpt_accuracy": report["baseline_gpt"].get("accuracy"),
        "models": {
            model: {
                "correct": item.get("correct"),
                "accuracy": item.get("accuracy"),
                "valid_but_wrong": item.get("valid_but_wrong"),
                "not_found_wrong": item.get("not_found_wrong"),
                "quote_validation_fail": item.get("quote_validation_fail"),
                "json_parse_fail": item.get("json_parse_fail"),
                "median_tokens_per_sec": item.get("median_tokens_per_sec"),
            }
            for model, item in report["models"].items()
        },
        "decision": report.get("decision"),
    }


if __name__ == "__main__":
    raise SystemExit(main())
