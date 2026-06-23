from __future__ import annotations

import csv
import html
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from pipeline.eval.surface_match import SurfaceOwner, allocate_spans, normalize_surface


@dataclass(frozen=True)
class AdherenceTerm:
    term_id: str
    source_term: str
    accepted_forms: tuple[str, ...]
    case_sensitive: bool = False


@dataclass(frozen=True)
class _FormSpec:
    term_id: str
    form: str
    normalized: str
    case_sensitive: bool


@dataclass(frozen=True)
class _OwnerMeta:
    role: str
    term_ids: tuple[str, ...]
    form: str


def score_occurrence_adherence(
    blocks: Iterable[Any],
    translations: dict[str, str],
    terms: list[AdherenceTerm],
    *,
    ruler: str,
    config: str,
    include_term_ids: set[str] | None = None,
    emit_audit: bool = True,
) -> tuple[dict[str, Any], list[dict[str, Any]], Counter[str]]:
    """Count accepted target occurrences after joint source/target allocation."""

    ordered_blocks = list(blocks)
    term_by_id = {term.term_id: term for term in terms}
    included = set(term_by_id) if include_term_ids is None else set(include_term_ids)
    source_by_block = _allocate_source(ordered_blocks, terms)
    source_totals: Counter[str] = Counter()
    for allocated in source_by_block.values():
        for term_id, spans in allocated.items():
            source_totals[term_id] += len(spans)

    totals = Counter()
    chapter_totals: dict[str, Counter[str]] = defaultdict(Counter)
    term_totals: dict[str, Counter[str]] = defaultdict(Counter)
    collision_groups: Counter[str] = Counter()
    audit_rows: list[dict[str, Any]] = []

    form_specs = _form_specs(terms)
    for block in ordered_blocks:
        block_sources = source_by_block.get(block.block_id, {})
        active_source = {
            term_id: spans
            for term_id, spans in block_sources.items()
            if term_id in included and spans
        }
        if not active_source:
            continue

        source_count = {term_id: len(spans) for term_id, spans in active_source.items()}
        denominator = sum(source_count.values())
        output = translations.get(block.block_id, "")
        allocation, owner_meta = _allocate_target(
            output,
            form_specs,
            active_term_ids=set(active_source),
        )

        active_spans: dict[str, list[Any]] = defaultdict(list)
        collision_span_nodes: list[tuple[tuple[str, ...], Any]] = []
        target_audit: list[tuple[str, _OwnerMeta, Any]] = []
        for owner_id, spans in allocation.items():
            meta = owner_meta[owner_id]
            for span in spans:
                target_audit.append((owner_id, meta, span))
                if meta.role == "active":
                    active_spans[meta.term_ids[0]].append(span)
                elif meta.role == "collision":
                    collision_span_nodes.append((meta.term_ids, span))
                    collision_groups[_norm(meta.form)] += 1

        confirmed_by_term = {
            term_id: min(count, len(active_spans.get(term_id, [])))
            for term_id, count in source_count.items()
        }
        confirmed = sum(confirmed_by_term.values())
        remaining = {
            term_id: max(0, source_count[term_id] - confirmed_by_term[term_id])
            for term_id in source_count
        }
        residual_capacity = _collision_capacity(collision_span_nodes, remaining)

        totals["denominator"] += denominator
        totals["confirmed"] += confirmed
        totals["residual_capacity"] += residual_capacity
        totals["collision_target_spans"] += len(collision_span_nodes)
        chapter = str(block.chapter_id)
        chapter_totals[chapter]["denominator"] += denominator
        chapter_totals[chapter]["confirmed"] += confirmed
        chapter_totals[chapter]["residual_capacity"] += residual_capacity
        for term_id, count in source_count.items():
            term_totals[term_id]["source"] += count
            term_totals[term_id]["target"] += len(active_spans.get(term_id, []))
            term_totals[term_id]["confirmed"] += confirmed_by_term[term_id]

        if emit_audit:
            for term_id, spans in active_source.items():
                term = term_by_id[term_id]
                for span in spans:
                    audit_rows.append(_audit_row(
                        ruler=ruler,
                        config=config,
                        block=block,
                        side="source",
                        role="denominator",
                        term_ids=(term_id,),
                        form=term.source_term,
                        span=span,
                        text=block.text,
                        credited=True,
                    ))
            credited_seen: Counter[str] = Counter()
            for _, meta, span in sorted(target_audit, key=lambda item: (item[2].start, item[2].end)):
                credited = False
                if meta.role == "active":
                    term_id = meta.term_ids[0]
                    credited = credited_seen[term_id] < confirmed_by_term.get(term_id, 0)
                    if credited:
                        credited_seen[term_id] += 1
                audit_rows.append(_audit_row(
                    ruler=ruler,
                    config=config,
                    block=block,
                    side="target",
                    role=meta.role,
                    term_ids=meta.term_ids,
                    form=meta.form,
                    span=span,
                    text=output,
                    credited=credited,
                ))

    denominator = totals["denominator"]
    confirmed = totals["confirmed"]
    residual = min(totals["residual_capacity"], max(0, denominator - confirmed))
    report = {
        "method": "joint_count_matched_v1",
        "ruler": ruler,
        "config": config,
        "numerator_confirmed": confirmed,
        "denominator": denominator,
        "adherence_lower": _ratio(confirmed, denominator),
        "adherence_upper": _ratio(confirmed + residual, denominator),
        "residual_capacity": residual,
        "collision_target_spans": totals["collision_target_spans"],
        "resolved_only": _ratio(confirmed, max(0, denominator - residual)),
        "per_chapter": {
            chapter: {
                "numerator_confirmed": counts["confirmed"],
                "denominator": counts["denominator"],
                "adherence_lower": _ratio(counts["confirmed"], counts["denominator"]),
                "adherence_upper": _ratio(
                    min(counts["denominator"], counts["confirmed"] + counts["residual_capacity"]),
                    counts["denominator"],
                ),
                "residual_capacity": min(
                    counts["residual_capacity"],
                    max(0, counts["denominator"] - counts["confirmed"]),
                ),
            }
            for chapter, counts in sorted(chapter_totals.items())
        },
        "collision_forms": [
            {"form": form, "target_spans": count}
            for form, count in collision_groups.most_common()
        ],
        "worst_terms": _worst_terms(term_totals, term_by_id),
    }
    return report, audit_rows, source_totals


