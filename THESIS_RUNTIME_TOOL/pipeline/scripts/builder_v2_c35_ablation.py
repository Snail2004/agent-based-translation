#!/usr/bin/env python3
"""Builder v2 C3.5 ablation: gate-only and prompt-v2 pin-owner runs."""
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
    PROMPT_VERSION_V1,
    PROMPT_VERSION_V2,
    apply_decollision_to_notebook,
    build_collision_groups,
    chunk_groups,
    gate_decollision_rows,
    load_notebook,
    prompt_text,
    validate_decollision_results,
)
from pipeline.scripts.builder_v2_c35_decollision import (
    _config_to_dict,
    _cost,
    _decision_counts,
    _ensure_openai_key,
    _metric_invariants,
    _result_record,
    _safe_auditor_recall_metrics,
    _sha256,
)


DEFAULT_DB = Path("data/jobs/d2l_p1/memory.sqlite3")
DEFAULT_NOTEBOOK = Path("data/reports/builder_v2_c3_audit_real/notebook_audited.json")
DEFAULT_V1_TRAIL = Path("data/reports/builder_v2_c35_decollision/decollision_trail.json")
DEFAULT_CONFIG = Path("pipeline/configs/llm_prepass.yaml")
DEFAULT_OUT = Path("data/reports/builder_v2_c35_ablation")


