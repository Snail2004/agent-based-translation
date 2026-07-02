from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable

import requests

from pipeline.agents.llm_client import LLMClient, estimate_prompt_tokens
from pipeline.agents.llm_config import LLMConfig, load_llm_config
from pipeline.eval.cascade_localize import (
    DEFAULT_MARGIN_THRESHOLD,
    FROZEN_DB_SHA_FIRST16,
    T1Region,
    TierDecision,
    _config_report,
    _group_items_by_block,
    _identity_to_dict,
    _occurrence_index_in_source_sentence,
    _prewarm_t1_embeddings,
    _score_locate_only_by_code,
    _sha256_file,
    _target_region_text,
    enumerate_occurrences,
    run_t1_regions_for_block,
    run_t2_rules,
)
from pipeline.eval.d2l_translate_score import (
    _load_gold_targets,
    _load_translations,
    _resolve_chapters,
    _scope_blocks,
)
from pipeline.eval.occurrence_adherence import AdherenceTerm
from pipeline.eval.llm_adjudicator import (
    PROMPT_VERSION as T3_PROMPT_VERSION,
    RESULT_SCHEMA as T3_RESULT_SCHEMA,
    AdjudicationInput,
    build_messages as build_t3_messages,
    validate_payload as validate_t3_payload,
)
from pipeline.eval.region_align import (
    EmbeddingCacheClient,
    EmbeddingModelConfig,
    preflight_embedding_model,
)
from pipeline.eval.region_align import parse_model_specs
from pipeline.eval.surface_match import normalize_surface
from pipeline.retrieval.context_builder import (
    PACK_INJECTION_ACTIONS,
    notebook_entries_to_term_rows,
)
from pipeline.translate.profiles import get_profile


DEFAULT_OUT_DIR = Path("data/reports/exp_s0s1_builderv2_v1")
DEFAULT_WORKDB = Path("data/jobs/exp_s0s1_full/memory.sqlite3")
DEFAULT_FROZEN_DB = Path("data/jobs/d2l_p1/memory.sqlite3")
DEFAULT_EXPERIMENT = "exp_s0s1_builderv2_v1"
DEFAULT_CHAPTER = "multilayer_perceptrons"
DEFAULT_EMBED_ENDPOINT = "http://127.0.0.1:1234/v1/embeddings"
DEFAULT_MODEL_SPEC = "bge-m3=text-embedding-bge-m3@gpustack/bge-m3-GGUF:Q8_0"
DEFAULT_CACHE_DIR = Path("data/eval/embed_cache/cascade_exp_mlp")
DEFAULT_T3_CACHE = DEFAULT_OUT_DIR / "cascade_t3_cache.sqlite3"
DEFAULT_NOTEBOOK = Path("data/reports/builder_v2_mlp_c35/notebook_decollided.json")
DEFAULT_GOLD_VARIANTS = Path("data/eval/d2l_gold_variants.csv")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run §35.10 experiment-scoped cascade localization for overlay marks."
    )
    parser.add_argument("--db", default=str(DEFAULT_WORKDB), help="Read-only work DB from the S0/S1 experiment.")
    parser.add_argument("--frozen-db", default=str(DEFAULT_FROZEN_DB), help="Frozen source DB hash guard.")
    parser.add_argument("--experiment", default=DEFAULT_EXPERIMENT)
    parser.add_argument("--chapter", default=DEFAULT_CHAPTER)
    parser.add_argument("--configs", default="S0,S1")
    parser.add_argument("--profile", default="technical_d2l_v1")
    parser.add_argument("--doc-id", default="d2l")
    parser.add_argument("--embed-endpoint", default=DEFAULT_EMBED_ENDPOINT)
    parser.add_argument("--models", default=DEFAULT_MODEL_SPEC)
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR))
    parser.add_argument("--margin-threshold", type=float, default=DEFAULT_MARGIN_THRESHOLD)
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--llm-config", default="pipeline/configs/llm_adjudicator.yaml")
    parser.add_argument("--llm-cache", default=str(DEFAULT_T3_CACHE))
    parser.add_argument("--confirm-usd", type=float, default=None)
    parser.add_argument("--t3-backend", default="openai", choices=["openai", "local-lmstudio"])
    parser.add_argument("--local-t3-endpoint", default="http://127.0.0.1:1234")
    parser.add_argument("--local-t3-model", default="google/gemma-4-12b")
    parser.add_argument("--local-t3-concurrency", type=int, default=1)
    parser.add_argument("--local-t3-timeout-sec", type=int, default=300)
    parser.add_argument("--gpt-fallback-on-validation-error", action="store_true")
    parser.add_argument("--notebook", default=str(DEFAULT_NOTEBOOK))
    parser.add_argument("--gold-variants", default=str(DEFAULT_GOLD_VARIANTS))
    parser.add_argument(
        "--term-scope",
        default="notebook_plus_gold",
        choices=["notebook_plus_gold", "legacy_registry"],
        help="Term ruler for §35.10. notebook_plus_gold is the production experiment scope.",
    )
    args = parser.parse_args()

    model_config = _single_model_config(args.models)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    config_list = _parse_configs(args.configs)
    llm_config = load_llm_config(args.llm_config)

    report = run_experiment_cascade(
        db_path=Path(args.db),
        frozen_db_path=Path(args.frozen_db),
        experiment_id=args.experiment,
        chapter=args.chapter,
        configs=config_list,
        profile_name=args.profile,
        doc_id=args.doc_id,
        embed_endpoint=args.embed_endpoint,
        model_config=model_config,
        cache_dir=Path(args.cache_dir),
        margin_threshold=args.margin_threshold,
        llm_config=llm_config,
        llm_cache=Path(args.llm_cache),
        notebook_path=Path(args.notebook),
        gold_variants_path=Path(args.gold_variants),
        term_scope=args.term_scope,
        out_dir=out_dir,
        preflight_only=args.preflight_only,
        confirm_usd=args.confirm_usd,
        t3_backend=args.t3_backend,
        local_t3_endpoint=args.local_t3_endpoint,
        local_t3_model=args.local_t3_model,
        local_t3_concurrency=args.local_t3_concurrency,
        local_t3_timeout_sec=args.local_t3_timeout_sec,
        gpt_fallback_on_validation_error=args.gpt_fallback_on_validation_error,
    )
    print(json.dumps(_console_summary(report), ensure_ascii=False, indent=2))
    return 0


