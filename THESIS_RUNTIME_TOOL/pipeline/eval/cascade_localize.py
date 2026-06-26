from __future__ import annotations

import csv
import hashlib
import html
import json
import sqlite3
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from math import sqrt
from pathlib import Path
from typing import Any, Iterable

from pipeline.eval.d2l_translate_score import (
    _load_registry_rows,
    _load_translations,
    _registry_adherence_terms,
    _resolve_chapters,
    _scope_blocks,
)
from pipeline.eval.occurrence_adherence import (
    AdherenceTerm,
    _allocate_source,
    _allocate_target,
    _form_specs,
)
from pipeline.eval.region_align import (
    DEFAULT_MODEL_CONFIGS,
    EmbeddingCacheClient,
    EmbeddingModelConfig,
    EmbeddingModelIdentity,
    containing_unit,
    preflight_embedding_model,
    split_sentences,
    span_in_ranges,
    top_k_target_sentences,
    union_ranges,
)
from pipeline.eval.llm_adjudicator import (
    PROMPT_VERSION as T3_PROMPT_VERSION,
    RESULT_SCHEMA as T3_RESULT_SCHEMA,
    AdjudicationInput,
    build_messages as build_t3_messages,
    validate_payload as validate_t3_payload,
)
from pipeline.eval.surface_match import find_spans, normalize_surface
from pipeline.translate.profiles import get_profile


DEFAULT_CHAPTERS = [
    "d2l_introduction",
    "d2l_preliminaries",
    "d2l_linear_networks",
    "d2l_multilayer_perceptrons",
]
FROZEN_DB_SHA_FIRST16 = "DA0F687894090D43"
DEFAULT_MARGIN_THRESHOLD = 0.20


@dataclass(frozen=True)
class CandidateSpan:
    start: int
    end: int
    surface: str
    role: str
    term_ids: tuple[str, ...]
    form: str


@dataclass(frozen=True)
class CascadeOccurrence:
    occ_id: str
    config: str
    block_id: str
    chapter_id: str
    source_term: str
    term_id: str
    source_start: int
    source_end: int
    source_surface: str
    source_sentence_idx: int
    source_sentence: str
    source_text: str
    target_text: str


@dataclass(frozen=True)
class T1Region:
    status: str
    sentence_indices: tuple[int, ...]
    ranges: tuple[tuple[int, int], ...]
    scores: tuple[float, ...]
    margin: float | None
    reason: str


@dataclass(frozen=True)
class TierDecision:
    occ_id: str
    config: str
    block_id: str
    chapter_id: str
    source_term: str
    term_id: str
    source_start: int
    source_end: int
    source_surface: str
    source_sentence_idx: int
    source_sentence: str
    source_text: str
    target_text: str
    resolved_by: str
    decision: str
    target_start: int | None = None
    target_end: int | None = None
    target_surface: str = ""
    matched_form_rank: str = ""
    escalate_reason: str = ""
    masquerade_suspect: bool = False
    accepted_forms: tuple[str, ...] = ()
    t1: dict[str, Any] | None = None
    candidates: tuple[dict[str, Any], ...] = ()


def run_cascade_localize(
    *,
    db_path: str | Path,
    experiment_id: str,
    configs: Iterable[str],
    chapters: list[str] | None,
    embed_endpoint: str,
    model_config: EmbeddingModelConfig | None = None,
    cache_dir: str | Path = "data/eval/embed_cache/cascade",
    margin_threshold: float = DEFAULT_MARGIN_THRESHOLD,
    tier_max: int = 2,
    profile_name: str = "technical_d2l_v1",
    doc_id: str = "d2l",
) -> dict[str, Any]:
    if tier_max > 2:
        raise RuntimeError(
            "Tier 3 GPT is prompt-review gated for EV-D2L-10. "
            "Run --tier-max 2 first and get user approval before any LLM call."
        )
    started = time.perf_counter()
    config_list = [str(item).upper() for item in configs]
    model_cfg = model_config or DEFAULT_MODEL_CONFIGS["bge-m3"]
    identity = preflight_embedding_model(endpoint=embed_endpoint, config=model_cfg)
    if identity.status != "available":
        raise RuntimeError(f"T1 embedding model unavailable: {identity.skipped_with_reason}")
    client = EmbeddingCacheClient(
        endpoint=embed_endpoint,
        model=identity.endpoint_model,
        model_alias=identity.alias,
        model_version=identity.cache_model_version(),
        query_prefix=identity.query_prefix,
        passage_prefix=identity.passage_prefix,
        prefix_profile=f"q={identity.query_prefix!r};p={identity.passage_prefix!r}",
        cache_dir=Path(cache_dir) / identity.alias,
    )

    occurrences_by_config: dict[str, list[tuple[CascadeOccurrence, list[CandidateSpan], AdherenceTerm]]] = {}
    db_hash = _sha256_file(Path(db_path))
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        profile = get_profile(profile_name)
        resolved_chapters = _resolve_chapters(conn, doc_id, chapters or DEFAULT_CHAPTERS)
        blocks = _scope_blocks(conn, doc_id, resolved_chapters, profile)
        registry_rows = _load_registry_rows(conn, doc_id)
        terms = _registry_adherence_terms(registry_rows, profile.name)
        for config in config_list:
            translations = _load_translations(conn, experiment_id, config)
            occurrences_by_config[config] = enumerate_occurrences(
                blocks=blocks,
                translations=translations,
                terms=terms,
                config=config,
            )
    finally:
        conn.close()

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
        for occurrence, target_spans, term in items:
            t1 = t1_by_occ[occurrence.occ_id]
            decisions.append(run_t2_rules(occurrence, term, target_spans, t1, terms))
        reports[config] = _config_report(
            config=config,
            decisions=decisions,
            occurrences=len(items),
            db_hash=db_hash,
            identity=identity,
            margin_threshold=margin_threshold,
            elapsed_seconds=time.perf_counter() - started,
            embed_stats=client.stats(),
        )

    return {
        "task": "EV-D2L-10",
        "mode": "tier_max_2_no_llm",
        "experiment_id": experiment_id,
        "profile": profile_name,
        "doc_id": doc_id,
        "configs": config_list,
        "chapters": chapters or DEFAULT_CHAPTERS,
        "frozen_db_sha256": db_hash,
        "frozen_db_sha256_first16": db_hash[:16].upper(),
        "frozen_db_expected_first16": FROZEN_DB_SHA_FIRST16,
        "frozen_db_matches_expected": db_hash[:16].upper() == FROZEN_DB_SHA_FIRST16,
        "model": _identity_to_dict(identity),
        "reports": reports,
    }


