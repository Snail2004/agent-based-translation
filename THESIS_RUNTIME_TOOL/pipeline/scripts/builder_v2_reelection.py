from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

from pipeline.prepass.builder_v2_decollision import normalize_target_key
from pipeline.retrieval.context_builder import notebook_entries_to_term_rows


WATCHLIST_VERSION = "builder_v2_reelection_watchlist_v1"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Builder-v2 blind canonical re-election lifecycle step."
    )
    parser.add_argument("--notebook", required=True)
    parser.add_argument("--db", default="data/jobs/d2l_p1/memory.sqlite3")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--expect-watchlist", nargs="*", default=[])
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args()

    notebook_path = Path(args.notebook)
    db_path = Path(args.db)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    notebook = _read_json(notebook_path)
    entries = notebook.get("entries") if isinstance(notebook, dict) else None
    if not isinstance(entries, list):
        raise SystemExit(f"Notebook is missing entries list: {notebook_path}")

    watchlist = build_watchlist(entries)
    evidence_counts = _evidence_counts_by_entry(entries)
    for item in watchlist:
        entry_id = item["entry_id"]
        item["evidence_blocks"] = evidence_counts.get(entry_id, 0)
        item["estimated_context_vote_calls_cap"] = min(30, item["evidence_blocks"])

    expected = {str(item).casefold() for item in args.expect_watchlist if str(item).strip()}
    found = {str(item["source_term"]).casefold() for item in watchlist}
    missing = sorted(expected - found)
    if missing:
        _write_json(
            out_dir / "reelection_preflight_failed.json",
            {
                "version": WATCHLIST_VERSION,
                "status": "failed_expected_watchlist_missing",
                "missing_expected_terms": missing,
                "watchlist": watchlist,
            },
        )
        raise SystemExit(f"Expected watchlist terms missing: {missing}")

    estimate = estimate_calls(watchlist)
    report = {
        "version": WATCHLIST_VERSION,
        "status": "preflight",
        "zero_api": True,
        "notebook": str(notebook_path),
        "db": str(db_path),
        "notebook_entries": len(entries),
        "watchlist_size": len(watchlist),
        "watchlist_reason_counts": dict(Counter(reason for item in watchlist for reason in item["watchlist_reasons"])),
        "estimated_calls": estimate,
        "expected_watchlist_terms": sorted(expected),
        "expected_watchlist_missing": missing,
        "db_readable": _db_readable(db_path),
    }
    _write_json(out_dir / "watchlist.json", watchlist)
    _write_json(out_dir / "reelection_preflight.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))

    if not args.preflight_only:
        raise SystemExit(
            "STOP-A reached. Re-run election mode only after reviewer approves the watchlist."
        )
    return 0


def build_watchlist(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = notebook_entries_to_term_rows(entries)
    collision_by_id = _collision_flags_by_entry(rows)
    by_id = {_entry_id(entry): entry for entry in entries if _entry_id(entry)}
    watchlist: list[dict[str, Any]] = []
    for entry_id, entry in sorted(by_id.items()):
        canonical = _canonical_target(entry)
        candidates = _candidate_targets(entry, canonical)
        competitors = [
            item for item in candidates
            if normalize_target_key(item["text"]) != normalize_target_key(canonical)
        ]
        if not competitors:
            continue
        audit = _entry_audit(entry)
        reasons: list[str] = []
        collision = collision_by_id.get(entry_id)
        if collision:
            reasons.append("collision_soft_fallback")
        if str(audit.get("audit_label") or "") == "polysemy_or_context_dependent":
            reasons.append("audit_polysemy")
        if not reasons:
            continue
        watchlist.append(
            {
                "entry_id": entry_id,
                "source_term": _source_term(entry, entry_id),
                "canonical_target_vi": canonical,
                "audit_label": str(audit.get("audit_label") or ""),
                "injection_action": str(audit.get("injection_action") or ""),
                "watchlist_reasons": reasons,
                "collision_soft_fallback": collision,
                "candidates": candidates,
                "competitors": competitors,
                "backtranslation_calls": len(candidates),
            }
        )
    return watchlist


def estimate_calls(watchlist: list[dict[str, Any]]) -> dict[str, int]:
    backtranslation = sum(int(item.get("backtranslation_calls") or 0) for item in watchlist)
    context_cap = sum(int(item.get("estimated_context_vote_calls_cap") or 0) for item in watchlist)
    return {
        "backtranslation_calls": backtranslation,
        "context_vote_calls_cap": context_cap,
        "total_cap": backtranslation + context_cap,
    }


def _collision_flags_by_entry(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        audit = row.get("audit") if isinstance(row.get("audit"), dict) else {}
        flag = audit.get("collision_soft_fallback")
        if isinstance(flag, dict):
            result[str(row.get("glossary_id") or row.get("source_term") or "")] = flag
    return result


def _entry_id(entry: dict[str, Any]) -> str:
    return str(
        entry.get("entry_id")
        or entry.get("concept_key")
        or entry.get("canonical_source_term")
        or entry.get("source_term")
        or ""
    ).strip()


def _source_term(entry: dict[str, Any], fallback: str) -> str:
    return str(
        entry.get("canonical_source_term")
        or entry.get("source_term")
        or entry.get("concept_key")
        or fallback
    ).strip()


def _canonical_target(entry: dict[str, Any]) -> str:
    return str(entry.get("canonical_target_vi") or entry.get("target_term") or "").strip()


def _entry_audit(entry: dict[str, Any]) -> dict[str, Any]:
    audit = entry.get("audit")
    if isinstance(audit, dict):
        return audit
    return {
        "audit_label": entry.get("audit_label") or "",
        "injection_action": entry.get("injection_action") or "",
        "priority_tier": entry.get("priority_tier") or "",
        "confidence": entry.get("confidence") or "",
    }


def _candidate_targets(entry: dict[str, Any], canonical: str) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    if canonical:
        result.append({"text": canonical, "source": "canonical"})
        seen.add(normalize_target_key(canonical))
    for variant in entry.get("target_variants") or []:
        text = str(variant.get("text") if isinstance(variant, dict) else variant).strip()
        key = normalize_target_key(text)
        if not text or key in seen:
            continue
        item = {"text": text, "source": "target_variant"}
        if isinstance(variant, dict):
            if variant.get("evidence_block_id"):
                item["evidence_block_id"] = str(variant["evidence_block_id"])
            if variant.get("variant_reason"):
                item["variant_reason"] = str(variant["variant_reason"])
        result.append(item)
        seen.add(key)
    return result


def _evidence_counts_by_entry(entries: list[dict[str, Any]]) -> dict[str, int]:
    result: dict[str, int] = {}
    for entry in entries:
        entry_id = _entry_id(entry)
        blocks: set[str] = set()
        for variant in entry.get("source_variants") or []:
            if not isinstance(variant, dict):
                continue
            for block_id in variant.get("evidence_block_ids") or []:
                if str(block_id or "").strip():
                    blocks.add(str(block_id))
        result[entry_id] = len(blocks)
    return result


def _db_readable(path: Path) -> bool:
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as con:
            con.execute("SELECT 1").fetchone()
        return True
    except sqlite3.Error:
        return False


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    raise SystemExit(main())
