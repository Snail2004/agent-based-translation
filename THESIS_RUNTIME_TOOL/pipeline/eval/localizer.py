from __future__ import annotations

import csv
import hashlib
import json
import random
import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Protocol

from pipeline.eval.occ_align import (
    DEFAULT_ALIGN_SEED,
    DEFAULT_SIMALIGN_METHOD,
    DEFAULT_SIMALIGN_MODEL,
    OccItem,
    WordAligner,
    align_independent,
    make_simalign_aligner,
    simalign_cache_key,
    simalign_cached,
)
from pipeline.eval.surface_match import SurfaceOwner, allocate_spans, find_spans, normalize_surface


LOCALIZER_NAMES = ("first_match", "longest_match", "simalign")


@dataclass(frozen=True)
class LocalizedSpan:
    start: int
    end: int
    surface: str
    method: str
    status: str = "found"
    source: str = "surface"


@dataclass(frozen=True)
class RepresentativeContext:
    resolved: bool
    skip_reason: str
    block_id: str | None
    en_sentence: str
    s0_window: str
    s1_window: str


@dataclass(frozen=True)
class LocalizerGoldRow:
    row_id: str
    item_id: str
    config: str
    source_term: str
    surface: str
    block_id: str
    registry_class: str
    source_text: str
    source_start: str
    source_end: str
    source_surface: str
    target_text: str
    candidate_1: str
    candidate_2: str
    candidate_3: str
    candidate_4: str
    candidates_blind_json: str
    prefilled: str
    human_edited: str
    gold_target_start: str
    gold_target_end: str
    gold_target_span: str
    note: str
    edge_kind: str = ""


class LocalizerAlignerFactory(Protocol):
    def __call__(self) -> WordAligner:
        ...


def localize_first_match(
    text: str,
    surface: str,
    *,
    language: str = "vi",
    candidate_surfaces: Iterable[str] = (),
) -> LocalizedSpan | None:
    del candidate_surfaces
    spans = find_spans(text, surface, language=_language(language))
    if not spans:
        return None
    start, end, found = spans[0]
    return LocalizedSpan(start=start, end=end, surface=found, method="first_match")


def localize_longest_match(
    text: str,
    surface: str,
    *,
    language: str = "vi",
    candidate_surfaces: Iterable[str] = (),
) -> LocalizedSpan | None:
    candidates = _candidate_surfaces(surface, candidate_surfaces)
    owners = [
        SurfaceOwner(f"candidate_{index}", candidate)
        for index, candidate in enumerate(candidates)
        if candidate
    ]
    allocated = allocate_spans(text, owners, language=_language(language))
    spans = [
        span
        for owner_spans in allocated.values()
        for span in owner_spans
    ]
    if not spans:
        return None
    best = sorted(
        spans,
        key=lambda span: (-(span.end - span.start), span.start, span.end, span.needle),
    )[0]
    return LocalizedSpan(
        start=best.start,
        end=best.end,
        surface=best.surface,
        method="longest_match",
    )


def localize_simalign(
    *,
    source_text: str,
    target_text: str,
    source_term: str,
    source_start: int,
    source_end: int,
    block_id: str,
    config: str,
    aligner: WordAligner,
    model: str = DEFAULT_SIMALIGN_MODEL,
    method: str = DEFAULT_SIMALIGN_METHOD,
    seed: int = DEFAULT_ALIGN_SEED,
) -> LocalizedSpan | None:
    occ = OccItem(
        occ_id=_occ_id(block_id, source_start, source_end, source_term),
        block_id=block_id,
        chapter_id="",
        source_term=source_term,
        glossary_id=source_term,
        src_start=source_start,
        src_end=source_end,
        src_surface=source_text[source_start:source_end],
        sentence_en=_sentence_for_span(source_text, source_start, source_end),
    )
    proposals = align_independent(
        [occ],
        {block_id: source_text},
        {block_id: target_text},
        config=config,
        aligner=aligner,
        align_method=method,
        model=model,
        seed=seed,
    )
    if not proposals:
        return None
    proposal = proposals[0]
    if proposal.target_start is None or proposal.target_end is None or not proposal.present:
        return None
    return LocalizedSpan(
        start=proposal.target_start,
        end=proposal.target_end,
        surface=proposal.target_surface or target_text[proposal.target_start:proposal.target_end],
        method="simalign",
        status=proposal.status,
        source="simalign",
    )


def localize_surface(
    text: str,
    surface: str,
    *,
    localizer: str = "longest_match",
    language: str = "vi",
    candidate_surfaces: Iterable[str] = (),
    source_text: str = "",
    source_term: str = "",
    source_start: int | None = None,
    source_end: int | None = None,
    block_id: str = "",
    config: str = "",
    aligner: WordAligner | None = None,
) -> LocalizedSpan | None:
    if localizer == "first_match":
        return localize_first_match(
            text,
            surface,
            language=language,
            candidate_surfaces=candidate_surfaces,
        )
    if localizer == "longest_match":
        return localize_longest_match(
            text,
            surface,
            language=language,
            candidate_surfaces=candidate_surfaces,
        )
    if localizer == "simalign":
        if aligner is None:
            return None
        if source_start is None or source_end is None:
            return None
        return localize_simalign(
            source_text=source_text,
            target_text=text,
            source_term=source_term,
            source_start=source_start,
            source_end=source_end,
            block_id=block_id,
            config=config,
            aligner=aligner,
        )
    raise ValueError(f"Unsupported localizer: {localizer}")