def run_t3_pilot(
    *,
    db_path: str | Path,
    experiment_id: str,
    configs: Iterable[str],
    chapters: list[str] | None,
    embed_endpoint: str,
    model_config: EmbeddingModelConfig,
    cache_dir: str | Path,
    margin_threshold: float,
    gold_reuse_paths: list[str | Path],
    llm_client: Any,
    limit: int,
    locate_only: bool = False,
    profile_name: str = "technical_d2l_v1",
    doc_id: str = "d2l",
) -> dict[str, Any]:
    """Run the review-gated T3 GPT pilot on reused human-labeled residual rows only."""

    if limit <= 0:
        raise ValueError("T3 pilot requires a positive --limit")
    reusable = _load_reusable_gold(gold_reuse_paths)
    tier2_report = run_cascade_localize(
        db_path=db_path,
        experiment_id=experiment_id,
        configs=configs,
        chapters=chapters,
        embed_endpoint=embed_endpoint,
        model_config=model_config,
        cache_dir=cache_dir,
        margin_threshold=margin_threshold,
        tier_max=2,
        profile_name=profile_name,
        doc_id=doc_id,
    )

    selected: list[tuple[dict[str, Any], dict[str, Any]]] = []
    reused_labeled_total = 0
    residual_total = 0
    for config in tier2_report["configs"]:
        for decision in tier2_report["reports"][config]["decisions"]:
            if decision.get("resolved_by") == "t2_credit":
                continue
            residual_total += 1
            reused = reusable.get(_reuse_key_from_decision(decision))
            if not reused:
                continue
            reused_labeled_total += 1
            if len(selected) < limit:
                selected.append((decision, reused))

    records: list[dict[str, Any]] = []
    confidence_correct: dict[str, Counter[str]] = defaultdict(Counter)
    status_counts: Counter[str] = Counter()
    adherence_counts: Counter[str] = Counter()
    adherence_by_config: dict[str, Counter[str]] = defaultdict(Counter)
    fresh_calls = 0
    cache_hits = 0
    prompt_tokens_all = 0
    completion_tokens_all = 0
    prompt_tokens_fresh = 0
    completion_tokens_fresh = 0
    cost_usd_fresh = 0.0
    cost_usd_cached_recorded = 0.0

    for decision, reused in selected:
        target_region = _target_region_text(decision)
        candidate_quotes = tuple(
            dict.fromkeys(
                str(item.get("surface") or "")
                for item in decision.get("candidates") or []
                if str(item.get("surface") or "")
            )
        )
        item = AdjudicationInput(
            occurrence_id=str(decision["occ_id"]),
            source_term=str(decision["source_term"]),
            occurrence_index=_occurrence_index_in_source_sentence(decision),
            source_sentence=str(decision["source_sentence"]),
            target_region=target_region,
        )
        result = llm_client.call(
            build_t3_messages(item),
            response_format=T3_RESULT_SCHEMA,
            tag=f"EV-D2L-10:T3:{T3_PROMPT_VERSION}:{decision['occ_id']}",
        )
        validated = validate_t3_payload(result.parsed_json, item.occurrence_id, target_region)
        if locate_only:
            code_score = _score_locate_only_by_code(validated, decision)
            scored = _score_locate_only_against_reused_gold(validated, reused)
            status_counts["found" if validated["found"] else "not_found"] += 1
            adherence_counts[str(code_score["adherence_label"])] += 1
            adherence_by_config[str(decision["config"])][str(code_score["adherence_label"])] += 1
        else:
            code_score = {}
            scored = _score_t3_against_reused_gold(validated, reused)
            status_counts[str(validated.get("status") or "")] += 1
        confidence_correct[str(validated["confidence"])][str(scored["correct"]).lower()] += 1
        prompt_tokens_all += result.usage.prompt_tokens
        completion_tokens_all += result.usage.completion_tokens
        cost_usd_cached_recorded += result.cost_usd
        if result.from_cache:
            cache_hits += 1
        else:
            fresh_calls += 1
            prompt_tokens_fresh += result.usage.prompt_tokens
            completion_tokens_fresh += result.usage.completion_tokens
            cost_usd_fresh += result.cost_usd
        records.append({
            "occ_id": decision["occ_id"],
            "config": decision["config"],
            "block_id": decision["block_id"],
            "source_term": decision["source_term"],
            "source_span": _format_span(
                int(decision["source_start"]),
                int(decision["source_end"]),
                str(decision["source_surface"]),
            ),
            "source_sentence": decision["source_sentence"],
            "target_region": target_region,
            "candidate_quotes": list(candidate_quotes),
            "escalate_reason": decision.get("escalate_reason", ""),
            "masquerade_suspect": bool(decision.get("masquerade_suspect")),
            "gold": reused,
            "llm": validated,
            "code_score": code_score,
            "json_error": result.json_error,
            "correct": scored["correct"],
            "correct_reason": scored["reason"],
            "from_cache": result.from_cache,
            "cache_key": result.cache_key,
            "usage": asdict(result.usage),
            "cost_usd": result.cost_usd if not result.from_cache else 0.0,
            "recorded_cached_cost_usd": result.cost_usd if result.from_cache else 0.0,
            "latency_ms": result.latency_ms,
        })

    correct = sum(1 for item in records if item["correct"])
    return {
        "task": "EV-D2L-10",
        "mode": "tier3_locate_only_reused_labeled_only" if locate_only else "tier3_pilot_reused_labeled_only",
        "prompt_version": T3_PROMPT_VERSION,
        "response_schema": "locate_only_v4" if locate_only else "occurrence_adjudication",
        "experiment_id": experiment_id,
        "profile": profile_name,
        "doc_id": doc_id,
        "configs": tier2_report["configs"],
        "chapters": tier2_report["chapters"],
        "frozen_db_sha256": tier2_report["frozen_db_sha256"],
        "frozen_db_sha256_first16": tier2_report["frozen_db_sha256_first16"],
        "frozen_db_expected_first16": tier2_report["frozen_db_expected_first16"],
        "frozen_db_matches_expected": tier2_report["frozen_db_matches_expected"],
        "t1_model": tier2_report["model"],
        "tier2_residual_total": residual_total,
        "reused_labeled_total": reused_labeled_total,
        "limit": limit,
        "attempted": len(records),
        "correct": correct,
        "accuracy": _ratio(correct, len(records)),
        "status_counts": dict(status_counts),
        "adherence_counts": dict(adherence_counts),
        "adherence_by_config": {
            config: {
                **dict(counts),
                "adherence_rate": _ratio(
                    counts["adherent"],
                    counts["adherent"] + counts["off_glossary"] + counts["not_rendered"],
                ),
                "off_glossary_pct": _ratio(
                    counts["off_glossary"],
                    counts["adherent"] + counts["off_glossary"] + counts["not_rendered"],
                ),
            }
            for config, counts in sorted(adherence_by_config.items())
        },
        "adherence_rate": _ratio(
            adherence_counts["adherent"],
            adherence_counts["adherent"] + adherence_counts["off_glossary"] + adherence_counts["not_rendered"],
        ) if locate_only else None,
        "off_glossary_pct": _ratio(
            adherence_counts["off_glossary"],
            adherence_counts["adherent"] + adherence_counts["off_glossary"] + adherence_counts["not_rendered"],
        ) if locate_only else None,
        "confidence_x_correct": {
            key: dict(value) for key, value in sorted(confidence_correct.items())
        },
        "llm": {
            "model": llm_client.config.model,
            "temperature": llm_client.config.temperature,
            "seed": llm_client.config.seed,
            "reasoning_effort": llm_client.config.reasoning_effort,
            "verbosity": llm_client.config.verbosity,
            "max_output_tokens": llm_client.config.max_output_tokens,
            "daily_token_cap": llm_client.config.daily_token_cap,
            "prompt_token_cap": llm_client.config.prompt_token_cap,
            "calls": len(records),
            "fresh_calls": fresh_calls,
            "cache_hits": cache_hits,
            "prompt_tokens_all_records": prompt_tokens_all,
            "completion_tokens_all_records": completion_tokens_all,
            "prompt_tokens_fresh": prompt_tokens_fresh,
            "completion_tokens_fresh": completion_tokens_fresh,
            "cost_usd_fresh": round(cost_usd_fresh, 12),
            "cached_recorded_cost_usd": round(cost_usd_cached_recorded, 12),
            "usage_today_after": llm_client.get_usage_today(),
        },
        "records": records,
        "scope_statement": (
            "Part A/R2 pilot only: T3 GPT runs only on reused human-labeled residual rows "
            "and is capped by --limit. LLM locates only when --locate-only is set; code scores. "
            "No DB writes and no headline metric changes."
        ),
    }


