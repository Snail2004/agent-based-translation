from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.prepass.concept_key import (
    concept_key,
    is_common_short_source,
    merge_reason,
    normalize_phrase,
    risk_flags,
)
from pipeline.prepass.span_resolver import _find_word_boundary_matches


DEFAULT_CHAPTERS = [
    "d2l_introduction",
    "d2l_preliminaries",
    "d2l_linear_networks",
    "d2l_multilayer_perceptrons",
]
PLURAL_MARKERS_VI = ("các ", "những ")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Probe number-variant consolidation on an existing Builder registry."
    )
    parser.add_argument("--db", required=True, help="Runtime memory SQLite path.")
    parser.add_argument("--out", required=True, help="Output directory for JSON/CSV reports.")
    parser.add_argument("--doc-id", default="d2l")
    parser.add_argument("--chapters", nargs="*", default=DEFAULT_CHAPTERS)
    args = parser.parse_args()

    db_path = Path(args.db)
    before_hash = _sha256(db_path)
    conn = _connect_ro(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = _load_registry_rows(conn, args.doc_id)
        blocks = _load_blocks(conn, args.doc_id, args.chapters)
    finally:
        conn.close()
    after_hash = _sha256(db_path)
    if before_hash != after_hash:
        raise RuntimeError("DB hash changed during read-only probe.")

    groups = _build_groups(rows, blocks)
    summary = _summary(rows, groups, before_hash)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "builder_v2_a_probe.json"
    groups_csv_path = out_dir / "builder_v2_a_groups.csv"
    merged_csv_path = out_dir / "builder_v2_a_merged_groups.csv"
    json_path.write_text(
        json.dumps({"summary": summary, "groups": groups}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _write_groups_csv(groups_csv_path, groups)
    _write_groups_csv(merged_csv_path, [group for group in groups if group["group_size"] > 1])

    print(json.dumps({
        "raw_terms": summary["raw_terms"],
        "virtual_terms": summary["virtual_terms"],
        "merged_groups": summary["merged_groups"],
        "merged_terms_removed": summary["merged_terms_removed"],
        "common_short_before": summary["common_short_before"],
        "common_short_after": summary["common_short_after"],
        "occurrence_sum_before": summary["occurrence_sum_before"],
        "occurrence_sum_after": summary["occurrence_sum_after"],
        "rematch_mismatches": summary["rematch_mismatch_groups"],
        "db_hash_unchanged": summary["db_hash_unchanged"],
        "json": str(json_path),
        "csv": str(groups_csv_path),
        "merged_csv": str(merged_csv_path),
    }, ensure_ascii=False, indent=2))
    return 0


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    resolved = db_path.resolve()
    return sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True)


def _load_registry_rows(conn: sqlite3.Connection, doc_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT glossary_id, source_term, target_term, term_type, occurrences_count,
               allowed_variants_json, evidence_span_ids_json
        FROM glossary_entries
        WHERE doc_id = ?
        ORDER BY source_term
        """,
        (doc_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _load_blocks(
    conn: sqlite3.Connection,
    doc_id: str,
    chapters: list[str],
) -> list[dict[str, str]]:
    chapter_ids = _resolve_chapters(conn, doc_id, chapters)
    placeholders = ",".join("?" for _ in chapter_ids)
    rows = conn.execute(
        f"""
        SELECT block_id, chapter_id, COALESCE(original_text, text, '') AS text,
               block_type, translation_mode
        FROM blocks
        WHERE doc_id = ? AND chapter_id IN ({placeholders})
          AND (
            COALESCE(translation_mode, 'translate') = 'translate'
            OR block_type = 'heading'
          )
        ORDER BY order_index, block_id
        """,
        (doc_id, *chapter_ids),
    ).fetchall()
    return [dict(row) for row in rows]


def _resolve_chapters(conn: sqlite3.Connection, doc_id: str, chapters: list[str]) -> list[str]:
    available = [
        str(row["chapter_id"])
        for row in conn.execute(
            "SELECT DISTINCT chapter_id FROM blocks WHERE doc_id = ? ORDER BY chapter_id",
            (doc_id,),
        ).fetchall()
    ]
    resolved: list[str] = []
    for requested in chapters:
        value = str(requested)
        matches = [
            chapter
            for chapter in available
            if chapter == value or chapter.endswith(f"_{value}")
        ]
        if not matches:
            raise ValueError(f"Chapter not found: {requested}")
        resolved.append(matches[0])
    return resolved


def _build_groups(rows: list[dict[str, Any]], blocks: list[dict[str, str]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[concept_key(str(row.get("source_term") or ""))].append(row)

    groups: list[dict[str, Any]] = []
    for key, items in sorted(grouped.items()):
        source_terms = [str(item.get("source_term") or "") for item in items]
        target_terms = [str(item.get("target_term") or "") for item in items]
        occurrence_sum = sum(int(item.get("occurrences_count") or 0) for item in items)
        rematch_count = _rematch_count(blocks, source_terms)
        canonical_source = _choose_canonical_source(key, items)
        target_counter = Counter(_clean_target(target) for target in target_terms if _clean_target(target))
        target_conflict_type = _target_conflict_type(target_terms)
        groups.append({
            "concept_key": key,
            "canonical_source_suggested": canonical_source,
            "group_size": len(items),
            "source_terms": sorted(source_terms, key=lambda item: (concept_key(item), item.casefold())),
            "targets": sorted(set(target_terms), key=lambda item: item.casefold()),
            "target_votes": dict(sorted(target_counter.items())),
            "canonical_target_suggested": _choose_canonical_target(items, canonical_source),
            "occurrence_sum": occurrence_sum,
            "rematch_count": rematch_count,
            "rematch_matches_occurrence_sum": rematch_count == occurrence_sum,
            "merge_reason": _group_merge_reason(source_terms),
            "risk_flags": risk_flags(source_terms, target_terms),
            "target_conflict_type": target_conflict_type,
            "glossary_ids": [str(item.get("glossary_id") or "") for item in items],
        })
    return groups


def _rematch_count(blocks: list[dict[str, str]], source_terms: list[str]) -> int:
    total = 0
    for source in sorted(set(source_terms), key=lambda item: (-len(item), item.casefold())):
        for block in blocks:
            total += len(_find_word_boundary_matches(str(block.get("text") or ""), source))
    return total


def _choose_canonical_source(key: str, items: list[dict[str, Any]]) -> str:
    exact = [item for item in items if normalize_phrase(str(item.get("source_term") or "")) == key]
    candidates = exact or items
    chosen = max(
        candidates,
        key=lambda item: (
            int(item.get("occurrences_count") or 0),
            -len(str(item.get("source_term") or "")),
            str(item.get("source_term") or "").casefold(),
        ),
    )
    return str(chosen.get("source_term") or "")


def _choose_canonical_target(items: list[dict[str, Any]], canonical_source: str) -> str:
    source_norm = normalize_phrase(canonical_source)
    source_items = [
        item
        for item in items
        if normalize_phrase(str(item.get("source_term") or "")) == source_norm
    ]
    candidates = source_items or items
    targets = []
    for item in candidates:
        target = _strip_vi_plural_marker(str(item.get("target_term") or ""))
        if target:
            targets.append((target, int(item.get("occurrences_count") or 0)))
    if not targets:
        return ""
    votes: Counter[str] = Counter()
    for target, count in targets:
        votes[target] += max(1, count)
    return sorted(votes, key=lambda target: (-votes[target], target.casefold()))[0]


def _target_conflict_type(targets: list[str]) -> str:
    cleaned = {_clean_target(target) for target in targets if _clean_target(target)}
    if len(cleaned) <= 1:
        return "none"
    without_plural = {_strip_vi_plural_marker(target) for target in cleaned if target}
    if len(without_plural) <= 1:
        return "plural_marker_only"
    return "target_divergence"


def _group_merge_reason(source_terms: list[str]) -> str:
    reasons = {merge_reason(source) for source in source_terms}
    reasons.discard("exact")
    return "+".join(sorted(reasons)) if reasons else "exact"


def _clean_target(target: str) -> str:
    return re.sub(r"\s+", " ", str(target or "").strip())


def _strip_vi_plural_marker(target: str) -> str:
    value = _clean_target(target)
    lowered = value.casefold()
    for marker in PLURAL_MARKERS_VI:
        if lowered.startswith(marker):
            return value[len(marker):].strip()
    return value


def _summary(rows: list[dict[str, Any]], groups: list[dict[str, Any]], db_hash: str) -> dict[str, Any]:
    raw_terms = len(rows)
    virtual_terms = len(groups)
    occurrence_before = sum(int(row.get("occurrences_count") or 0) for row in rows)
    occurrence_after = sum(int(group["occurrence_sum"]) for group in groups)
    rematch_mismatches = [
        {
            "concept_key": group["concept_key"],
            "source_terms": group["source_terms"],
            "occurrence_sum": group["occurrence_sum"],
            "rematch_count": group["rematch_count"],
        }
        for group in groups
        if not group["rematch_matches_occurrence_sum"]
    ]
    common_before = sum(1 for row in rows if is_common_short_source(str(row.get("source_term") or "")))
    common_after = sum(1 for group in groups if is_common_short_source(str(group.get("concept_key") or "")))
    return {
        "phase": "BUILDER-V2-A",
        "db_read_only": True,
        "db_hash_sha256": db_hash,
        "db_hash_unchanged": True,
        "raw_terms": raw_terms,
        "virtual_terms": virtual_terms,
        "merged_groups": sum(1 for group in groups if group["group_size"] > 1),
        "merged_terms_removed": raw_terms - virtual_terms,
        "common_short_definition": "single alphabetic source token, len<=7, not in DONT_SINGULARIZE_TOKENS",
        "common_short_before": common_before,
        "common_short_after": common_after,
        "occurrence_sum_before": occurrence_before,
        "occurrence_sum_after": occurrence_after,
        "occurrence_sum_conserved": occurrence_before == occurrence_after,
        "rematch_mismatch_groups": len(rematch_mismatches),
        "rematch_mismatches_sample": rematch_mismatches[:25],
        "target_conflict_counts": dict(Counter(group["target_conflict_type"] for group in groups)),
        "risk_flag_counts": dict(Counter(flag for group in groups for flag in group["risk_flags"])),
        "note": (
            "Phase A is an offline virtual merge probe. It does not mutate glossary_entries "
            "and does not use eval_glossary_gold."
        ),
    }


def _write_groups_csv(path: Path, groups: list[dict[str, Any]]) -> None:
    fields = [
        "concept_key",
        "canonical_source_suggested",
        "group_size",
        "source_terms",
        "targets",
        "target_votes",
        "canonical_target_suggested",
        "occurrence_sum",
        "rematch_count",
        "rematch_matches_occurrence_sum",
        "merge_reason",
        "risk_flags",
        "target_conflict_type",
        "glossary_ids",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for group in groups:
            row = dict(group)
            for key in ["source_terms", "targets", "risk_flags", "glossary_ids"]:
                row[key] = " | ".join(str(item) for item in row[key])
            row["target_votes"] = json.dumps(row["target_votes"], ensure_ascii=False, sort_keys=True)
            writer.writerow(row)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