def write_occurrence_audit(
    csv_path: str | Path,
    html_path: str | Path,
    rows: list[dict[str, Any]],
) -> None:
    fields = [
        "ruler", "config", "block_id", "chapter_id", "side", "role",
        "term_ids", "form", "start", "end", "surface", "credited", "context",
    ]
    csv_out = Path(csv_path)
    csv_out.parent.mkdir(parents=True, exist_ok=True)
    with csv_out.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    html_out = Path(html_path)
    html_out.parent.mkdir(parents=True, exist_ok=True)
    body = []
    for row in rows:
        role = html.escape(str(row["role"]))
        body.append(
            f'<tr class="{role}"><td>{html.escape(str(row["ruler"]))}</td>'
            f'<td>{html.escape(str(row["config"]))}</td>'
            f'<td>{html.escape(str(row["block_id"]))}</td>'
            f'<td>{html.escape(str(row["side"]))}</td><td>{role}</td>'
            f'<td>{html.escape(str(row["term_ids"]))}</td>'
            f'<td>{html.escape(str(row["form"]))}</td>'
            f'<td>{html.escape(str(row["surface"]))}</td>'
            f'<td>{html.escape(str(row["credited"]))}</td>'
            f'<td>{html.escape(str(row["context"]))}</td></tr>'
        )
    html_out.write_text(
        "<!doctype html><meta charset=\"utf-8\"><title>Occurrence adherence audit</title>"
        "<style>body{font:13px system-ui;margin:20px}table{border-collapse:collapse;width:100%}"
        "th,td{border:1px solid #ddd;padding:5px;vertical-align:top}.active{background:#e9f8ef}"
        ".shadow{background:#f1f3f5}.collision{background:#fff0e6}.denominator{background:#e8f1ff}"
        "td:last-child{max-width:520px;white-space:pre-wrap}</style>"
        "<h1>Occurrence adherence audit</h1>"
        "<p>Rows are emitted from the exact joint allocation used by the scorer.</p>"
        "<table><thead><tr><th>Ruler</th><th>Config</th><th>Block</th><th>Side</th>"
        "<th>Role</th><th>Term owners</th><th>Form</th><th>Surface</th><th>Credited</th>"
        "<th>Context</th></tr></thead><tbody>" + "".join(body) + "</tbody></table>",
        encoding="utf-8",
    )


def _allocate_source(blocks: list[Any], terms: list[AdherenceTerm]) -> dict[str, dict[str, list[Any]]]:
    result: dict[str, dict[str, list[Any]]] = {}
    for block in blocks:
        tokens = _token_set(block.text)
        owners = [
            SurfaceOwner(term.term_id, term.source_term, term.case_sensitive)
            for term in terms
            if _possible(term.source_term, tokens)
        ]
        result[block.block_id] = allocate_spans(block.text, owners, language="en")
    return result