def run_experiment_cascade(
    *,
    db_path: Path,
    frozen_db_path: Path,
    experiment_id: str,
    chapter: str,
    configs: list[str],
    profile_name: str,
    doc_id: str,
    embed_endpoint: str,
    model_config: EmbeddingModelConfig,
    cache_dir: Path,
    margin_threshold: float,
    llm_config: LLMConfig,
    llm_cache: Path,
    notebook_path: Path,
    gold_variants_path: Path,
    term_scope: str,
    out_dir: Path,
    preflight_only: bool,
    confirm_usd: float | None,
    t3_backend: str,
    local_t3_endpoint: str,
    local_t3_model: str,
    local_t3_concurrency: int,
    local_t3_timeout_sec: int,
    gpt_fallback_on_validation_error: bool,
) -> dict[str, Any]:
    started = time.perf_counter()
    workdb_hash_before = _sha256_file(db_path)
    frozen_hash_before = _sha256_file(frozen_db_path)
    identity = preflight_embedding_model(endpoint=embed_endpoint, config=model_config)
    endpoint_report = _identity_to_dict(identity)
    if identity.status != "available":
        report = _base_report(
            mode="preflight_embedding_unavailable",
            experiment_id=experiment_id,
            chapter=chapter,
            configs=configs,
            endpoint_report=endpoint_report,
            frozen_hash_before=frozen_hash_before,
            frozen_hash_after=_sha256_file(frozen_db_path),
            workdb_hash_before=workdb_hash_before,
            workdb_hash_after=_sha256_file(db_path),
            elapsed_seconds=time.perf_counter() - started,
        )
        report["embedding_unavailable"] = True
        report["embedding_error"] = endpoint_report.get("skipped_with_reason", "")
        _write_json(out_dir / "cascade_mlp_preflight.json", report)
        return report

    client = EmbeddingCacheClient(
        endpoint=embed_endpoint,
        model=identity.endpoint_model,
        model_alias=identity.alias,
        model_version=identity.cache_model_version(),
        query_prefix=identity.query_prefix,
        passage_prefix=identity.passage_prefix,
        prefix_profile=f"q={identity.query_prefix!r};p={identity.passage_prefix!r}",
        cache_dir=cache_dir / identity.alias,
    )
    reports = _run_t1_t2_readonly(
        db_path=db_path,
        experiment_id=experiment_id,
        configs=configs,
        chapter=chapter,
        profile_name=profile_name,
        doc_id=doc_id,
        identity=identity,
        client=client,
        margin_threshold=margin_threshold,
        notebook_path=notebook_path,
        gold_variants_path=gold_variants_path,
        term_scope=term_scope,
        started=started,
    )
    estimates = {
        config: estimate_t3_for_report(reports[config], llm_config)
        for config in configs
    }
    total_estimate = _sum_estimates(estimates)
    report = _base_report(
        mode="preflight" if preflight_only else "run",
        experiment_id=experiment_id,
        chapter=chapter,
        configs=configs,
        endpoint_report=endpoint_report,
        frozen_hash_before=frozen_hash_before,
        frozen_hash_after=_sha256_file(frozen_db_path),
        workdb_hash_before=workdb_hash_before,
        workdb_hash_after=_sha256_file(db_path),
        elapsed_seconds=time.perf_counter() - started,
    )
    report.update({
        "task": "BUILDER-V2 §35.10",
        "scope_statement": (
            "Display-only occurrence localization cascade. B/D scores remain final; "
            "this report only adds overlay marks and diagnostics."
        ),
        "prompt_version": T3_PROMPT_VERSION,
        "llm": _llm_config_dict(llm_config),
        "t3_backend": t3_backend,
        "local_t3": {
            "endpoint": local_t3_endpoint,
            "model": local_t3_model,
            "concurrency": local_t3_concurrency,
            "timeout_sec": local_t3_timeout_sec,
            "request_profile": _local_t3_request_profile(local_t3_model),
            "gpt_fallback_on_validation_error": gpt_fallback_on_validation_error,
        } if t3_backend == "local-lmstudio" else None,
        "t3_estimate_by_config": estimates,
        "t3_estimate_total": total_estimate,
        "t3_estimate_over_500_calls": total_estimate["calls"] > 500,
        "term_scope": term_scope,
        "notebook_path": str(notebook_path),
        "gold_variants_path": str(gold_variants_path),
        "reports": reports,
    })
    if preflight_only:
        _write_json(out_dir / "cascade_mlp_preflight.json", report)
        return report

    if confirm_usd is None:
        raise SystemExit("--confirm-usd is required for non-preflight T3 runs")
    cost_gate_value = 0.0 if t3_backend == "local-lmstudio" else total_estimate["cost_cap_usd"]
    if cost_gate_value > confirm_usd:
        raise SystemExit(
            f"T3 estimated cap ${cost_gate_value:.6f} exceeds --confirm-usd ${confirm_usd:.6f}"
        )

    llm_client: LLMClient | None = None
    if t3_backend == "openai" or gpt_fallback_on_validation_error:
        _ensure_openai_key()
        llm_client = LLMClient(config=llm_config, cache_path=llm_cache)
    local_client = None
    if t3_backend == "local-lmstudio":
        local_client = LocalLMStudioT3Client(
            endpoint=local_t3_endpoint,
            model=local_t3_model,
            cache_path=llm_cache,
            timeout_sec=local_t3_timeout_sec,
        )
    run_stats = {}
    for config in configs:
        if t3_backend == "local-lmstudio":
            assert local_client is not None
            updated, stats = _run_t3_for_config_local(
                reports[config],
                local_client=local_client,
                fallback_client=llm_client,
                concurrency=local_t3_concurrency,
                use_gpt_fallback=gpt_fallback_on_validation_error,
            )
        else:
            assert llm_client is not None
            updated, stats = _run_t3_for_config(reports[config], llm_client)
        reports[config] = updated
        run_stats[config] = stats
        _write_json(out_dir / f"cascade_mlp_{config}.json", updated)
    report["mode"] = "run"
    report["reports"] = reports
    report["t3_run_stats"] = run_stats
    report["t3_run_total"] = _sum_run_stats(run_stats)
    report["frozen_db_sha256_after"] = _sha256_file(frozen_db_path)
    report["workdb_sha256_after"] = _sha256_file(db_path)
    _write_json(out_dir / "cascade_mlp_summary.json", report)
    return report


