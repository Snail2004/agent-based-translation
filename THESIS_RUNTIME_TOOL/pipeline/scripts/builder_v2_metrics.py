from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.eval.builder_gold import _count_matches, _normalize_source, _normalize_vi
from pipeline.prepass.concept_key import concept_key
from pipeline.prepass.db_source import load_document_from_connection


DEFAULT_DB = Path("data/jobs/d2l_p1/memory.sqlite3")
DEFAULT_ARTIFACT_DIR = Path("data/reports/builder_v2_c2_pilot")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Score Builder v2 C2 artifact against same-chapter v1 and eval-only gold."
    )
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--doc-id", default="d2l")
    parser.add_argument("--chapter", required=True)
    parser.add_argument("--artifact-dir", default=str(DEFAULT_ARTIFACT_DIR))
    parser.add_argument("--out")
    args = parser.parse_args()

    artifact_dir = Path(args.artifact_dir)
    db_path = Path(args.db)
    report = build_metrics_report(
        db_path=db_path,
        doc_id=args.doc_id,
        chapter_id=args.chapter,
        artifact_dir=artifact_dir,
    )
    out_path = Path(args.out) if args.out else artifact_dir / "builder_v2_c2_metrics.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(_summary(report, out_path), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def build_metrics_report(
    *,
    db_path: Path,
    doc_id: str,
    chapter_id: str,
    artifact_dir: Path,
) -> dict[str, Any]:
    notebook = _read_json(artifact_dir / "notebook.json")
    raw_outputs = _read_json(artifact_dir / "raw_outputs.json")
    run_report = _read_json(artifact_dir / "builder_v2_c2_pilot_report.json")
    cost_log = _read_json(artifact_dir / "cost_log.json")

    before_hash = _sha256(db_path)
    conn = _connect_ro(db_path)
    conn.row_factory = sqlite3.Row
    try:
        document = load_document_from_connection(conn, doc_id, [chapter_id], translate_only=True)
        resolved_chapters = [str(chapter["chapter_id"]) for chapter in document["chapters"]]
        block_ids = {
            str(block["block_id"])
            for chapter in document["chapters"]
            for block in chapter.get("blocks", [])
        }
        source_text = "\n\n".join(
            str(block.get("source_text") or block.get("clean_text") or "")
            for chapter in document["chapters"]
            for block in chapter.get("blocks", [])
        )
        gold_terms = _present_gold_terms(conn, doc_id, source_text)
        v1_terms = _load_v1_chapter_terms(conn, doc_id, block_ids)
    finally:
        conn.close()
    after_hash = _sha256(db_path)
    if before_hash != after_hash:
        raise RuntimeError("Frozen DB hash changed during Builder v2 metrics.")

    v2_terms, v2_entry_count, v2_merged_variant_groups, v2_duplicate_entry_groups = _load_v2_notebook_terms(notebook)
    v1_score = _score_terms_vs_gold(v1_terms, gold_terms)
    v2_score = _score_terms_vs_gold(v2_terms, gold_terms, builder_term_count=v2_entry_count)
    raw_occ = _raw_effective_occurrences(raw_outputs)
    notebook_occ = _notebook_occurrences(notebook)
    rejected_occ = _rejected_occurrences(notebook)
    conflicts = _conflicts(notebook)
    v1_number_splits = _concept_split_groups(v1_terms)

    return {
        "phase": "BUILDER-V2-C2-METRICS",
        "doc_id": doc_id,
        "chapters": resolved_chapters,
        "db_path": str(db_path),
        "db_sha256": before_hash,
        "db_hash_unchanged": before_hash == after_hash,
        "artifact_dir": str(artifact_dir),
        "status": str(run_report.get("status") or "unknown"),
        "note": "DEV metric for C2 pilot only; gold is eval-only and not injected.",
        "entry_counts_scope_matched": {
            "v1_chapter_terms": len(v1_terms),
            "v2_notebook_entries": v2_entry_count,
            "v2_rejected_stoplist": len(notebook.get("rejected_terms") or []),
        },
        "recall_vs_gold_dev": {
            "gold_terms_present": len(gold_terms),
            "v1": v1_score,
            "v2": v2_score,
        },
        "stoplist_rejects": {
            "count": len(notebook.get("rejected_terms") or []),
            "sample": (notebook.get("rejected_terms") or [])[:25],
        },
        "number_variant_splits": {
            "v1_separate_entry_groups": len(v1_number_splits),
            "v1_separate_entry_sample": v1_number_splits[:25],
            "v2_duplicate_entry_groups": len(v2_duplicate_entry_groups),
            "v2_duplicate_entry_sample": v2_duplicate_entry_groups[:25],
            "v2_merged_source_variant_groups": len(v2_merged_variant_groups),
            "v2_merged_source_variant_sample": v2_merged_variant_groups[:25],
        },
        "conflicts": conflicts,
        "occurrence_conservation": {
            "raw_effective_input_occurrences": raw_occ,
            "notebook_occurrences": notebook_occ,
            "rejected_occurrences": rejected_occ,
            "notebook_plus_rejected": notebook_occ + rejected_occ,
            "conserved": raw_occ == notebook_occ + rejected_occ,
        },
        "cost": {
            "actual_cost_usd": round(sum(float(row.get("cost_usd") or 0.0) for row in cost_log), 8),
            "calls_logged": len(cost_log),
            "cache_hits": sum(1 for row in cost_log if bool(row.get("from_cache"))),
            "cache_misses": sum(1 for row in cost_log if not bool(row.get("from_cache"))),
            "prompt_tokens": sum(int(row.get("prompt_tokens") or 0) for row in cost_log),
            "completion_tokens": sum(int(row.get("completion_tokens") or 0) for row in cost_log),
            "reasoning_tokens": sum(int(row.get("reasoning_tokens") or 0) for row in cost_log),
        },
    }


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


