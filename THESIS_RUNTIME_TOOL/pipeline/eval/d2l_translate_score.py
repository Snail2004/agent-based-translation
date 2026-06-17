from __future__ import annotations

import csv
import json
import re
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from pipeline.eval.term_policy import (
    TermPolicyAssets,
    annotate_constraint_strength,
    apply_glossary_fixes,
    load_term_policy_assets,
)
from pipeline.eval.thesis_scoring import normalize_apostrophe
from pipeline.translate.profiles import (
    get_profile,
    injection_role_for_term,
    term_is_injection_eligible,
)


METRIC_VERSION = "d2l_translate_score_v2"
DEFAULT_EVAL_ROOT = Path(__file__).resolve().parents[2] / "data" / "eval"

_MASK_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"```.*?```", flags=re.DOTALL),
    re.compile(r"`[^`]*`"),
    re.compile(r":[A-Za-z0-9_-]+:`[^`]*`"),
    re.compile(r"\$[^$\n]*\$"),
    re.compile(r"https?://[^\s)`]+", flags=re.IGNORECASE),
    re.compile(r"\b[\w.-]+\.(?:ai|io|com|org)(?:/[^\s)`]*)?", flags=re.IGNORECASE),
    re.compile(r"\.\. _[^:\n]+:"),
)


@dataclass(frozen=True)
class ScopeBlock:
    block_id: str
    chapter_id: str
    order_index: int
    block_type: str
    text: str


def score_d2l_translation_run(
    db_path: str | Path,
    *,
    chapters: list[str],
    out_path: str | Path,
    experiment_id: str = "d2l_p3",
    profile_name: str = "technical_d2l_v1",
    gold_variants_path: str | Path | None = None,
    term_policy_root: str | Path | None = None,
    doc_id: str = "d2l",
) -> dict[str, Any]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        profile = get_profile(profile_name)
        resolved_chapters = _resolve_chapters(conn, doc_id, chapters)
        scope_blocks = _scope_blocks(conn, doc_id, resolved_chapters, profile)
        passthrough_blocks = _passthrough_blocks(conn, doc_id, resolved_chapters, profile)
        translations = {
            config: _load_translations(conn, experiment_id, config)
            for config in ["S0", "S1"]
        }
        accepted_gold = _load_gold_targets(
            conn,
            doc_id=doc_id,
            variants_path=gold_variants_path,
        )
        registry_rows = _load_registry_rows(conn, doc_id)
        policy_assets = load_term_policy_assets(term_policy_root or DEFAULT_EVAL_ROOT)
        eval_registry_rows = _prepare_eval_registry_rows(registry_rows, policy_assets)
        b_scores = {
            config: _score_tar_vs_gold(scope_blocks, translations[config], accepted_gold)
            for config in ["S0", "S1"]
        }
        d_scores = {
            config: _score_registry_consistency(
                scope_blocks,
                translations[config],
                eval_registry_rows,
                profile_name=profile.name,
            )
            for config in ["S0", "S1"]
        }
        a_score = _score_tar_vs_registry(
            scope_blocks,
            translations["S1"],
            registry_rows,
            profile_name=profile.name,
        )
        report = {
            "scored_at": datetime.now(UTC).isoformat(),
            "metric_version": METRIC_VERSION,
            "experiment_id": experiment_id,
            "profile": profile.name,
            "doc_id": doc_id,
            "chapters": resolved_chapters,
            "scope": _scope_report(
                conn,
                doc_id,
                resolved_chapters,
                scope_blocks,
                passthrough_blocks,
                translations,
            ),
            "B_tar_vs_gold": b_scores,
            "D_registry_consistency": d_scores,
            "A_tar_vs_registry": {"S1": a_score},
            "term_policy": _term_policy_report(eval_registry_rows, policy_assets),
            "injection": {
                "registry": _registry_stats(registry_rows, profile.name),
                "packs": _pack_injection_stats(conn, experiment_id),
            },
            "stage_gate": _stage_gate(
                scope_blocks=scope_blocks,
                passthrough_blocks=passthrough_blocks,
                translations=translations,
                registry_rows=registry_rows,
                profile_name=profile.name,
            ),
            "samples": _sample_blocks(scope_blocks, passthrough_blocks, translations),
            "limitations": [
                "D_surface_v2 is a deterministic block-level surface diagnostic, not word alignment and not a defended quality headline.",
                "D_surface_v2 detects only registry canonical/allowed Vietnamese forms; unseen synonyms are reported as undetected.",
                "D_surface_v2 headline is hard-tier only; soft/preserve/entity/ignore_for_consistency tiers are reported for transparency.",
                "Eval-overlay glossary fixes remove selected cross-term leakage without mutating frozen runtime memory.",
                "Caption/image/label blocks are passthrough by P3 design and excluded from B/D denominators.",
            ],
        }
    finally:
        conn.close()

    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def _resolve_chapters(
    conn: sqlite3.Connection,
    doc_id: str,
    requested: list[str],
) -> list[str]:
    rows = conn.execute(
        """
        SELECT chapter_id, MIN(order_index) AS first_order
        FROM blocks
        WHERE doc_id = ?
        GROUP BY chapter_id
        ORDER BY first_order
        """,
        (doc_id,),
    ).fetchall()
    available = [str(row["chapter_id"]) for row in rows]
    resolved: list[str] = []
    for item in requested:
        value = str(item)
        matches = [
            chapter_id
            for chapter_id in available
            if chapter_id == value or chapter_id.endswith(f"_{value}")
        ]
        if not matches:
            raise ValueError(f"Chapter not found: {item}")
        resolved.append(matches[0])
    return resolved


