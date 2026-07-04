from __future__ import annotations

import argparse
import json
import sqlite3
import time
from collections import Counter
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import Any

import requests

from pipeline.prepass.builder_v2_decollision import normalize_target_key
from pipeline.retrieval.context_builder import notebook_entries_to_term_rows


WATCHLIST_VERSION = "builder_v2_reelection_watchlist_v1"
ELECTION_VERSION = "builder_v2_reelection_election_v1"
ELECTION_POLICY_VERSION = "round2_polysemy_context_exact_match_challenger_threshold_v1"
DEFAULT_LMSTUDIO_ENDPOINT = "http://127.0.0.1:1234"
DEFAULT_LMSTUDIO_MODEL = "google/gemma-4-12b"

BACKTRANSLATION_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "builder_v2_blind_backtranslation",
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "english": {"type": "string"},
            },
            "required": ["english"],
        },
        "strict": True,
    },
}

CONTEXT_VOTE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "builder_v2_context_vote",
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "vietnamese": {"type": "string"},
            },
            "required": ["vietnamese"],
        },
        "strict": True,
    },
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Builder-v2 blind canonical re-election lifecycle step."
    )
    parser.add_argument("--notebook", required=True)
    parser.add_argument("--db", default="data/jobs/d2l_p1/memory.sqlite3")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--expect-watchlist", nargs="*", default=[])
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--run-election", action="store_true")
    parser.add_argument("--lmstudio-endpoint", default=DEFAULT_LMSTUDIO_ENDPOINT)
    parser.add_argument("--lmstudio-model", default=DEFAULT_LMSTUDIO_MODEL)
    parser.add_argument("--cache-db", default="")
    parser.add_argument("--timeout-sec", type=int, default=120)
    parser.add_argument("--context-vote-cap", type=int, default=30)
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
    evidence_block_ids = _evidence_block_ids_by_entry(entries)
    for item in watchlist:
        entry_id = item["entry_id"]
        item["evidence_blocks"] = evidence_counts.get(entry_id, 0)
        item["evidence_block_ids"] = evidence_block_ids.get(entry_id, [])
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

    if args.run_election:
        cache_path = Path(args.cache_db) if args.cache_db else out_dir / "reelection_lmstudio_cache.sqlite3"
        client = LocalLMStudioClient(
            endpoint=args.lmstudio_endpoint,
            model=args.lmstudio_model,
            cache_path=cache_path,
            timeout_sec=args.timeout_sec,
        )
        result = run_election(
            entries=entries,
            watchlist=watchlist,
            db_path=db_path,
            client=client,
            context_vote_cap=args.context_vote_cap,
        )
        reelected_notebook = dict(notebook)
        reelected_notebook["entries"] = result["entries"]
        reelected_notebook["reelection_version"] = ELECTION_VERSION
        reelected_notebook["reelection_summary"] = result["summary"]
        _write_json(out_dir / "notebook_reelected.json", reelected_notebook)
        _write_json(out_dir / "reelection_log.json", result["log"])
        _write_json(out_dir / "reelection_report.json", result["summary"])
        print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
        return 0

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