def enumerate_occurrences(
    *,
    blocks: Iterable[Any],
    translations: dict[str, str],
    terms: list[AdherenceTerm],
    config: str,
) -> list[tuple[CascadeOccurrence, list[CandidateSpan], AdherenceTerm]]:
    block_list = list(blocks)
    term_by_id = {term.term_id: term for term in terms}
    source_by_block = _allocate_source(block_list, terms)
    form_specs = _form_specs(terms)
    result: list[tuple[CascadeOccurrence, list[CandidateSpan], AdherenceTerm]] = []
    for block in block_list:
        active_source = {
            term_id: spans
            for term_id, spans in source_by_block.get(block.block_id, {}).items()
            if spans and term_id in term_by_id
        }
        if not active_source:
            continue
        target_text = translations.get(block.block_id, "")
        allocation, owner_meta = _allocate_target(
            target_text,
            form_specs,
            active_term_ids=set(active_source),
        )
        target_spans = _target_spans(allocation, owner_meta, target_text)
        for term_id, source_spans in sorted(active_source.items()):
            term = term_by_id[term_id]
            for source_index, span in enumerate(sorted(source_spans, key=lambda item: (item.start, item.end))):
                sentence_idx, source_sentence = _source_sentence_info(block.text, int(span.start), int(span.end))
                occurrence = CascadeOccurrence(
                    occ_id=f"{config}:{block.block_id}:{term_id}:{source_index}",
                    config=config,
                    block_id=str(block.block_id),
                    chapter_id=str(block.chapter_id),
                    source_term=term.source_term,
                    term_id=term_id,
                    source_start=int(span.start),
                    source_end=int(span.end),
                    source_surface=str(block.text[span.start:span.end]),
                    source_sentence_idx=sentence_idx,
                    source_sentence=source_sentence,
                    source_text=str(block.text),
                    target_text=target_text,
                )
                result.append((occurrence, target_spans, term))
    return result


def run_t1_region(
    occurrence: CascadeOccurrence,
    *,
    client: EmbeddingCacheClient,
    margin_threshold: float = DEFAULT_MARGIN_THRESHOLD,
) -> T1Region:
    target_units = split_sentences(occurrence.target_text)
    if not target_units:
        return T1Region("empty_target", (), (), (), None, "no_target_sentence")
    if len(target_units) == 1:
        unit = target_units[0]
        return T1Region(
            "single_sentence",
            (0,),
            ((unit.start, unit.end),),
            (),
            None,
            "single_target_sentence",
        )
    ranked = top_k_target_sentences(occurrence.source_sentence, target_units, k=2, client=client)
    if not ranked:
        return T1Region("empty_rank", (), (), (), None, "embedding_returned_no_rank")
    margin = None
    selected: list[RankedTextUnit] = [ranked[0]]
    if len(ranked) > 1:
        margin = ranked[0].score - ranked[1].score
        if margin < margin_threshold:
            selected.append(ranked[1])
    ranges = tuple(union_ranges(item.unit for item in selected))
    return T1Region(
        "ranked",
        tuple(item.index for item in selected),
        ranges,
        tuple(round(item.score, 6) for item in selected),
        None if margin is None else round(margin, 6),
        "top1" if len(selected) == 1 else "low_margin_top2",
    )