def _load_v1_chapter_terms(
    conn: sqlite3.Connection,
    doc_id: str,
    block_ids: set[str],
) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT source_term, target_term, allowed_variants_json, evidence_span_ids_json
        FROM glossary_entries
        WHERE doc_id = ?
        ORDER BY source_term
        """,
        (doc_id,),
    ).fetchall()
    terms: dict[str, dict[str, Any]] = {}
    for row in rows:
        evidence = [str(item) for item in _json_list(row["evidence_span_ids_json"])]
        if not (set(evidence) & block_ids):
            continue
        source = str(row["source_term"] or "")
        target = str(row["target_term"] or "")
        variants = [str(item) for item in _json_list(row["allowed_variants_json"])]
        terms[_normalize_source(source)] = {
            "source_terms": [source],
            "target_term": target,
            "accepted_targets": {
                _normalize_vi(item)
                for item in [target, *variants]
                if str(item).strip()
            },
        }
    return terms


def _load_v2_notebook_terms(
    notebook: dict[str, Any]
) -> tuple[dict[str, dict[str, Any]], int, list[dict[str, Any]], list[dict[str, Any]]]:
    terms: dict[str, dict[str, Any]] = {}
    concept_entry_sources: dict[str, list[str]] = defaultdict(list)
    merged_variant_groups: list[dict[str, Any]] = []
    entry_count = 0
    for entry in notebook.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        entry_count += 1
        canonical_source = str(entry.get("canonical_source_term") or "")
        canonical_target = str(entry.get("canonical_target_vi") or "")
        source_terms = [canonical_source]
        for variant in entry.get("source_variants") or []:
            if isinstance(variant, dict) and str(variant.get("surface") or "").strip():
                source_terms.append(str(variant["surface"]))
        target_terms = [canonical_target]
        for variant in entry.get("target_variants") or []:
            if isinstance(variant, dict) and str(variant.get("text") or "").strip():
                target_terms.append(str(variant["text"]))
        payload = {
            "source_terms": _stable_unique(source_terms),
            "target_term": canonical_target,
            "accepted_targets": {
                _normalize_vi(item)
                for item in target_terms
                if str(item).strip()
            },
        }
        key = str(entry.get("concept_key") or concept_key(canonical_source))
        concept_entry_sources[key].append(canonical_source)
        if len(payload["source_terms"]) > 1:
            merged_variant_groups.append(
                {
                    "concept_key": key,
                    "source_terms": payload["source_terms"],
                    "canonical_source_term": canonical_source,
                }
            )
        for source in payload["source_terms"]:
            if source.strip():
                terms[_normalize_source(source)] = payload
    duplicate_entry_groups = [
        {
            "concept_key": key,
            "source_terms": sorted(_stable_unique(values), key=lambda value: value.casefold()),
        }
        for key, values in concept_entry_sources.items()
        if len(_stable_unique(values)) > 1
    ]
    return (
        terms,
        entry_count,
        sorted(merged_variant_groups, key=lambda item: (item["concept_key"], item["canonical_source_term"])),
        sorted(duplicate_entry_groups, key=lambda item: item["concept_key"]),
    )


def _score_terms_vs_gold(
    terms: dict[str, dict[str, Any]],
    gold_terms: dict[str, dict[str, Any]],
    *,
    builder_term_count: int | None = None,
) -> dict[str, Any]:
    missing: list[dict[str, str]] = []
    conflicts: list[dict[str, str]] = []
    matched = 0
    agreed = 0
    for key, gold in sorted(gold_terms.items(), key=lambda item: item[1]["source_term"].casefold()):
        builder = terms.get(key)
        if builder is None:
            missing.append(
                {
                    "source_term": gold["source_term"],
                    "gold_target": " | ".join(gold["target_display"]),
                }
            )
            continue
        matched += 1
        if builder["accepted_targets"] & gold["targets"]:
            agreed += 1
        else:
            conflicts.append(
                {
                    "source_term": gold["source_term"],
                    "builder_target": str(builder.get("target_term") or ""),
                    "gold_target": " | ".join(gold["target_display"]),
                }
            )
    gold_count = len(gold_terms)
    return {
        "builder_terms": len(terms) if builder_term_count is None else builder_term_count,
        "matched_terms": matched,
        "agreement_terms": agreed,
        "recall": round(matched / gold_count, 6) if gold_count else 0.0,
        "agreement": round(agreed / matched, 6) if matched else 0.0,
        "missing_terms": missing,
        "conflicts": conflicts,
    }


def _concept_split_groups(terms: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, set[str]] = defaultdict(set)
    for term in terms.values():
        for source in term["source_terms"]:
            groups[concept_key(source)].add(source)
    results = [
        {"concept_key": key, "source_terms": sorted(values, key=lambda value: value.casefold())}
        for key, values in groups.items()
        if len(values) > 1
    ]
    return sorted(results, key=lambda item: (item["concept_key"], item["source_terms"]))


def _raw_effective_occurrences(raw_outputs: list[dict[str, Any]]) -> int:
    total = 0
    for item in raw_outputs:
        parsed = item.get("parsed_output") if isinstance(item, dict) else None
        if not isinstance(parsed, dict):
            continue
        for bucket in ("new_terms", "updates_to_existing", "seen_existing_terms"):
            for term in parsed.get(bucket) or []:
                if isinstance(term, dict):
                    total += _occurrence_count(term)
    return total


def _notebook_occurrences(notebook: dict[str, Any]) -> int:
    return sum(
        int(entry.get("occurrences_total") or 0)
        for entry in notebook.get("entries") or []
        if isinstance(entry, dict)
    )


def _rejected_occurrences(notebook: dict[str, Any]) -> int:
    return sum(
        int(entry.get("occurrence_count") or 0)
        for entry in notebook.get("rejected_terms") or []
        if isinstance(entry, dict)
    )


def _conflicts(notebook: dict[str, Any]) -> dict[str, Any]:
    counter: Counter[str] = Counter()
    sample: list[dict[str, Any]] = []
    for entry in notebook.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        for conflict in entry.get("conflict_ledger") or []:
            if not isinstance(conflict, dict):
                continue
            conflict_type = str(conflict.get("type") or "unknown")
            counter[conflict_type] += 1
            if len(sample) < 25:
                sample.append(
                    {
                        "source_term": entry.get("canonical_source_term"),
                        "concept_key": entry.get("concept_key"),
                        "type": conflict_type,
                        "proposed_target": conflict.get("proposed_target"),
                        "reason": conflict.get("reason"),
                        "window": conflict.get("window"),
                    }
                )
    return {"count": sum(counter.values()), "types": dict(sorted(counter.items())), "sample": sample}


def _occurrence_count(term: dict[str, Any]) -> int:
    raw = term.get("occurrence_count", term.get("occurrences_count"))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 0
    evidence = term.get("evidence_block_ids") or term.get("evidence_span_ids") or term.get("block_ids") or []
    return value if value > 0 else max(1, len(evidence))


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _json_list(value: Any) -> list[Any]:
    try:
        parsed = json.loads(str(value or "[]"))
    except Exception:
        return []
    return parsed if isinstance(parsed, list) else []


def _stable_unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        clean = re.sub(r"\s+", " ", str(value).strip())
        if not clean or clean.casefold() in seen:
            continue
        seen.add(clean.casefold())
        result.append(clean)
    return result


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    resolved = db_path.resolve()
    return sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True)


def _sha256(path: Path) -> str:
    return __import__("hashlib").sha256(path.read_bytes()).hexdigest().upper()


def _summary(report: dict[str, Any], out_path: Path) -> dict[str, Any]:
    return {
        "phase": report["phase"],
        "status": report["status"],
        "chapters": report["chapters"],
        "entry_counts_scope_matched": report["entry_counts_scope_matched"],
        "recall_vs_gold_dev": {
            "gold_terms_present": report["recall_vs_gold_dev"]["gold_terms_present"],
            "v1": {
                "recall": report["recall_vs_gold_dev"]["v1"]["recall"],
                "agreement": report["recall_vs_gold_dev"]["v1"]["agreement"],
                "matched_terms": report["recall_vs_gold_dev"]["v1"]["matched_terms"],
            },
            "v2": {
                "recall": report["recall_vs_gold_dev"]["v2"]["recall"],
                "agreement": report["recall_vs_gold_dev"]["v2"]["agreement"],
                "matched_terms": report["recall_vs_gold_dev"]["v2"]["matched_terms"],
            },
        },
        "stoplist_rejects": report["stoplist_rejects"]["count"],
        "number_variant_splits": {
            "v1_separate_entry_groups": report["number_variant_splits"]["v1_separate_entry_groups"],
            "v2_duplicate_entry_groups": report["number_variant_splits"]["v2_duplicate_entry_groups"],
            "v2_merged_source_variant_groups": report["number_variant_splits"]["v2_merged_source_variant_groups"],
        },
        "conflicts": report["conflicts"]["types"],
        "occurrence_conserved": report["occurrence_conservation"]["conserved"],
        "actual_cost_usd": report["cost"]["actual_cost_usd"],
        "out": str(out_path),
    }


if __name__ == "__main__":
    raise SystemExit(main())