def run_election(
    *,
    entries: list[dict[str, Any]],
    watchlist: list[dict[str, Any]],
    db_path: Path,
    client: "LocalLMStudioClient",
    context_vote_cap: int,
) -> dict[str, Any]:
    updated_entries = deepcopy(entries)
    by_id = {_entry_id(entry): entry for entry in updated_entries if _entry_id(entry)}
    watchlist_ids = {str(item["entry_id"]) for item in watchlist}
    original_canonicals = {
        _entry_id(entry): _canonical_target(entry)
        for entry in entries
        if _entry_id(entry)
    }
    log_entries: list[dict[str, Any]] = []
    for item in watchlist:
        entry_id = str(item["entry_id"])
        entry = by_id.get(entry_id)
        if entry is None:
            log_entries.append(
                {
                    "entry_id": entry_id,
                    "source_term": item.get("source_term") or entry_id,
                    "status": "election_error",
                    "error": "entry_not_found",
                    "changed": False,
                }
            )
            continue
        election = _elect_entry(
            item=item,
            db_path=db_path,
            client=client,
            context_vote_cap=context_vote_cap,
        )
        winner = str(election.get("winner") or "").strip()
        incumbent = _canonical_target(entry)
        changed = bool(winner and normalize_target_key(winner) != normalize_target_key(incumbent))
        gate_status = "not_applicable"
        if changed:
            entry["canonical_target_vi"] = winner
            collision = _new_collision_for_entry(updated_entries, entry_id)
            if collision:
                entry["canonical_target_vi"] = incumbent
                gate_status = "blocked_new_collision"
                changed = False
                election["blocked_new_collision"] = collision
            else:
                gate_status = "passed"
                _append_reelection_audit(entry, incumbent, winner, election)
        election.update(
            {
                "entry_id": entry_id,
                "source_term": item.get("source_term") or _source_term(entry, entry_id),
                "incumbent": incumbent,
                "winner": winner or incumbent,
                "changed": changed,
                "gate_status": gate_status,
                "watchlist_reasons": item.get("watchlist_reasons") or [],
            }
        )
        log_entries.append(election)
    changed_ids = sorted(
        entry_id
        for entry_id, entry in by_id.items()
        if normalize_target_key(_canonical_target(entry)) != normalize_target_key(original_canonicals.get(entry_id, ""))
    )
    outside_watchlist_changed = sorted(entry_id for entry_id in changed_ids if entry_id not in watchlist_ids)
    summary = {
        "version": ELECTION_VERSION,
        "policy_version": ELECTION_POLICY_VERSION,
        "round2_threshold_note": "challenger_threshold_was_set_after_reviewing_round1_data",
        "status": "stop_b",
        "notebook_entries": len(entries),
        "watchlist_size": len(watchlist),
        "changed_count": len(changed_ids),
        "changed_entry_ids": changed_ids,
        "outside_watchlist_changed": outside_watchlist_changed,
        "status_counts": dict(Counter(str(item.get("status") or "") for item in log_entries)),
        "gate_counts": dict(Counter(str(item.get("gate_status") or "") for item in log_entries)),
        "lmstudio": client.stats(),
    }
    return {
        "entries": updated_entries,
        "log": {
            "version": ELECTION_VERSION,
            "policy_version": ELECTION_POLICY_VERSION,
            "entries": log_entries,
        },
        "summary": summary,
    }


def _elect_entry(
    *,
    item: dict[str, Any],
    db_path: Path,
    client: "LocalLMStudioClient",
    context_vote_cap: int,
) -> dict[str, Any]:
    source_term = str(item.get("source_term") or item.get("entry_id") or "").strip()
    candidates = [str(candidate.get("text") or "").strip() for candidate in item.get("candidates") or []]
    candidates = _unique_nonempty(candidates)
    backtranslations: list[dict[str, Any]] = []
    backtranslation_vote_counts: Counter[str] = Counter()
    errors: list[str] = []
    incumbent = str(item.get("canonical_target_vi") or "").strip()
    is_polysemy = "audit_polysemy" in set(item.get("watchlist_reasons") or [])
    if is_polysemy:
        backtranslation_status = "backtranslation_bypassed_polysemy"
    else:
        for candidate in candidates:
            response = _call_json_with_retry(
                client,
                _backtranslation_messages(candidate),
                response_format=BACKTRANSLATION_SCHEMA,
            )
            english = str((response.get("parsed_json") or {}).get("english") or "").strip()
            is_source_string = _target_string_equals_source(candidate, source_term)
            is_match = _english_matches_source(english, source_term) and not is_source_string
            if is_match:
                backtranslation_vote_counts[candidate] += 1
            if response.get("json_error"):
                errors.append(str(response["json_error"]))
            backtranslations.append(
                {
                    "candidate": candidate,
                    "english": english,
                    "matched_source": is_match,
                    "blocked_source_string_candidate": bool(is_source_string),
                    "json_error": response.get("json_error") or "",
                    "attempts": response.get("attempts") or 1,
                    "from_cache": bool(response.get("from_cache")),
                }
            )
        backtranslation_status = "backtranslation_completed"
    if _only_incumbent_has_votes(backtranslation_vote_counts, incumbent):
        return {
            "status": "confirmed_backtranslation",
            "winner": incumbent,
            "policy_version": ELECTION_POLICY_VERSION,
            "backtranslation_status": backtranslation_status,
            "backtranslations": backtranslations,
            "context_votes": [],
            "vote_counts": dict(backtranslation_vote_counts),
            "backtranslation_vote_counts": dict(backtranslation_vote_counts),
            "context_vote_counts": {},
            "errors": errors,
        }
    context_result = _context_vote(
        item=item,
        source_term=source_term,
        candidates=candidates,
        db_path=db_path,
        client=client,
        cap=context_vote_cap,
        incumbent=incumbent,
        prior_vote_counts=backtranslation_vote_counts,
    )
    return {
        "status": context_result["status"],
        "winner": context_result["winner"],
        "policy_version": ELECTION_POLICY_VERSION,
        "backtranslation_status": backtranslation_status,
        "backtranslations": backtranslations,
        "context_votes": context_result["votes"],
        "vote_counts": context_result["vote_counts"],
        "backtranslation_vote_counts": dict(backtranslation_vote_counts),
        "context_vote_counts": context_result["context_vote_counts"],
        "errors": errors + context_result["errors"],
    }


