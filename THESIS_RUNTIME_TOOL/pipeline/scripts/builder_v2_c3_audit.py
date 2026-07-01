#!/usr/bin/env python3
"""Builder v2 Stage C3 Term-Auditor driver."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.agents.llm_client import LLMClient, LLMResult, estimate_prompt_tokens
from pipeline.agents.llm_config import LLMConfig, load_llm_config
from pipeline.eval.builder_gold import _count_matches, _normalize_source, _normalize_vi
from pipeline.prepass.builder_v2_audit import (
    PROMPT_VERSION,
    apply_audit_to_notebook,
    build_term_cards,
    chunk_cards,
    prompt_text,
    simulate_injection_order,
    validate_audit_results,
)
from pipeline.prepass.builder_v2_guards import apply_surface_ownership_guard
from pipeline.prepass.db_source import load_document_from_connection
from pipeline.prepass.span_resolver import _find_word_boundary_matches
from pipeline.scripts.builder_v2_metrics import _load_v2_notebook_terms, _score_terms_vs_gold
from pipeline.translate.profiles import PROFILES, injection_role_for_term, term_is_injection_eligible
from pipeline.translate.windower import build_windows


DEFAULT_DB = Path("data/jobs/d2l_p1/memory.sqlite3")
DEFAULT_NOTEBOOK = Path("data/reports/builder_v2_c2_pilot/notebook.json")
DEFAULT_CONFIG = Path("pipeline/configs/llm_prepass.yaml")
DEFAULT_OUT = Path("data/reports/builder_v2_c3_audit_estimate")
DEFAULT_OUTPUT_TOKENS_PER_CARD = 96
AUDITOR_DROP_LABELS = {"generic_low_value", "descriptive_phrase"}
AUDITOR_KEEP_LABELS = {
    "keep_as_translate_term",
    "preserve_token",
    "polysemy_or_context_dependent",
    "uncertain_low_conf",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Builder v2 C3 Term-Auditor estimate/apply driver.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--notebook", default=str(DEFAULT_NOTEBOOK))
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--chunk-size", type=int, default=40)
    parser.add_argument("--prompt-token-budget", type=int)
    parser.add_argument("--estimate-only", action="store_true")
    parser.add_argument("--audit-json", help="Optional existing audit output to validate/apply; never calls API.")
    parser.add_argument("--confirm-usd", type=float, help="Allow real API calls only if estimated cap cost is <= this amount.")
    parser.add_argument("--cache-db", help="Separate C3 LLM cache path. Defaults to <out>/llm_cache.sqlite3.")
    parser.add_argument("--api-key-file", help="Optional API key file. Env OPENAI_API_KEY still takes precedence.")
    parser.add_argument("--doc-id", default="d2l")
    parser.add_argument("--chapter", default="preliminaries")
    parser.add_argument("--profile", default="technical_d2l_v1")
    parser.add_argument("--context-budget", type=int, default=500)
    parser.add_argument("--min-injection-occurrences", type=int)
    args = parser.parse_args()

    if not args.estimate_only and not args.audit_json and args.confirm_usd is None:
        raise SystemExit("C3 §5 supports --estimate-only or --audit-json apply only; no API path is enabled.")

    report = run_c3(
        db_path=Path(args.db),
        notebook_path=Path(args.notebook),
        config_path=Path(args.config),
        out_dir=Path(args.out),
        chunk_size=args.chunk_size,
        prompt_token_budget=args.prompt_token_budget,
        estimate_only=args.estimate_only,
        audit_json=Path(args.audit_json) if args.audit_json else None,
        min_injection_occurrences=args.min_injection_occurrences,
        confirm_usd=args.confirm_usd,
        cache_db=Path(args.cache_db) if args.cache_db else None,
        api_key_file=Path(args.api_key_file) if args.api_key_file else None,
        doc_id=args.doc_id,
        chapter_id=args.chapter,
        profile_name=args.profile,
        context_budget_tokens=args.context_budget,
    )
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def run_c3(
    *,
    db_path: Path,
    notebook_path: Path,
    config_path: Path,
    out_dir: Path,
    chunk_size: int = 40,
    prompt_token_budget: int | None = None,
    estimate_only: bool = True,
    audit_json: Path | None = None,
    min_injection_occurrences: int | None = None,
    confirm_usd: float | None = None,
    cache_db: Path | None = None,
    api_key_file: Path | None = None,
    doc_id: str = "d2l",
    chapter_id: str = "preliminaries",
    profile_name: str = "technical_d2l_v1",
    context_budget_tokens: int = 500,
    transport: Any | None = None,
) -> dict[str, Any]:
    config = load_llm_config(config_path)
    profile = PROFILES[profile_name]
    min_occ = int(
        min_injection_occurrences
        if min_injection_occurrences is not None
        else profile.min_injection_occurrences
    )
    db_hash_before = _sha256(db_path)
    notebook = json.loads(notebook_path.read_text(encoding="utf-8"))
    guarded_notebook, surface_guard_report = apply_surface_ownership_guard(notebook)
    entries = guarded_notebook["entries"]
    cards = build_term_cards(entries, db_path)
    effective_prompt_budget = prompt_token_budget or int(int(config.prompt_token_cap) * 0.9)
    chunks = chunk_cards(cards, chunk_size, prompt_token_cap=effective_prompt_budget)

    out_dir.mkdir(parents=True, exist_ok=True)
    prompts_dir = out_dir / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    for chunk in chunks:
        (prompts_dir / f"{chunk.chunk_id}.txt").write_text(
            prompt_text(chunk.messages) + "\n",
            encoding="utf-8",
        )

    cards_path = out_dir / "cards.json"
    cards_path.write_text(
        json.dumps(cards, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (out_dir / "notebook_surface_guarded.json").write_text(
        json.dumps(guarded_notebook, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (out_dir / "surface_ownership_report.json").write_text(
        json.dumps(surface_guard_report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (out_dir / "surface_quarantine.json").write_text(
        json.dumps(surface_guard_report["surface_quarantine"], ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    chunk_audit = [
        {
            "chunk_id": chunk.chunk_id,
            "index": chunk.index,
            "cards": len(chunk.cards),
            "entry_ids": [card["entry_id"] for card in chunk.cards],
            "prompt_file": f"prompts/{chunk.chunk_id}.txt",
            "prompt_tokens_est": chunk.prompt_tokens_est,
        }
        for chunk in chunks
    ]
    (out_dir / "chunks.json").write_text(
        json.dumps(chunk_audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    audit_rows: list[dict[str, Any]] | None = None
    audited_notebook: dict[str, Any] | None = None
    injection_preview: list[dict[str, Any]] | None = None
    llm_run: dict[str, Any] | None = None
    metrics: dict[str, Any] | None = None
    if audit_json is not None:
        raw = json.loads(audit_json.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError("--audit-json must contain a JSON array")
        expected = [card["entry_id"] for card in cards]
        audit_rows = validate_audit_results(raw, expected)
    elif not estimate_only:
        prompt_total_for_gate = sum(chunk.prompt_tokens_est for chunk in chunks)
        output_cap_for_gate = len(chunks) * int(config.max_output_tokens)
        estimate_cap = _cost(config, prompt_total_for_gate, output_cap_for_gate)
        if confirm_usd is None:
            raise ValueError("confirm_usd is required for real C3 API calls")
        if estimate_cap > float(confirm_usd):
            raise RuntimeError(
                f"C3 estimate cap ${estimate_cap:.6f} exceeds --confirm-usd ${float(confirm_usd):.6f}"
            )
        key_source = _ensure_openai_key(api_key_file=api_key_file, allow_skip=transport is not None)
        llm_run = _run_real_audit(
            chunks=chunks,
            config=config,
            cache_path=cache_db or out_dir / "llm_cache.sqlite3",
            transport=transport,
        )
        llm_run["api_key_source"] = key_source
        audit_rows = llm_run.get("audit_rows") if llm_run.get("status") != "degraded" else None

    if audit_rows is not None:
        audited_notebook = apply_audit_to_notebook(guarded_notebook, audit_rows)
        injection_preview = simulate_injection_order(
            audited_notebook["entries"],
            min_injection_occurrences=min_occ,
        )
        (out_dir / "audit_trail.json").write_text(
            json.dumps(audit_rows, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (out_dir / "notebook_audited.json").write_text(
            json.dumps(audited_notebook, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (out_dir / "injection_preview.json").write_text(
            json.dumps(injection_preview, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        metrics = _build_auditor_recall_metrics(
            db_path=db_path,
            doc_id=doc_id,
            chapter_id=chapter_id,
            notebook=audited_notebook,
            profile_name=profile_name,
        )
        (out_dir / "builder_v2_c3_auditor_metrics.json").write_text(
            json.dumps(metrics, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    prompt_tokens = [chunk.prompt_tokens_est for chunk in chunks]
    prompt_total = sum(prompt_tokens)
    output_nominal = len(cards) * DEFAULT_OUTPUT_TOKENS_PER_CARD
    output_cap = len(chunks) * int(config.max_output_tokens)
    db_hash_after = _sha256(db_path)
    if db_hash_before != db_hash_after:
        raise RuntimeError("Frozen DB hash changed during C3.")

    status = "estimate_only"
    if audit_json is not None:
        status = "applied_existing_audit"
    elif llm_run is not None:
        status = str(llm_run.get("status") or "completed")
    metric_summary = _metric_summary(metrics)
    report = {
        "phase": "BUILDER-V2-C3-TERM-AUDITOR",
        "status": status,
        "prompt_version": PROMPT_VERSION,
        "zero_api": llm_run is None,
        "zero_db_write": True,
        "blind_to_gold": True,
        "db_path": str(db_path),
        "db_sha256": db_hash_before,
        "db_hash_unchanged": True,
        "notebook_path": str(notebook_path),
        "model_config": _config_to_dict(config),
        "chunk_size": chunk_size,
        "prompt_token_cap": int(config.prompt_token_cap),
        "prompt_token_budget": effective_prompt_budget,
        "cards": len(cards),
        "calls": len(chunks),
        "prompt_tokens": {
            "total": prompt_total,
            "min": min(prompt_tokens) if prompt_tokens else 0,
            "max": max(prompt_tokens) if prompt_tokens else 0,
            "avg": (prompt_total / len(prompt_tokens)) if prompt_tokens else 0,
        },
        "estimated_output_tokens_nominal": output_nominal,
        "estimated_output_tokens_cap": output_cap,
        "estimated_total_tokens_nominal": prompt_total + output_nominal,
        "estimated_total_tokens_cap": prompt_total + output_cap,
        "estimated_cost_usd_nominal": _cost(config, prompt_total, output_nominal),
        "estimated_cost_usd_cap": _cost(config, prompt_total, output_cap),
        "artifacts": {
            "cards": "cards.json",
            "chunks": "chunks.json",
            "prompts_dir": "prompts",
            "notebook_surface_guarded": "notebook_surface_guarded.json",
            "surface_ownership_report": "surface_ownership_report.json",
            "surface_quarantine": "surface_quarantine.json",
            "estimate_report": "builder_v2_c3_audit_estimate.json",
            "audit_trail": "audit_trail.json" if audit_rows is not None else None,
            "notebook_audited": "notebook_audited.json" if audit_rows is not None else None,
            "injection_preview": "injection_preview.json" if audit_rows is not None else None,
            "cost_log": "cost_log.json" if llm_run is not None else None,
            "raw_outputs": "raw_outputs.json" if llm_run is not None else None,
            "metrics": "builder_v2_c3_auditor_metrics.json" if metrics is not None else None,
        },
        "simulation_status": _simulation_status(audit_rows, audit_json, llm_run),
        "llm_run": llm_run,
        "metrics": metric_summary or None,
        "summary": {
            "status": status,
            "cards": len(cards),
            "calls": len(chunks),
            "prompt_tokens_total": prompt_total,
            "estimated_output_tokens_nominal": output_nominal,
            "estimated_output_tokens_cap": output_cap,
            "estimated_cost_usd_nominal": _cost(config, prompt_total, output_nominal),
            "estimated_cost_usd_cap": _cost(config, prompt_total, output_cap),
            "actual_cost_usd": (llm_run or {}).get("actual_cost_usd"),
            "parse_failure_count": (llm_run or {}).get("parse_failure_count"),
            "surface_detached_count": surface_guard_report["detached_count"],
            "surface_quarantined_count": surface_guard_report["quarantined_count"],
            "metric_a_registry_recall": metric_summary.get("metric_a_registry_recall"),
            "metric_b_post_auditor_recall": metric_summary.get("metric_b_post_auditor_recall"),
            "auditor_recall_delta": metric_summary.get("auditor_recall_delta"),
            "false_drop_gold_terms": metric_summary.get("false_drop_gold_terms"),
            "zero_api": llm_run is None,
            "db_hash_unchanged": True,
        },
    }
    (out_dir / "builder_v2_c3_audit_estimate.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return report


def _run_real_audit(
    *,
    chunks: list[Any],
    config: LLMConfig,
    cache_path: Path,
    transport: Any | None = None,
) -> dict[str, Any]:
    client = LLMClient(config=config, cache_path=cache_path, transport=transport)
    audit_rows: list[dict[str, Any]] = []
    cost_log: list[dict[str, Any]] = []
    raw_outputs: list[dict[str, Any]] = []
    parse_failures = 0
    degraded_chunks: list[dict[str, Any]] = []

    for chunk in chunks:
        expected = [card["entry_id"] for card in chunk.cards]
        parsed_rows: list[dict[str, Any]] | None = None
        errors: list[str] = []
        attempts: list[dict[str, Any]] = []
        for attempt in (1, 2):
            result = client.call(
                chunk.messages,
                response_format=None,
                tag=f"builder_v2_c3:{chunk.chunk_id}:attempt{attempt}",
                bypass_cache=(attempt == 2),
            )
            attempts.append(_result_record(result, attempt))
            try:
                parsed = json.loads(result.text)
                if not isinstance(parsed, list):
                    raise ValueError("Auditor response is not a JSON array")
                parsed_rows = validate_audit_results(parsed, expected)
                break
            except Exception as exc:  # noqa: BLE001 - logged, then retried/degraded.
                errors.append(str(exc))
                if attempt == 1:
                    parse_failures += 1
                    continue
                degraded_chunks.append(
                    {"chunk_id": chunk.chunk_id, "entry_ids": expected, "errors": errors}
                )
        cost_log.extend(attempts)
        raw_outputs.append(
            {
                "chunk_id": chunk.chunk_id,
                "entry_ids": expected,
                "attempts": attempts,
                "errors": errors,
                "parsed_rows": parsed_rows,
            }
        )
        if parsed_rows is not None:
            audit_rows.extend(parsed_rows)

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
        "audit_rows": audit_rows,
    }


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


def _build_auditor_recall_metrics(
    *,
    db_path: Path,
    doc_id: str,
    chapter_id: str,
    notebook: dict[str, Any],
    profile_name: str,
) -> dict[str, Any]:
    before_hash = _sha256(db_path)
    conn = _connect_ro(db_path)
    conn.row_factory = sqlite3.Row
    try:
        document = load_document_from_connection(conn, doc_id, [chapter_id], translate_only=True)
        source_text = "\n\n".join(
            str(block.get("source_text") or block.get("clean_text") or "")
            for chapter in document["chapters"]
            for block in chapter.get("blocks", [])
        )
        gold_terms = _present_gold_terms(conn, doc_id, source_text)
    finally:
        conn.close()
    after_hash = _sha256(db_path)
    if before_hash != after_hash:
        raise RuntimeError("Frozen DB hash changed during C3 auditor metrics.")

    registry_terms, registry_entry_count, _, _ = _load_v2_notebook_terms(notebook)
    filtered_notebook = {
        **notebook,
        "entries": [
            entry
            for entry in notebook.get("entries") or []
            if isinstance(entry, dict) and _audit_label(entry) not in AUDITOR_DROP_LABELS
        ],
    }
    filtered_terms, filtered_entry_count, _, _ = _load_v2_notebook_terms(filtered_notebook)
    metric_a = _score_terms_vs_gold(
        registry_terms,
        gold_terms,
        builder_term_count=registry_entry_count,
    )
    metric_b = _score_terms_vs_gold(
        filtered_terms,
        gold_terms,
        builder_term_count=filtered_entry_count,
    )
    labels = Counter(
        _audit_label(entry)
        for entry in notebook.get("entries") or []
        if isinstance(entry, dict)
    )
    false_drop = _auditor_false_drop_report(notebook, registry_terms, filtered_terms, gold_terms)
    keep_terms = _terms_for_label(notebook, "keep_as_translate_term")
    terms_by_label = _terms_by_label(notebook)
    delta = round(float(metric_a["recall"]) - float(metric_b["recall"]), 6)
    return {
        "phase": "BUILDER-V2-C3-AUDITOR-RECALL-METRICS",
        "doc_id": doc_id,
        "chapter": chapter_id,
        "profile": profile_name,
        "db_sha256": before_hash,
        "db_hash_unchanged": before_hash == after_hash,
        "zero_api": True,
        "gold_eval_only": True,
        "auditor_prompt_blind_to_gold": True,
        "note": (
            "Eval-only metric. Metric A measures registry recall before Auditor filtering. "
            "Metric B applies Auditor labels directly to the dictionary: generic_low_value "
            "and descriptive_phrase are treated as dropped; keep/preserve/polysemy/uncertain "
            "are retained. No Translator pack budget and no occurrence filter are applied."
        ),
        "drop_labels": sorted(AUDITOR_DROP_LABELS),
        "keep_labels": sorted(AUDITOR_KEEP_LABELS),
        "entry_counts": {
            "registry_entries": registry_entry_count,
            "post_auditor_kept_entries": filtered_entry_count,
            "post_auditor_dropped_entries": registry_entry_count - filtered_entry_count,
        },
        "gold_denominator": {
            "scope": "gold source terms present in chapter source text",
            "terms": len(gold_terms),
        },
        "recall_vs_gold_dev": {
            "gold_terms_present": len(gold_terms),
            "metric_a_registry": metric_a,
            "metric_b_post_auditor": metric_b,
            "auditor_recall_delta": delta,
            "v1_registry_reference": {
                "recall": 0.6316,
                "note": "Reference only; not a pass/fail floor for C3 Auditor.",
            },
        },
        "audit_label_counts": dict(sorted(labels.items())),
        "false_drop": false_drop,
        "keep_as_translate_term_terms": keep_terms,
        "terms_by_label": terms_by_label,
        "removed_occurrence_filter": {
            "profile_min_injection_occurrences": PROFILES[profile_name].min_injection_occurrences,
            "decision": (
                "technical_d2l_v1 no longer filters by occurrences_count; Auditor semantic "
                "labels are the precision gate. This changes S1 injection behavior and "
                "requires Claude review before production translation."
            ),
        },
    }


def _audit_label(entry: dict[str, Any]) -> str:
    return str(((entry.get("audit") or {}).get("audit_label") or "missing"))


def _auditor_false_drop_report(
    notebook: dict[str, Any],
    registry_terms: dict[str, dict[str, Any]],
    filtered_terms: dict[str, dict[str, Any]],
    gold_terms: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    dropped_by_source: dict[str, dict[str, Any]] = {}
    for entry in notebook.get("entries") or []:
        if not isinstance(entry, dict) or _audit_label(entry) not in AUDITOR_DROP_LABELS:
            continue
        audit = entry.get("audit") or {}
        for source in _source_terms(entry):
            dropped_by_source[_normalize_source(source)] = {
                "source_term": source,
                "occurrences_total": int(entry.get("occurrences_total") or 0),
                "audit_label": _audit_label(entry),
                "reason": str(audit.get("reason") or ""),
                "builder_target": str(entry.get("canonical_target_vi") or ""),
            }
    hits: list[dict[str, Any]] = []
    for key, gold in sorted(gold_terms.items(), key=lambda item: item[1]["source_term"].casefold()):
        if key not in registry_terms or key in filtered_terms or key not in dropped_by_source:
            continue
        dropped = dropped_by_source[key]
        hits.append(
            {
                "source_term": gold["source_term"],
                "occurrences_total": dropped["occurrences_total"],
                "audit_label": dropped["audit_label"],
                "reason": dropped["reason"],
                "gold_target": " | ".join(gold["target_display"]),
                "builder_target": dropped["builder_target"],
            }
        )
    return {
        "definition": (
            "Auditor false-drop = gold term present in Metric A registry terms but removed "
            "from Metric B solely because Auditor assigned a drop label."
        ),
        "count": len(hits),
        "terms": hits,
    }


def _terms_for_label(notebook: dict[str, Any], label: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for entry in notebook.get("entries") or []:
        if not isinstance(entry, dict) or _audit_label(entry) != label:
            continue
        rows.append(_audit_term_row(entry))
    return sorted(rows, key=lambda item: str(item["source_term"]).casefold())


def _terms_by_label(notebook: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for entry in notebook.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        buckets.setdefault(_audit_label(entry), []).append(_audit_term_row(entry))
    return {
        label: sorted(rows, key=lambda item: str(item["source_term"]).casefold())
        for label, rows in sorted(buckets.items())
    }


def _audit_term_row(entry: dict[str, Any]) -> dict[str, Any]:
    audit = entry.get("audit") or {}
    return {
        "source_term": str(entry.get("canonical_source_term") or ""),
        "target_term": str(entry.get("canonical_target_vi") or ""),
        "occurrences_total": int(entry.get("occurrences_total") or 0),
        "audit_label": _audit_label(entry),
        "priority_tier": str(audit.get("priority_tier") or ""),
        "injection_action": str(audit.get("injection_action") or ""),
        "confidence": str(audit.get("confidence") or ""),
        "reason": str(audit.get("reason") or ""),
    }


def _build_injected_pack_metrics(
    *,
    db_path: Path,
    doc_id: str,
    chapter_id: str,
    notebook: dict[str, Any],
    profile_name: str,
    context_budget_tokens: int,
) -> dict[str, Any]:
    before_hash = _sha256(db_path)
    conn = _connect_ro(db_path)
    conn.row_factory = sqlite3.Row
    try:
        document = load_document_from_connection(conn, doc_id, [chapter_id], translate_only=True)
        source_text = "\n\n".join(
            str(block.get("source_text") or block.get("clean_text") or "")
            for chapter in document["chapters"]
            for block in chapter.get("blocks", [])
        )
        gold_terms = _present_gold_terms(conn, doc_id, source_text)
        windows = build_windows(
            conn,
            doc_id,
            [chapter_id],
            block_types=PROFILES[profile_name].translatable_block_types,
        )
        blocks_by_window = [_fetch_window_blocks(conn, window) for window in windows]
    finally:
        conn.close()
    after_hash = _sha256(db_path)
    if before_hash != after_hash:
        raise RuntimeError("Frozen DB hash changed during C3 metrics.")

    terms_all, entry_count, _, _ = _load_v2_notebook_terms(notebook)
    production_entries = _production_entries(notebook, profile_name)
    injected_terms = _simulate_window_injection(
        production_entries,
        blocks_by_window,
        context_budget_tokens=context_budget_tokens,
    )
    injected_terms_by_source: dict[str, dict[str, Any]] = {}
    for entry in injected_terms["included_entries"].values():
        for source in entry["source_terms"]:
            injected_terms_by_source[_normalize_source(source)] = {
                "source_terms": entry["source_terms"],
                "target_term": entry["target_term"],
                "accepted_targets": entry["accepted_targets"],
            }
    recall_score = _score_terms_vs_gold(
        injected_terms_by_source,
        gold_terms,
        builder_term_count=len(injected_terms["included_entries"]),
    )
    registry_score = _score_terms_vs_gold(terms_all, gold_terms, builder_term_count=entry_count)
    false_drop = _false_drop_report(notebook, gold_terms)
    labels = Counter(
        str(((entry.get("audit") or {}).get("audit_label") or "missing"))
        for entry in notebook.get("entries") or []
        if isinstance(entry, dict)
    )
    return {
        "phase": "BUILDER-V2-C3-INJECTED-PACK-METRICS",
        "doc_id": doc_id,
        "chapter": chapter_id,
        "profile": profile_name,
        "context_budget_tokens": context_budget_tokens,
        "db_sha256": before_hash,
        "db_hash_unchanged": before_hash == after_hash,
        "note": (
            "Eval-only metric. Auditor prompt/card builder is blind to gold; this report reads "
            "eval_glossary_gold after audit. Window injection is simulated from audited notebook "
            "using profile eligibility, source-surface anchors, auditor tier sort, and token budget."
        ),
        "entry_counts": {
            "registry_entries": entry_count,
            "eligible_entries": len(production_entries),
            "injected_unique_entries": len(injected_terms["included_entries"]),
        },
        "audit_label_counts": dict(sorted(labels.items())),
        "recall_vs_gold_dev": {
            "gold_terms_present": len(gold_terms),
            "registry_before_budget": registry_score,
            "injected_pack": recall_score,
            "floor_v1": 0.6316,
            "pass_floor": recall_score["recall"] >= 0.6316,
        },
        "false_drop": false_drop,
        "window_injection": {
            "windows": len(blocks_by_window),
            "included_total_events": injected_terms["included_total_events"],
            "dropped_total_events": injected_terms["dropped_total_events"],
            "windows_with_drops": injected_terms["windows_with_drops"],
            "sample_drops": injected_terms["sample_drops"][:50],
        },
    }


def _production_entries(notebook: dict[str, Any], profile_name: str) -> list[dict[str, Any]]:
    profile = PROFILES[profile_name]
    rows: list[dict[str, Any]] = []
    for entry in notebook.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        source = str(entry.get("canonical_source_term") or "")
        target = str(entry.get("canonical_target_vi") or "")
        variants = [
            str(item.get("text") or "")
            for item in entry.get("target_variants") or []
            if isinstance(item, dict) and str(item.get("text") or "").strip()
        ]
        row = {
            "entry_id": str(entry.get("concept_key") or source),
            "source_term": source,
            "source_terms": _source_terms(entry),
            "target_term": target,
            "accepted_targets": {_normalize_vi(item) for item in [target, *variants] if str(item).strip()},
            "allowed_variants_json": json.dumps(variants, ensure_ascii=False),
            "term_type": str(entry.get("term_type") or "term"),
            "do_not_translate": 1 if bool(entry.get("do_not_translate")) else 0,
            "occurrences_count": int(entry.get("occurrences_total") or 0),
            "audit": dict(entry.get("audit") or {}),
        }
        if term_is_injection_eligible(row, profile):
            rows.append(row)
    return rows


def _simulate_window_injection(
    entries: list[dict[str, Any]],
    blocks_by_window: list[list[dict[str, Any]]],
    *,
    context_budget_tokens: int,
) -> dict[str, Any]:
    included: dict[str, dict[str, Any]] = {}
    dropped: list[dict[str, Any]] = []
    included_total = 0
    dropped_total = 0
    windows_with_drops = 0
    for window_index, blocks in enumerate(blocks_by_window, start=1):
        text = "\n\n".join(str(block.get("source_text") or block.get("clean_text") or "") for block in blocks)
        anchors: list[dict[str, Any]] = []
        for entry in entries:
            count = sum(len(_find_word_boundary_matches(text, surface)) for surface in entry["source_terms"])
            if count <= 0:
                continue
            line = _term_line(entry)
            anchors.append(
                {
                    "entry": entry,
                    "line": line,
                    "count": count,
                    "token_estimate": max(1, estimate_prompt_tokens([{"role": "user", "content": line}], None)),
                    "sort_key": _tier_sort_key(entry, count),
                }
            )
        budget_used = 0
        window_drops = 0
        for item in sorted(anchors, key=lambda value: value["sort_key"]):
            entry = item["entry"]
            if budget_used + item["token_estimate"] <= context_budget_tokens:
                budget_used += item["token_estimate"]
                included[entry["entry_id"]] = entry
                included_total += 1
            else:
                window_drops += 1
                dropped_total += 1
                if len(dropped) < 100:
                    dropped.append(
                        {
                            "window": window_index,
                            "entry_id": entry["entry_id"],
                            "source_term": entry["source_term"],
                            "priority_tier": str((entry.get("audit") or {}).get("priority_tier") or "high"),
                            "occurrences_count": entry["occurrences_count"],
                            "reason": "budget",
                        }
                    )
        if window_drops:
            windows_with_drops += 1
    return {
        "included_entries": included,
        "included_total_events": included_total,
        "dropped_total_events": dropped_total,
        "windows_with_drops": windows_with_drops,
        "sample_drops": dropped,
    }


def _tier_sort_key(entry: dict[str, Any], count: int) -> tuple[Any, ...]:
    tier_rank = {"high": 0, "medium": 1, "review": 2, "low": 3}
    tier = str((entry.get("audit") or {}).get("priority_tier") or "high")
    return (
        tier_rank.get(tier, tier_rank["review"]),
        -count,
        -int(entry.get("occurrences_count") or 0),
        str(entry.get("source_term") or "").casefold(),
        str(entry.get("entry_id") or ""),
    )


def _term_line(entry: dict[str, Any]) -> str:
    action = str((entry.get("audit") or {}).get("injection_action") or "translate")
    source = str(entry.get("source_term") or "")
    target = str(entry.get("target_term") or "")
    if action == "preserve" or injection_role_for_term(entry) == "preserve":
        return f"Preserve {source} unchanged."
    if action == "context_sensitive_translate":
        return f"{source} => {target} (context-sensitive; use variants only when context fits)"
    return f"{source} => {target}"


def _source_terms(entry: dict[str, Any]) -> list[str]:
    values = [str(entry.get("canonical_source_term") or "")]
    for variant in entry.get("source_variants") or []:
        if isinstance(variant, dict):
            values.append(str(variant.get("surface") or ""))
    return _stable_unique(values)


def _false_drop_report(notebook: dict[str, Any], gold_terms: dict[str, dict[str, Any]]) -> dict[str, Any]:
    low_labels = {"generic_low_value", "descriptive_phrase"}
    low_entries: dict[str, dict[str, Any]] = {}
    for entry in notebook.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        audit = entry.get("audit") or {}
        if str(audit.get("audit_label") or "") not in low_labels:
            continue
        for source in _source_terms(entry):
            low_entries[_normalize_source(source)] = {
                "source_term": source,
                "label": str(audit.get("audit_label") or ""),
                "reason": str(audit.get("reason") or ""),
                "target": str(entry.get("canonical_target_vi") or ""),
            }
    hits = [
        {
            "source_term": gold["source_term"],
            "gold_target": " | ".join(gold["target_display"]),
            "audit_label": low_entries[key]["label"],
            "reason": low_entries[key]["reason"],
            "builder_target": low_entries[key]["target"],
        }
        for key, gold in sorted(gold_terms.items(), key=lambda item: item[1]["source_term"].casefold())
        if key in low_entries
    ]
    return {"low_value_gold_hits": len(hits), "low_value_gold_terms": hits}


def _present_gold_terms(
    conn: sqlite3.Connection,
    doc_id: str,
    source_text: str,
) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT source_term, target_term
        FROM eval_glossary_gold
        WHERE doc_id = ?
        ORDER BY source_term, target_term
        """,
        (doc_id,),
    ).fetchall()
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        source = str(row["source_term"] or "")
        if not _count_matches(source_text, source):
            continue
        target = str(row["target_term"] or "")
        key = _normalize_source(source)
        entry = result.setdefault(
            key,
            {"source_term": source, "targets": set(), "target_display": []},
        )
        entry["targets"].add(_normalize_vi(target))
        if target not in entry["target_display"]:
            entry["target_display"].append(target)
    return result


