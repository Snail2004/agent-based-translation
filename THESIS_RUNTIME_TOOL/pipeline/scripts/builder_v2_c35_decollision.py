#!/usr/bin/env python3
"""Builder v2 Stage C3.5 de-collision driver."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.agents.llm_client import LLMClient, LLMResult
from pipeline.agents.llm_config import LLMConfig, load_llm_config
from pipeline.prepass.builder_v2_decollision import (
    PROMPT_VERSION,
    apply_decollision_to_notebook,
    build_collision_groups,
    chunk_groups,
    gate_decollision_rows,
    load_notebook,
    prompt_text,
    promote_ledger_canonical_candidates,
    validate_decollision_results,
)
from pipeline.scripts.builder_v2_c3_audit import _build_auditor_recall_metrics


DEFAULT_DB = Path("data/jobs/d2l_p1/memory.sqlite3")
DEFAULT_NOTEBOOK = Path("data/reports/builder_v2_c3_audit_real/notebook_audited.json")
DEFAULT_CONFIG = Path("pipeline/configs/llm_prepass.yaml")
DEFAULT_OUT = Path("data/reports/builder_v2_c35_decollision")


def main() -> int:
    parser = argparse.ArgumentParser(description="Builder v2 C3.5 de-collision pass.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--notebook", default=str(DEFAULT_NOTEBOOK))
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--max-groups-per-call", type=int, default=8)
    parser.add_argument("--prompt-token-budget", type=int)
    parser.add_argument("--estimate-only", action="store_true")
    parser.add_argument("--decollision-json", help="Existing JSON array of model decisions; 0 API.")
    parser.add_argument("--confirm-usd", type=float, help="Allow real API calls only if cap estimate <= amount.")
    parser.add_argument("--cache-db", help="Separate C3.5 LLM cache path. Defaults to <out>/llm_cache.sqlite3.")
    parser.add_argument("--api-key-file", help="Optional API key file. Env OPENAI_API_KEY still takes precedence.")
    parser.add_argument("--doc-id", default="d2l")
    parser.add_argument("--chapter", default="preliminaries")
    parser.add_argument("--profile", default="technical_d2l_v1")
    args = parser.parse_args()

    if not args.estimate_only and not args.decollision_json and args.confirm_usd is None:
        raise SystemExit("Use --estimate-only, --decollision-json, or --confirm-usd.")

    report = run_c35(
        db_path=Path(args.db),
        notebook_path=Path(args.notebook),
        config_path=Path(args.config),
        out_dir=Path(args.out),
        max_groups_per_call=args.max_groups_per_call,
        prompt_token_budget=args.prompt_token_budget,
        estimate_only=args.estimate_only,
        decollision_json=Path(args.decollision_json) if args.decollision_json else None,
        confirm_usd=args.confirm_usd,
        cache_db=Path(args.cache_db) if args.cache_db else None,
        api_key_file=Path(args.api_key_file) if args.api_key_file else None,
        doc_id=args.doc_id,
        chapter_id=args.chapter,
        profile_name=args.profile,
    )
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def run_c35(
    *,
    db_path: Path,
    notebook_path: Path,
    config_path: Path,
    out_dir: Path,
    max_groups_per_call: int = 8,
    prompt_token_budget: int | None = None,
    estimate_only: bool = True,
    decollision_json: Path | None = None,
    confirm_usd: float | None = None,
    cache_db: Path | None = None,
    api_key_file: Path | None = None,
    doc_id: str = "d2l",
    chapter_id: str = "preliminaries",
    profile_name: str = "technical_d2l_v1",
    transport: Any | None = None,
) -> dict[str, Any]:
    config = load_llm_config(config_path)
    effective_prompt_budget = prompt_token_budget or int(int(config.prompt_token_cap) * 0.9)
    db_hash_before = _sha256(db_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_notebook = load_notebook(notebook_path)
    pre_promotion_metrics = _safe_auditor_recall_metrics(
        db_path=db_path,
        doc_id=doc_id,
        chapter_id=chapter_id,
        notebook=raw_notebook,
        profile_name=profile_name,
    )
    notebook, promotion_trail = promote_ledger_canonical_candidates(raw_notebook)
    (out_dir / "ledger_promotion_trail.json").write_text(
        json.dumps(promotion_trail, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (out_dir / "notebook_promoted.json").write_text(
        json.dumps(notebook, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    baseline_metrics = _safe_auditor_recall_metrics(
        db_path=db_path,
        doc_id=doc_id,
        chapter_id=chapter_id,
        notebook=notebook,
        profile_name=profile_name,
    )
    groups = build_collision_groups(notebook, db_path)
    chunks = chunk_groups(
        groups,
        max_groups=max_groups_per_call,
        prompt_token_cap=effective_prompt_budget,
    )

    prompts_dir = out_dir / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    for chunk in chunks:
        (prompts_dir / f"{chunk.chunk_id}.txt").write_text(
            prompt_text(chunk.messages) + "\n",
            encoding="utf-8",
        )
    (out_dir / "collision_groups.json").write_text(
        json.dumps(groups, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (out_dir / "chunks.json").write_text(
        json.dumps(
            [
                {
                    "chunk_id": chunk.chunk_id,
                    "groups": [group["group_id"] for group in chunk.groups],
                    "entry_ids": [member["entry_id"] for member in chunk.members],
                    "prompt_file": f"prompts/{chunk.chunk_id}.txt",
                    "prompt_tokens_est": chunk.prompt_tokens_est,
                }
                for chunk in chunks
            ],
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    decollision_rows: list[dict[str, Any]] | None = None
    llm_run: dict[str, Any] | None = None
    if decollision_json is not None:
        raw = json.loads(decollision_json.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError("--decollision-json must contain a JSON array")
        decollision_rows = validate_decollision_results(raw, groups)
    elif not estimate_only:
        cap_cost = _cost(config, sum(chunk.prompt_tokens_est for chunk in chunks), len(chunks) * config.max_output_tokens)
        if confirm_usd is None:
            raise ValueError("confirm_usd is required for real C3.5 API calls")
        if cap_cost > float(confirm_usd):
            raise RuntimeError(
                f"C3.5 estimate cap ${cap_cost:.6f} exceeds --confirm-usd ${float(confirm_usd):.6f}"
            )
        key_source = _ensure_openai_key(api_key_file=api_key_file, allow_skip=transport is not None)
        llm_run = _run_real_decollision(
            chunks=chunks,
            groups=groups,
            config=config,
            cache_path=cache_db or out_dir / "llm_cache.sqlite3",
            transport=transport,
        )
        llm_run["api_key_source"] = key_source
        if llm_run.get("status") != "degraded":
            decollision_rows = llm_run["decollision_rows"]

    applied_notebook: dict[str, Any] | None = None
    post_metrics: dict[str, Any] | None = None
    if decollision_rows is not None:
        decollision_rows = gate_decollision_rows(decollision_rows, gated=True, notebook=notebook)
        applied_notebook = apply_decollision_to_notebook(notebook, decollision_rows)
        (out_dir / "decollision_trail.json").write_text(
            json.dumps(decollision_rows, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (out_dir / "notebook_decollided.json").write_text(
            json.dumps(applied_notebook, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        post_metrics = _safe_auditor_recall_metrics(
            db_path=db_path,
            doc_id=doc_id,
            chapter_id=chapter_id,
            notebook=applied_notebook,
            profile_name=profile_name,
        )
        if post_metrics is not None:
            (out_dir / "builder_v2_c35_metrics.json").write_text(
                json.dumps(
                    {
                        "before": baseline_metrics,
                        "after": post_metrics,
                        "invariants": _metric_invariants(baseline_metrics, post_metrics),
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

    prompt_total = sum(chunk.prompt_tokens_est for chunk in chunks)
    output_cap = len(chunks) * config.max_output_tokens
    db_hash_after = _sha256(db_path)
    if db_hash_before != db_hash_after:
        raise RuntimeError("Frozen DB hash changed during C3.5.")
    status = "estimate_only"
    if decollision_json is not None:
        status = "applied_existing_decollision_json"
    elif llm_run is not None:
        status = str(llm_run.get("status") or "completed")

    label = _decision_counts(decollision_rows or [])
    report = {
        "phase": "BUILDER-V2-C3.5-DECOLLISION",
        "status": status,
        "prompt_version": PROMPT_VERSION,
        "ledger_promotion_version": notebook.get("ledger_promotion_version"),
        "ledger_promotion_summary": notebook.get("ledger_promotion_summary"),
        "zero_api": llm_run is None,
        "zero_db_write": True,
        "blind_to_gold": True,
        "db_path": str(db_path),
        "db_sha256": db_hash_before,
        "db_hash_unchanged": True,
        "notebook_path": str(notebook_path),
        "model_config": _config_to_dict(config),
        "groups": len(groups),
        "members": sum(len(group["members"]) for group in groups),
        "calls": len(chunks),
        "prompt_tokens": {
            "total": prompt_total,
            "max": max([chunk.prompt_tokens_est for chunk in chunks], default=0),
        },
        "estimated_output_tokens_cap": output_cap,
        "estimated_cost_usd_cap": _cost(config, prompt_total, output_cap),
        "actual_cost_usd": (llm_run or {}).get("actual_cost_usd"),
        "parse_failure_count": (llm_run or {}).get("parse_failure_count"),
        "decision_counts": label,
        "promotion_metric_invariants": _metric_invariants(pre_promotion_metrics, baseline_metrics) if baseline_metrics else None,
        "metric_invariants": _metric_invariants(baseline_metrics, post_metrics) if post_metrics else None,
        "metrics_available": baseline_metrics is not None,
        "llm_run": llm_run,
        "artifacts": {
            "collision_groups": "collision_groups.json",
            "chunks": "chunks.json",
            "ledger_promotion_trail": "ledger_promotion_trail.json",
            "notebook_promoted": "notebook_promoted.json",
            "prompts_dir": "prompts",
            "report": "builder_v2_c35_decollision_report.json",
            "decollision_trail": "decollision_trail.json" if decollision_rows is not None else None,
            "notebook_decollided": "notebook_decollided.json" if applied_notebook is not None else None,
            "metrics": "builder_v2_c35_metrics.json" if post_metrics is not None else None,
            "cost_log": "cost_log.json" if llm_run is not None else None,
            "raw_outputs": "raw_outputs.json" if llm_run is not None else None,
        },
        "summary": {
            "status": status,
            "groups": len(groups),
            "members": sum(len(group["members"]) for group in groups),
            "calls": len(chunks),
            "prompt_tokens_total": prompt_total,
            "estimated_cost_usd_cap": _cost(config, prompt_total, output_cap),
            "actual_cost_usd": (llm_run or {}).get("actual_cost_usd"),
            "decision_counts": label,
            "ledger_promotion_summary": notebook.get("ledger_promotion_summary"),
            "promotion_metric_invariants": _metric_invariants(pre_promotion_metrics, baseline_metrics) if baseline_metrics else None,
            "metric_invariants": _metric_invariants(baseline_metrics, post_metrics) if post_metrics else None,
            "metrics_available": baseline_metrics is not None,
            "zero_api": llm_run is None,
            "db_hash_unchanged": True,
        },
    }
    (out_dir / "builder_v2_c35_decollision_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return report


def _run_real_decollision(
    *,
    chunks: list[Any],
    groups: list[dict[str, Any]],
    config: LLMConfig,
    cache_path: Path,
    transport: Any | None = None,
) -> dict[str, Any]:
    client = LLMClient(config=config, cache_path=cache_path, transport=transport)
    rows: list[dict[str, Any]] = []
    cost_log: list[dict[str, Any]] = []
    raw_outputs: list[dict[str, Any]] = []
    parse_failures = 0
    degraded_chunks: list[dict[str, Any]] = []
    groups_by_id = {group["group_id"]: group for group in groups}

    for chunk in chunks:
        parsed_rows: list[dict[str, Any]] | None = None
        errors: list[str] = []
        attempts: list[dict[str, Any]] = []
        chunk_groups = [groups_by_id[group["group_id"]] for group in chunk.groups]
        for attempt in (1, 2):
            result = client.call(
                chunk.messages,
                response_format=None,
                tag=f"builder_v2_c35:{chunk.chunk_id}:attempt{attempt}",
                bypass_cache=(attempt == 2),
            )
            attempts.append(_result_record(result, attempt))
            try:
                parsed = json.loads(result.text)
                if not isinstance(parsed, list):
                    raise ValueError("Decollision response is not a JSON array")
                parsed_rows = validate_decollision_results(parsed, chunk_groups)
                break
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))
                if attempt == 1:
                    parse_failures += 1
                    continue
                degraded_chunks.append(
                    {
                        "chunk_id": chunk.chunk_id,
                        "entry_ids": [member["entry_id"] for member in chunk.members],
                        "errors": errors,
                    }
                )
        cost_log.extend(attempts)
        raw_outputs.append(
            {
                "chunk_id": chunk.chunk_id,
                "entry_ids": [member["entry_id"] for member in chunk.members],
                "attempts": attempts,
                "errors": errors,
                "parsed_rows": parsed_rows,
            }
        )
        if parsed_rows is not None:
            rows.extend(parsed_rows)

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    (cache_path.parent / "cost_log.json").write_text(
        json.dumps(cost_log, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (cache_path.parent / "raw_outputs.json").write_text(
        json.dumps(raw_outputs, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "status": "degraded" if degraded_chunks else "completed",
        "cache_db": str(cache_path),
        "actual_cost_usd": round(sum(float(row.get("cost_usd") or 0.0) for row in cost_log), 12),
        "calls_logged": len(cost_log),
        "cache_hits": sum(1 for row in cost_log if bool(row.get("from_cache"))),
        "cache_misses": sum(1 for row in cost_log if not bool(row.get("from_cache"))),
        "prompt_tokens": sum(int(row.get("prompt_tokens") or 0) for row in cost_log),
        "completion_tokens": sum(int(row.get("completion_tokens") or 0) for row in cost_log),
        "reasoning_tokens": sum(int(row.get("reasoning_tokens") or 0) for row in cost_log),
        "parse_failure_count": parse_failures,
        "degraded_chunks": degraded_chunks,
        "decollision_rows": rows,
    }


def _safe_auditor_recall_metrics(
    *,
    db_path: Path,
    doc_id: str,
    chapter_id: str,
    notebook: dict[str, Any],
    profile_name: str,
) -> dict[str, Any] | None:
    try:
        return _build_auditor_recall_metrics(
            db_path=db_path,
            doc_id=doc_id,
            chapter_id=chapter_id,
            notebook=notebook,
            profile_name=profile_name,
        )
    except Exception:
        return None


def _result_record(result: LLMResult, attempt: int) -> dict[str, Any]:
    return {
        "attempt": attempt,
        "model": result.model,
        "prompt_tokens": int(result.usage.prompt_tokens),
        "cached_tokens": int(result.usage.cached_tokens),
        "completion_tokens": int(result.usage.completion_tokens),
        "reasoning_tokens": int(result.usage.reasoning_tokens),
        "cost_usd": float(result.cost_usd),
        "latency_ms": int(result.latency_ms),
        "from_cache": bool(result.from_cache),
        "cache_key": result.cache_key,
        "system_fingerprint": result.system_fingerprint,
        "text": result.text,
    }


def _metric_invariants(before: dict[str, Any], after: dict[str, Any] | None) -> dict[str, Any] | None:
    if after is None:
        return None
    before_recall = before["recall_vs_gold_dev"]
    after_recall = after["recall_vs_gold_dev"]
    checks = {
        "gold_terms_present_same": before_recall["gold_terms_present"] == after_recall["gold_terms_present"],
        "metric_a_matched_terms_same": (
            before_recall["metric_a_registry"]["matched_terms"]
            == after_recall["metric_a_registry"]["matched_terms"]
        ),
        "metric_a_recall_same": (
            before_recall["metric_a_registry"]["recall"]
            == after_recall["metric_a_registry"]["recall"]
        ),
        "metric_b_matched_terms_same": (
            before_recall["metric_b_post_auditor"]["matched_terms"]
            == after_recall["metric_b_post_auditor"]["matched_terms"]
        ),
        "metric_b_recall_same": (
            before_recall["metric_b_post_auditor"]["recall"]
            == after_recall["metric_b_post_auditor"]["recall"]
        ),
        "entry_counts_same": before["entry_counts"] == after["entry_counts"],
    }
    return {
        **checks,
        "all_required_pass": all(checks.values()),
        "note": "Agreement may change after canonical repair and is reported separately, not used as invariant.",
    }


def _decision_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        decision = str(row.get("decision") or "")
        counts[decision] = counts.get(decision, 0) + 1
    return dict(sorted(counts.items()))


def _config_to_dict(config: LLMConfig) -> dict[str, Any]:
    return {
        "model": config.model,
        "temperature": config.temperature,
        "seed": config.seed,
        "reasoning_effort": config.reasoning_effort,
        "verbosity": config.verbosity,
        "max_output_tokens": config.max_output_tokens,
        "pricing": dict(config.pricing),
    }


def _cost(config: LLMConfig, prompt_tokens: int, completion_tokens: int) -> float:
    return (
        (prompt_tokens / 1_000_000) * float(config.pricing["input"])
        + (completion_tokens / 1_000_000) * float(config.pricing["output"])
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def _ensure_openai_key(*, api_key_file: Path | None = None, allow_skip: bool = False) -> str:
    if os.environ.get("OPENAI_API_KEY"):
        return "env:OPENAI_API_KEY"
    candidates: list[Path] = []
    if api_key_file is not None:
        candidates.append(api_key_file)
    candidates.extend(
        [
            Path("OPENAI-KEY-2.txt"),
            Path("../OPENAI-KEY-2.txt"),
            Path("THESIS_RUNTIME_TOOL/OPENAI-KEY-2.txt"),
            Path("OPENAI_API_KEY.txt"),
            Path("../OPENAI_API_KEY.txt"),
            Path("OPENAI-KEY-1.txt"),
            Path("../OPENAI-KEY-1.txt"),
            Path("THESIS_RUNTIME_TOOL/OPENAI-KEY-1.txt"),
        ]
    )
    for path in candidates:
        if not path.exists():
            continue
        value = path.read_text(encoding="utf-8").strip()
        if value:
            os.environ["OPENAI_API_KEY"] = value
            return f"file:{path.name}"
    if allow_skip:
        return "test_transport:no_key"
    raise RuntimeError("OPENAI_API_KEY is not set and no usable OPENAI-KEY file was found")


if __name__ == "__main__":
    raise SystemExit(main())