def representative_context(
    blocks: Iterable[Any],
    translations: dict[str, dict[str, str]],
    *,
    source_term: str,
    s0_surface: str,
    s1_surface: str,
    localizer: str = "longest_match",
) -> RepresentativeContext:
    fallback_source = ""
    candidate_surfaces = _candidate_surfaces(s0_surface, [s1_surface])
    for block in blocks:
        block_text = str(getattr(block, "text", ""))
        source_spans = find_spans(block_text, source_term, language="en")
        if not source_spans:
            continue
        source_start, source_end, _source = source_spans[0]
        fallback_source = fallback_source or _sentence_for_span(block_text, source_start, source_end)
        block_id = str(getattr(block, "block_id", ""))
        s0_text = translations["S0"].get(block_id, "")
        s1_text = translations["S1"].get(block_id, "")
        s0_span = localize_surface(
            s0_text,
            s0_surface,
            localizer=localizer,
            candidate_surfaces=candidate_surfaces,
            source_text=block_text,
            source_term=source_term,
            source_start=source_start,
            source_end=source_end,
            block_id=block_id,
            config="S0",
        )
        s1_span = localize_surface(
            s1_text,
            s1_surface,
            localizer=localizer,
            candidate_surfaces=candidate_surfaces,
            source_text=block_text,
            source_term=source_term,
            source_start=source_start,
            source_end=source_end,
            block_id=block_id,
            config="S1",
        )
        if s0_span is None or s1_span is None:
            continue
        if _surface_key(s0_span.surface) == _surface_key(s1_span.surface):
            continue
        return RepresentativeContext(
            resolved=True,
            skip_reason="",
            block_id=block_id,
            en_sentence=_sentence_for_span(block_text, source_start, source_end),
            s0_window=_mark_sentence_for_span(s0_text, s0_span.start, s0_span.end),
            s1_window=_mark_sentence_for_span(s1_text, s1_span.start, s1_span.end),
        )
    return RepresentativeContext(
        resolved=False,
        skip_reason="no block contains source term plus genuinely different target spans",
        block_id=None,
        en_sentence=fallback_source,
        s0_window="",
        s1_window="",
    )


def rep_occ_valid(
    *,
    source_term: str,
    s0_surface: str,
    s1_surface: str,
    blocks: Iterable[Any],
    translations: dict[str, dict[str, str]],
    localizer: str = "longest_match",
) -> tuple[bool, str | None, str]:
    rep = representative_context(
        blocks,
        translations,
        source_term=source_term,
        s0_surface=s0_surface,
        s1_surface=s1_surface,
        localizer=localizer,
    )
    if rep.resolved:
        return True, rep.block_id, ""
    return False, None, rep.skip_reason or "non_override_at_occurrence"


def build_localizer_gold(
    override_rows: list[dict[str, Any]],
    *,
    blocks_by_id: dict[str, str],
    translations: dict[str, dict[str, str]],
    include_edges: bool = True,
) -> list[LocalizerGoldRow]:
    rows: list[LocalizerGoldRow] = []
    for row in override_rows:
        block_id = str(row.get("rep_block_id") or "")
        if not block_id:
            continue
        source_text = blocks_by_id.get(block_id, "")
        source_term = str(row.get("source_term") or "")
        source_span = _first_source_span(source_text, source_term)
        for config, surface_key in [("S0", "s0_surface"), ("S1", "s1_surface")]:
            surface = str(row.get(surface_key) or "")
            target_text = translations.get(config, {}).get(block_id, "")
            if not surface or not target_text:
                continue
            candidates = _blind_candidates(
                target_text=target_text,
                surface=surface,
                source_text=source_text,
                source_term=source_term,
                source_span=source_span,
                block_id=block_id,
                config=config,
                candidate_surfaces=[row.get("s0_surface", ""), row.get("s1_surface", "")],
            )
            rows.append(_gold_row(
                item_id=str(row.get("item_id") or _stable_row_id(source_term)),
                config=config,
                source_term=source_term,
                surface=surface,
                block_id=block_id,
                registry_class="in",
                source_text=source_text,
                source_span=source_span,
                target_text=target_text,
                candidates=candidates,
                edge_kind="",
            ))

    if include_edges:
        rows.extend(_edge_rows(blocks_by_id=blocks_by_id, translations=translations))

    return rows


def run_localizers(
    gold_rows: Iterable[LocalizerGoldRow | dict[str, Any]],
    *,
    aligner_factory: LocalizerAlignerFactory | None = None,
    simalign_cache_dir: str | Path | None = None,
) -> dict[str, dict[str, LocalizedSpan | None]]:
    rows = [_row_dict(row) for row in gold_rows]
    proposals: dict[str, dict[str, LocalizedSpan | None]] = {name: {} for name in LOCALIZER_NAMES}
    for row in rows:
        row_id = str(row["row_id"])
        candidates = _candidate_surfaces_from_row(row)
        for name in ("first_match", "longest_match"):
            proposals[name][row_id] = localize_surface(
                str(row["target_text"]),
                str(row["surface"]),
                localizer=name,
                candidate_surfaces=candidates,
                source_text=str(row.get("source_text") or ""),
                source_term=str(row.get("source_term") or ""),
                source_start=_int_or_none(row.get("source_start")),
                source_end=_int_or_none(row.get("source_end")),
                block_id=str(row.get("block_id") or ""),
                config=str(row.get("config") or ""),
            )
        proposals["simalign"][row_id] = None
    if aligner_factory is not None:
        proposals["simalign"].update(_run_simalign_rows(
            rows,
            aligner_factory,
            cache_dir=simalign_cache_dir,
        ))
    return proposals