def main() -> int:
    parser = argparse.ArgumentParser(description="Builder v2 C3.5 gate/prompt-v2 ablation.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--notebook", default=str(DEFAULT_NOTEBOOK))
    parser.add_argument("--v1-trail", default=str(DEFAULT_V1_TRAIL))
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--max-groups-per-call", type=int, default=8)
    parser.add_argument("--prompt-token-budget", type=int)
    parser.add_argument("--estimate-only", action="store_true")
    parser.add_argument("--confirm-usd", type=float)
    parser.add_argument("--cache-db")
    parser.add_argument("--api-key-file")
    parser.add_argument("--doc-id", default="d2l")
    parser.add_argument("--chapter", default="preliminaries")
    parser.add_argument("--profile", default="technical_d2l_v1")
    args = parser.parse_args()

    if not args.estimate_only and args.confirm_usd is None:
        raise SystemExit("Use --estimate-only or --confirm-usd.")

    report = run_ablation(
        db_path=Path(args.db),
        notebook_path=Path(args.notebook),
        v1_trail_path=Path(args.v1_trail),
        config_path=Path(args.config),
        out_dir=Path(args.out),
        max_groups_per_call=args.max_groups_per_call,
        prompt_token_budget=args.prompt_token_budget,
        estimate_only=args.estimate_only,
        confirm_usd=args.confirm_usd,
        cache_db=Path(args.cache_db) if args.cache_db else None,
        api_key_file=Path(args.api_key_file) if args.api_key_file else None,
        doc_id=args.doc_id,
        chapter_id=args.chapter,
        profile_name=args.profile,
    )
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def run_ablation(
    *,
    db_path: Path,
    notebook_path: Path,
    v1_trail_path: Path,
    config_path: Path,
    out_dir: Path,
    max_groups_per_call: int = 8,
    prompt_token_budget: int | None = None,
    estimate_only: bool = True,
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
    notebook = load_notebook(notebook_path)
    baseline_metrics = _safe_auditor_recall_metrics(
        db_path=db_path,
        doc_id=doc_id,
        chapter_id=chapter_id,
        notebook=notebook,
        profile_name=profile_name,
    )

    out_dir.mkdir(parents=True, exist_ok=True)

    groups_v1 = build_collision_groups(notebook, db_path, prompt_version=PROMPT_VERSION_V1)
    raw_v1 = json.loads(v1_trail_path.read_text(encoding="utf-8"))
    rows_v1 = validate_decollision_results(raw_v1, groups_v1)
    run1_rows = gate_decollision_rows(rows_v1, gated=True)
    run1_notebook = apply_decollision_to_notebook(
        notebook,
        run1_rows,
        prompt_version=PROMPT_VERSION_V1,
    )
    run1_metrics = _safe_auditor_recall_metrics(
        db_path=db_path,
        doc_id=doc_id,
        chapter_id=chapter_id,
        notebook=run1_notebook,
        profile_name=profile_name,
    )
    _write_arm(
        out_dir / "run1_gate",
        notebook=run1_notebook,
        rows=run1_rows,
        metrics_before=baseline_metrics,
        metrics_after=run1_metrics,
    )

    groups_v2 = build_collision_groups(notebook, db_path, prompt_version=PROMPT_VERSION_V2)
    chunks_v2 = chunk_groups(
        groups_v2,
        max_groups=max_groups_per_call,
        prompt_token_cap=effective_prompt_budget,
        prompt_version=PROMPT_VERSION_V2,
    )
    _write_groups_and_prompts(out_dir / "run2_prompt_v2", groups_v2, chunks_v2)

    prompt_total = sum(chunk.prompt_tokens_est for chunk in chunks_v2)
    output_cap = len(chunks_v2) * int(config.max_output_tokens)
    cap_cost = _cost(config, prompt_total, output_cap)

    llm_run: dict[str, Any] | None = None
    run2u_metrics = None
    run2_metrics = None
    run2u_rows: list[dict[str, Any]] | None = None
    run2_rows: list[dict[str, Any]] | None = None
    if not estimate_only:
        if confirm_usd is None:
            raise ValueError("confirm_usd is required for real Run-2")
        if cap_cost > float(confirm_usd):
            raise RuntimeError(
                f"Run-2 estimate cap ${cap_cost:.6f} exceeds --confirm-usd ${float(confirm_usd):.6f}"
            )
        key_source = _ensure_openai_key(api_key_file=api_key_file, allow_skip=transport is not None)
        llm_run = _run_prompt_v2(
            chunks=chunks_v2,
            groups=groups_v2,
            config=config,
            cache_path=cache_db or out_dir / "run2_prompt_v2" / "llm_cache.sqlite3",
            transport=transport,
        )
        llm_run["api_key_source"] = key_source
        if llm_run.get("status") != "degraded":
            validated = llm_run["decollision_rows"]
            run2u_rows = gate_decollision_rows(validated, gated=False)
            run2_rows = gate_decollision_rows(validated, gated=True)
            run2u_notebook = apply_decollision_to_notebook(
                notebook,
                run2u_rows,
                prompt_version=PROMPT_VERSION_V2,
            )
            run2_notebook = apply_decollision_to_notebook(
                notebook,
                run2_rows,
                prompt_version=PROMPT_VERSION_V2,
            )
            run2u_metrics = _safe_auditor_recall_metrics(
                db_path=db_path,
                doc_id=doc_id,
                chapter_id=chapter_id,
                notebook=run2u_notebook,
                profile_name=profile_name,
            )
            run2_metrics = _safe_auditor_recall_metrics(
                db_path=db_path,
                doc_id=doc_id,
                chapter_id=chapter_id,
                notebook=run2_notebook,
                profile_name=profile_name,
            )
            _write_arm(
                out_dir / "run2u_prompt_v2_ungated",
                notebook=run2u_notebook,
                rows=run2u_rows,
                metrics_before=baseline_metrics,
                metrics_after=run2u_metrics,
            )
            _write_arm(
                out_dir / "run2_prompt_v2_gated",
                notebook=run2_notebook,
                rows=run2_rows,
                metrics_before=baseline_metrics,
                metrics_after=run2_metrics,
            )

    db_hash_after = _sha256(db_path)
    if db_hash_before != db_hash_after:
        raise RuntimeError("Frozen DB hash changed during ablation.")

    comparison = {
        "baseline": _metric_row("baseline", baseline_metrics),
        "arm0_v1_ungated": _metric_row_from_path(
            "arm0_v1_ungated",
            Path("data/reports/builder_v2_c35_decollision/builder_v2_c35_metrics.json"),
        ),
        "run1_v1_gated": _metric_row("run1_v1_gated", run1_metrics),
        "run2u_v2_ungated": _metric_row("run2u_v2_ungated", run2u_metrics),
        "run2_v2_gated": _metric_row("run2_v2_gated", run2_metrics),
    }
    report = {
        "phase": "BUILDER-V2-C3.5-ABLATION",
        "status": "estimate_only" if estimate_only else str((llm_run or {}).get("status") or "completed"),
        "zero_api": llm_run is None,
        "zero_db_write": True,
        "blind_to_gold": True,
        "db_sha256": db_hash_before,
        "db_hash_unchanged": True,
        "model_config": _config_to_dict(config),
        "run1_decision_counts": _decision_counts(run1_rows),
        "run2_decision_counts": _decision_counts(run2_rows or []),
        "run2u_decision_counts": _decision_counts(run2u_rows or []),
        "prompt_v2": {
            "groups": len(groups_v2),
            "members": sum(len(group["members"]) for group in groups_v2),
            "calls": len(chunks_v2),
            "prompt_tokens_total": prompt_total,
            "estimated_cost_usd_cap": cap_cost,
        },
        "actual_cost_usd": (llm_run or {}).get("actual_cost_usd"),
        "parse_failure_count": (llm_run or {}).get("parse_failure_count"),
        "metric_comparison": comparison,
        "llm_run": llm_run,
        "summary": {
            "status": "estimate_only" if estimate_only else str((llm_run or {}).get("status") or "completed"),
            "run1_decision_counts": _decision_counts(run1_rows),
            "run2_decision_counts": _decision_counts(run2_rows or []),
            "prompt_v2_calls": len(chunks_v2),
            "prompt_v2_tokens": prompt_total,
            "estimated_cost_usd_cap": cap_cost,
            "actual_cost_usd": (llm_run or {}).get("actual_cost_usd"),
            "comparison": comparison,
            "zero_api": llm_run is None,
            "db_hash_unchanged": True,
        },
    }
    (out_dir / "ablation_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return report


def _run_prompt_v2(
    *,
    chunks: list[Any],
    groups: list[dict[str, Any]],
    config: LLMConfig,
    cache_path: Path,
    transport: Any | None = None,
) -> dict[str, Any]:
    client = LLMClient(config=config, cache_path=cache_path, transport=transport)
    groups_by_id = {group["group_id"]: group for group in groups}
    rows: list[dict[str, Any]] = []
    cost_log: list[dict[str, Any]] = []
    raw_outputs: list[dict[str, Any]] = []
    parse_failures = 0
    degraded_chunks: list[dict[str, Any]] = []

    for chunk in chunks:
        parsed_rows = None
        errors: list[str] = []
        attempts: list[dict[str, Any]] = []
        chunk_groups = [groups_by_id[group["group_id"]] for group in chunk.groups]
        for attempt in (1, 2):
            result = client.call(
                chunk.messages,
                response_format=None,
                tag=f"builder_v2_c35_v2:{chunk.chunk_id}:attempt{attempt}",
                bypass_cache=(attempt == 2),
            )
            attempts.append(_result_record(result, attempt))
            try:
                parsed = json.loads(result.text)
                if not isinstance(parsed, list):
                    raise ValueError("Decollision response is not a JSON array")
                parsed_rows = validate_decollision_results(parsed, chunk_groups, require_owner=True)
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


def _write_groups_and_prompts(out_dir: Path, groups: list[dict[str, Any]], chunks: list[Any]) -> None:
    prompts_dir = out_dir / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "collision_groups.json").write_text(
        json.dumps(groups, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    for chunk in chunks:
        (prompts_dir / f"{chunk.chunk_id}.txt").write_text(
            prompt_text(chunk.messages) + "\n",
            encoding="utf-8",
        )
    (out_dir / "chunks.json").write_text(
        json.dumps(
            [
                {
                    "chunk_id": chunk.chunk_id,
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


def _write_arm(
    out_dir: Path,
    *,
    notebook: dict[str, Any],
    rows: list[dict[str, Any]],
    metrics_before: dict[str, Any] | None,
    metrics_after: dict[str, Any] | None,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "decollision_trail.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (out_dir / "notebook_decollided.json").write_text(
        json.dumps(notebook, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if metrics_after is not None:
        (out_dir / "metrics.json").write_text(
            json.dumps(
                {
                    "before": metrics_before,
                    "after": metrics_after,
                    "invariants": _metric_invariants(metrics_before, metrics_after) if metrics_before else None,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )


def _metric_row(label: str, metrics: dict[str, Any] | None) -> dict[str, Any] | None:
    if metrics is None:
        return None
    recall = metrics["recall_vs_gold_dev"]
    return {
        "label": label,
        "metric_a_agreement": recall["metric_a_registry"]["agreement"],
        "metric_a_recall": recall["metric_a_registry"]["recall"],
        "metric_a_matched_terms": recall["metric_a_registry"]["matched_terms"],
        "metric_b_agreement": recall["metric_b_post_auditor"]["agreement"],
        "metric_b_recall": recall["metric_b_post_auditor"]["recall"],
        "metric_b_matched_terms": recall["metric_b_post_auditor"]["matched_terms"],
    }


def _metric_row_from_path(label: str, path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    return _metric_row(label, raw.get("after"))


if __name__ == "__main__":
    raise SystemExit(main())