def run_t1_regions_for_block(
    occurrences: list[CascadeOccurrence],
    *,
    client: EmbeddingCacheClient,
    margin_threshold: float = DEFAULT_MARGIN_THRESHOLD,
) -> dict[str, T1Region]:
    if not occurrences:
        return {}
    target_units = split_sentences(occurrences[0].target_text)
    if not target_units:
        return {
            occurrence.occ_id: T1Region("empty_target", (), (), (), None, "no_target_sentence")
            for occurrence in occurrences
        }
    if len(target_units) == 1:
        unit = target_units[0]
        region = T1Region(
            "single_sentence",
            (0,),
            ((unit.start, unit.end),),
            (),
            None,
            "single_target_sentence",
        )
        return {occurrence.occ_id: region for occurrence in occurrences}

    source_by_key: dict[tuple[int, str], list[CascadeOccurrence]] = defaultdict(list)
    for occurrence in occurrences:
        source_by_key[(occurrence.source_sentence_idx, occurrence.source_sentence)].append(occurrence)
    source_keys = list(source_by_key)
    source_inputs = [f"{client.query_prefix}{text}" for _, text in source_keys]
    target_inputs = [f"{client.passage_prefix}{unit.text}" for unit in target_units]
    vectors = client.embed([*source_inputs, *target_inputs])
    source_vectors = vectors[:len(source_inputs)]
    target_vectors = vectors[len(source_inputs):]

    region_by_source: dict[tuple[int, str], T1Region] = {}
    for key, source_vector in zip(source_keys, source_vectors, strict=True):
        ranked = sorted(
            [
                (index, target_units[index], _cosine(source_vector, target_vector))
                for index, target_vector in enumerate(target_vectors)
            ],
            key=lambda item: (-item[2], item[0]),
        )[:2]
        selected = [ranked[0]]
        margin = None
        if len(ranked) > 1:
            margin = ranked[0][2] - ranked[1][2]
            if margin < margin_threshold:
                selected.append(ranked[1])
        region_by_source[key] = T1Region(
            "ranked",
            tuple(item[0] for item in selected),
            tuple(union_ranges(item[1] for item in selected)),
            tuple(round(item[2], 6) for item in selected),
            None if margin is None else round(margin, 6),
            "top1" if len(selected) == 1 else "low_margin_top2",
        )
    return {
        occurrence.occ_id: region_by_source[(occurrence.source_sentence_idx, occurrence.source_sentence)]
        for occurrence in occurrences
    }


def _prewarm_t1_embeddings(
    items: list[tuple[CascadeOccurrence, list[CandidateSpan], AdherenceTerm]],
    client: EmbeddingCacheClient,
) -> None:
    texts: list[str] = []
    seen: set[str] = set()
    for occurrence, _target_spans, _term in items:
        query = f"{client.query_prefix}{occurrence.source_sentence}"
        if query not in seen:
            seen.add(query)
            texts.append(query)
        for unit in split_sentences(occurrence.target_text):
            passage = f"{client.passage_prefix}{unit.text}"
            if passage not in seen:
                seen.add(passage)
                texts.append(passage)
    if texts:
        client.embed(texts)


def run_t2_rules(
    occurrence: CascadeOccurrence,
    term: AdherenceTerm,
    target_spans: list[CandidateSpan],
    t1: T1Region,
    all_terms: list[AdherenceTerm],
) -> TierDecision:
    in_region = [
        span for span in target_spans
        if span_in_ranges(span.start, span.end, t1.ranges)
    ]
    same_term = [
        span for span in in_region
        if occurrence.term_id in span.term_ids and span.role in {"active", "collision"}
    ]
    same_term_all_block = [
        span for span in target_spans
        if occurrence.term_id in span.term_ids and span.role in {"active", "collision"}
    ]
    masquerade = not same_term and bool(same_term_all_block)
    base = _base_decision(occurrence, term, t1, same_term, masquerade)
    if t1.status.startswith("empty"):
        return _residual(base, "C0_no_t1_region")
    if not same_term:
        return _residual(base, "C0_no_accepted_form_in_t1_region")
    if any(span.role == "collision" for span in same_term):
        return _residual(base, "cross_term_collision")
    if len(same_term) > 1:
        return _residual(base, "C2plus_multiple_same_term_forms")

    span = same_term[0]
    form_rank = _form_rank(term, span.surface)
    if form_rank == "primary":
        return _credit(base, span, form_rank, "C1_primary")
    if _shared_variant(span.surface, occurrence.term_id, all_terms):
        return _residual(
            _replace_base_with_span(base, span, form_rank),
            "C1_variant_shared_with_other_term",
        )
    return _credit(base, span, form_rank, "C1_variant_flagged")