def _context_vote(
    *,
    item: dict[str, Any],
    source_term: str,
    candidates: list[str],
    db_path: Path,
    client: "LocalLMStudioClient",
    cap: int,
    incumbent: str,
    prior_vote_counts: Counter[str] | None = None,
) -> dict[str, Any]:
    contexts = _load_context_blocks(db_path, item, cap=cap)
    votes: list[dict[str, Any]] = []
    vote_counts: Counter[str] = Counter(prior_vote_counts or {})
    context_vote_counts: Counter[str] = Counter()
    errors: list[str] = []
    for context in contexts:
        response = _call_json_with_retry(
            client,
            _context_vote_messages(source_term, str(context["text"])),
            response_format=CONTEXT_VOTE_SCHEMA,
        )
        vietnamese = str((response.get("parsed_json") or {}).get("vietnamese") or "").strip()
        matched_candidate = _match_vietnamese_candidate(vietnamese, candidates)
        if matched_candidate:
            vote_counts[matched_candidate] += 1
            context_vote_counts[matched_candidate] += 1
        if response.get("json_error"):
            errors.append(str(response["json_error"]))
        votes.append(
            {
                "block_id": context["block_id"],
                "order_index": context["order_index"],
                "sentence": context["text"],
                "vietnamese": vietnamese,
                "matched_candidate": matched_candidate,
                "json_error": response.get("json_error") or "",
                "attempts": response.get("attempts") or 1,
                "from_cache": bool(response.get("from_cache")),
            }
        )
    if not votes:
        return {
            "status": "election_error",
            "winner": str(item.get("canonical_target_vi") or ""),
            "votes": votes,
            "vote_counts": dict(vote_counts),
            "context_vote_counts": dict(context_vote_counts),
            "errors": errors + ["no_context_blocks"],
        }
    winner = _winning_challenger(vote_counts, incumbent)
    if winner:
        return {
            "status": "elected_context_vote",
            "winner": winner,
            "votes": votes,
            "vote_counts": dict(vote_counts),
            "context_vote_counts": dict(context_vote_counts),
            "errors": errors,
        }
    return {
        "status": "unresolved_tie",
        "winner": str(item.get("canonical_target_vi") or ""),
        "votes": votes,
        "vote_counts": dict(vote_counts),
        "context_vote_counts": dict(context_vote_counts),
        "errors": errors,
    }


def _call_json_with_retry(
    client: "LocalLMStudioClient",
    messages: list[dict[str, str]],
    *,
    response_format: dict[str, Any],
) -> dict[str, Any]:
    first = client.call(messages, response_format=response_format)
    if not first.get("json_error"):
        first["attempts"] = 1
        return first
    retry_messages = messages + [
        {
            "role": "user",
            "content": "Your previous answer was not valid JSON. Return only valid JSON matching the schema.",
        }
    ]
    second = client.call(retry_messages, response_format=response_format)
    second["attempts"] = 2
    if second.get("json_error"):
        second["first_json_error"] = first.get("json_error") or ""
    return second


def _backtranslation_messages(vietnamese_term: str) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "You translate Vietnamese technical terms into concise English. "
                "Return JSON only. Do not explain."
            ),
        },
        {
            "role": "user",
            "content": (
                "Translate this Vietnamese term or phrase into the most likely concise English "
                f"technical term:\n{vietnamese_term}"
            ),
        },
    ]


def _context_vote_messages(source_term: str, source_sentence: str) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "You choose a concise Vietnamese rendering for one English technical term in context. "
                "Return JSON only. Do not explain."
            ),
        },
        {
            "role": "user",
            "content": (
                f"English term: {source_term}\n"
                f"English context sentence/block: {source_sentence}\n"
                "What concise Vietnamese term should translate the English term in this context?"
            ),
        },
    ]