def _run_simalign_rows(
    rows: list[dict[str, Any]],
    aligner_factory: LocalizerAlignerFactory,
    *,
    cache_dir: str | Path | None = None,
) -> dict[str, LocalizedSpan | None]:
    result: dict[str, LocalizedSpan | None] = {}
    aligner: WordAligner | None = None
    grouped: dict[tuple[str, str, int, int, int, int], list[dict[str, Any]]] = {}
    for row in rows:
        window = _simalign_sentence_window(row)
        if window is None:
            result[str(row["row_id"])] = None
            continue
        config = str(row.get("config") or "")
        key = (
            config,
            str(row.get("block_id") or ""),
            window["source_start"],
            window["source_end"],
            window["target_start"],
            window["target_end"],
        )
        enriched = dict(row)
        enriched["_simalign_window"] = window
        grouped.setdefault(key, []).append(enriched)

    def get_aligner() -> WordAligner:
        nonlocal aligner
        if aligner is None:
            aligner = aligner_factory()
        return aligner

    for key, group_rows in grouped.items():
        config, _block_id, source_window_start, _source_window_end, target_window_start, _target_window_end = key
        first_window = group_rows[0]["_simalign_window"]
        source_window = str(first_window["source_text"])
        target_window = str(first_window["target_text"])
        group_id = _simalign_group_id(key, source_window, target_window)
        occs: list[OccItem] = []
        full_target = str(group_rows[0].get("target_text") or "")
        for row in group_rows:
            row_id = str(row["row_id"])
            source_start = _int_or_none(row.get("source_start"))
            source_end = _int_or_none(row.get("source_end"))
            source_term = str(row.get("source_term") or "")
            if source_start is None or source_end is None:
                result[row_id] = None
                continue
            occs.append(OccItem(
                occ_id=row_id,
                block_id=group_id,
                chapter_id="",
                source_term=source_term,
                glossary_id=source_term,
                src_start=source_start - source_window_start,
                src_end=source_end - source_window_start,
                src_surface=source_window[source_start - source_window_start:source_end - source_window_start],
                sentence_en=source_window,
            ))
        if not occs:
            continue
        def compute() -> list[Any]:
            return align_independent(
                occs,
                {group_id: source_window},
                {group_id: target_window},
                config=config,
                aligner=get_aligner(),
                align_method=DEFAULT_SIMALIGN_METHOD,
                model=DEFAULT_SIMALIGN_MODEL,
                seed=DEFAULT_ALIGN_SEED,
            )

        if cache_dir is not None:
            cache_key = simalign_cache_key(
                model=DEFAULT_SIMALIGN_MODEL,
                method=DEFAULT_SIMALIGN_METHOD,
                seed=DEFAULT_ALIGN_SEED,
                block_ids=[group_id],
                config=config,
                source_hash=_text_hash(source_window),
                target_hash=_text_hash(target_window),
            )
            proposal_rows = simalign_cached(cache_dir=cache_dir, cache_key=cache_key, compute=compute)
        else:
            proposal_rows = [asdict(proposal) for proposal in compute()]

        for proposal in proposal_rows:
            occ_id = str(proposal["occ_id"])
            target_start = _int_or_none(proposal.get("target_start"))
            target_end = _int_or_none(proposal.get("target_end"))
            present = bool(proposal.get("present"))
            if target_start is None or target_end is None or not present:
                result[occ_id] = None
                continue
            absolute_start = target_window_start + target_start
            absolute_end = target_window_start + target_end
            result[occ_id] = LocalizedSpan(
                start=absolute_start,
                end=absolute_end,
                surface=full_target[absolute_start:absolute_end],
                method="simalign",
                status=str(proposal.get("status") or ""),
                source=str(proposal.get("source") or "simalign"),
            )
    return result


def score_localizer_bakeoff(
    gold_rows: Iterable[LocalizerGoldRow | dict[str, Any]],
    proposals: dict[str, dict[str, LocalizedSpan | None]],
) -> dict[str, Any]:
    rows = [_row_dict(row) for row in gold_rows]
    missing_gold = [
        row["row_id"]
        for row in rows
        if _int_or_none(row.get("gold_target_start")) is None
        or _int_or_none(row.get("gold_target_end")) is None
    ]
    if missing_gold:
        raise ValueError(f"localizer gold incomplete: {len(missing_gold)} rows missing target offsets")

    metric_a_rows = [row for row in rows if row.get("registry_class") == "in"]
    metric_b_rows = [row for row in rows if row.get("registry_class") == "out"]
    scores: dict[str, Any] = {}
    for name in LOCALIZER_NAMES:
        exact = 0
        missing = 0
        regression_fail: list[str] = []
        for row in metric_a_rows:
            proposal = proposals.get(name, {}).get(row["row_id"])
            if proposal is None:
                missing += 1
                if _is_regression_row(row):
                    regression_fail.append(str(row["row_id"]))
                continue
            ok = proposal.start == int(row["gold_target_start"]) and proposal.end == int(row["gold_target_end"])
            exact += int(ok)
            if _is_regression_row(row) and not ok:
                regression_fail.append(str(row["row_id"]))
        scores[name] = {
            "metricA_n": len(metric_a_rows),
            "metricA_exact": exact,
            "metricA_accuracy": exact / len(metric_a_rows) if metric_a_rows else None,
            "missing": missing,
            "regression_fail": regression_fail,
            "eligible_for_recommendation": not regression_fail and (name != "simalign" or name in proposals),
        }

    metric_b = {}
    for name in LOCALIZER_NAMES:
        exact = 0
        for row in metric_b_rows:
            proposal = proposals.get(name, {}).get(row["row_id"])
            if proposal is None:
                continue
            exact += int(
                proposal.start == int(row["gold_target_start"])
                and proposal.end == int(row["gold_target_end"])
            )
        metric_b[name] = {
            "n": len(metric_b_rows),
            "exact": exact,
            "recall": exact / len(metric_b_rows) if metric_b_rows else None,
        }

    recommendation = _recommend(scores)
    return {
        "metricA": scores,
        "metricB_out_of_registry": metric_b,
        "recommendation": recommendation,
        "gold_rows": len(rows),
        "human_adjudicated_pct": _human_adjudicated_pct(rows),
        "rubber_stamp_warning": _rubber_stamp_warning(rows),
        "localizers": list(LOCALIZER_NAMES),
    }