def estimate_t3_for_report(config_report: dict[str, Any], llm_config: LLMConfig) -> dict[str, Any]:
    residuals = [
        decision for decision in config_report.get("decisions", [])
        if decision.get("resolved_by") != "t2_credit"
    ]
    prompt_tokens = 0
    prompt_token_max = 0
    for decision in residuals:
        messages = _build_t3_messages_for_decision(decision)
        tokens = estimate_prompt_tokens(messages, T3_RESULT_SCHEMA)
        prompt_tokens += tokens
        prompt_token_max = max(prompt_token_max, tokens)
    input_cost = prompt_tokens * llm_config.pricing["input"] / 1_000_000
    output_tokens_cap = len(residuals) * llm_config.max_output_tokens
    output_cost_cap = output_tokens_cap * llm_config.pricing["output"] / 1_000_000
    return {
        "calls": len(residuals),
        "prompt_tokens_estimate": prompt_tokens,
        "prompt_tokens_max_single_call": prompt_token_max,
        "output_tokens_cap": output_tokens_cap,
        "cost_cap_usd": round(input_cost + output_cost_cap, 12),
        "input_cost_estimate_usd": round(input_cost, 12),
        "output_cost_cap_usd": round(output_cost_cap, 12),
    }


def _run_t1_t2_readonly(
    *,
    db_path: Path,
    experiment_id: str,
    configs: list[str],
    chapter: str,
    profile_name: str,
    doc_id: str,
    identity: Any,
    client: EmbeddingCacheClient,
    margin_threshold: float,
    notebook_path: Path,
    gold_variants_path: Path,
    term_scope: str,
    started: float,
) -> dict[str, Any]:
    occurrences_by_config = {}
    db_hash = _sha256_file(db_path)
    with _connect_readonly(db_path) as conn:
        profile = get_profile(profile_name)
        resolved_chapters = _resolve_chapters(conn, doc_id, [chapter])
        blocks = _scope_blocks(conn, doc_id, resolved_chapters, profile)
        terms, term_scope_report = _load_scope_terms(
            conn=conn,
            doc_id=doc_id,
            notebook_path=notebook_path,
            gold_variants_path=gold_variants_path,
            term_scope=term_scope,
        )
        for config in configs:
            translations = _load_translations(conn, experiment_id, config)
            occurrences_by_config[config] = enumerate_occurrences(
                blocks=blocks,
                translations=translations,
                terms=terms,
                config=config,
            )

    reports: dict[str, Any] = {}
    for config, items in occurrences_by_config.items():
        decisions: list[TierDecision] = []
        _prewarm_t1_embeddings(items, client)
        t1_by_occ: dict[str, T1Region] = {}
        for block_items in _group_items_by_block(items).values():
            t1_by_occ.update(
                run_t1_regions_for_block(
                    [item[0] for item in block_items],
                    client=client,
                    margin_threshold=margin_threshold,
                )
            )
        term_list = [item[2] for item in items]
        # The term catalog is shared for every occurrence; use a stable unique
        # list instead of per-block duplicates when checking shared variants.
        all_terms = {term.term_id: term for term in term_list}
        for occurrence, target_spans, term in items:
            t1 = t1_by_occ[occurrence.occ_id]
            decisions.append(run_t2_rules(occurrence, term, target_spans, t1, list(all_terms.values())))
        report = _config_report(
            config=config,
            decisions=decisions,
            occurrences=len(items),
            db_hash=db_hash,
            identity=identity,
            margin_threshold=margin_threshold,
            elapsed_seconds=time.perf_counter() - started,
            embed_stats=client.stats(),
        )
        report["phase"] = "35.10_preflight"
        report["tier_split"] = _tier_split(report)
        report["term_scope"] = term_scope_report
        reports[config] = report
    return reports