def _load_context_blocks(db_path: Path, item: dict[str, Any], *, cap: int) -> list[dict[str, Any]]:
    block_ids: list[str] = []
    for candidate in item.get("candidates") or []:
        block_id = str(candidate.get("evidence_block_id") or "").strip()
        if block_id and block_id not in block_ids:
            block_ids.append(block_id)
    entry_evidence = []
    # watchlist.json only stores counts, so use DB rows from candidate evidence first.
    if len(block_ids) < cap:
        entry_evidence = list(item.get("evidence_block_ids") or [])
    for block_id in entry_evidence:
        block_id = str(block_id or "").strip()
        if block_id and block_id not in block_ids:
            block_ids.append(block_id)
    if not block_ids:
        return []
    placeholders = ",".join("?" * len(block_ids))
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            f"""
            SELECT block_id, order_index, COALESCE(NULLIF(original_text, ''), text) AS source_text
            FROM blocks
            WHERE block_id IN ({placeholders})
            ORDER BY order_index
            """,
            block_ids,
        ).fetchall()
    contexts: list[dict[str, Any]] = []
    for row in rows:
        text = _squash_ws(str(row["source_text"] or ""))
        if not text:
            continue
        contexts.append(
            {
                "block_id": str(row["block_id"]),
                "order_index": int(row["order_index"]),
                "text": text[:1200],
            }
        )
        if len(contexts) >= cap:
            break
    return contexts


def _new_collision_for_entry(entries: list[dict[str, Any]], entry_id: str) -> dict[str, Any] | None:
    rows = notebook_entries_to_term_rows(entries)
    for row in rows:
        if str(row.get("glossary_id") or row.get("source_term") or "") != entry_id:
            continue
        audit = row.get("audit") if isinstance(row.get("audit"), dict) else {}
        flag = audit.get("collision_soft_fallback")
        if isinstance(flag, dict):
            return flag
    return None


def _append_reelection_audit(
    entry: dict[str, Any],
    incumbent: str,
    winner: str,
    election: dict[str, Any],
) -> None:
    audit = entry.get("audit")
    if not isinstance(audit, dict):
        audit = {}
        entry["audit"] = audit
    audit["reelection"] = {
        "version": ELECTION_VERSION,
        "old_canonical_target_vi": incumbent,
        "new_canonical_target_vi": winner,
        "status": election.get("status") or "",
    }


def _english_matches_source(english: str, source_term: str) -> bool:
    english_key = _loose_english_key(english)
    source_key = _loose_english_key(source_term)
    if not english_key or not source_key:
        return False
    if english_key == source_key:
        return True
    english_tokens = english_key.split()
    source_tokens = source_key.split()
    if english_tokens == source_tokens:
        return True
    english_singular = _singularize_phrase(english_key)
    source_singular = _singularize_phrase(source_key)
    return english_singular == source_singular


def _loose_english_key(text: str) -> str:
    chars = [ch.casefold() if ch.isalnum() else " " for ch in str(text or "")]
    return _squash_ws("".join(chars))


def _singularize_phrase(text: str) -> str:
    return " ".join(_singularize_english_token(token) for token in text.split())


def _singularize_english_token(token: str) -> str:
    if len(token) > 4 and (
        token.endswith("ses")
        or token.endswith("xes")
        or token.endswith("zes")
        or token.endswith("ches")
        or token.endswith("shes")
    ):
        return token[:-2]
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _target_string_equals_source(candidate: str, source_term: str) -> bool:
    return _loose_english_key(candidate) == _loose_english_key(source_term)


def _only_incumbent_has_votes(vote_counts: Counter[str], incumbent: str) -> bool:
    if not vote_counts:
        return False
    incumbent_key = normalize_target_key(incumbent)
    for candidate, count in vote_counts.items():
        if count and normalize_target_key(candidate) != incumbent_key:
            return False
    return bool(vote_counts.get(incumbent))


def _winning_challenger(vote_counts: Counter[str], incumbent: str) -> str:
    incumbent_key = normalize_target_key(incumbent)
    incumbent_votes = sum(
        count for candidate, count in vote_counts.items()
        if normalize_target_key(candidate) == incumbent_key
    )
    challengers = [
        (candidate, count)
        for candidate, count in vote_counts.items()
        if normalize_target_key(candidate) != incumbent_key
    ]
    if not challengers:
        return ""
    challengers.sort(key=lambda item: (-item[1], normalize_target_key(item[0])))
    top_candidate, top_count = challengers[0]
    second_count = challengers[1][1] if len(challengers) > 1 else -1
    if top_count >= 2 and top_count > incumbent_votes and top_count > second_count:
        return top_candidate
    return ""