def write_reports(report: dict[str, Any], out_prefix: str | Path) -> list[Path]:
    prefix = Path(out_prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    summary_path = prefix.with_name(prefix.name + "_summary.json")
    summary_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    paths.append(summary_path)
    for config, item in report["reports"].items():
        path = prefix.with_name(prefix.name + f"_{config}.json")
        path.write_text(json.dumps(item, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        _write_html(path.with_suffix(".html"), item)
        paths.extend([path, path.with_suffix(".html")])
    return paths


def build_residual_gold(
    *,
    report_paths: list[str | Path],
    reuse_paths: list[str | Path],
    out_path: str | Path,
) -> dict[str, Any]:
    reusable = _load_reusable_gold(reuse_paths)
    rows: list[dict[str, Any]] = []
    for path in report_paths:
        report = json.loads(Path(path).read_text(encoding="utf-8"))
        for decision in report.get("decisions", []):
            stratum = _gold_stratum(decision)
            if not stratum:
                continue
            key = _reuse_key_from_decision(decision)
            reused = reusable.get(key, {})
            prefilled = _r2_reused_gold(reused, decision)
            rows.append({
                "occ_id": decision["occ_id"],
                "config": decision["config"],
                "block_id": decision["block_id"],
                "chapter_id": decision["chapter_id"],
                "source_term": decision["source_term"],
                "term_id": decision["term_id"],
                "stratum": stratum,
                "source_span": _format_span(
                    int(decision["source_start"]),
                    int(decision["source_end"]),
                    str(decision["source_surface"]),
                ),
                "source_sentence": decision["source_sentence"],
                "source_text": decision.get("source_text", ""),
                "target_text": decision.get("target_text", ""),
                "candidates": json.dumps(decision.get("candidates") or [], ensure_ascii=False),
                "accepted_forms": json.dumps(decision.get("accepted_forms") or [], ensure_ascii=False),
                "credited_target_surface": decision.get("target_surface", ""),
                "resolved_by": decision["resolved_by"],
                "decision": decision["decision"],
                "escalate_reason": decision.get("escalate_reason", ""),
                "masquerade_suspect": str(bool(decision.get("masquerade_suspect"))).lower(),
                "gold_label": prefilled.get("gold_label", ""),
                "gold_target_start": prefilled.get("gold_target_start", ""),
                "gold_target_end": prefilled.get("gold_target_end", ""),
                "gold_target_span": prefilled.get("gold_quote", ""),
                "gold_quote": prefilled.get("gold_quote", ""),
                "registry_missing_form_flag": "",
                "annotator_note": prefilled.get("annotator_note", "prefilled_from_reuse" if reused else ""),
            })
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "occ_id", "config", "block_id", "chapter_id", "source_term", "term_id",
        "stratum", "source_span", "source_sentence", "source_text", "target_text", "candidates",
        "accepted_forms", "credited_target_surface",
        "resolved_by", "decision", "escalate_reason", "masquerade_suspect",
        "gold_label", "gold_target_start", "gold_target_end", "gold_target_span", "gold_quote",
        "registry_missing_form_flag", "annotator_note",
    ]
    with out.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    _write_residual_html(out.with_suffix(".html"), rows)
    return {
        "out": str(out),
        "html": str(out.with_suffix(".html")),
        "rows": len(rows),
        "prefilled": sum(bool(row["gold_label"]) for row in rows),
        "to_label": sum(not bool(row["gold_label"]) for row in rows),
        "strata": dict(Counter(row["stratum"] for row in rows)),
    }


def score_residual_gold(gold_path: str | Path) -> dict[str, Any]:
    rows = _read_csv(Path(gold_path))
    missing = [row["occ_id"] for row in rows if not str(row.get("gold_label") or "").strip()]
    if missing:
        raise ValueError(f"Gold rows are not fully labeled: {missing[:10]}")
    by_config: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        config = str(row.get("config") or "")
        label = str(row.get("gold_label") or "")
        by_config[config][label] += 1
        if str(row.get("masquerade_suspect") or "").lower() == "true" and label == "not_rendered":
            by_config[config]["masquerade_true"] += 1
    return {
        "gold": str(gold_path),
        "rows": len(rows),
        "by_config": {
            key: {
                **dict(value),
                "adherence_rate": _ratio(
                    value["adherent"],
                    value["adherent"] + value["off_glossary"] + value["not_rendered"],
                ),
                "off_glossary_pct": _ratio(
                    value["off_glossary"],
                    value["adherent"] + value["off_glossary"] + value["not_rendered"],
                ),
            }
            for key, value in sorted(by_config.items())
        },
    }


def _gold_stratum(decision: dict[str, Any]) -> str:
    if decision.get("resolved_by") == "t2_credit":
        if decision.get("escalate_reason") == "C1_variant_flagged":
            return "control:C1_variant_flagged"
        return ""
    reason = str(decision.get("escalate_reason") or "")
    if reason.startswith("C0"):
        return "residual:C0"
    if reason == "C1_variant_shared_with_other_term":
        return "residual:off_glossary_candidate"
    return f"residual:{reason or 'unknown'}"


def _r2_reused_gold(
    reused: dict[str, Any],
    decision: dict[str, Any],
) -> dict[str, Any]:
    if not reused:
        return {}
    label = str(reused.get("gold_label") or "").strip()
    quote = str(reused.get("gold_target_span") or "").strip()
    if label in {"rendered", "localized"}:
        code = _score_locate_only_by_code(
            {"found": True, "target_quote": quote},
            decision,
        )
        label = str(code["adherence_label"])
    elif label in {"omitted", "not_found"}:
        label = "not_rendered"
    elif label == "ambiguous":
        label = ""
    return {
        "gold_label": label,
        "gold_quote": quote,
        "gold_target_start": reused.get("gold_target_start", ""),
        "gold_target_end": reused.get("gold_target_end", ""),
        "annotator_note": reused.get("annotator_note", ""),
    }


def _target_region_text(decision: dict[str, Any]) -> str:
    text = str(decision.get("target_text") or "")
    ranges = []
    for item in (decision.get("t1") or {}).get("ranges") or []:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        try:
            start = max(0, int(item[0]))
            end = min(len(text), int(item[1]))
        except (TypeError, ValueError):
            continue
        if start < end:
            ranges.append((start, end))
    if not ranges:
        return text
    return "\n...\n".join(text[start:end] for start, end in ranges)


def _score_t3_against_reused_gold(
    validated: dict[str, Any],
    reused: dict[str, Any],
) -> dict[str, Any]:
    gold_label = str(reused.get("gold_label") or "").strip()
    gold_span = str(reused.get("gold_target_span") or "").strip()
    status = str(validated.get("status") or "")
    quote = str(validated.get("target_quote") or "")
    if gold_label in {"rendered", "localized"}:
        correct = status == "localized" and _quote_equal(quote, gold_span)
        return {
            "correct": correct,
            "reason": "rendered_quote_match" if correct else "rendered_quote_mismatch",
        }
    if gold_label in {"not_rendered", "omitted", "not_found"}:
        correct = status in {"omitted", "not_found"}
        return {
            "correct": correct,
            "reason": "not_rendered_status_match" if correct else "not_rendered_status_mismatch",
        }
    if gold_label == "ambiguous":
        correct = status == "ambiguous"
        return {
            "correct": correct,
            "reason": "ambiguous_status_match" if correct else "ambiguous_status_mismatch",
        }
    return {"correct": False, "reason": f"unsupported_gold_label:{gold_label}"}


def _quote_equal(left: str, right: str) -> bool:
    return " ".join(str(left).split()) == " ".join(str(right).split())


def _occurrence_index_in_source_sentence(decision: dict[str, Any]) -> int:
    source_sentence = str(decision.get("source_sentence") or "")
    source_term = str(decision.get("source_term") or "")
    source_text = str(decision.get("source_text") or "")
    source_start = int(decision.get("source_start") or 0)
    sentence_start = _find_sentence_start(source_text, source_sentence, source_start)
    relative_start = max(0, source_start - sentence_start)
    spans = find_spans(source_sentence, source_term, language="en")
    if not spans:
        return 1
    before_or_at = [
        span for span in spans
        if span[0] <= relative_start
    ]
    return max(1, len(before_or_at))


def _find_sentence_start(source_text: str, source_sentence: str, source_start: int) -> int:
    if not source_text or not source_sentence:
        return 0
    cursor = 0
    while True:
        index = source_text.find(source_sentence, cursor)
        if index < 0:
            return 0
        if index <= source_start <= index + len(source_sentence):
            return index
        cursor = index + 1


def _score_locate_only_by_code(
    validated: dict[str, Any],
    decision: dict[str, Any],
) -> dict[str, Any]:
    target_region = _target_region_text(decision)
    if not bool(validated.get("found")):
        return {
            "adherence_label": "not_rendered",
            "accepted_form": "",
            "highlight_surface": "",
            "highlight_start_in_quote": None,
            "highlight_end_in_quote": None,
            "target_quote_start_in_region": None,
            "target_quote_end_in_region": None,
            "polysemy_suspect": _polysemy_suspect(str(decision.get("source_term") or "")),
        }
    quote = _clean_located_quote(str(validated.get("target_quote") or ""))
    quote_span = _locate_quote_span_in_region(
        target_region,
        quote,
        str(validated.get("left_context") or ""),
    )
    match = _best_accepted_form_in_quote(quote, tuple(decision.get("accepted_forms") or ()))
    label = "adherent" if match is not None else "off_glossary"
    return {
        "adherence_label": label,
        "accepted_form": "" if match is None else match["accepted_form"],
        "highlight_surface": quote if match is None else match["surface"],
        "highlight_start_in_quote": None if match is None else match["start"],
        "highlight_end_in_quote": None if match is None else match["end"],
        "target_quote_start_in_region": quote_span[0],
        "target_quote_end_in_region": quote_span[1],
        "target_quote_clean": quote,
        "polysemy_suspect": _polysemy_suspect(str(decision.get("source_term") or "")),
    }


def _best_accepted_form_in_quote(
    quote: str,
    accepted_forms: tuple[str, ...],
) -> dict[str, Any] | None:
    matches: list[dict[str, Any]] = []
    for form in accepted_forms:
        for start, end, surface in find_spans(quote, form, language="vi"):
            matches.append({
                "accepted_form": form,
                "start": start,
                "end": end,
                "surface": surface,
            })
    if not matches:
        return None
    return sorted(
        matches,
        key=lambda item: (-(int(item["end"]) - int(item["start"])), int(item["start"]), str(item["accepted_form"])),
    )[0]


def _score_locate_only_against_reused_gold(
    validated: dict[str, Any],
    reused: dict[str, Any],
) -> dict[str, Any]:
    gold_label = str(reused.get("gold_label") or "").strip()
    gold_span = _clean_located_quote(str(reused.get("gold_target_span") or ""))
    quote = _clean_located_quote(str(validated.get("target_quote") or ""))
    if gold_label in {"rendered", "localized", "adherent", "off_glossary"}:
        correct = bool(validated.get("found")) and _quote_contains_either(quote, gold_span)
        return {
            "correct": correct,
            "reason": "locate_contains_gold" if correct else "locate_misses_gold",
        }
    if gold_label in {"not_rendered", "omitted", "not_found"}:
        correct = not bool(validated.get("found"))
        return {
            "correct": correct,
            "reason": "not_rendered_match" if correct else "not_rendered_mismatch",
        }
    if gold_label == "not_applicable":
        return {"correct": True, "reason": "not_applicable_excluded"}
    return {"correct": False, "reason": f"unsupported_gold_label:{gold_label}"}


def _quote_contains_either(left: str, right: str) -> bool:
    if not left or not right:
        return False
    return left in right or right in left


def _clean_located_quote(value: str) -> str:
    return " ".join(str(value or "").replace("*", "").split())


def _locate_quote_span_in_region(
    target_region: str,
    quote: str,
    left_context: str,
) -> tuple[int | None, int | None]:
    if not quote:
        return None, None
    candidates: list[tuple[int, int]] = []
    cursor = 0
    while True:
        index = target_region.find(quote, cursor)
        if index < 0:
            break
        candidates.append((index, index + len(quote)))
        cursor = index + max(1, len(quote))
    if not candidates:
        return None, None
    clean_left = _clean_located_quote(left_context)
    if clean_left:
        for start, end in candidates:
            prefix = _clean_located_quote(target_region[max(0, start - len(left_context) - 20):start])
            if prefix.endswith(clean_left):
                return start, end
    if len(candidates) == 1:
        return candidates[0]
    return candidates[0]


def _polysemy_suspect(source_term: str) -> bool:
    tokens = [item for item in normalize_surface(source_term).casefold().split() if item]
    return len(tokens) == 1 and len(tokens[0]) <= 4


def _target_spans(allocation: dict[str, list[Any]], owner_meta: dict[str, Any], text: str) -> list[CandidateSpan]:
    spans: list[CandidateSpan] = []
    for owner_id, allocated in allocation.items():
        meta = owner_meta[owner_id]
        for span in allocated:
            spans.append(
                CandidateSpan(
                    start=int(span.start),
                    end=int(span.end),
                    surface=str(text[span.start:span.end]),
                    role=str(meta.role),
                    term_ids=tuple(str(item) for item in meta.term_ids),
                    form=str(meta.form),
                )
            )
    return sorted(spans, key=lambda item: (item.start, item.end, item.role, item.term_ids))


def _group_items_by_block(
    items: list[tuple[CascadeOccurrence, list[CandidateSpan], AdherenceTerm]]
) -> dict[str, list[tuple[CascadeOccurrence, list[CandidateSpan], AdherenceTerm]]]:
    grouped: dict[str, list[tuple[CascadeOccurrence, list[CandidateSpan], AdherenceTerm]]] = defaultdict(list)
    for item in items:
        grouped[item[0].block_id].append(item)
    return grouped


def _cosine(a: list[float], b: list[float]) -> float:
    norm_a = sqrt(sum(value * value for value in a))
    norm_b = sqrt(sum(value * value for value in b))
    if norm_a == 0 or norm_b == 0:
        return -1.0
    return sum(x * y for x, y in zip(a, b, strict=True)) / (norm_a * norm_b)


def _source_sentence_info(source_text: str, start: int, end: int) -> tuple[int, str]:
    units = split_sentences(source_text)
    containing = containing_unit(units, start, end)
    if containing is None:
        return 0, source_text
    for index, unit in enumerate(units):
        if unit == containing:
            return index, unit.text
    return 0, containing.text


def _base_decision(
    occurrence: CascadeOccurrence,
    term: AdherenceTerm,
    t1: T1Region,
    candidates: list[CandidateSpan],
    masquerade: bool,
) -> TierDecision:
    return TierDecision(
        occ_id=occurrence.occ_id,
        config=occurrence.config,
        block_id=occurrence.block_id,
        chapter_id=occurrence.chapter_id,
        source_term=occurrence.source_term,
        term_id=occurrence.term_id,
        source_start=occurrence.source_start,
        source_end=occurrence.source_end,
        source_surface=occurrence.source_surface,
        source_sentence_idx=occurrence.source_sentence_idx,
        source_sentence=occurrence.source_sentence,
        source_text=occurrence.source_text,
        target_text=occurrence.target_text,
        resolved_by="t3_stub",
        decision="ambiguous",
        masquerade_suspect=masquerade,
        accepted_forms=tuple(term.accepted_forms),
        t1=asdict(t1),
        candidates=tuple(_candidate_to_dict(span) for span in candidates),
    )


def _replace_base_with_span(base: TierDecision, span: CandidateSpan, rank: str) -> TierDecision:
    return TierDecision(
        **{
            **asdict(base),
            "target_start": span.start,
            "target_end": span.end,
            "target_surface": span.surface,
            "matched_form_rank": rank,
            "candidates": base.candidates,
        }
    )


def _credit(base: TierDecision, span: CandidateSpan, rank: str, reason: str) -> TierDecision:
    return TierDecision(
        **{
            **asdict(base),
            "resolved_by": "t2_credit",
            "decision": "rendered",
            "target_start": span.start,
            "target_end": span.end,
            "target_surface": span.surface,
            "matched_form_rank": rank,
            "escalate_reason": reason,
            "candidates": base.candidates,
        }
    )


def _residual(base: TierDecision, reason: str) -> TierDecision:
    decision = "not_rendered" if reason.startswith("C0") else "ambiguous"
    return TierDecision(
        **{
            **asdict(base),
            "resolved_by": "t3_stub",
            "decision": decision,
            "escalate_reason": reason,
            "candidates": base.candidates,
        }
    )


def _candidate_to_dict(span: CandidateSpan) -> dict[str, Any]:
    return {
        "start": span.start,
        "end": span.end,
        "surface": span.surface,
        "role": span.role,
        "term_ids": list(span.term_ids),
        "form": span.form,
    }


def _form_rank(term: AdherenceTerm, surface: str) -> str:
    if not term.accepted_forms:
        return "novel"
    key = _norm_vi(surface)
    primary = _norm_vi(term.accepted_forms[0])
    if key == primary:
        return "primary"
    if key in {_norm_vi(item) for item in term.accepted_forms[1:]}:
        return "variant"
    return "novel"


def _shared_variant(surface: str, term_id: str, terms: list[AdherenceTerm]) -> bool:
    key = _norm_vi(surface)
    owners = [
        term.term_id
        for term in terms
        if key in {_norm_vi(item) for item in term.accepted_forms}
    ]
    return any(owner != term_id for owner in owners)


def _norm_vi(value: str) -> str:
    return normalize_surface(value).strip().casefold()


def _config_report(
    *,
    config: str,
    decisions: list[TierDecision],
    occurrences: int,
    db_hash: str,
    identity: EmbeddingModelIdentity,
    margin_threshold: float,
    elapsed_seconds: float,
    embed_stats: dict[str, Any],
) -> dict[str, Any]:
    resolved = [item for item in decisions if item.resolved_by == "t2_credit"]
    residual = [item for item in decisions if item.resolved_by != "t2_credit"]
    reasons = Counter(item.escalate_reason for item in decisions)
    return {
        "task": "EV-D2L-10",
        "phase": "B",
        "config": config,
        "denominator": occurrences,
        "t2_resolved": len(resolved),
        "t2_resolved_pct": _ratio(len(resolved), occurrences),
        "t3_residual": len(residual),
        "t3_residual_pct": _ratio(len(residual), occurrences),
        "human_required_stub": len(residual),
        "masquerade_suspect_count": sum(item.masquerade_suspect for item in decisions),
        "masquerade_suspect_pct": _ratio(sum(item.masquerade_suspect for item in decisions), occurrences),
        "breakdown": dict(sorted(reasons.items())),
        "llm_calls": 0,
        "llm_cost_usd": 0.0,
        "margin_threshold": margin_threshold,
        "model": _identity_to_dict(identity),
        "embedding": embed_stats,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "frozen_db_sha256": db_hash,
        "frozen_db_sha256_first16": db_hash[:16].upper(),
        "scope_statement": "Eval-only cascade report. Does not mutate frozen DB or headline metrics.",
        "decisions": [asdict(item) for item in decisions],
    }


def _identity_to_dict(identity: EmbeddingModelIdentity) -> dict[str, Any]:
    return {
        "alias": identity.alias,
        "endpoint_model": identity.endpoint_model,
        "model_version": identity.model_version,
        "hf_repo": identity.hf_repo,
        "quant": identity.quant,
        "display_name": identity.display_name,
        "context_length": identity.context_length,
        "embedding_dim": identity.embedding_dim,
        "query_prefix": identity.query_prefix,
        "passage_prefix": identity.passage_prefix,
        "status": identity.status,
        "skipped_with_reason": identity.skipped_with_reason,
    }


def _ratio(num: int, den: int) -> float | None:
    return None if den == 0 else round(num / den, 6)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _format_span(start: int, end: int, surface: str) -> str:
    return f"{start}:{end}:{surface}"


def _parse_span(value: str) -> tuple[int | None, int | None]:
    parts = str(value or "").split(":", 2)
    if len(parts) < 2:
        return None, None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None, None


def _read_csv(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _load_reusable_gold(paths: Iterable[str | Path]) -> dict[tuple[str, str, int, int], dict[str, Any]]:
    reusable: dict[tuple[str, str, int, int], dict[str, Any]] = {}
    for path in paths:
        for row in _read_csv(Path(path)):
            config = str(row.get("config") or "")
            block_id = str(row.get("block_id") or "")
            if row.get("source_span"):
                start, end = _parse_span(str(row.get("source_span")))
            else:
                start = _int_or_none(row.get("source_start"))
                end = _int_or_none(row.get("source_end"))
            if not config or not block_id or start is None or end is None:
                continue
            label = str(row.get("gold_label") or "").strip()
            span = str(row.get("gold_target_span") or "").strip()
            target_start = row.get("gold_target_start", "")
            target_end = row.get("gold_target_end", "")
            if _looks_like_offset_span(span) and row.get("target_text"):
                target_start, target_end = _parse_target_offset_span(span)
                if target_start is not None and target_end is not None:
                    target_text = str(row.get("target_text") or "")
                    span = target_text[target_start:target_end]
            if not label and span:
                label = "rendered"
            if not label:
                continue
            reusable[(config, block_id, start, end)] = {
                "gold_label": label,
                "gold_target_start": target_start,
                "gold_target_end": target_end,
                "gold_target_span": span,
                "annotator_note": row.get("annotator_note") or row.get("note") or "",
            }
    return reusable


def _reuse_key_from_decision(decision: dict[str, Any]) -> tuple[str, str, int, int]:
    return (
        str(decision["config"]),
        str(decision["block_id"]),
        int(decision["source_start"]),
        int(decision["source_end"]),
    )


def _int_or_none(value: Any) -> int | None:
    try:
        if value is None or str(value).strip() == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _looks_like_offset_span(value: str) -> bool:
    parts = str(value or "").split(":")
    return len(parts) == 2 and all(part.strip().isdigit() for part in parts)


def _parse_target_offset_span(value: str) -> tuple[int | None, int | None]:
    parts = str(value or "").split(":")
    if len(parts) != 2:
        return None, None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None, None


def _write_html(path: Path, report: dict[str, Any]) -> None:
    rows = []
    for item in report.get("decisions", [])[:2000]:
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(item['occ_id']))}</td>"
            f"<td>{html.escape(str(item['source_term']))}</td>"
            f"<td>{html.escape(str(item['resolved_by']))}</td>"
            f"<td>{html.escape(str(item['decision']))}</td>"
            f"<td>{html.escape(str(item.get('target_surface') or ''))}</td>"
            f"<td>{html.escape(str(item.get('escalate_reason') or ''))}</td>"
            f"<td>{html.escape(str(item.get('masquerade_suspect') or False))}</td>"
            "</tr>"
        )
    path.write_text(
        "<!doctype html><meta charset='utf-8'><title>Cascade localize</title>"
        "<style>body{font:14px system-ui;margin:20px}table{border-collapse:collapse;width:100%}"
        "td,th{border:1px solid #ddd;padding:5px;vertical-align:top}</style>"
        f"<h1>Cascade localize {html.escape(str(report.get('config')))}</h1>"
        f"<p>T2 resolved: {report.get('t2_resolved')} / {report.get('denominator')}; "
        f"residual: {report.get('t3_residual')}; masquerade suspects: {report.get('masquerade_suspect_count')}</p>"
        "<table><thead><tr><th>Occurrence</th><th>Term</th><th>Tier</th><th>Decision</th>"
        "<th>Surface</th><th>Reason</th><th>Masquerade</th></tr></thead><tbody>"
        + "".join(rows) + "</tbody></table>",
        encoding="utf-8",
    )