def _allocate_target(
    output: str,
    specs: list[_FormSpec],
    *,
    active_term_ids: set[str],
) -> tuple[dict[str, list[Any]], dict[str, _OwnerMeta]]:
    tokens = _token_set(output)
    possible = [spec for spec in specs if _possible(spec.form, tokens)]
    active_by_form: dict[str, set[str]] = defaultdict(set)
    specs_by_form: dict[str, list[_FormSpec]] = defaultdict(list)
    for spec in possible:
        specs_by_form[spec.normalized].append(spec)
        if spec.term_id in active_term_ids:
            active_by_form[spec.normalized].add(spec.term_id)

    owners: list[SurfaceOwner] = []
    metadata: dict[str, _OwnerMeta] = {}
    for index, (normalized, grouped) in enumerate(sorted(specs_by_form.items())):
        active_ids = tuple(sorted(active_by_form.get(normalized, set())))
        representative = max(grouped, key=lambda item: (len(item.form), item.form))
        if len(active_ids) >= 2:
            owner_id = f"collision:{index}"
            role = "collision"
            term_ids = active_ids
            case_sensitive = all(item.case_sensitive for item in grouped if item.term_id in active_ids)
        elif len(active_ids) == 1:
            owner_id = f"active:{index}:{active_ids[0]}"
            role = "active"
            term_ids = active_ids
            active_specs = [item for item in grouped if item.term_id == active_ids[0]]
            representative = max(active_specs, key=lambda item: (len(item.form), item.form))
            case_sensitive = all(item.case_sensitive for item in active_specs)
        else:
            owner_id = f"shadow:{index}"
            role = "shadow"
            term_ids = tuple(sorted({item.term_id for item in grouped}))
            case_sensitive = all(item.case_sensitive for item in grouped)
        owners.append(SurfaceOwner(owner_id, representative.form, case_sensitive))
        metadata[owner_id] = _OwnerMeta(role, term_ids, representative.form)
    return allocate_spans(output, owners, language="vi"), metadata


def _form_specs(terms: list[AdherenceTerm]) -> list[_FormSpec]:
    specs: list[_FormSpec] = []
    for term in terms:
        seen: set[str] = set()
        for form in term.accepted_forms:
            normalized = _norm(form)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            specs.append(_FormSpec(term.term_id, form, normalized, term.case_sensitive))
    return specs


def _collision_capacity(
    collision_spans: list[tuple[tuple[str, ...], Any]],
    remaining: dict[str, int],
) -> int:
    slots = [
        (term_id, slot)
        for term_id, capacity in sorted(remaining.items())
        for slot in range(capacity)
    ]
    adjacency = [
        [slot for slot in slots if slot[0] in term_ids]
        for term_ids, _ in collision_spans
    ]
    slot_to_span: dict[tuple[str, int], int] = {}

    def assign(span_index: int, seen: set[tuple[str, int]]) -> bool:
        for slot in adjacency[span_index]:
            if slot in seen:
                continue
            seen.add(slot)
            previous = slot_to_span.get(slot)
            if previous is None or assign(previous, seen):
                slot_to_span[slot] = span_index
                return True
        return False

    matched = 0
    for index in range(len(collision_spans)):
        if assign(index, set()):
            matched += 1
    return matched


def _audit_row(
    *,
    ruler: str,
    config: str,
    block: Any,
    side: str,
    role: str,
    term_ids: tuple[str, ...],
    form: str,
    span: Any,
    text: str,
    credited: bool,
) -> dict[str, Any]:
    left = max(0, span.start - 70)
    right = min(len(text), span.end + 70)
    context = text[left:span.start] + "[[" + text[span.start:span.end] + "]]" + text[span.end:right]
    return {
        "ruler": ruler,
        "config": config,
        "block_id": str(block.block_id),
        "chapter_id": str(block.chapter_id),
        "side": side,
        "role": role,
        "term_ids": " | ".join(term_ids),
        "form": form,
        "start": span.start,
        "end": span.end,
        "surface": span.surface,
        "credited": str(bool(credited)).lower(),
        "context": context,
    }


def _worst_terms(
    totals: dict[str, Counter[str]],
    term_by_id: dict[str, AdherenceTerm],
) -> list[dict[str, Any]]:
    rows = []
    for term_id, counts in totals.items():
        rows.append({
            "term_id": term_id,
            "source_term": term_by_id[term_id].source_term,
            "source_occurrences": counts["source"],
            "accepted_target_occurrences": counts["target"],
            "confirmed_credit": counts["confirmed"],
            "rate": _ratio(counts["confirmed"], counts["source"]),
        })
    rows.sort(key=lambda item: (item["rate"], -item["source_occurrences"], item["source_term"]))
    return rows[:30]


def _token_set(text: str) -> set[str]:
    return set(re.findall(r"\w+", normalize_surface(str(text or "")).casefold(), flags=re.UNICODE))


def _possible(needle: str, tokens: set[str]) -> bool:
    required = _token_set(needle)
    return bool(required) and required.issubset(tokens)


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", normalize_surface(str(value or "")).casefold().strip())


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0
