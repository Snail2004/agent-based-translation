from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.prepass.builder_v2_consolidate import (
    Notebook,
    apply_builder_output,
    notebook_to_canonical_json,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Replay frozen glossary_entries through Builder v2 C1 consolidation."
    )
    parser.add_argument("--db", default="data/jobs/d2l_p1/memory.sqlite3")
    parser.add_argument("--doc-id", default="d2l")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    db_path = Path(args.db)
    before_hash = _sha256(db_path)
    conn = _connect_ro(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = _load_registry_rows(conn, args.doc_id)
        block_meta = _load_block_meta(conn, args.doc_id)
    finally:
        conn.close()
    after_hash = _sha256(db_path)
    if before_hash != after_hash:
        raise RuntimeError("DB hash changed during read-only consolidation simulation.")

    notebook = Notebook()
    sorted_rows = sorted(
        rows,
        key=lambda row: (
            _first_evidence_order(row["evidence_block_ids"], block_meta),
            str(row["source_term"]).casefold(),
            str(row["glossary_id"]),
        ),
    )
    for index, row in enumerate(sorted_rows):
        window_id = _window_id(row, index)
        block_types = {
            block_id: str(block_meta.get(block_id, {}).get("block_type") or "")
            for block_id in row["evidence_block_ids"]
        }
        apply_builder_output(
            notebook,
            {"new_terms": [_row_to_new_term(row)]},
            window_id=window_id,
            block_types_by_id=block_types,
        )

    report = _build_report(
        rows=rows,
        notebook=notebook,
        db_path=db_path,
        doc_id=args.doc_id,
        db_sha256=before_hash,
    )

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / "builder_v2_c1_sim_report.json"
    notebook_path = out_dir / "builder_v2_c1_notebook.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    notebook_path.write_text(
        notebook_to_canonical_json(notebook) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "phase": report["phase"],
                "raw_entries": report["entries"]["before"],
                "notebook_entries": report["entries"]["after_notebook"],
                "rejected_stoplist": report["rejected_stoplist"]["count"],
                "merged_by_concept_key": report["decision_counts"].get("merged_by_concept_key", 0),
                "conflicts": report["conflicts"]["count"],
                "occurrence_conserved": report["conservation"]["occurrence_conserved"],
                "evidence_conserved": report["conservation"]["evidence_conserved"],
                "zero_api": True,
                "zero_db_write": report["db_hash_unchanged"],
                "report": str(report_path),
                "notebook": str(notebook_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    resolved = db_path.resolve()
    return sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True)


def _load_registry_rows(conn: sqlite3.Connection, doc_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT glossary_id, source_term, target_term, term_type, do_not_translate,
               allowed_variants_json, evidence_span_ids_json, occurrences_count
        FROM glossary_entries
        WHERE doc_id = ?
        ORDER BY source_term, glossary_id
        """,
        (doc_id,),
    ).fetchall()
    results: list[dict[str, Any]] = []
    for row in rows:
        results.append(
            {
                "glossary_id": str(row["glossary_id"] or ""),
                "source_term": str(row["source_term"] or ""),
                "target_term": str(row["target_term"] or ""),
                "term_type": str(row["term_type"] or "term"),
                "do_not_translate": bool(row["do_not_translate"]),
                "allowed_variants": _json_list(row["allowed_variants_json"]),
                "evidence_block_ids": [
                    str(item) for item in _json_list(row["evidence_span_ids_json"]) if str(item)
                ],
                "occurrences_count": int(row["occurrences_count"] or 0),
            }
        )
    return results


def _load_block_meta(conn: sqlite3.Connection, doc_id: str) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT block_id, order_index, block_type
        FROM blocks
        WHERE doc_id = ?
        ORDER BY order_index, block_id
        """,
        (doc_id,),
    ).fetchall()
    return {
        str(row["block_id"]): {
            "order_index": int(row["order_index"] or 0),
            "block_type": str(row["block_type"] or ""),
        }
        for row in rows
    }


def _row_to_new_term(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_term": row["source_term"],
        "canonical_target_vi": row["target_term"],
        "term_type": row["term_type"],
        "do_not_translate": row["do_not_translate"],
        "evidence_block_ids": row["evidence_block_ids"],
        "occurrence_count": row["occurrences_count"],
    }


def _build_report(
    *,
    rows: list[dict[str, Any]],
    notebook: Notebook,
    db_path: Path,
    doc_id: str,
    db_sha256: str,
) -> dict[str, Any]:
    decision_counts = Counter(item.action for item in notebook.decision_log)
    conflict_types = Counter(
        conflict.type
        for entry in notebook.entries.values()
        for conflict in entry.conflict_ledger
    )
    input_occurrences_raw = sum(int(row["occurrences_count"] or 0) for row in rows)
    input_occurrences_effective = sum(_effective_occurrence_count(row) for row in rows)
    notebook_occurrences = sum(entry.occurrences_total for entry in notebook.entries.values())
    rejected_occurrences = sum(int(item.get("occurrence_count") or 0) for item in notebook.rejected_terms)
    input_evidence = sum(len(set(row["evidence_block_ids"])) for row in rows)
    notebook_evidence = sum(
        len(set(variant.evidence_block_ids))
        for entry in notebook.entries.values()
        for variant in entry.source_variants
    )
    rejected_evidence = sum(len(set(item.get("evidence_block_ids") or [])) for item in notebook.rejected_terms)
    return {
        "phase": "BUILDER-V2-C1-SIM",
        "doc_id": doc_id,
        "db_path": str(db_path),
        "db_sha256": db_sha256,
        "db_hash_unchanged": True,
        "zero_api": True,
        "zero_db_write": True,
        "scope": {
            "source": "glossary_entries replay only",
            "limit": (
                "This simulation exercises create/merge/stoplist/number consolidation. "
                "It is not a C2 dry-run because the historical raw four-bucket LLM output "
                "does not exist in glossary_entries."
            ),
            "gold_used": False,
        },
        "entries": {
            "before": len(rows),
            "after_notebook": len(notebook.entries),
            "after_notebook_plus_rejected": len(notebook.entries) + len(notebook.rejected_terms),
            "delta_notebook": len(notebook.entries) - len(rows),
        },
        "decision_counts": dict(sorted(decision_counts.items())),
        "rejected_stoplist": {
            "count": len(notebook.rejected_terms),
            "sample": notebook.rejected_terms[:25],
        },
        "merged_by_concept_key": {
            "count": decision_counts.get("merged_by_concept_key", 0),
            "sample": [
                item.__dict__
                for item in notebook.decision_log
                if item.action == "merged_by_concept_key"
            ][:25],
        },
        "conflicts": {
            "count": sum(conflict_types.values()),
            "types": dict(sorted(conflict_types.items())),
            "sample": [
                {
                    "concept_key": entry.concept_key,
                    "source_term": entry.canonical_source_term,
                    "conflict": conflict.__dict__,
                }
                for entry in notebook.entries.values()
                for conflict in entry.conflict_ledger
            ][:25],
        },
        "conservation": {
            "occurrence_input": input_occurrences_effective,
            "occurrence_input_raw_db": input_occurrences_raw,
            "occurrence_input_effective": input_occurrences_effective,
            "occurrence_notebook": notebook_occurrences,
            "occurrence_rejected": rejected_occurrences,
            "occurrence_conserved": input_occurrences_effective == notebook_occurrences + rejected_occurrences,
            "occurrence_note": (
                "Rows with occurrences_count<=0 use max(1, evidence_count) for the "
                "online replay, so conservation is checked against occurrence_input_effective."
            ),
            "evidence_input_unique_refs": input_evidence,
            "evidence_notebook_unique_refs": notebook_evidence,
            "evidence_rejected_unique_refs": rejected_evidence,
            "evidence_conserved": input_evidence == notebook_evidence + rejected_evidence,
        },
    }


def _first_evidence_order(row_evidence: list[str], block_meta: dict[str, dict[str, Any]]) -> int:
    orders = [
        int(block_meta[block_id]["order_index"])
        for block_id in row_evidence
        if block_id in block_meta
    ]
    return min(orders) if orders else 10**9


def _effective_occurrence_count(row: dict[str, Any]) -> int:
    raw = int(row.get("occurrences_count") or 0)
    return raw if raw > 0 else max(1, len(row.get("evidence_block_ids") or []))


def _window_id(row: dict[str, Any], index: int) -> str:
    evidence = row.get("evidence_block_ids") or []
    return str(evidence[0]) if evidence else f"sim_no_evidence_{index:05d}"


def _json_list(value: Any) -> list[Any]:
    try:
        parsed = json.loads(str(value or "[]"))
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest().upper()


if __name__ == "__main__":
    raise SystemExit(main())