def _write_residual_html(path: Path, rows: list[dict[str, Any]]) -> None:
    headers = list(rows[0].keys()) if rows else []
    payload = json.dumps({"headers": headers, "rows": rows}, ensure_ascii=False)
    path.write_text(
        "<!doctype html><meta charset='utf-8'><title>Cascade residual gold</title>"
        "<style>body{font:14px system-ui;margin:20px;background:#f7f8fa;color:#17202a}"
        ".toolbar{position:sticky;top:0;background:#fff;border:1px solid #ccd3db;padding:10px;margin-bottom:12px;z-index:5}"
        ".card{background:#fff;border:1px solid #d9dee5;border-radius:8px;padding:12px;margin:12px 0}"
        ".grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.box{border:1px solid #e0e4ea;border-radius:6px;padding:10px;background:#fbfcfe}"
        "pre{white-space:pre-wrap;font:14px/1.55 system-ui;margin:0}.muted{color:#637083}.pill{display:inline-block;border-radius:999px;padding:2px 8px;background:#eef2f7;margin-right:6px}"
        "button{margin:3px;padding:5px 9px;border:1px solid #b8c2cf;background:#fff;border-radius:5px;cursor:pointer}"
        "button.active{background:#145c9e;color:#fff;border-color:#145c9e}.quote{background:#fff2a8}.missing{background:#ffe9e9}</style>"
        "<h1>Cascade residual gold worksheet</h1>"
        "<div class='toolbar'>"
        "<b>Labels:</b> adherent / off_glossary / not_rendered / not_applicable. "
        "Bôi đen text trong Target rồi bấm <b>Use selection</b>. "
        "<button onclick='exportCsv()'>Export CSV</button> "
        "<span id='stats'></span></div>"
        "<div id='app'></div>"
        f"<script>const PAYLOAD = {payload};\n"
        "const rows = PAYLOAD.rows;\n"
        "function esc(s){return String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}\n"
        "function setLabel(i,label){rows[i].gold_label=label; rows[i].annotator_note='human_review'; render();}\n"
        "function useSelection(i){const s=String(window.getSelection()); if(s.trim()){rows[i].gold_quote=s.trim(); rows[i].gold_target_span=s.trim(); rows[i].annotator_note='human_selected_span'; render();}}\n"
        "function toggleMissing(i){rows[i].registry_missing_form_flag = rows[i].registry_missing_form_flag === 'true' ? '' : 'true'; render();}\n"
        "function rowClass(r){return r.registry_missing_form_flag==='true'?'card missing':'card';}\n"
        "function render(){document.getElementById('stats').textContent = `${rows.length} rows · labeled ${rows.filter(r=>r.gold_label).length}`;"
        "document.getElementById('app').innerHTML = rows.map((r,i)=>`"
        "<section class='${rowClass(r)}'>"
        "<div><span class='pill'>${esc(r.config)}</span><span class='pill'>${esc(r.stratum)}</span><b>${esc(r.source_term)}</b> <span class='muted'>${esc(r.occ_id)}</span></div>"
        "<div class='grid'><div class='box'><b>Source sentence</b><pre>${esc(r.source_sentence)}</pre></div>"
        "<div class='box target'><b>Target</b><pre>${esc(r.target_text)}</pre></div></div>"
        "<p><b>T2:</b> ${esc(r.resolved_by)} / ${esc(r.escalate_reason)} · <b>credited:</b> ${esc(r.credited_target_surface)} · <b>accepted:</b> ${esc(r.accepted_forms)}</p>"
        "<p><b>Gold quote:</b> <span class='quote'>${esc(r.gold_quote || r.gold_target_span)}</span></p>"
        "<div>${['adherent','off_glossary','not_rendered','not_applicable'].map(l=>`<button class='${r.gold_label===l?'active':''}' onclick='setLabel(${i},\"${l}\")'>${l}</button>`).join('')}"
        "<button onclick='useSelection(${i})'>Use selection</button><button class='${r.registry_missing_form_flag==='true'?'active':''}' onclick='toggleMissing(${i})'>registry missing form</button></div>"
        "<p class='muted'>${esc(r.annotator_note)}</p></section>`).join('');}\n"
        "function csvCell(v){const s=String(v??''); return /[\",\\n]/.test(s) ? '\"'+s.replace(/\"/g,'\"\"')+'\"' : s;}\n"
        "function exportCsv(){const headers=PAYLOAD.headers; const lines=[headers.join(',')]; for(const r of rows){lines.push(headers.map(h=>csvCell(r[h])).join(','));}"
        "const blob=new Blob(['\\ufeff'+lines.join('\\n')],{type:'text/csv;charset=utf-8'}); const a=document.createElement('a'); a.href=URL.createObjectURL(blob); a.download='cascade_residual_gold.csv'; a.click();}\n"
        "render();</script>",
        encoding="utf-8",
    )