def validate_gold_occ_matches_scorer_rep_occ(
    gold_rows: Iterable[LocalizerGoldRow | dict[str, Any]],
    override_rows: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Hard-fail when human gold points at a different source occurrence.

    The bake-off gold is only meaningful if the human-marked target span is tied
    to the same representative source occurrence that the scorer/override-set is
    evaluating. This is intentionally source-side only; target localization is
    what the bake-off measures.
    """
    rows = [_row_dict(row) for row in gold_rows]
    override_by_item = {
        str(row.get("item_id") or ""): dict(row)
        for row in override_rows
        if row.get("item_id")
    }
    failures: list[dict[str, Any]] = []
    checked = 0
    edge_checked = 0
    seen_item_source: dict[str, tuple[str, int, int]] = {}

    for row in rows:
        start = _int_or_none(row.get("source_start"))
        end = _int_or_none(row.get("source_end"))
        source_text = str(row.get("source_text") or "")
        source_term = str(row.get("source_term") or "")
        row_id = str(row.get("row_id") or "")
        item_id = str(row.get("item_id") or "")
        block_id = str(row.get("block_id") or "")
        if start is None or end is None:
            failures.append({
                "row_id": row_id,
                "term": source_term,
                "reason": "missing_gold_source_offsets",
                "gold_offset": [row.get("source_start"), row.get("source_end")],
            })
            continue
        if start < 0 or end <= start or end > len(source_text):
            failures.append({
                "row_id": row_id,
                "term": source_term,
                "reason": "gold_source_offsets_out_of_bounds",
                "gold_offset": [start, end],
                "source_length": len(source_text),
            })
            continue
        if source_text[start:end] != str(row.get("source_surface") or ""):
            failures.append({
                "row_id": row_id,
                "term": source_term,
                "reason": "source_surface_mismatch",
                "gold_offset": [start, end],
                "gold_surface": row.get("source_surface"),
                "actual_surface": source_text[start:end],
            })
            continue

        current_source = (block_id, start, end)
        prior_source = seen_item_source.setdefault(item_id, current_source)
        if prior_source != current_source:
            failures.append({
                "row_id": row_id,
                "term": source_term,
                "reason": "item_configs_disagree_on_source_occurrence",
                "gold_offset": [start, end],
                "prior_offset": [prior_source[1], prior_source[2]],
                "block_id": block_id,
                "prior_block_id": prior_source[0],
            })
            continue

        override = override_by_item.get(item_id)
        if override is None:
            if str(row.get("registry_class") or "") == "in" and not row.get("edge_kind"):
                failures.append({
                    "row_id": row_id,
                    "term": source_term,
                    "reason": "missing_override_for_in_registry_gold_row",
                    "gold_offset": [start, end],
                })
                continue
            edge_checked += 1
            if (start, end, source_text[start:end]) not in find_spans(source_text, source_term, language="en"):
                failures.append({
                    "row_id": row_id,
                    "term": source_term,
                    "reason": "gold_source_occurrence_not_matched_by_scorer_matcher",
                    "gold_offset": [start, end],
                })
            continue

        checked += 1
        expected_block = str(override.get("rep_block_id") or "")
        if block_id != expected_block:
            failures.append({
                "row_id": row_id,
                "term": source_term,
                "reason": "block_id_mismatch",
                "gold_block_id": block_id,
                "scorer_block_id": expected_block,
                "gold_offset": [start, end],
            })
            continue
        expected_span = _expected_source_span_from_override(row, override)
        if expected_span is None:
            failures.append({
                "row_id": row_id,
                "term": source_term,
                "reason": "cannot_resolve_scorer_rep_occurrence",
                "gold_offset": [start, end],
                "scorer_sentence": override.get("en_sentence"),
            })
            continue
        if (start, end) != expected_span[:2]:
            failures.append({
                "row_id": row_id,
                "term": source_term,
                "reason": "gold_occ_differs_from_scorer_rep_occ",
                "gold_offset": [start, end],
                "scorer_offset": [expected_span[0], expected_span[1]],
                "gold_sentence": _sentence_for_span(source_text, start, end),
                "scorer_sentence": override.get("en_sentence"),
            })

    report = {
        "checked": checked,
        "edge_checked": edge_checked,
        "failures": failures,
    }
    if failures:
        preview = "; ".join(
            f"{failure.get('term')} {failure.get('gold_offset')} vs {failure.get('scorer_offset', 'n/a')}"
            for failure in failures[:5]
        )
        raise ValueError(f"gold occurrence reconciliation failed: {len(failures)} failure(s): {preview}")
    return report


def write_gold_csv(path: str | Path, rows: list[LocalizerGoldRow]) -> None:
    _write_csv(path, [asdict(row) for row in rows])


def read_gold_csv(path: str | Path) -> list[dict[str, Any]]:
    with Path(path).open("r", encoding="utf-8", newline="") as fh:
        return [dict(row) for row in csv.DictReader(fh)]


def write_bakeoff_report(path: str | Path, report: dict[str, Any]) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def render_localizer_gold_html(rows: list[dict[str, Any] | LocalizerGoldRow]) -> str:
    payload = [_row_dict(row) for row in rows]
    data_json = json.dumps(payload, ensure_ascii=False)
    return f"""<!doctype html>
<html lang="vi">
<head>
  <meta charset="utf-8">
  <title>Localizer Gold Worksheet</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f6f8fa;
      --panel: #ffffff;
      --text: #24292f;
      --muted: #57606a;
      --border: #d8dee4;
      --accent: #0969da;
      --accent-bg: #ddf4ff;
      --warn: #9a6700;
      --warn-bg: #fff8c5;
      --ok: #1a7f37;
      --ok-bg: #dafbe1;
      --danger: #cf222e;
      --danger-bg: #ffebe9;
      --mark: #fff3bf;
      --candidate: #f6f8fa;
      --candidate-selected: #dbeafe;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.45;
    }}
    header {{
      position: sticky;
      top: 0;
      z-index: 5;
      background: rgba(246, 248, 250, 0.94);
      backdrop-filter: blur(8px);
      border-bottom: 1px solid var(--border);
    }}
    .topbar {{
      max-width: 1220px;
      margin: 0 auto;
      padding: 16px 20px;
      display: grid;
      gap: 10px;
    }}
    h1 {{ margin: 0; font-size: 24px; letter-spacing: 0; }}
    .muted {{ color: var(--muted); font-size: 13px; }}
    .toolbar {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }}
    button, .button {{
      appearance: none;
      border: 1px solid var(--border);
      background: var(--panel);
      color: var(--text);
      border-radius: 6px;
      padding: 8px 12px;
      font: inherit;
      font-size: 14px;
      cursor: pointer;
    }}
    button.primary {{ background: var(--accent); color: #fff; border-color: var(--accent); }}
    button:hover {{ border-color: var(--accent); }}
    main {{
      max-width: 1220px;
      margin: 0 auto;
      padding: 18px 20px 48px;
    }}
    .stats {{ display: flex; flex-wrap: wrap; gap: 8px; }}
    .pill {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      border: 1px solid var(--border);
      border-radius: 999px;
      background: var(--panel);
      padding: 4px 9px;
      font-size: 13px;
      color: var(--muted);
    }}
    .pill.warn {{ color: var(--warn); background: var(--warn-bg); border-color: #f0d98c; }}
    .pill.ok {{ color: var(--ok); background: var(--ok-bg); border-color: #aceebb; }}
    .item {{
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--panel);
      margin: 14px 0;
      overflow: hidden;
    }}
    .item.need {{ border-left: 5px solid var(--warn); }}
    .item.done {{ border-left: 5px solid var(--ok); }}
    .item-head {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      padding: 14px 16px;
      border-bottom: 1px solid var(--border);
      background: #fbfbfc;
    }}
    .item-title {{ font-weight: 650; }}
    .grid {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(360px, 0.85fr);
      gap: 14px;
      padding: 16px;
    }}
    @media (max-width: 900px) {{ .grid {{ grid-template-columns: 1fr; }} }}
    .label {{ font-size: 12px; color: var(--muted); font-weight: 700; text-transform: uppercase; }}
    .text-box {{
      white-space: pre-wrap;
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 10px;
      margin: 6px 0 12px;
      background: #fff;
      font-size: 15px;
      user-select: text;
    }}
    .target-box {{ cursor: text; }}
    mark {{
      background: var(--mark);
      border: 1px solid #d4a72c;
      border-radius: 3px;
      padding: 0 2px;
    }}
    mark.source {{ background: var(--accent-bg); border-color: #80ccff; }}
    mark.gold {{ background: var(--ok-bg); border-color: #6fdd8b; }}
    .candidate {{
      width: 100%;
      text-align: left;
      background: var(--candidate);
      margin: 7px 0;
      display: grid;
      gap: 2px;
    }}
    .candidate.selected {{
      border-color: var(--accent);
      background: var(--candidate-selected);
    }}
    .candidate .surface {{ font-weight: 650; }}
    .selection-button {{
      width: 100%;
      margin: 0 0 10px;
      border-style: dashed;
      color: var(--accent);
    }}
    .form-grid {{
      display: grid;
      grid-template-columns: 110px 110px minmax(0, 1fr);
      gap: 8px;
      margin-top: 10px;
    }}
    input, textarea {{
      width: 100%;
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 8px;
      font: inherit;
      font-size: 14px;
    }}
    textarea {{ min-height: 58px; resize: vertical; }}
    .status {{
      font-weight: 650;
      font-size: 13px;
    }}
    .status.need {{ color: var(--warn); }}
    .status.done {{ color: var(--ok); }}
    .status.auto {{ color: var(--muted); }}
    .hidden {{ display: none; }}
    .help {{
      border: 1px solid var(--border);
      background: var(--panel);
      border-radius: 8px;
      padding: 12px;
      margin-top: 12px;
      color: var(--muted);
      font-size: 14px;
    }}
    code {{ background: #f6f8fa; border: 1px solid var(--border); padding: 1px 4px; border-radius: 4px; }}
  </style>
</head>
<body>
  <header>
    <div class="topbar">
      <div>
        <h1>Localizer Gold Worksheet</h1>
        <div class="muted">Điền gold span cho các dòng <code>human_required</code>. Gold = cụm VI nhỏ nhất diễn đạt trọn term nguồn tại occurrence này.</div>
      </div>
      <div class="toolbar">
        <button id="show-needed">Chỉ dòng cần người</button>
        <button id="show-all">Hiện tất cả</button>
        <button id="export" class="primary">Export localizer_gold.csv</button>
      </div>
      <div id="stats" class="stats"></div>
    </div>
  </header>
  <main>
    <div class="help">
      Chọn một candidate nếu đúng. Nếu cả hai candidate đều chưa đủ, bôi đen cụm đúng trong <code>Target output</code> rồi bấm <code>Dùng phần bôi đen</code>; hệ thống sẽ tự điền <code>start/end/span</code>.
      Sau khi xong, bấm export và thay file <code>THESIS_RUNTIME_TOOL/data/eval/localizer_gold.csv</code> bằng file tải về.
    </div>
    <div id="items"></div>
  </main>
  <script>
    const rows = {data_json};
    let showMode = "needed";
    const headers = rows.length ? Object.keys(rows[0]) : [];

    function escapeHtml(value) {{
      return String(value ?? "").replace(/[&<>"']/g, ch => ({{"&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;","'":"&#39;"}})[ch]);
    }}
    function candidateList(row) {{
      return ["candidate_1", "candidate_2", "candidate_3", "candidate_4"]
        .map(key => row[key])
        .filter(Boolean)
        .map(value => {{
          try {{ return JSON.parse(value); }} catch (_err) {{ return null; }}
        }})
        .filter(Boolean);
    }}
    function isDone(row) {{
      return String(row.gold_target_start || "") !== "" && String(row.gold_target_end || "") !== "" && String(row.gold_target_span || "") !== "";
    }}
    function needsHuman(row) {{
      return row.prefilled === "human_required" || !isDone(row);
    }}
    function highlightSpan(text, start, end, cls = "") {{
      const s = Number(start);
      const e = Number(end);
      const value = String(text ?? "");
      if (!Number.isFinite(s) || !Number.isFinite(e) || s < 0 || e <= s || e > value.length) {{
        return escapeHtml(value);
      }}
      return escapeHtml(value.slice(0, s)) + `<mark class="${{cls}}">` + escapeHtml(value.slice(s, e)) + "</mark>" + escapeHtml(value.slice(e));
    }}
    function rowStatus(row) {{
      if (row.prefilled === "human_required" && !isDone(row)) return ["need", "human required"];
      if (row.human_edited === "true") return ["done", "human edited"];
      if (isDone(row)) return ["auto", row.prefilled === "auto" ? "auto prefilled" : "filled"];
      return ["need", "missing"];
    }}
    function renderStats() {{
      const total = rows.length;
      const need = rows.filter(needsHuman).length;
      const missing = rows.filter(row => !isDone(row)).length;
      const human = rows.filter(row => row.human_edited === "true").length;
      document.getElementById("stats").innerHTML = `
        <span class="pill">rows: ${{total}}</span>
        <span class="pill warn">need review: ${{need}}</span>
        <span class="pill warn">missing: ${{missing}}</span>
        <span class="pill ok">human edited: ${{human}}</span>
      `;
    }}
    function render() {{
      renderStats();
      const root = document.getElementById("items");
      root.innerHTML = "";
      rows.forEach((row, index) => {{
        const visible = showMode === "all" || needsHuman(row);
        const [statusClass, statusText] = rowStatus(row);
        const item = document.createElement("section");
        item.className = `item ${{needsHuman(row) ? "need" : "done"}}${{visible ? "" : " hidden"}}`;
        item.dataset.index = index;
        const candidates = candidateList(row);
        const candidatesHtml = candidates.length ? candidates.map((cand, candIndex) => {{
          const selected = String(row.gold_target_start) === String(cand.start) && String(row.gold_target_end) === String(cand.end);
          return `<button class="candidate${{selected ? " selected" : ""}}" data-index="${{index}}" data-candidate="${{candIndex}}">
            <span class="label">candidate ${{candIndex + 1}}</span>
            <span class="surface">${{escapeHtml(cand.surface)}}</span>
            <span class="muted">start=${{cand.start}} · end=${{cand.end}}</span>
          </button>`;
        }}).join("") : `<div class="muted">Không có candidate surface. Điền thủ công nếu cần.</div>`;
        item.innerHTML = `
          <div class="item-head">
            <div>
              <div class="item-title">${{escapeHtml(row.row_id)}}</div>
              <div class="muted">${{escapeHtml(row.source_term)}} · ${{escapeHtml(row.config)}} · ${{escapeHtml(row.registry_class)}} · ${{escapeHtml(row.block_id)}}</div>
            </div>
            <div class="status ${{statusClass}}">${{statusText}}</div>
          </div>
          <div class="grid">
            <div>
              <div class="label">Source term / source text</div>
              <div class="text-box">${{highlightSpan(row.source_text, row.source_start, row.source_end, "source")}}</div>
              <div class="label">Target output</div>
              <div class="text-box target-box" data-target-box="${{index}}">${{highlightSpan(row.target_text, row.gold_target_start, row.gold_target_end, "gold")}}</div>
            </div>
            <div>
              <div class="label">Candidates blind</div>
              ${{candidatesHtml}}
              <button class="selection-button" data-use-selection="${{index}}">Dùng phần bôi đen trong Target output</button>
              <div class="form-grid">
                <label><span class="label">start</span><input data-field="gold_target_start" data-index="${{index}}" value="${{escapeHtml(row.gold_target_start)}}"></label>
                <label><span class="label">end</span><input data-field="gold_target_end" data-index="${{index}}" value="${{escapeHtml(row.gold_target_end)}}"></label>
                <label><span class="label">span</span><input data-field="gold_target_span" data-index="${{index}}" value="${{escapeHtml(row.gold_target_span)}}"></label>
              </div>
              <p><span class="label">note</span><textarea data-field="note" data-index="${{index}}">${{escapeHtml(row.note)}}</textarea></p>
            </div>
          </div>
        `;
        root.appendChild(item);
      }});
      attachHandlers();
    }}
    function attachHandlers() {{
      document.querySelectorAll(".candidate").forEach(button => {{
        button.addEventListener("mousedown", event => event.preventDefault());
        button.addEventListener("click", () => {{
          const row = rows[Number(button.dataset.index)];
          const cand = candidateList(row)[Number(button.dataset.candidate)];
          row.gold_target_start = String(cand.start);
          row.gold_target_end = String(cand.end);
          row.gold_target_span = String(cand.surface);
          row.human_edited = "true";
          render();
        }});
      }});
      document.querySelectorAll("[data-use-selection]").forEach(button => {{
        button.addEventListener("mousedown", event => event.preventDefault());
        button.addEventListener("click", () => {{
          const index = Number(button.dataset.useSelection);
          const row = rows[index];
          const box = document.querySelector(`[data-target-box="${{index}}"]`);
          const selected = selectedTextInBox(box);
          if (!selected) {{
            window.alert("Hãy bôi đen một cụm trong Target output của đúng dòng này trước.");
            return;
          }}
          row.gold_target_start = String(selected.start);
          row.gold_target_end = String(selected.end);
          row.gold_target_span = selected.surface;
          row.human_edited = "true";
          render();
        }});
      }});
      document.querySelectorAll("input[data-field], textarea[data-field]").forEach(input => {{
        input.addEventListener("input", () => {{
          const row = rows[Number(input.dataset.index)];
          row[input.dataset.field] = input.value;
          row.human_edited = "true";
          renderStats();
        }});
      }});
    }}
    function selectedTextInBox(box) {{
      const selection = window.getSelection();
      if (!box || !selection || selection.rangeCount === 0 || selection.toString().length === 0) {{
        return null;
      }}
      const range = selection.getRangeAt(0);
      if (!box.contains(range.commonAncestorContainer)) {{
        return null;
      }}
      const before = document.createRange();
      before.selectNodeContents(box);
      before.setEnd(range.startContainer, range.startOffset);
      const start = before.toString().length;
      const surface = selection.toString();
      const end = start + surface.length;
      if (start < 0 || end <= start) {{
        return null;
      }}
      return {{start, end, surface}};
    }}
    function csvEscape(value) {{
      const text = String(value ?? "");
      return /[",\\n\\r]/.test(text) ? '"' + text.replaceAll('"', '""') + '"' : text;
    }}
    function exportCsv() {{
      const csv = [headers.join(",")].concat(rows.map(row => headers.map(key => csvEscape(row[key])).join(","))).join("\\n") + "\\n";
      const blob = new Blob([csv], {{type: "text/csv;charset=utf-8"}});
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "localizer_gold.csv";
      a.click();
      URL.revokeObjectURL(url);
    }}
    document.getElementById("show-needed").addEventListener("click", () => {{ showMode = "needed"; render(); }});
    document.getElementById("show-all").addEventListener("click", () => {{ showMode = "all"; render(); }});
    document.getElementById("export").addEventListener("click", exportCsv);
    render();
  </script>
</body>
</html>
"""


def prefill_gold_rows(rows: list[LocalizerGoldRow]) -> list[LocalizerGoldRow]:
    filled: list[LocalizerGoldRow] = []
    for row in rows:
        candidates = [candidate for candidate in _candidate_fields(asdict(row)) if candidate]
        unique = []
        seen = set()
        for candidate in candidates:
            try:
                payload = json.loads(candidate)
                key = (payload.get("start"), payload.get("end"), payload.get("surface"))
            except json.JSONDecodeError:
                key = candidate
            if key not in seen:
                seen.add(key)
                unique.append(candidate)
        if len(unique) == 1:
            payload = json.loads(unique[0])
            filled.append(
                LocalizerGoldRow(
                    **{
                        **asdict(row),
                        "prefilled": "auto",
                        "gold_target_start": str(payload["start"]),
                        "gold_target_end": str(payload["end"]),
                        "gold_target_span": str(payload["surface"]),
                    }
                )
            )
        else:
            filled.append(
                LocalizerGoldRow(
                    **{
                        **asdict(row),
                        "prefilled": "human_required",
                        "gold_target_start": "",
                        "gold_target_end": "",
                        "gold_target_span": "",
                    }
                )
            )
    return filled


def _blind_candidates(
    *,
    target_text: str,
    surface: str,
    source_text: str,
    source_term: str,
    source_span: tuple[int, int, str] | None,
    block_id: str,
    config: str,
    candidate_surfaces: Iterable[str],
) -> list[dict[str, Any]]:
    spans = {
        "first_match": localize_first_match(target_text, surface, candidate_surfaces=candidate_surfaces),
        "longest_match": localize_longest_match(target_text, surface, candidate_surfaces=candidate_surfaces),
    }
    # SimAlign is intentionally not run while building the default template; the
    # score path can inject a real or fake aligner. Keep the blind slot explicit.
    spans["simalign"] = None
    if source_span is not None:
        del source_text, source_term, block_id, config

    candidates = [
        {"start": span.start, "end": span.end, "surface": span.surface}
        for span in spans.values()
        if span is not None
    ]
    rng = random.Random(_stable_row_id(target_text + surface))
    rng.shuffle(candidates)
    return candidates


def _gold_row(
    *,
    item_id: str,
    config: str,
    source_term: str,
    surface: str,
    block_id: str,
    registry_class: str,
    source_text: str,
    source_span: tuple[int, int, str] | None,
    target_text: str,
    candidates: list[dict[str, Any]],
    edge_kind: str,
) -> LocalizerGoldRow:
    fields = [json.dumps(candidate, ensure_ascii=False, sort_keys=True) for candidate in candidates[:4]]
    while len(fields) < 4:
        fields.append("")
    source_start, source_end, source_surface = ("", "", "")
    if source_span is not None:
        source_start, source_end, source_surface = str(source_span[0]), str(source_span[1]), source_span[2]
    row = LocalizerGoldRow(
        row_id=f"{item_id}:{config}",
        item_id=item_id,
        config=config,
        source_term=source_term,
        surface=surface,
        block_id=block_id,
        registry_class=registry_class,
        source_text=source_text,
        source_start=source_start,
        source_end=source_end,
        source_surface=source_surface,
        target_text=target_text,
        candidate_1=fields[0],
        candidate_2=fields[1],
        candidate_3=fields[2],
        candidate_4=fields[3],
        candidates_blind_json=json.dumps(fields, ensure_ascii=False),
        prefilled="",
        human_edited="",
        gold_target_start="",
        gold_target_end="",
        gold_target_span="",
        note="",
        edge_kind=edge_kind,
    )
    return prefill_gold_rows([row])[0]


def _edge_rows(
    *,
    blocks_by_id: dict[str, str],
    translations: dict[str, dict[str, str]],
) -> list[LocalizerGoldRow]:
    rows: list[LocalizerGoldRow] = []
    edge_specs = [
        {
            "item_id": "edge_set_to_bo",
            "source_term": "set",
            "surface": "bộ",
            "registry_class": "out",
            "edge_kind": "out_of_registry",
            "block_hint": "d2l_preliminaries_index_b003",
        },
        {
            "item_id": "edge_learning_machine_learning",
            "source_term": "machine learning",
            "surface": "học máy",
            "registry_class": "in",
            "edge_kind": "cross_term",
            "block_hint": "d2l_preliminaries_index_b002",
        },
    ]
    for spec in edge_specs:
        block_id = _find_block_id(blocks_by_id, spec["source_term"], str(spec["block_hint"]))
        if not block_id:
            continue
        source_text = blocks_by_id[block_id]
        source_span = _first_source_span(source_text, spec["source_term"])
        for config in ("S0", "S1"):
            target_text = translations.get(config, {}).get(block_id, "")
            if not target_text:
                continue
            candidates = _blind_candidates(
                target_text=target_text,
                surface=str(spec["surface"]),
                source_text=source_text,
                source_term=str(spec["source_term"]),
                source_span=source_span,
                block_id=block_id,
                config=config,
                candidate_surfaces=[str(spec["surface"])],
            )
            rows.append(_gold_row(
                item_id=str(spec["item_id"]),
                config=config,
                source_term=str(spec["source_term"]),
                surface=str(spec["surface"]),
                block_id=block_id,
                registry_class=str(spec["registry_class"]),
                source_text=source_text,
                source_span=source_span,
                target_text=target_text,
                candidates=candidates,
                edge_kind=str(spec["edge_kind"]),
            ))
    return rows


def _find_block_id(blocks_by_id: dict[str, str], term: str, hint: str) -> str:
    if hint in blocks_by_id and find_spans(blocks_by_id[hint], term, language="en"):
        return hint
    for block_id, text in sorted(blocks_by_id.items()):
        if find_spans(text, term, language="en"):
            return block_id
    return ""


def _first_source_span(source_text: str, source_term: str) -> tuple[int, int, str] | None:
    spans = find_spans(source_text, source_term, language="en")
    if not spans:
        return None
    return spans[0]


def _candidate_surfaces(surface: str, candidates: Iterable[str] = ()) -> list[str]:
    result: list[str] = []
    seen = set()
    for item in [surface, *list(candidates)]:
        text = str(item or "").strip()
        key = _surface_key(text)
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return result


def _candidate_surfaces_from_row(row: dict[str, Any]) -> list[str]:
    result = [str(row.get("surface") or "")]
    for field in _candidate_fields(row):
        if not field:
            continue
        try:
            payload = json.loads(field)
        except json.JSONDecodeError:
            continue
        surface = str(payload.get("surface") or "")
        if surface:
            result.append(surface)
    return _candidate_surfaces(result[0], result[1:])


def _candidate_fields(row: dict[str, Any]) -> list[str]:
    return [
        str(row.get("candidate_1") or ""),
        str(row.get("candidate_2") or ""),
        str(row.get("candidate_3") or ""),
        str(row.get("candidate_4") or ""),
    ]


def _row_dict(row: LocalizerGoldRow | dict[str, Any]) -> dict[str, Any]:
    return asdict(row) if isinstance(row, LocalizerGoldRow) else dict(row)


def _language(value: str) -> str:
    if value not in {"en", "vi"}:
        raise ValueError(f"Unsupported localizer language: {value}")
    return value


def _surface_key(value: str) -> str:
    normalized = normalize_surface(value)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized.casefold()


def _sentence_for_span(text: str, start: int, end: int) -> str:
    value = str(text or "")
    left = max(value.rfind(".", 0, start), value.rfind("?", 0, start), value.rfind("!", 0, start), value.rfind("\n", 0, start))
    right_candidates = [idx for idx in [value.find(".", end), value.find("?", end), value.find("!", end), value.find("\n", end)] if idx >= 0]
    right = min(right_candidates) + 1 if right_candidates else len(value)
    return re.sub(r"\s+", " ", value[left + 1:right]).strip()


def _mark_sentence_for_span(text: str, start: int, end: int) -> str:
    sentence = _sentence_for_span(text, start, end)
    value = str(text or "")
    sentence_start = value.find(sentence)
    if sentence_start < 0 or not sentence:
        return value[:start] + "«" + value[start:end] + "»" + value[end:]
    local_start = max(0, start - sentence_start)
    local_end = max(local_start, end - sentence_start)
    return sentence[:local_start] + "«" + sentence[local_start:local_end] + "»" + sentence[local_end:]


def _stable_row_id(value: str) -> str:
    digest = hashlib.sha1(_surface_key(value).encode("utf-8")).hexdigest()[:10]
    slug = re.sub(r"[^a-z0-9]+", "_", _surface_key(value)).strip("_")[:48] or "row"
    return f"loc_{slug}_{digest}"


def _occ_id(block_id: str, start: int, end: int, source_term: str) -> str:
    digest = hashlib.sha1(f"{block_id}:{start}:{end}:{source_term}".encode("utf-8")).hexdigest()[:12]
    return f"loc_{digest}"


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _simalign_sentence_window(row: dict[str, Any]) -> dict[str, Any] | None:
    source_text = str(row.get("source_text") or "")
    target_text = str(row.get("target_text") or "")
    source_start = _int_or_none(row.get("source_start"))
    source_end = _int_or_none(row.get("source_end"))
    if source_start is None or source_end is None or not source_text or not target_text:
        return None
    source_spans = _sentence_spans(source_text)
    source_index = _sentence_index_for_span(source_spans, source_start, source_end)
    if source_index is None:
        return None
    source_window_start, source_window_end = source_spans[source_index]
    return {
        "source_start": source_window_start,
        "source_end": source_window_end,
        "source_text": source_text[source_window_start:source_window_end],
        "target_start": 0,
        "target_end": len(target_text),
        "target_text": target_text,
    }


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    value = str(text or "")
    spans: list[tuple[int, int]] = []
    start = 0
    for index, char in enumerate(value):
        if char not in ".?!\n":
            continue
        end = index + 1
        segment_start, segment_end = _trim_span(value, start, end)
        if segment_end > segment_start:
            spans.append((segment_start, segment_end))
        start = end
    segment_start, segment_end = _trim_span(value, start, len(value))
    if segment_end > segment_start:
        spans.append((segment_start, segment_end))
    return spans or [(0, len(value))]


def _sentence_index_for_span(
    spans: list[tuple[int, int]],
    start: int,
    end: int,
) -> int | None:
    for index, (span_start, span_end) in enumerate(spans):
        if span_start <= start and end <= span_end:
            return index
    return None


def _trim_span(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _simalign_group_id(key: tuple[str, str, int, int, int, int], source: str, target: str) -> str:
    return "loc_simalign_" + _text_hash(json.dumps([key, source, target], ensure_ascii=False))[:16]


def _text_hash(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _expected_source_span_from_override(
    row: dict[str, Any],
    override: dict[str, Any],
) -> tuple[int, int, str] | None:
    source_text = str(row.get("source_text") or "")
    source_term = str(row.get("source_term") or "")
    expected_sentence = _sentence_key(str(override.get("en_sentence") or ""))
    if not source_text or not source_term or not expected_sentence:
        return None
    matches = [
        span
        for span in find_spans(source_text, source_term, language="en")
        if _sentence_key(_sentence_for_span(source_text, span[0], span[1])) == expected_sentence
    ]
    if not matches:
        return None
    return matches[0]


def _sentence_key(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _is_regression_row(row: dict[str, Any]) -> bool:
    if row.get("edge_kind"):
        return True
    return str(row.get("source_term") or "") in {
        "MNIST dataset",
        "machine learning",
    }


def _recommend(scores: dict[str, Any]) -> str | None:
    eligible = [
        (name, row.get("metricA_accuracy"))
        for name, row in scores.items()
        if row.get("eligible_for_recommendation") and row.get("metricA_accuracy") is not None
    ]
    if not eligible:
        return None
    best_score = max(float(score) for _name, score in eligible)
    winners = {name for name, score in eligible if float(score) == best_score}
    if "longest_match" in winners:
        return "longest_match"
    return sorted(winners)[0]


def _human_adjudicated_pct(rows: list[dict[str, Any]]) -> float:
    if not rows:
        return 0.0
    needed = [row for row in rows if row.get("prefilled") == "human_required"]
    if not needed:
        return 0.0
    edited = [row for row in needed if str(row.get("human_edited") or "").lower() in {"true", "1", "yes"}]
    return len(edited) / len(needed)


def _rubber_stamp_warning(rows: list[dict[str, Any]]) -> bool:
    required = [row for row in rows if row.get("prefilled") == "human_required"]
    if not required:
        return False
    edited = [row for row in required if str(row.get("human_edited") or "").lower() in {"true", "1", "yes"}]
    return len(edited) == 0


def _write_csv(path: str | Path, rows: list[dict[str, Any]]) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        out.write_text("", encoding="utf-8")
        return
    with out.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