def _scope_blocks(
    conn: sqlite3.Connection,
    doc_id: str,
    chapters: list[str],
    profile: Any,
) -> list[ScopeBlock]:
    block_types = profile.translatable_block_types
    if block_types is None:
        type_sql = ""
        params: list[Any] = [doc_id, *chapters]
    else:
        type_placeholders = ",".join("?" * len(block_types))
        type_sql = f"AND block_type IN ({type_placeholders})"
        params = [doc_id, *chapters, *sorted(block_types)]
    placeholders = ",".join("?" * len(chapters))
    rows = conn.execute(
        f"""
        SELECT block_id, chapter_id, order_index, block_type, text, original_text
        FROM blocks
        WHERE doc_id = ? AND chapter_id IN ({placeholders})
          {type_sql}
        ORDER BY order_index
        """,
        params,
    ).fetchall()
    return [
        ScopeBlock(
            block_id=str(row["block_id"]),
            chapter_id=str(row["chapter_id"]),
            order_index=int(row["order_index"]),
            block_type=str(row["block_type"] or ""),
            text=str(row["original_text"] or row["text"] or ""),
        )
        for row in rows
    ]


def _passthrough_blocks(
    conn: sqlite3.Connection,
    doc_id: str,
    chapters: list[str],
    profile: Any,
) -> list[ScopeBlock]:
    block_types = profile.passthrough_block_types
    if not block_types:
        return []
    placeholders = ",".join("?" * len(chapters))
    type_placeholders = ",".join("?" * len(block_types))
    rows = conn.execute(
        f"""
        SELECT block_id, chapter_id, order_index, block_type, text, original_text
        FROM blocks
        WHERE doc_id = ? AND chapter_id IN ({placeholders})
          AND block_type IN ({type_placeholders})
        ORDER BY order_index
        """,
        [doc_id, *chapters, *sorted(block_types)],
    ).fetchall()
    return [
        ScopeBlock(
            block_id=str(row["block_id"]),
            chapter_id=str(row["chapter_id"]),
            order_index=int(row["order_index"]),
            block_type=str(row["block_type"] or ""),
            text=str(row["original_text"] or row["text"] or ""),
        )
        for row in rows
    ]


def _load_translations(
    conn: sqlite3.Connection,
    experiment_id: str,
    config: str,
) -> dict[str, str]:
    rows = conn.execute(
        """
        SELECT block_id, output_text
        FROM translation_runs
        WHERE experiment_id = ? AND config = ? AND stage = 'draft'
        """,
        (experiment_id, config),
    ).fetchall()
    return {str(row["block_id"]): str(row["output_text"] or "") for row in rows}