def _match_vietnamese_candidate(vietnamese: str, candidates: list[str]) -> str:
    output_key = normalize_target_key(vietnamese)
    if not output_key:
        return ""
    for candidate in candidates:
        if normalize_target_key(candidate) == output_key:
            return candidate
    for candidate in candidates:
        candidate_key = normalize_target_key(candidate)
        if candidate_key and (candidate_key in output_key or output_key in candidate_key):
            return candidate
    return ""


def _unique_nonempty(items: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        value = str(item or "").strip()
        key = normalize_target_key(value)
        if not value or key in seen:
            continue
        result.append(value)
        seen.add(key)
    return result


def _squash_ws(text: str) -> str:
    return " ".join(str(text or "").split())


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


def _evidence_block_ids_by_entry(entries: list[dict[str, Any]]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for entry in entries:
        entry_id = _entry_id(entry)
        block_ids: list[str] = []
        for variant in entry.get("source_variants") or []:
            if not isinstance(variant, dict):
                continue
            for block_id in variant.get("evidence_block_ids") or []:
                block_id = str(block_id or "").strip()
                if block_id and block_id not in block_ids:
                    block_ids.append(block_id)
        result[entry_id] = block_ids
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


class LocalLMStudioClient:
    def __init__(
        self,
        *,
        endpoint: str,
        model: str,
        cache_path: Path,
        timeout_sec: int,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.cache_path = cache_path
        self.timeout_sec = timeout_sec
        self.calls = 0
        self.cache_hits = 0
        self.transport_errors = 0
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def call(self, messages: list[dict[str, str]], *, response_format: dict[str, Any]) -> dict[str, Any]:
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
            "top_p": 1.0,
            "top_k": 0,
            "min_p": 0.0,
            "seed": 20260612,
            "repeat_penalty": 1.0,
            "max_tokens": 256,
            "stream": False,
            "response_format": response_format,
            "reasoning_effort": "none",
        }
        request_json = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        cache_key = sha256(request_json.encode("utf-8")).hexdigest()
        cached = self._load_cached(cache_key)
        if cached is not None:
            self.cache_hits += 1
            cached["from_cache"] = True
            return cached
        self.calls += 1
        started = time.perf_counter()
        try:
            response = requests.post(
                f"{self.endpoint}/v1/chat/completions",
                json=body,
                timeout=self.timeout_sec,
            )
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            self.transport_errors += 1
            latency_ms = int((time.perf_counter() - started) * 1000)
            return {
                "text": "",
                "parsed_json": None,
                "json_error": f"local_transport_error:{type(exc).__name__}",
                "latency_ms": latency_ms,
                "from_cache": False,
                "cache_key": cache_key,
                "usage": {},
                "model_echo": "",
            }
        latency_ms = int((time.perf_counter() - started) * 1000)
        choice = (payload.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        content = str(message.get("content") or "")
        parsed_json, json_error = _parse_json_content(content)
        result = {
            "text": content,
            "parsed_json": parsed_json,
            "json_error": json_error,
            "latency_ms": latency_ms,
            "from_cache": False,
            "cache_key": cache_key,
            "usage": payload.get("usage") or {},
            "model_echo": payload.get("model") or "",
            "finish_reason": choice.get("finish_reason") or "",
        }
        if not json_error:
            self._store_cached(cache_key, request_json, result)
        return result

    def stats(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "model": self.model,
            "calls": self.calls,
            "cache_hits": self.cache_hits,
            "transport_errors": self.transport_errors,
            "cache_path": str(self.cache_path),
        }

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.cache_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS local_lmstudio_reelection_cache (
                    cache_key TEXT PRIMARY KEY,
                    model TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    def _load_cached(self, cache_key: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT response_json FROM local_lmstudio_reelection_cache WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
        if row is None:
            return None
        return json.loads(str(row["response_json"]))

    def _store_cached(self, cache_key: str, request_json: str, result: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO local_lmstudio_reelection_cache (
                    cache_key, model, request_json, response_json
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    cache_key,
                    self.model,
                    request_json,
                    json.dumps(result, ensure_ascii=False, sort_keys=True),
                ),
            )


def _parse_json_content(content: str) -> tuple[Any | None, str]:
    if not str(content or "").strip():
        return None, "empty_content"
    try:
        return json.loads(content), ""
    except json.JSONDecodeError as exc:
        return None, f"json_decode_error:{exc.msg}"


if __name__ == "__main__":
    raise SystemExit(main())
