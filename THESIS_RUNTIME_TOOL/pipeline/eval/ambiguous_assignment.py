from __future__ import annotations

import csv
import hashlib
import json
import random
import sqlite3
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
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
    _allocate_source,
    _allocate_target,
    _form_specs,
)
from pipeline.eval.region_align import (
    DEFAULT_EMBED_MODEL,
    EmbeddingModelConfig,
    EmbeddingCacheClient,
    EmbeddingModelIdentity,
    containing_unit,
    preflight_embedding_model,
    split_sentences,
    span_in_ranges,
    top_k_target_sentences,
    union_ranges,
)
from pipeline.translate.profiles import get_profile


DEFAULT_CHAPTERS = [
    "d2l_introduction",
    "d2l_preliminaries",
    "d2l_linear_networks",
    "d2l_multilayer_perceptrons",
]
FROZEN_DB_SHA_FIRST16 = "DA0F687894090D43"
SAMPLE_SEED = 42


@dataclass(frozen=True)
class CandidateSpan:
    start: int
    end: int
    surface: str
    role: str
    term_ids: tuple[str, ...]
    form: str
    credited: bool


@dataclass(frozen=True)
class ProbeRow:
    probe_type: str
    block_id: str
    chapter: str
    config: str
    source_term: str
    source_term_id: str
    source_occ_id: str
    source_start: int
    source_end: int
    source_surface: str
    source_sentence: str
    source_text: str
    target_text: str
    block_is_multi_sentence: bool
    candidate_spans: tuple[CandidateSpan, ...]
    ev08_role: str


@dataclass(frozen=True)
class Decision:
    reference: str
    status: str
    start: int | None = None
    end: int | None = None
    surface: str = ""
    reason: str = ""