def _load_scope_terms(
    *,
    conn: sqlite3.Connection,
    doc_id: str,
    notebook_path: Path,
    gold_variants_path: Path,
    term_scope: str,
) -> tuple[list[AdherenceTerm], dict[str, Any]]:
    if term_scope == "legacy_registry":
        from pipeline.eval.d2l_translate_score import _load_registry_rows, _registry_adherence_terms

        rows = _load_registry_rows(conn, doc_id)
        terms = _registry_adherence_terms(rows, "technical_d2l_v1")
        return terms, {
            "source": "legacy_registry",
            "raw_rows": len(rows),
            "terms": len(terms),
        }
    notebook_payload = json.loads(notebook_path.read_text(encoding="utf-8"))
    entries = notebook_payload.get("entries") if isinstance(notebook_payload, dict) else []
    if not isinstance(entries, list):
        raise ValueError(f"Notebook is missing entries list: {notebook_path}")
    notebook_rows = notebook_entries_to_term_rows(entries)
    accepted_gold = _load_gold_targets(
        conn,
        doc_id=doc_id,
        variants_path=gold_variants_path,
    )
    terms_by_source: dict[str, dict[str, Any]] = {}
    notebook_source_count = 0
    for row in notebook_rows:
        action = _injection_action(row)
        if action not in PACK_INJECTION_ACTIONS:
            continue
        forms = _target_forms_from_row(row, include_source_for_preserve=(action == "preserve"))
        if not forms:
            continue
        for surface in _source_surfaces_from_row(row):
            notebook_source_count += 1
            _merge_scope_term(
                terms_by_source,
                source=surface,
                forms=forms,
                origin="notebook",
            )
    gold_source_count = 0
    for item in accepted_gold.values():
        source = str(item.get("source_term") or "").strip()
        targets = [str(value) for value in item.get("targets") or [] if str(value).strip()]
        if not source or not targets:
            continue
        gold_source_count += 1
        _merge_scope_term(
            terms_by_source,
            source=source,
            forms=targets,
            origin="gold",
        )
    terms = [
        AdherenceTerm(
            term_id=f"scope:{key}",
            source_term=str(item["source_term"]),
            accepted_forms=tuple(item["forms"]),
            case_sensitive=False,
        )
        for key, item in sorted(terms_by_source.items())
        if item["forms"]
    ]
    return terms, {
        "source": "notebook_plus_gold",
        "notebook_path": str(notebook_path),
        "gold_variants_path": str(gold_variants_path),
        "notebook_entries": len(entries),
        "notebook_rows": len(notebook_rows),
        "notebook_source_surfaces_considered": notebook_source_count,
        "gold_sources_considered": gold_source_count,
        "terms_after_dedup": len(terms),
    }


def _merge_scope_term(
    terms_by_source: dict[str, dict[str, Any]],
    *,
    source: str,
    forms: list[str],
    origin: str,
) -> None:
    source = str(source or "").strip()
    if not source:
        return
    key = _normalize_source_key(source)
    entry = terms_by_source.setdefault(
        key,
        {"source_term": source, "forms": [], "origins": set()},
    )
    entry["origins"].add(origin)
    for form in forms:
        _append_unique_form(entry["forms"], form)


def _source_surfaces_from_row(row: dict[str, Any]) -> list[str]:
    surfaces = row.get("source_surfaces")
    if isinstance(surfaces, list) and surfaces:
        result = [str(item).strip() for item in surfaces if str(item).strip()]
    else:
        result = [str(row.get("source_term") or "").strip()]
    return _stable_unique_text(result)


def _target_forms_from_row(row: dict[str, Any], *, include_source_for_preserve: bool) -> list[str]:
    forms = [str(row.get("target_term") or "").strip()]
    variants = row.get("target_variants")
    if isinstance(variants, list):
        forms.extend(str(item).strip() for item in variants if str(item).strip())
    else:
        try:
            parsed = json.loads(str(variants or "[]"))
        except Exception:
            parsed = []
        if isinstance(parsed, list):
            forms.extend(str(item).strip() for item in parsed if str(item).strip())
    if include_source_for_preserve:
        forms.extend(_source_surfaces_from_row(row))
    return _stable_unique_text([item for item in forms if item])


def _injection_action(row: dict[str, Any]) -> str:
    audit = row.get("audit")
    if isinstance(audit, dict):
        return str(audit.get("injection_action") or "").strip()
    return ""