def _fetch_window_blocks(conn: sqlite3.Connection, window: Any) -> list[dict[str, Any]]:
    if not window.block_ids:
        return []
    placeholders = ",".join("?" * len(window.block_ids))
    rows = conn.execute(
        f"""
        SELECT block_id, doc_id, chapter_id, order_index, block_type, text, text AS source_text
        FROM blocks
        WHERE block_id IN ({placeholders})
        ORDER BY order_index
        """,
        list(window.block_ids),
    ).fetchall()
    return [dict(row) for row in rows]


def _metric_summary(metrics: dict[str, Any] | None) -> dict[str, Any]:
    if not metrics:
        return {}
    recall_block = metrics["recall_vs_gold_dev"]
    metric_a = recall_block["metric_a_registry"]
    metric_b = recall_block["metric_b_post_auditor"]
    false_drop = metrics["false_drop"]
    return {
        "metric_a_registry_recall": metric_a["recall"],
        "metric_a_matched_terms": metric_a["matched_terms"],
        "metric_b_post_auditor_recall": metric_b["recall"],
        "metric_b_matched_terms": metric_b["matched_terms"],
        "auditor_recall_delta": recall_block["auditor_recall_delta"],
        "gold_terms_present": recall_block["gold_terms_present"],
        "false_drop_gold_terms": false_drop["count"],
    }


def _simulation_status(
    audit_rows: list[dict[str, Any]] | None,
    audit_json: Path | None,
    llm_run: dict[str, Any] | None,
) -> str:
    if audit_rows is None:
        return "not_run_no_audit_labels"
    if audit_json is not None:
        return "run_from_existing_audit_json"
    if llm_run is not None:
        return "run_from_real_audit_api"
    return "run_from_unknown_audit_source"


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


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)


def _stable_unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        clean = re.sub(r"\s+", " ", str(value or "").strip())
        key = clean.casefold()
        if not clean or key in seen:
            continue
        seen.add(key)
        result.append(clean)
    return result


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