def build_gold_stub(
    *,
    db_path: str | Path,
    report_path: str | Path,
    experiment_id: str,
    n: int,
    n_control: int,
    out_path: str | Path,
) -> dict[str, Any]:
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    rows, stratification = enumerate_probe_rows(
        db_path=db_path,
        experiment_id=experiment_id,
        chapters=report.get("chapters") or DEFAULT_CHAPTERS,
    )
    selected = _sample_rows(rows, n=n, n_control=n_control)
    output = Path(out_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    _write_stub(output, selected)
    return {
        "out": str(output),
        "rows": len(selected),
        "ev08_a_registry": _ev08_a_registry_counters(report),
        "population": {key: len(value) for key, value in _group_by_type(rows).items()},
        "selected": dict(Counter(row.probe_type for row in selected)),
        "stratification": stratification,
    }


def enumerate_probe_rows(
    *,
    db_path: str | Path,
    experiment_id: str,
    chapters: list[str],
    profile_name: str = "technical_d2l_v1",
    doc_id: str = "d2l",
) -> tuple[list[ProbeRow], dict[str, Any]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        profile = get_profile(profile_name)
        resolved = _resolve_chapters(conn, doc_id, chapters)
        blocks = _scope_blocks(conn, doc_id, resolved, profile)
        block_by_id = {block.block_id: block for block in blocks}
        registry_rows = _load_registry_rows(conn, doc_id)
        terms = _registry_adherence_terms(registry_rows, profile.name)
        source_by_block = _allocate_source(blocks, terms)
        form_specs = _form_specs(terms)
        rows: list[ProbeRow] = []
        population = Counter()
        multi = Counter()
        for config in ["S0", "S1"]:
            translations = _load_translations(conn, experiment_id, config)
            for block_id, source_alloc in source_by_block.items():
                block = block_by_id.get(block_id)
                if block is None:
                    continue
                active_source = {term_id: spans for term_id, spans in source_alloc.items() if spans}
                if not active_source:
                    continue
                output = translations.get(block_id, "")
                allocation, owner_meta = _allocate_target(
                    output,
                    form_specs,
                    active_term_ids=set(active_source),
                )
                target_spans = _target_spans(allocation, owner_meta, output)
                active_by_term: dict[str, list[CandidateSpan]] = defaultdict(list)
                collision_by_term: dict[str, list[CandidateSpan]] = defaultdict(list)
                for span in target_spans:
                    if span.role == "active":
                        active_by_term[span.term_ids[0]].append(span)
                    elif span.role == "collision":
                        for term_id in span.term_ids:
                            collision_by_term[term_id].append(span)

                block_multi = _is_multi_sentence(block.text) or _is_multi_sentence(output)
                for term_id, source_spans in active_source.items():
                    active_candidates = tuple(sorted(active_by_term.get(term_id, []), key=_span_key))
                    collision_candidates = tuple(sorted(collision_by_term.get(term_id, []), key=_span_key))
                    term = _term_by_id(terms, term_id)
                    if term is None:
                        continue
                    if collision_candidates:
                        probe_type = "true_collision"
                        candidates = collision_candidates
                        ev08_role = "collision"
                    elif len(active_candidates) > len(source_spans):
                        probe_type = "variant_stealing"
                        candidates = active_candidates
                        ev08_role = "active_surplus"
                    elif len(source_spans) == 1 and len(active_candidates) == 1 and len(active_source) == 1:
                        probe_type = "control"
                        candidates = active_candidates
                        ev08_role = "active_unique"
                    else:
                        continue
                    for index, source_span in enumerate(source_spans):
                        row = _probe_row(
                            probe_type=probe_type,
                            block=block,
                            config=config,
                            term_id=term_id,
                            source_term=term.source_term,
                            source_index=index,
                            source_span=source_span,
                            target_text=output,
                            candidates=candidates,
                            block_multi=block_multi,
                            ev08_role=ev08_role,
                        )
                        rows.append(row)
                        population[probe_type] += 1
                        multi[(probe_type, "multi" if block_multi else "single")] += 1
        return rows, {
            "population": dict(population),
            "single_multi": {f"{key[0]}:{key[1]}": value for key, value in sorted(multi.items())},
        }
    finally:
        conn.close()


def evaluate_probe(
    *,
    gold_path: str | Path,
    db_path: str | Path,
    experiment_id: str,
    k: int,
    embed_endpoint: str,
    out_path: str | Path,
    cache_dir: str | Path,
    model: str = DEFAULT_EMBED_MODEL,
    model_configs: list[EmbeddingModelConfig] | None = None,
    position_window: int = 0,
) -> dict[str, Any]:
    gold_rows = _read_labeled_gold(Path(gold_path))
    configs = model_configs or [EmbeddingModelConfig("labse", model, "legacy")]
    identities: list[EmbeddingModelIdentity] = [
        preflight_embedding_model(endpoint=embed_endpoint, config=config)
        for config in configs
    ]
    available = [identity for identity in identities if identity.status == "available"]
    clients = {
        identity.alias: EmbeddingCacheClient(
            endpoint=embed_endpoint,
            model=identity.endpoint_model,
            model_alias=identity.alias,
            model_version=identity.cache_model_version(),
            query_prefix=identity.query_prefix,
            passage_prefix=identity.passage_prefix,
            prefix_profile=f"q={identity.query_prefix!r};p={identity.passage_prefix!r}",
            cache_dir=Path(cache_dir) / identity.alias,
        )
        for identity in available
    }
    start = time.perf_counter()
    decisions: list[dict[str, Any]] = []
    for row in gold_rows:
        base_decisions = {
            "legacy_block_count": _legacy_decision(row),
            "abstain_baseline": _abstain_baseline_decision(row),
            "position_narrow": _position_decision(row, position_window=position_window),
        }
        for reference, decision in base_decisions.items():
            decisions.append(_decision_record(row, decision))
        # Diagnostic only: window=1 is reported but not used as the primary decision.
        if position_window == 0:
            decisions.append(_decision_record(row, _position_decision(row, position_window=1, reference="position_narrow_w1")))
        for identity in available:
            reference = f"region_narrow@{identity.alias}"
            decision = _region_decision(
                row,
                client=clients[identity.alias],
                k=k,
                reference=reference,
            )
            decisions.append(_decision_record(row, decision))
            for sensitivity_k in [1, 2]:
                if sensitivity_k != k:
                    sensitivity_reference = f"{reference}:k{sensitivity_k}"
                    decisions.append(
                        _decision_record(
                            row,
                            _region_decision(
                                row,
                                client=clients[identity.alias],
                                k=sensitivity_k,
                                reference=sensitivity_reference,
                            ),
                        )
                    )

    report = _probe_report(
        gold_rows=gold_rows,
        decisions=decisions,
        db_path=Path(db_path),
        k=k,
        position_window=position_window,
        elapsed_seconds=time.perf_counter() - start,
        model_identities=identities,
        embed_stats={alias: client.stats() for alias, client in clients.items()},
    )
    output = Path(out_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _write_html_audit(output.with_suffix(".html"), gold_rows, decisions)
    return report


def _target_spans(allocation: dict[str, list[Any]], owner_meta: dict[str, Any], text: str) -> list[CandidateSpan]:
    result: list[CandidateSpan] = []
    credited_seen: Counter[str] = Counter()
    for owner_id, spans in allocation.items():
        meta = owner_meta[owner_id]
        for span in spans:
            credited = False
            if meta.role == "active":
                # This mirrors the deterministic order used by the EV-08 lower-bound audit.
                credited_seen[meta.term_ids[0]] += 1
                credited = True
            result.append(
                CandidateSpan(
                    start=int(span.start),
                    end=int(span.end),
                    surface=str(text[span.start:span.end]),
                    role=str(meta.role),
                    term_ids=tuple(str(item) for item in meta.term_ids),
                    form=str(meta.form),
                    credited=credited,
                )
            )
    return sorted(result, key=_span_key)


def _probe_row(
    *,
    probe_type: str,
    block: Any,
    config: str,
    term_id: str,
    source_term: str,
    source_index: int,
    source_span: Any,
    target_text: str,
    candidates: tuple[CandidateSpan, ...],
    block_multi: bool,
    ev08_role: str,
) -> ProbeRow:
    source_sentences = split_sentences(block.text)
    containing = containing_unit(source_sentences, int(source_span.start), int(source_span.end))
    source_sentence = containing.text if containing is not None else block.text
    return ProbeRow(
        probe_type=probe_type,
        block_id=str(block.block_id),
        chapter=str(block.chapter_id),
        config=config,
        source_term=source_term,
        source_term_id=term_id,
        source_occ_id=f"{config}:{block.block_id}:{term_id}:{source_index}",
        source_start=int(source_span.start),
        source_end=int(source_span.end),
        source_surface=str(block.text[source_span.start:source_span.end]),
        source_sentence=source_sentence,
        source_text=str(block.text),
        target_text=target_text,
        block_is_multi_sentence=block_multi,
        candidate_spans=candidates,
        ev08_role=ev08_role,
    )


def _sample_rows(rows: list[ProbeRow], *, n: int, n_control: int) -> list[ProbeRow]:
    mandatory = [
        row for row in rows
        if row.probe_type == "variant_stealing"
        and row.block_id == "d2l_introduction_index_b003"
        and row.source_term.casefold() == "user"
    ]
    groups: dict[tuple[str, str, str, bool], list[ProbeRow]] = defaultdict(list)
    mandatory_ids = {row.source_occ_id for row in mandatory}
    for row in rows:
        if row.source_occ_id in mandatory_ids:
            continue
        groups[(row.probe_type, row.chapter, row.config, row.block_is_multi_sentence)].append(row)
    rng = random.Random(SAMPLE_SEED)
    for group_rows in groups.values():
        rng.shuffle(group_rows)
    selected: list[ProbeRow] = []
    target_by_type = {"control": n_control}
    non_control = [row for row in rows if row.probe_type != "control"]
    target_by_type.update({
        probe_type: max(1, round(n * count / max(1, len(non_control))))
        for probe_type, count in Counter(row.probe_type for row in non_control).items()
    })
    for probe_type, target in target_by_type.items():
        type_groups = [items for key, items in groups.items() if key[0] == probe_type]
        selected.extend(_round_robin(type_groups, target))
    selected_by_id = {row.source_occ_id: row for row in [*mandatory, *selected]}
    return sorted(
        selected_by_id.values(),
        key=lambda row: (row.probe_type, row.chapter, row.config, row.block_id, row.source_start),
    )


def _round_robin(groups: list[list[ProbeRow]], target: int) -> list[ProbeRow]:
    result: list[ProbeRow] = []
    index = 0
    while len(result) < target and any(index < len(group) for group in groups):
        for group in groups:
            if index < len(group):
                result.append(group[index])
                if len(result) >= target:
                    break
        index += 1
    return result


def _write_stub(path: Path, rows: list[ProbeRow]) -> None:
    fields = [
        "probe_type", "block_id", "chapter", "config", "source_term",
        "source_term_id", "source_occ_id", "source_span", "source_sentence",
        "source_text", "target_text", "block_is_multi_sentence",
        "candidate_target_spans", "candidate_target_forms", "candidate_term_ids",
        "ev08_role", "gold_target_span", "gold_label", "annotator_note",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(_row_to_csv(row))


def _row_to_csv(row: ProbeRow) -> dict[str, Any]:
    return {
        "probe_type": row.probe_type,
        "block_id": row.block_id,
        "chapter": row.chapter,
        "config": row.config,
        "source_term": row.source_term,
        "source_term_id": row.source_term_id,
        "source_occ_id": row.source_occ_id,
        "source_span": _format_span(row.source_start, row.source_end, row.source_surface),
        "source_sentence": row.source_sentence,
        "source_text": row.source_text,
        "target_text": row.target_text,
        "block_is_multi_sentence": str(row.block_is_multi_sentence).lower(),
        "candidate_target_spans": json.dumps([_candidate_to_dict(item) for item in row.candidate_spans], ensure_ascii=False),
        "candidate_target_forms": " | ".join(sorted({item.form for item in row.candidate_spans})),
        "candidate_term_ids": " | ".join(sorted({term_id for item in row.candidate_spans for term_id in item.term_ids})),
        "ev08_role": row.ev08_role,
        "gold_target_span": "",
        "gold_label": "",
        "annotator_note": "",
    }


def _read_labeled_gold(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    missing = [
        row.get("source_occ_id") or f"row#{index + 1}"
        for index, row in enumerate(rows)
        if not str(row.get("gold_label") or "").strip()
    ]
    if missing:
        raise ValueError(f"Gold rows are not fully labeled: {missing[:10]}")
    for row in rows:
        row["_candidate_spans"] = json.loads(row.get("candidate_target_spans") or "[]")
        row["_gold_range"] = _parse_gold_range(row)
    return rows


def _legacy_decision(row: dict[str, Any]) -> Decision:
    candidates = _candidate_spans(row)
    if not candidates:
        return Decision("legacy_block_count", "abstain", reason="no_candidate")
    return _assign("legacy_block_count", candidates[0], "first_block_candidate")


def _abstain_baseline_decision(row: dict[str, Any]) -> Decision:
    candidates = _candidate_spans(row)
    if row["probe_type"] == "true_collision":
        return Decision("abstain_baseline", "abstain", reason="ev08_collision_band")
    if not candidates:
        return Decision("abstain_baseline", "abstain", reason="no_candidate")
    return _assign("abstain_baseline", candidates[0], "ev08_lower_count_candidate")


def _position_decision(
    row: dict[str, Any],
    *,
    position_window: int,
    reference: str = "position_narrow",
) -> Decision:
    candidates = _candidate_spans(row)
    if not candidates:
        return Decision(reference, "reject", reason="no_candidate")
    ranges, reason = _position_ranges(row, position_window=position_window)
    survivors = [
        item for item in candidates
        if span_in_ranges(int(item["start"]), int(item["end"]), ranges)
    ]
    if not survivors:
        return Decision(reference, "reject", reason=f"{reason};survivors=0")
    if len(survivors) != 1:
        return Decision(reference, "abstain", reason=f"{reason};survivors={len(survivors)}")
    return _assign(reference, survivors[0], reason)


def _position_ranges(row: dict[str, Any], *, position_window: int) -> tuple[list[tuple[int, int]], str]:
    source_units = split_sentences(str(row.get("source_text") or ""))
    target_units = split_sentences(str(row.get("target_text") or ""))
    if not target_units:
        return [], "no_target_sentences"
    if len(source_units) <= 1 or len(target_units) <= 1:
        return [(0, len(str(row.get("target_text") or "")))], "degenerate_position_region"
    source_start = _source_start(row)
    source_end = _source_end(row)
    source_index = 0
    for index, unit in enumerate(source_units):
        if unit.start <= source_start and source_end <= unit.end:
            source_index = index
            break
    rel = source_index / max(1, len(source_units) - 1)
    target_index = round(rel * (len(target_units) - 1))
    lo = max(0, target_index - position_window)
    hi = min(len(target_units) - 1, target_index + position_window)
    return union_ranges(target_units[lo:hi + 1]), (
        f"source_sentence={source_index};target_sentence={target_index};"
        f"position_window={position_window}"
    )


def _region_decision(
    row: dict[str, Any],
    *,
    client: EmbeddingCacheClient,
    k: int,
    reference: str = "region_narrow",
) -> Decision:
    candidates = _candidate_spans(row)
    if not candidates:
        return Decision(reference, "reject", reason="no_candidate")
    target_sentences = split_sentences(str(row["target_text"]))
    ranked = top_k_target_sentences(
        str(row["source_sentence"]),
        target_sentences,
        k=k,
        client=client,
    )
    ranges = union_ranges(item.unit for item in ranked)
    survivors = [
        item for item in candidates
        if span_in_ranges(int(item["start"]), int(item["end"]), ranges)
    ]
    if not survivors:
        return Decision(reference, "reject", reason=f"top{k}_sentence_union;survivors=0")
    if len(survivors) != 1:
        return Decision(reference, "abstain", reason=f"top{k}_sentence_union;survivors={len(survivors)}")
    return _assign(reference, survivors[0], f"top{k}_sentence_union")


def _decision_record(row: dict[str, Any], decision: Decision) -> dict[str, Any]:
    gold_label = str(row["gold_label"]).strip()
    gold_range = row.get("_gold_range")
    correct = _decision_correct(decision, gold_label, gold_range)
    return {
        "source_occ_id": row["source_occ_id"],
        "probe_type": row["probe_type"],
        "config": row["config"],
        "block_id": row["block_id"],
        "reference": decision.reference,
        "status": decision.status,
        "start": decision.start,
        "end": decision.end,
        "surface": decision.surface,
        "reason": decision.reason,
        "gold_label": gold_label,
        "gold_target_span": row.get("gold_target_span", ""),
        "correct": correct,
    }


def _decision_correct(decision: Decision, gold_label: str, gold_range: tuple[int, int] | None) -> bool:
    if gold_label == "ambiguous":
        return decision.status == "abstain"
    if gold_label == "not_rendered":
        return decision.status in {"abstain", "reject"}
    if decision.status != "assign" or gold_range is None:
        return False
    return (decision.start, decision.end) == gold_range


def _probe_report(
    *,
    gold_rows: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    db_path: Path,
    k: int,
    position_window: int,
    elapsed_seconds: float,
    model_identities: list[EmbeddingModelIdentity],
    embed_stats: dict[str, Any],
) -> dict[str, Any]:
    by_ref_type: dict[str, dict[str, Any]] = {}
    primary_references = _primary_references(decisions)
    for reference in primary_references:
        for probe_type in ["true_collision", "variant_stealing", "control"]:
            subset = [item for item in decisions if item["reference"] == reference and item["probe_type"] == probe_type]
            by_ref_type[f"{reference}:{probe_type}"] = _metric_summary(subset)
    a0_wrong = [
        item for item in decisions
        if item["reference"] == "abstain_baseline" and not item["correct"]
    ]
    by_reference = _decisions_by_reference(decisions)
    return {
        "task": "EV-D2L-09",
        "scope": "eval-only ambiguous-assignment probe; alignment != Builder-quality",
        "k": k,
        "position_window_primary": position_window,
        "position_window_diagnostic": 1 if position_window == 0 else None,
        "rows": len(gold_rows),
        "row_counts": dict(Counter(row["probe_type"] for row in gold_rows)),
        "metrics": by_ref_type,
        "abstain_baseline_wrong_outcomes": {
            reference: _outcome_breakdown(a0_wrong, lookup)
            for reference, lookup in by_reference.items()
            if reference != "abstain_baseline"
        },
        "hard_subset_model_minus_position": _model_minus_position(decisions),
        "sensitivity": _sensitivity_metrics(decisions),
        "stratification": _gold_stratification(gold_rows),
        "elapsed_seconds": round(elapsed_seconds, 3),
        "models": [_identity_to_dict(identity) for identity in model_identities],
        "embedding": embed_stats,
        "frozen_db_sha256": _sha256_file(db_path),
        "frozen_db_sha256_first16": _sha256_file(db_path)[:16].upper(),
        "decision_note": (
            "Apply the pre-registered task rule in section 6. This report is DEV method "
            "selection only, not a trust number."
        ),
    }


def _metric_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(items)
    assigned = [item for item in items if item["status"] == "assign"]
    rejected = [item for item in items if item["status"] == "reject"]
    abstained = [item for item in items if item["status"] == "abstain"]
    correct = [item for item in items if item["correct"]]
    correct_assigned = [item for item in assigned if item["correct"]]
    wrong_assigned = [item for item in assigned if not item["correct"]]
    correct_rejected = [item for item in rejected if item["correct"]]
    wrong_rejected = [item for item in rejected if not item["correct"]]
    return {
        "total": total,
        "assigned": len(assigned),
        "assign_correct": len(correct_assigned),
        "assign_wrong": len(wrong_assigned),
        "rejected": len(rejected),
        "reject_correct": len(correct_rejected),
        "reject_wrong": len(wrong_rejected),
        "abstained": len(abstained),
        "correct": len(correct),
        "wrong": total - len(correct),
        "assignment_precision": _ratio(len(correct_assigned), len(assigned)),
        "non_abstain_accuracy": _ratio(len(correct_assigned) + len(correct_rejected), len(assigned) + len(rejected)),
        "coverage": _ratio(len(assigned) + len(rejected), total),
        "abstain_rate": _ratio(len(abstained), total),
    }


def _outcome_breakdown(
    baseline_wrong: list[dict[str, Any]],
    region_by_occ: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    counter = Counter()
    for item in baseline_wrong:
        region = region_by_occ.get(item["source_occ_id"])
        if region is None:
            counter["missing_region"] += 1
        elif region["correct"]:
            counter["region_correct"] += 1
        elif region["status"] == "abstain":
            counter["region_abstain"] += 1
        elif region["status"] == "reject":
            counter["region_reject"] += 1
        else:
            counter["region_wrong"] += 1
    return dict(counter)


def _decisions_by_reference(decisions: list[dict[str, Any]]) -> dict[str, dict[str, dict[str, Any]]]:
    grouped: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for item in decisions:
        grouped[item["reference"]][item["source_occ_id"]] = item
    return grouped


def _primary_references(decisions: list[dict[str, Any]]) -> list[str]:
    references = sorted({item["reference"] for item in decisions})
    return [
        reference for reference in references
        if ":k" not in reference and not reference.endswith("_w1")
    ]


def _sensitivity_metrics(decisions: list[dict[str, Any]]) -> dict[str, Any]:
    references = sorted({
        item["reference"] for item in decisions
        if ":k" in item["reference"] or item["reference"].endswith("_w1")
    })
    return {
        reference: _metric_summary([item for item in decisions if item["reference"] == reference])
        for reference in references
    }


def _model_minus_position(decisions: list[dict[str, Any]]) -> dict[str, Any]:
    hard = {"true_collision", "variant_stealing"}
    position = [
        item for item in decisions
        if item["reference"] == "position_narrow" and item["probe_type"] in hard
    ]
    position_precision = _metric_summary(position)["assignment_precision"]
    result: dict[str, Any] = {"position_narrow_assignment_precision": position_precision}
    for reference in sorted({item["reference"] for item in decisions if item["reference"].startswith("region_narrow@") and ":k" not in item["reference"]}):
        subset = [
            item for item in decisions
            if item["reference"] == reference and item["probe_type"] in hard
        ]
        precision = _metric_summary(subset)["assignment_precision"]
        result[reference] = {
            "assignment_precision": precision,
            "delta_vs_position": None if precision is None or position_precision is None else round(precision - position_precision, 6),
        }
    return result


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


def _gold_stratification(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counter = Counter()
    for row in rows:
        key = f"{row['probe_type']}:{'multi' if str(row.get('block_is_multi_sentence')) == 'true' else 'single'}"
        counter[key] += 1
    return dict(counter)


def _write_html_audit(path: Path, gold_rows: list[dict[str, Any]], decisions: list[dict[str, Any]]) -> None:
    rows_by_occ = defaultdict(list)
    for item in decisions:
        rows_by_occ[item["source_occ_id"]].append(item)
    body: list[str] = []
    for row in gold_rows:
        body.append(
            "<section>"
            f"<h3>{_escape(row['probe_type'])} · {_escape(row['source_occ_id'])}</h3>"
            f"<p><b>Source:</b> {_escape(row['source_sentence'])}</p>"
            f"<p><b>Gold:</b> {_escape(row.get('gold_label',''))} {_escape(row.get('gold_target_span',''))}</p>"
            "<table><tr><th>Reference</th><th>Status</th><th>Surface</th><th>Correct</th><th>Reason</th></tr>"
        )
        for item in rows_by_occ[row["source_occ_id"]]:
            body.append(
                "<tr>"
                f"<td>{_escape(item['reference'])}</td><td>{_escape(item['status'])}</td>"
                f"<td>{_escape(item.get('surface') or '')}</td><td>{_escape(str(item['correct']))}</td>"
                f"<td>{_escape(item.get('reason') or '')}</td>"
                "</tr>"
            )
        body.append("</table></section>")
    path.write_text(
        "<!doctype html><meta charset=\"utf-8\"><title>Ambiguous assignment probe</title>"
        "<style>body{font:14px system-ui;margin:20px}section{border:1px solid #ddd;padding:12px;margin:12px 0}"
        "table{border-collapse:collapse;width:100%}td,th{border:1px solid #ddd;padding:4px;text-align:left}</style>"
        + "".join(body),
        encoding="utf-8",
    )


def _parse_gold_range(row: dict[str, Any]) -> tuple[int, int] | None:
    label = str(row.get("gold_label") or "").strip()
    if label in {"not_rendered", "ambiguous"}:
        return None
    raw = str(row.get("gold_target_span") or "").strip()
    if not raw:
        raise ValueError(f"Missing gold_target_span for {row.get('source_occ_id')}")
    if ":" in raw:
        left, right = raw.split(":", 1)
        return int(left), int(right)
    target = str(row.get("target_text") or "")
    start = target.find(raw)
    if start < 0 or target.find(raw, start + 1) >= 0:
        raise ValueError(f"Gold quote is missing or ambiguous for {row.get('source_occ_id')}: {raw!r}")
    return start, start + len(raw)


def _source_start(row: dict[str, Any]) -> int:
    raw = str(row.get("source_span") or "")
    if ":" in raw:
        return int(raw.split(":", 2)[0])
    return int(row.get("source_start") or 0)


def _source_end(row: dict[str, Any]) -> int:
    raw = str(row.get("source_span") or "")
    if ":" in raw:
        return int(raw.split(":", 2)[1])
    return int(row.get("source_end") or _source_start(row))


def _candidate_spans(row: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(row.get("_candidate_spans") or [], key=lambda item: (int(item["start"]), int(item["end"])))


def _assign(reference: str, span: dict[str, Any], reason: str) -> Decision:
    return Decision(
        reference,
        "assign",
        start=int(span["start"]),
        end=int(span["end"]),
        surface=str(span.get("surface") or ""),
        reason=reason,
    )


def _format_span(start: int, end: int, surface: str) -> str:
    return f"{start}:{end}:{surface}"


def _candidate_to_dict(item: CandidateSpan) -> dict[str, Any]:
    return {
        "start": item.start,
        "end": item.end,
        "surface": item.surface,
        "role": item.role,
        "term_ids": list(item.term_ids),
        "form": item.form,
        "credited": item.credited,
    }


def _span_key(item: CandidateSpan) -> tuple[int, int, str]:
    return item.start, item.end, item.surface


def _group_by_type(rows: Iterable[ProbeRow]) -> dict[str, list[ProbeRow]]:
    groups: dict[str, list[ProbeRow]] = defaultdict(list)
    for row in rows:
        groups[row.probe_type].append(row)
    return groups


def _ev08_a_registry_counters(report: dict[str, Any]) -> dict[str, Any]:
    a_registry = report.get("A_registry_occurrence_adherence", {}).get("S1", {})
    return {
        "denominator": a_registry.get("denominator"),
        "confirmed": a_registry.get("numerator_confirmed"),
        "residual_capacity": a_registry.get("residual_capacity"),
        "collision_target_spans": a_registry.get("collision_target_spans"),
    }


def _is_multi_sentence(text: str) -> bool:
    return len(split_sentences(text)) > 1


def _term_by_id(terms: list[Any], term_id: str) -> Any | None:
    for term in terms:
        if term.term_id == term_id:
            return term
    return None


def _ratio(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else round(numerator / denominator, 6)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _escape(value: Any) -> str:
    import html

    return html.escape(str(value))