def _append_unique_form(values: list[str], value: str) -> None:
    key = _normalize_vi_key(value)
    if value and key not in {_normalize_vi_key(item) for item in values}:
        values.append(value)


def _stable_unique_text(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        key = _normalize_source_key(value)
        if value and key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _normalize_source_key(value: str) -> str:
    return normalize_surface(str(value or "")).casefold().strip()


def _normalize_vi_key(value: str) -> str:
    return normalize_surface(str(value or "")).casefold().strip()


def _run_t3_for_config(config_report: dict[str, Any], llm_client: LLMClient) -> tuple[dict[str, Any], dict[str, Any]]:
    updated_decisions = []
    validation_errors = Counter()
    fresh_calls = 0
    cache_hits = 0
    prompt_tokens_fresh = 0
    completion_tokens_fresh = 0
    cost_usd_fresh = 0.0
    samples = []
    for decision in config_report.get("decisions", []):
        if decision.get("resolved_by") == "t2_credit":
            updated_decisions.append(decision)
            continue
        result = llm_client.call(
            _build_t3_messages_for_decision(decision),
            response_format=T3_RESULT_SCHEMA,
            tag=f"BUILDER-V2:35.10:{T3_PROMPT_VERSION}:{decision['occ_id']}",
        )
        target_region = _target_region_text(decision)
        validated = validate_t3_payload(result.parsed_json, str(decision["occ_id"]), target_region)
        if validated.get("validation_error"):
            validation_errors[str(validated["validation_error"])] += 1
        if result.from_cache:
            cache_hits += 1
        else:
            fresh_calls += 1
            prompt_tokens_fresh += result.usage.prompt_tokens
            completion_tokens_fresh += result.usage.completion_tokens
            cost_usd_fresh += result.cost_usd
        merged = dict(decision)
        merged.update({
            "resolved_by": "t3_llm",
            "decision": "localized" if validated.get("found") else "not_rendered",
            "target_quote": validated.get("target_quote", ""),
            "left_context": validated.get("left_context", ""),
            "confidence": validated.get("confidence", "low"),
            "validation_error": validated.get("validation_error", ""),
            "t3_code_score": _score_locate_only_by_code(validated, decision),
            "t3_from_cache": result.from_cache,
            "t3_cache_key": result.cache_key,
            "t3_usage": asdict(result.usage),
            "t3_cost_usd": 0.0 if result.from_cache else result.cost_usd,
        })
        if len(samples) < 5 and validated.get("found"):
            samples.append({
                "occ_id": merged["occ_id"],
                "block_id": merged["block_id"],
                "source_term": merged["source_term"],
                "target_quote": merged["target_quote"],
                "confidence": merged["confidence"],
                "adherence_label": merged["t3_code_score"].get("adherence_label"),
            })
        updated_decisions.append(merged)

    updated_report = dict(config_report)
    updated_report["phase"] = "35.10_run"
    updated_report["decisions"] = updated_decisions
    updated_report["llm_calls"] = fresh_calls + cache_hits
    updated_report["llm_cost_usd"] = round(cost_usd_fresh, 12)
    updated_report["tier_split"] = _tier_split(updated_report)
    stats = {
        "calls": fresh_calls + cache_hits,
        "fresh_calls": fresh_calls,
        "cache_hits": cache_hits,
        "prompt_tokens_fresh": prompt_tokens_fresh,
        "completion_tokens_fresh": completion_tokens_fresh,
        "cost_usd_fresh": round(cost_usd_fresh, 12),
        "validation_errors": dict(validation_errors),
        "sample_marks": samples,
    }
    return updated_report, stats


def _run_t3_for_config_local(
    config_report: dict[str, Any],
    *,
    local_client: "LocalLMStudioT3Client",
    fallback_client: LLMClient | None,
    concurrency: int,
    use_gpt_fallback: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    decisions = list(config_report.get("decisions", []))
    updated_decisions: list[dict[str, Any] | None] = [None] * len(decisions)
    residual_jobs: list[tuple[int, dict[str, Any]]] = []
    for index, decision in enumerate(decisions):
        if decision.get("resolved_by") == "t2_credit":
            updated_decisions[index] = decision
        else:
            residual_jobs.append((index, decision))

    validation_errors = Counter()
    local_validation_errors = Counter()
    fallback_validation_errors = Counter()
    local_fresh_calls = 0
    local_cache_hits = 0
    local_prompt_tokens = 0
    local_completion_tokens = 0
    fallback_fresh_calls = 0
    fallback_cache_hits = 0
    fallback_prompt_tokens = 0
    fallback_completion_tokens = 0
    fallback_cost_usd = 0.0
    samples = []

    def run_local(job: tuple[int, dict[str, Any]]) -> tuple[int, dict[str, Any]]:
        index, decision = job
        return index, local_client.call(_build_t3_messages_for_decision(decision))

    max_workers = max(1, int(concurrency))
    local_results: dict[int, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(run_local, job) for job in residual_jobs]
        for future in as_completed(futures):
            index, result = future.result()
            local_results[index] = result

    for index, decision in residual_jobs:
        result = local_results[index]
        target_region = _target_region_text(decision)
        validated = validate_t3_payload(result["parsed_json"], str(decision["occ_id"]), target_region)
        validation_error = str(validated.get("validation_error") or "")
        used_fallback = False
        fallback_result = None
        if validation_error:
            local_validation_errors[validation_error] += 1
            if use_gpt_fallback:
                if fallback_client is None:
                    raise RuntimeError("GPT fallback requested but fallback_client is not configured")
                fallback_result = fallback_client.call(
                    _build_t3_messages_for_decision(decision),
                    response_format=T3_RESULT_SCHEMA,
                    tag=f"BUILDER-V2:35.10:fallback:{T3_PROMPT_VERSION}:{decision['occ_id']}",
                )
                validated = validate_t3_payload(
                    fallback_result.parsed_json,
                    str(decision["occ_id"]),
                    target_region,
                )
                validation_error = str(validated.get("validation_error") or "")
                used_fallback = True
                if validation_error:
                    fallback_validation_errors[validation_error] += 1

        if validation_error:
            validation_errors[validation_error] += 1

        if result["from_cache"]:
            local_cache_hits += 1
        else:
            local_fresh_calls += 1
            local_prompt_tokens += int((result.get("usage") or {}).get("prompt_tokens") or 0)
            local_completion_tokens += int((result.get("usage") or {}).get("completion_tokens") or 0)

        if fallback_result is not None:
            if fallback_result.from_cache:
                fallback_cache_hits += 1
            else:
                fallback_fresh_calls += 1
                fallback_prompt_tokens += fallback_result.usage.prompt_tokens
                fallback_completion_tokens += fallback_result.usage.completion_tokens
                fallback_cost_usd += fallback_result.cost_usd

        merged = dict(decision)
        merged.update({
            "resolved_by": "t3_llm",
            "decision": "localized" if validated.get("found") else "not_rendered",
            "target_quote": validated.get("target_quote", ""),
            "left_context": validated.get("left_context", ""),
            "confidence": validated.get("confidence", "low"),
            "validation_error": validated.get("validation_error", ""),
            "t3_code_score": _score_locate_only_by_code(validated, decision),
            "t3_backend": "gpt_fallback" if used_fallback else "local_lmstudio",
            "t3_local_model": local_client.model,
            "t3_from_cache": bool(result["from_cache"]),
            "t3_cache_key": result["cache_key"],
            "t3_usage": result.get("usage") or {},
            "t3_cost_usd": 0.0,
        })
        if used_fallback and fallback_result is not None:
            merged.update({
                "t3_fallback_from_cache": fallback_result.from_cache,
                "t3_fallback_cache_key": fallback_result.cache_key,
                "t3_fallback_usage": asdict(fallback_result.usage),
                "t3_fallback_cost_usd": 0.0 if fallback_result.from_cache else fallback_result.cost_usd,
                "t3_cost_usd": 0.0 if fallback_result.from_cache else fallback_result.cost_usd,
            })
        if len(samples) < 5 and validated.get("found"):
            samples.append({
                "occ_id": merged["occ_id"],
                "block_id": merged["block_id"],
                "source_term": merged["source_term"],
                "target_quote": merged["target_quote"],
                "confidence": merged["confidence"],
                "adherence_label": merged["t3_code_score"].get("adherence_label"),
                "backend": merged["t3_backend"],
            })
        updated_decisions[index] = merged

    final_decisions = [item for item in updated_decisions if item is not None]
    updated_report = dict(config_report)
    updated_report["phase"] = "35.10_run"
    updated_report["decisions"] = final_decisions
    updated_report["llm_calls"] = len(residual_jobs)
    updated_report["llm_cost_usd"] = round(fallback_cost_usd, 12)
    updated_report["tier_split"] = _tier_split(updated_report)
    stats = {
        "calls": len(residual_jobs),
        "local_calls": len(residual_jobs),
        "local_fresh_calls": local_fresh_calls,
        "local_cache_hits": local_cache_hits,
        "fresh_calls": local_fresh_calls,
        "cache_hits": local_cache_hits,
        "prompt_tokens_fresh": local_prompt_tokens,
        "completion_tokens_fresh": local_completion_tokens,
        "cost_usd_fresh": round(fallback_cost_usd, 12),
        "validation_errors": dict(validation_errors),
        "local_validation_errors": dict(local_validation_errors),
        "gpt_fallback_calls": fallback_fresh_calls + fallback_cache_hits,
        "gpt_fallback_fresh_calls": fallback_fresh_calls,
        "gpt_fallback_cache_hits": fallback_cache_hits,
        "gpt_fallback_prompt_tokens_fresh": fallback_prompt_tokens,
        "gpt_fallback_completion_tokens_fresh": fallback_completion_tokens,
        "gpt_fallback_cost_usd_fresh": round(fallback_cost_usd, 12),
        "gpt_fallback_validation_errors": dict(fallback_validation_errors),
        "sample_marks": samples,
    }
    return updated_report, stats


class LocalLMStudioT3Client:
    def __init__(
        self,
        *,
        endpoint: str,
        model: str,
        cache_path: Path,
        timeout_sec: int,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.cache_path = cache_path
        self.timeout_sec = timeout_sec
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def call(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        request_body = {
            "model": self.model,
            "messages": messages,
            **_local_t3_request_profile(self.model),
        }
        request_json = json.dumps(request_body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        cache_key = sha256(request_json.encode("utf-8")).hexdigest()
        cached = self._load_cached(cache_key)
        if cached is not None:
            return cached
        started = time.perf_counter()
        response = requests.post(
            f"{self.endpoint}/v1/chat/completions",
            json=request_body,
            timeout=self.timeout_sec,
        )
        response.raise_for_status()
        latency_ms = int((time.perf_counter() - started) * 1000)
        payload = response.json()
        choice = (payload.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        content = str(message.get("content") or "")
        parsed_json, json_error = _parse_json_content(content)
        usage = _openai_compatible_usage(payload.get("usage") or {})
        result = {
            "text": content,
            "parsed_json": parsed_json,
            "json_error": json_error,
            "usage": usage,
            "latency_ms": latency_ms,
            "from_cache": False,
            "cache_key": cache_key,
            "finish_reason": choice.get("finish_reason") or "",
            "model_echo": payload.get("model") or "",
        }
        self._store_cached(cache_key, request_json, result)
        return result

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.cache_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS local_lmstudio_t3_cache (
                    cache_key TEXT PRIMARY KEY,
                    model TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    def _load_cached(self, cache_key: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT response_json FROM local_lmstudio_t3_cache WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
        if row is None:
            return None
        result = json.loads(str(row["response_json"]))
        result["from_cache"] = True
        return result

    def _store_cached(self, cache_key: str, request_json: str, result: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO local_lmstudio_t3_cache (
                    cache_key, model, request_json, response_json
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    cache_key,
                    self.model,
                    request_json,
                    json.dumps(result, ensure_ascii=False, sort_keys=True),
                ),
            )


def _local_t3_request_profile(model: str) -> dict[str, Any]:
    return {
        "temperature": 0,
        "top_p": 1.0,
        "top_k": 0,
        "min_p": 0.0,
        "seed": 20260612,
        "repeat_penalty": 1.0,
        "max_tokens": 512,
        "stream": False,
        "response_format": T3_RESULT_SCHEMA,
        "reasoning_effort": "none",
    }


def _parse_json_content(content: str) -> tuple[Any | None, str]:
    if not str(content or "").strip():
        return None, "empty_content"
    try:
        return json.loads(content), ""
    except json.JSONDecodeError as exc:
        return None, f"json_decode_error:{exc.msg}"


def _openai_compatible_usage(usage: dict[str, Any]) -> dict[str, int]:
    completion_details = usage.get("completion_tokens_details") or {}
    prompt_details = usage.get("prompt_tokens_details") or {}
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    return {
        "prompt_tokens": prompt,
        "cached_tokens": int(prompt_details.get("cached_tokens") or 0),
        "completion_tokens": completion,
        "reasoning_tokens": int(completion_details.get("reasoning_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or (prompt + completion)),
    }


def _build_t3_messages_for_decision(decision: dict[str, Any]) -> list[dict[str, str]]:
    target_region = _target_region_text(decision)
    item = AdjudicationInput(
        occurrence_id=str(decision["occ_id"]),
        source_term=str(decision["source_term"]),
        occurrence_index=_occurrence_index_in_source_sentence(decision),
        source_sentence=str(decision["source_sentence"]),
        target_region=target_region,
    )
    return build_t3_messages(item)


def _tier_split(config_report: dict[str, Any]) -> dict[str, Any]:
    decisions = list(config_report.get("decisions", []))
    counts = Counter()
    reasons = Counter()
    validation = Counter()
    for decision in decisions:
        resolved_by = str(decision.get("resolved_by") or "")
        if resolved_by == "t2_credit":
            counts["cascade_t2"] += 1
        elif resolved_by == "t3_llm" and decision.get("target_quote"):
            counts["cascade_t3_llm"] += 1
        else:
            counts["t3_residual_estimate"] += 1
        reason = str(decision.get("escalate_reason") or "")
        if reason:
            reasons[reason] += 1
        error = str(decision.get("validation_error") or "")
        if error:
            validation[error] += 1
    denominator = len(decisions)
    return {
        "denominator": denominator,
        "counts": dict(counts),
        "shares": {
            key: (value / denominator if denominator else 0.0)
            for key, value in sorted(counts.items())
        },
        "escalate_reasons": dict(sorted(reasons.items())),
        "validation_errors": dict(sorted(validation.items())),
    }


def _sum_estimates(estimates: dict[str, dict[str, Any]]) -> dict[str, Any]:
    fields = [
        "calls",
        "prompt_tokens_estimate",
        "output_tokens_cap",
        "cost_cap_usd",
        "input_cost_estimate_usd",
        "output_cost_cap_usd",
    ]
    result = {field: 0 for field in fields}
    result["prompt_tokens_max_single_call"] = 0
    for item in estimates.values():
        for field in fields:
            result[field] += item.get(field, 0)
        result["prompt_tokens_max_single_call"] = max(
            result["prompt_tokens_max_single_call"],
            item.get("prompt_tokens_max_single_call", 0),
        )
    for field in ["cost_cap_usd", "input_cost_estimate_usd", "output_cost_cap_usd"]:
        result[field] = round(float(result[field]), 12)
    return result


def _sum_run_stats(stats: dict[str, dict[str, Any]]) -> dict[str, Any]:
    result = Counter()
    validation = Counter()
    local_validation = Counter()
    fallback_validation = Counter()
    int_fields = [
        "calls",
        "fresh_calls",
        "cache_hits",
        "prompt_tokens_fresh",
        "completion_tokens_fresh",
        "local_calls",
        "local_fresh_calls",
        "local_cache_hits",
        "gpt_fallback_calls",
        "gpt_fallback_fresh_calls",
        "gpt_fallback_cache_hits",
        "gpt_fallback_prompt_tokens_fresh",
        "gpt_fallback_completion_tokens_fresh",
    ]
    float_fields = [
        "cost_usd_fresh",
        "gpt_fallback_cost_usd_fresh",
    ]
    for item in stats.values():
        for key in int_fields:
            result[key] += int(item.get(key) or 0)
        for key in float_fields:
            result[key] += float(item.get(key) or 0.0)
        validation.update(item.get("validation_errors") or {})
        local_validation.update(item.get("local_validation_errors") or {})
        fallback_validation.update(item.get("gpt_fallback_validation_errors") or {})
    return {
        **dict(result),
        "cost_usd_fresh": round(float(result["cost_usd_fresh"]), 12),
        "gpt_fallback_cost_usd_fresh": round(float(result["gpt_fallback_cost_usd_fresh"]), 12),
        "validation_errors": dict(validation),
        "local_validation_errors": dict(local_validation),
        "gpt_fallback_validation_errors": dict(fallback_validation),
    }


def _base_report(
    *,
    mode: str,
    experiment_id: str,
    chapter: str,
    configs: list[str],
    endpoint_report: dict[str, Any],
    frozen_hash_before: str,
    frozen_hash_after: str,
    workdb_hash_before: str,
    workdb_hash_after: str,
    elapsed_seconds: float,
) -> dict[str, Any]:
    return {
        "task": "BUILDER-V2 §35.10",
        "mode": mode,
        "experiment_id": experiment_id,
        "chapter": chapter,
        "configs": configs,
        "embedding": endpoint_report,
        "frozen_db_sha256_before": frozen_hash_before,
        "frozen_db_sha256_after": frozen_hash_after,
        "frozen_db_expected_first16": FROZEN_DB_SHA_FIRST16,
        "frozen_db_matches_expected": frozen_hash_before[:16].upper() == FROZEN_DB_SHA_FIRST16,
        "workdb_sha256_before": workdb_hash_before,
        "workdb_sha256_after": workdb_hash_after,
        "workdb_unchanged": workdb_hash_before == workdb_hash_after,
        "elapsed_seconds": round(elapsed_seconds, 3),
    }


def _llm_config_dict(config: LLMConfig) -> dict[str, Any]:
    return {
        "model": config.model,
        "temperature": config.temperature,
        "seed": config.seed,
        "reasoning_effort": config.reasoning_effort,
        "verbosity": config.verbosity,
        "max_output_tokens": config.max_output_tokens,
        "prompt_token_cap": config.prompt_token_cap,
        "pricing": dict(config.pricing),
    }


def _console_summary(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "out": "data/reports/exp_s0s1_builderv2_v1",
        "mode": report.get("mode"),
        "embedding_status": (report.get("embedding") or {}).get("status"),
        "t3_backend": report.get("t3_backend"),
        "local_t3": report.get("local_t3"),
        "t3_estimate_total": report.get("t3_estimate_total"),
        "t3_estimate_by_config": report.get("t3_estimate_by_config"),
        "t3_run_total": report.get("t3_run_total"),
        "over_500_calls": report.get("t3_estimate_over_500_calls"),
        "workdb_unchanged": report.get("workdb_unchanged"),
        "frozen_db_matches_expected": report.get("frozen_db_matches_expected"),
    }


def _connect_readonly(path: Path) -> sqlite3.Connection:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _single_model_config(spec: str) -> EmbeddingModelConfig:
    models = parse_model_specs(spec)
    if len(models) != 1:
        raise SystemExit("§35.10 accepts exactly one embedding model; use bge-m3 only.")
    return models[0]


def _parse_configs(value: str) -> list[str]:
    configs = [item.strip().upper() for item in value.split(",") if item.strip()]
    if not configs:
        raise SystemExit("--configs must include at least one config")
    return configs


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _ensure_openai_key() -> str:
    if os.getenv("OPENAI_API_KEY"):
        return "env:OPENAI_API_KEY"
    candidates = [
        Path("OPENAI-KEY-2.txt"),
        Path("OPENAI-KEY-1.txt"),
        Path("../OPENAI-KEY-2.txt"),
        Path("../OPENAI-KEY-1.txt"),
        Path("THESIS_RUNTIME_TOOL/OPENAI-KEY-2.txt"),
        Path("THESIS_RUNTIME_TOOL/OPENAI-KEY-1.txt"),
    ]
    for path in candidates:
        if not path.exists():
            continue
        key = path.read_text(encoding="utf-8").strip()
        if key:
            os.environ["OPENAI_API_KEY"] = key
            return f"file:{path.name}"
    raise RuntimeError("OPENAI_API_KEY is not set and no OPENAI-KEY-*.txt fallback was found")


if __name__ == "__main__":
    raise SystemExit(main())
