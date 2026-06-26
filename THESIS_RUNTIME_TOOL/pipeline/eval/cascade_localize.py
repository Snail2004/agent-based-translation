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
from pipeline.eval.surface_match import normalize_surface
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
    target_text: str
    resolved_by: str
    decision: str
    target_start: int | None = None
    target_end: int | None = None
    target_surface: str = ""
    matched_form_rank: str = ""
    escalate_reason: str = ""
    masquerade_suspect: bool = False
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
    base = _base_decision(occurrence, t1, same_term, masquerade)
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
            if decision.get("resolved_by") == "t2_credit":
                continue
            key = _reuse_key_from_decision(decision)
            reused = reusable.get(key, {})
            rows.append({
                "occ_id": decision["occ_id"],
                "config": decision["config"],
                "block_id": decision["block_id"],
                "chapter_id": decision["chapter_id"],
                "source_term": decision["source_term"],
                "term_id": decision["term_id"],
                "source_span": _format_span(
                    int(decision["source_start"]),
                    int(decision["source_end"]),
                    str(decision["source_surface"]),
                ),
                "source_sentence": decision["source_sentence"],
                "target_text": decision.get("target_text", ""),
                "candidates": json.dumps(decision.get("candidates") or [], ensure_ascii=False),
                "resolved_by": decision["resolved_by"],
                "decision": decision["decision"],
                "escalate_reason": decision.get("escalate_reason", ""),
                "masquerade_suspect": str(bool(decision.get("masquerade_suspect"))).lower(),
                "gold_label": reused.get("gold_label", ""),
                "gold_target_start": reused.get("gold_target_start", ""),
                "gold_target_end": reused.get("gold_target_end", ""),
                "gold_target_span": reused.get("gold_target_span", ""),
                "annotator_note": reused.get("annotator_note", "prefilled_from_reuse" if reused else ""),
            })
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "occ_id", "config", "block_id", "chapter_id", "source_term", "term_id",
        "source_span", "source_sentence", "target_text", "candidates",
        "resolved_by", "decision", "escalate_reason", "masquerade_suspect",
        "gold_label", "gold_target_start", "gold_target_end", "gold_target_span",
        "annotator_note",
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
        "by_config": {key: dict(value) for key, value in sorted(by_config.items())},
    }


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
        target_text=occurrence.target_text,
        resolved_by="t3_stub",
        decision="ambiguous",
        masquerade_suspect=masquerade,
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
            if not label and span:
                label = "rendered"
            if not label:
                continue
            reusable[(config, block_id, start, end)] = {
                "gold_label": label,
                "gold_target_start": row.get("gold_target_start", ""),
                "gold_target_end": row.get("gold_target_end", ""),
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
    body = []
    for row in rows:
        body.append(
            "<section>"
            f"<h3>{html.escape(row['occ_id'])}</h3>"
            f"<p><b>Source term:</b> {html.escape(row['source_term'])}</p>"
            f"<p><b>Source sentence:</b> {html.escape(row['source_sentence'])}</p>"
            f"<p><b>Reason:</b> {html.escape(row['escalate_reason'])}</p>"
            f"<pre>{html.escape(row['target_text'])}</pre>"
            "</section>"
        )
    path.write_text(
        "<!doctype html><meta charset='utf-8'><title>Cascade residual gold</title>"
        "<style>body{font:14px system-ui;margin:20px}section{border:1px solid #ddd;padding:12px;margin:12px 0}"
        "pre{white-space:pre-wrap;background:#f7f7f7;padding:10px}</style>"
        "<h1>Cascade residual gold worksheet</h1>"
        + "".join(body),
        encoding="utf-8",
    )