def _load_gold_targets(
    conn: sqlite3.Connection,
    *,
    doc_id: str,
    variants_path: str | Path | None,
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
    accepted: dict[str, dict[str, Any]] = {}
    for row in rows:
        source = str(row["source_term"] or "").strip()
        target = str(row["target_term"] or "").strip()
        if not source or not target:
            continue
        entry = accepted.setdefault(
            _normalize_source(source),
            {"source_term": source, "targets": []},
        )
        _append_unique(entry["targets"], target)

    if variants_path is not None and Path(variants_path).exists():
        with Path(variants_path).open("r", encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                source = str(row.get("source_term") or "").strip()
                variant = str(row.get("variant_vi") or "").strip()
                if not source or not variant:
                    continue
                key = _normalize_source(source)
                entry = accepted.setdefault(
                    key,
                    {"source_term": source, "targets": []},
                )
                _append_unique(entry["targets"], variant)
    return accepted


def _load_registry_rows(conn: sqlite3.Connection, doc_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT glossary_id, source_term, target_term, term_type, do_not_translate,
               case_sensitive,
               allowed_variants_json, forbidden_variants_json, occurrences_count
        FROM glossary_entries
        WHERE doc_id = ?
        ORDER BY source_term
        """,
        (doc_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _prepare_eval_registry_rows(
    registry_rows: list[dict[str, Any]],
    policy_assets: TermPolicyAssets,
) -> list[dict[str, Any]]:
    fixed = apply_glossary_fixes(registry_rows, policy_assets.fixes)
    return annotate_constraint_strength(fixed, policy_assets)


def _term_policy_report(
    registry_rows: list[dict[str, Any]],
    policy_assets: TermPolicyAssets,
) -> dict[str, Any]:
    counts = Counter(str(row.get("constraint_strength") or "unknown") for row in registry_rows)
    return {
        "source": "eval_overlay",
        "read_only": True,
        "paths": policy_assets.paths,
        "counts": dict(sorted(counts.items())),
        "glossary_fixes": len(policy_assets.fixes),
        "overrides": len(policy_assets.overrides),
    }


def _score_tar_vs_gold(
    scope_blocks: list[ScopeBlock],
    translations: dict[str, str],
    accepted_gold: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    pairs = _gold_pairs(scope_blocks, accepted_gold)
    return {
        "flat": _score_pairs(pairs, translations),
        "recurring": _score_pairs(
            [pair for pair in pairs if pair["source_occurrences"] >= 2],
            translations,
        ),
    }


def _score_tar_vs_registry(
    scope_blocks: list[ScopeBlock],
    translations: dict[str, str],
    registry_rows: list[dict[str, Any]],
    *,
    profile_name: str,
) -> dict[str, Any]:
    profile = get_profile(profile_name)
    registry: dict[str, dict[str, Any]] = {}
    for row in registry_rows:
        if not term_is_injection_eligible(row, profile):
            continue
        source = str(row.get("source_term") or "")
        target = str(row.get("target_term") or "")
        if not source or not target:
            continue
        registry[_normalize_source(source)] = {
            "source_term": source,
            "targets": [target],
        }
    pairs = _gold_pairs(scope_blocks, registry)
    return _score_pairs(pairs, translations)


def _score_registry_consistency(
    scope_blocks: list[ScopeBlock],
    translations: dict[str, str],
    registry_rows: list[dict[str, Any]],
    *,
    profile_name: str,
) -> dict[str, Any]:
    term_reports: list[dict[str, Any]] = []
    source_text_by_block = {block.block_id: block.text for block in scope_blocks}
    for row in registry_rows:
        source = str(row.get("source_term") or "")
        target = str(row.get("target_term") or "")
        case_sensitive = _case_sensitive(row)
        constraint_strength = str(row.get("constraint_strength") or "soft")
        source_blocks = [
            block_id
            for block_id, text in source_text_by_block.items()
            if _count_source_matches(text, source, case_sensitive=case_sensitive) > 0
        ]
        if len(source_blocks) < 2:
            continue
        candidates = _accepted_registry_forms(row, canonical_only=False)
        forms_used: Counter[str] = Counter()
        for block_id in source_blocks:
            output = translations.get(block_id, "")
            for candidate, count in _count_non_overlapping_forms(
                output,
                candidates,
                case_sensitive=case_sensitive,
            ).items():
                forms_used[candidate] += count
        distinct = len(forms_used)
        if distinct == 0:
            status = "undetected"
        elif distinct == 1:
            status = "consistent"
        else:
            status = "drift"
        term_reports.append(
            {
                "source_term": source,
                "target_term": target,
                "source_blocks": len(source_blocks),
                "status": status,
                "forms_used": dict(forms_used),
                "case_sensitive": case_sensitive,
                "constraint_strength": constraint_strength,
            }
        )
    by_tier = {
        tier: _term_report_summary(
            [item for item in term_reports if item["constraint_strength"] == tier]
        )
        for tier in ["hard", "soft", "preserve", "entity", "ignore_for_consistency"]
    }
    headline = by_tier["hard"]
    return {
        "method": "block_surface_v2",
        "alignment": False,
        "headline_ready": False,
        "headline_tier": "hard",
        "overall": headline["overall"],
        "detected_only": headline["detected_only"],
        "terms": headline["terms"],
        "consistent_terms": headline["consistent_terms"],
        "drift_terms": headline["drift_terms"],
        "undetected_terms": headline["undetected_terms"],
        "all_terms": len(term_reports),
        "by_tier": by_tier,
        "terms_all": term_reports,
        "worst_terms": [
            item for item in term_reports
            if item["constraint_strength"] == "hard" and item["status"] in {"drift", "undetected"}
        ][:30],
    }


def _term_report_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(items)
    consistent = sum(1 for item in items if item["status"] == "consistent")
    drift = sum(1 for item in items if item["status"] == "drift")
    undetected = sum(1 for item in items if item["status"] == "undetected")
    detected = total - undetected
    return {
        "overall": _ratio(consistent, total),
        "detected_only": _ratio(consistent, detected),
        "terms": total,
        "consistent_terms": consistent,
        "drift_terms": drift,
        "undetected_terms": undetected,
    }


def _gold_pairs(
    scope_blocks: list[ScopeBlock],
    accepted: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    source_occurrences: Counter[str] = Counter()
    block_counts: dict[tuple[str, str], int] = {}
    for block in scope_blocks:
        for key, item in accepted.items():
            count = _count_source_matches(block.text, item["source_term"])
            if count:
                source_occurrences[key] += count
                block_counts[(block.block_id, key)] = count
    pairs: list[dict[str, Any]] = []
    block_chapters = {block.block_id: block.chapter_id for block in scope_blocks}
    for (block_id, key), count in sorted(block_counts.items()):
        item = accepted[key]
        pairs.append(
            {
                "block_id": block_id,
                "chapter_id": block_chapters.get(block_id, ""),
                "source_term": item["source_term"],
                "accepted_targets": list(item["targets"]),
                "occurrences_in_block": count,
                "source_occurrences": source_occurrences[key],
            }
        )
    return pairs


def _score_pairs(pairs: list[dict[str, Any]], translations: dict[str, str]) -> dict[str, Any]:
    total = len(pairs)
    adherent = 0
    weighted_total = 0
    weighted_adherent = 0
    chapter_total: Counter[str] = Counter()
    chapter_adherent: Counter[str] = Counter()
    worst_terms: Counter[str] = Counter()
    term_total: Counter[str] = Counter()
    for pair in pairs:
        output = translations.get(str(pair["block_id"]), "")
        ok = any(_has_vi(output, target) for target in pair["accepted_targets"])
        weight = int(pair["occurrences_in_block"])
        weighted_total += weight
        chapter = str(pair["chapter_id"])
        term = str(pair["source_term"])
        chapter_total[chapter] += 1
        term_total[term] += 1
        if ok:
            adherent += 1
            weighted_adherent += weight
            chapter_adherent[chapter] += 1
        else:
            worst_terms[term] += 1
    return {
        "overall": _ratio(adherent, total),
        "pairs": total,
        "adherent_pairs": adherent,
        "occurrence_weighted": _ratio(weighted_adherent, weighted_total),
        "occurrences": weighted_total,
        "per_chapter": {
            chapter: _ratio(chapter_adherent[chapter], chapter_total[chapter])
            for chapter in sorted(chapter_total)
        },
        "worst_terms": [
            {"source_term": term, "misses": misses, "pairs": term_total[term]}
            for term, misses in worst_terms.most_common(20)
        ],
    }


def _scope_report(
    conn: sqlite3.Connection,
    doc_id: str,
    chapters: list[str],
    scope_blocks: list[ScopeBlock],
    passthrough_blocks: list[ScopeBlock],
    translations: dict[str, dict[str, str]],
) -> dict[str, Any]:
    placeholders = ",".join("?" * len(chapters))
    rows = conn.execute(
        f"""
        SELECT block_type, COUNT(*) AS count
        FROM blocks
        WHERE doc_id = ? AND chapter_id IN ({placeholders})
        GROUP BY block_type
        ORDER BY block_type
        """,
        [doc_id, *chapters],
    ).fetchall()
    scope_ids = {block.block_id for block in scope_blocks}
    return {
        "translated_block_types": dict(Counter(block.block_type for block in scope_blocks)),
        "passthrough_block_types": dict(Counter(block.block_type for block in passthrough_blocks)),
        "all_block_types": {str(row["block_type"]): int(row["count"]) for row in rows},
        "scope_blocks": len(scope_blocks),
        "passthrough_blocks": len(passthrough_blocks),
        "translation_counts": {
            config: len(scope_ids & set(outputs))
            for config, outputs in translations.items()
        },
        "scope_equals_translation_runs": {
            config: scope_ids == set(outputs)
            for config, outputs in translations.items()
            if outputs
        },
    }


def _registry_stats(registry_rows: list[dict[str, Any]], profile_name: str) -> dict[str, int]:
    profile = get_profile(profile_name)
    raw = len(registry_rows)
    eligible = 0
    preserve = 0
    hapax = 0
    for row in registry_rows:
        role = injection_role_for_term(row)
        if role == "preserve":
            preserve += 1
        if role == "translate" and int(row.get("occurrences_count") or 0) < profile.min_injection_occurrences:
            hapax += 1
        if term_is_injection_eligible(row, profile):
            eligible += 1
    return {
        "raw_registry": raw,
        "translation_eligible": eligible,
        "preserve_count": preserve,
        "hapax_dropped": hapax,
    }


def _pack_injection_stats(conn: sqlite3.Connection, experiment_id: str) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT payload_json
        FROM memory_packs
        WHERE config = 'S1'
        ORDER BY created_at, pack_id
        """
    ).fetchall()
    counts: list[int] = []
    for row in rows:
        try:
            payload = json.loads(str(row["payload_json"] or "{}"))
        except json.JSONDecodeError:
            continue
        block_ids = payload.get("block_ids") or []
        # Keep all current S1 packs; P3 uses D2L-only window ids. The experiment_id
        # is stored on translation_runs, not memory_packs.
        if not block_ids:
            continue
        pack = payload.get("context_pack") or {}
        counts.append(len(pack.get("glossary_lines") or []))
    return {
        "windows": len(counts),
        "injected_terms_min": min(counts) if counts else 0,
        "injected_terms_avg": round(sum(counts) / len(counts), 4) if counts else 0,
        "injected_terms_max": max(counts) if counts else 0,
    }


def _stage_gate(
    *,
    scope_blocks: list[ScopeBlock],
    passthrough_blocks: list[ScopeBlock],
    translations: dict[str, dict[str, str]],
    registry_rows: list[dict[str, Any]],
    profile_name: str,
) -> dict[str, Any]:
    scope_ids = {block.block_id for block in scope_blocks}
    passthrough_ids = {block.block_id for block in passthrough_blocks}
    profile = get_profile(profile_name)
    eligible_rows = [
        row for row in registry_rows if term_is_injection_eligible(row, profile)
    ]
    preserve_eligible = [
        row for row in eligible_rows if injection_role_for_term(row) == "preserve"
    ]
    return {
        "no_passthrough_translated": {
            config: not bool(set(outputs) & passthrough_ids)
            for config, outputs in translations.items()
            if outputs
        },
        "scope_equals_translation_runs": {
            config: set(outputs) == scope_ids
            for config, outputs in translations.items()
            if outputs
        },
        "preserve_terms_excluded_from_injection": len(preserve_eligible) == 0,
        "eligible_terms_have_occurrences_ge_2": all(
            int(row.get("occurrences_count") or 0) >= profile.min_injection_occurrences
            for row in eligible_rows
        ),
        "manual_passthrough_audit_required": True,
    }


def _sample_blocks(
    scope_blocks: list[ScopeBlock],
    passthrough_blocks: list[ScopeBlock],
    translations: dict[str, dict[str, str]],
) -> dict[str, Any]:
    translated_samples = []
    for block in scope_blocks[:5]:
        translated_samples.append(
            {
                "block_id": block.block_id,
                "source": block.text[:500],
                "S0": translations["S0"].get(block.block_id, "")[:500],
                "S1": translations["S1"].get(block.block_id, "")[:500],
            }
        )
    return {
        "translated": translated_samples,
        "passthrough_audit": [
            {
                "block_id": block.block_id,
                "block_type": block.block_type,
                "source": block.text[:500],
            }
            for block in passthrough_blocks[:5]
        ],
    }


def _accepted_registry_forms(row: dict[str, Any], *, canonical_only: bool) -> list[str]:
    forms = [str(row.get("target_term") or "").strip()]
    if not canonical_only:
        forms.extend(str(item).strip() for item in _json_list(row.get("allowed_variants_json")))
    seen: set[str] = set()
    result: list[str] = []
    for form in forms:
        key = _normalize_vi(form)
        if form and key not in seen:
            seen.add(key)
            result.append(form)
    return result


@lru_cache(maxsize=8192)
def _mask_non_prose(text: str) -> str:
    value = str(text or "")
    if not value:
        return ""
    mask = [False] * len(value)
    for pattern in _MASK_PATTERNS:
        for match in pattern.finditer(value):
            start, end = match.span()
            for index in range(start, end):
                mask[index] = True
    return "".join(" " if is_masked else char for char, is_masked in zip(value, mask, strict=True))


def _count_source_matches(
    text: str,
    source_term: str,
    *,
    case_sensitive: bool = False,
) -> int:
    return len(_find_surface_matches(text, source_term, case_sensitive=case_sensitive))


def _has_vi(text: str, needle: str, *, case_sensitive: bool = False) -> bool:
    return bool(_find_surface_matches(text, needle, case_sensitive=case_sensitive))


def _count_non_overlapping_forms(
    text: str,
    candidates: list[str],
    *,
    case_sensitive: bool = False,
) -> Counter[str]:
    normalized_text = _normalize_for_match(_mask_non_prose(text), case_sensitive=case_sensitive)
    if not normalized_text:
        return Counter()

    prepared: list[tuple[str, str, int]] = []
    for candidate in candidates:
        pattern = _surface_pattern(candidate, case_sensitive=case_sensitive)
        if not pattern:
            continue
        normalized_candidate = _normalize_for_match(candidate, case_sensitive=case_sensitive).strip()
        prepared.append((candidate, pattern, len(normalized_candidate)))
    prepared.sort(key=lambda item: item[2], reverse=True)

    occupied = [False] * len(normalized_text)
    used: Counter[str] = Counter()
    for candidate, pattern, _length in prepared:
        for match in re.finditer(pattern, normalized_text, flags=re.UNICODE):
            start, end = match.span()
            if start == end or any(occupied[start:end]):
                continue
            used[candidate] += 1
            for index in range(start, end):
                occupied[index] = True
            # The D metric is block-level: one vote per form per source block.
            break
    return used


def _find_surface_matches(
    text: str,
    needle: str,
    *,
    case_sensitive: bool = False,
) -> list[re.Match[str]]:
    normalized_text = _normalize_for_match(_mask_non_prose(text), case_sensitive=case_sensitive)
    pattern = _surface_pattern(needle, case_sensitive=case_sensitive)
    if not normalized_text or not pattern:
        return []
    return list(re.finditer(pattern, normalized_text, flags=re.UNICODE))


def _surface_pattern(needle: str, *, case_sensitive: bool = False) -> str:
    normalized_needle = _normalize_for_match(needle, case_sensitive=case_sensitive).strip()
    if not normalized_needle:
        return ""
    pieces = [piece for piece in re.split(r"\s+", normalized_needle) if piece]
    if not pieces:
        return ""
    body = r"\s+".join(re.escape(piece) for piece in pieces)
    prefix = r"(?<!\w)" if _is_word_char(normalized_needle[0]) else ""
    suffix = r"(?!\w)" if _is_word_char(normalized_needle[-1]) else ""
    return prefix + body + suffix


def _normalize_source(text: str) -> str:
    return unicodedata.normalize("NFC", normalize_apostrophe(str(text))).casefold()


def _normalize_vi(text: str) -> str:
    return re.sub(
        r"\s+",
        " ",
        unicodedata.normalize("NFC", normalize_apostrophe(str(text))).casefold().strip(),
    )


def _normalize_for_match(text: str, *, case_sensitive: bool = False) -> str:
    normalized = unicodedata.normalize("NFC", normalize_apostrophe(str(text)))
    return normalized if case_sensitive else normalized.casefold()


def _is_word_char(value: str) -> bool:
    return bool(re.match(r"\w", value, flags=re.UNICODE))


def _case_sensitive(row: dict[str, Any]) -> bool:
    return bool(int(row.get("case_sensitive") or 0))


def _json_list(value: Any) -> list[Any]:
    try:
        parsed = json.loads(str(value or "[]"))
    except Exception:
        return []
    return parsed if isinstance(parsed, list) else []


def _append_unique(values: list[str], value: str) -> None:
    if _normalize_vi(value) not in {_normalize_vi(item) for item in values}:
        values.append(value)


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0
