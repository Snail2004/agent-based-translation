from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from pipeline.agents.llm_client import estimate_prompt_tokens
from pipeline.prepass.concept_key import concept_key, singularize_token
from pipeline.prepass.db_source import load_document_from_connection
from pipeline.prepass.prompt import render_chapter_blocks
from pipeline.prepass.runner import PrepassWindow, build_d2l_prepass_windows
from pipeline.prepass.span_resolver import _find_word_boundary_matches


PROMPT_VERSION = "d2l_terminology_v8"
PACK_TOKEN_CAP = 1500
PROMPT_TOKEN_CAP = 6000
PACK_VERSION = "builder_v2_memory_pack_stage_c1_slim"
PACK_PROVENANCE = "glossary_entries"
RESPONSE_FORMAT = {"type": "json_object"}
OUTPUT_TOKEN_CAP_ESTIMATE = 1200
CONFLICT_FIXTURE_TERMS = ("dataset", "loss", "activation")


SYSTEM_PROMPT = """You are the World Builder agent for an autonomous English→Vietnamese technical-book
translation pipeline (D2L). Read ONLY the English source window provided. Maintain a
terminology registry consistent across the whole book. Never use any Vietnamese
reference, glossary, gold, or answer key — build from the English source and YOUR OWN
prior notes only.

INPUTS:
- ENGLISH_SOURCE_WINDOW: source blocks with [block_id] markers.
- MEMORY_PACK: terms YOU already coined in earlier windows that also appear in this
  window (YOUR OWN notebook — a continuity aid, NOT an answer key). Each item:
  source_term, canonical_target_vi, allowed_variants[], and for near-number items the
  related surface seen in this window.

JOB: account for every controlled term/concept visible in this window by placing it in
EXACTLY ONE of four buckets. Favour RECALL — extract generously; a downstream
deterministic filter (NOT you) decides which terms are consistency-bearing.

Hard rules:
- Prompt version: d2l_terminology_v8. Return ONLY valid JSON matching the contract.
  Keep strings concise; no commentary outside JSON.
- A controlled term needs book-wide consistency: ML concepts, math/statistics terms,
  model/layer/architecture names, abbreviations, framework/API names, named
  datasets/algorithms.
- New-term restraint (applies to `new_terms` ONLY): by default do NOT create a NEW
  standalone entry for an ordinary English word (input, output, value, number, result,
  example, sample, set, case, problem, step, size). DO create one when the word is used
  as a controlled ML/math concept, is repeated as a concept across evidence blocks,
  appears in a definition/heading/math context, or is already in MEMORY_PACK. When a
  precise multi-word term covers the concept ("input layer", "loss function", "feature
  map"), emit that and do not also emit the bare head as a separate new term.
- Existing MEMORY_PACK terms are NEVER subject to that restraint: they must always be
  accounted (see RECALL RULE). If you think a pack term is too generic to be a real term,
  report it in `conflicts` with conflict_type "termhood_suspected" — never drop it
  silently.
- Prefer ONE canonical source surface per concept, singular base form. Record number
  variants ("features" vs "feature") as updates_to_existing, not as new terms.
- Each new term commits to ONE canonical Vietnamese target with FULL diacritics
  ("tác nhân", not "tac nhan"); other acceptable VI forms go in target_variants.
- term_type ∈ {term, abbreviation, proper_noun, code_api}. do_not_translate=true for
  framework/library/API/dataset names kept in English.

FOUR BUCKETS:
1. new_terms — controlled terms NOT in MEMORY_PACK. Fields: source_term (singular
   canonical), canonical_target_vi, term_type, do_not_translate, termhood (short reason),
   target_variants[], evidence_block_ids[].
2. updates_to_existing — a MEMORY_PACK term appearing here that gains something: add
   source_variant(s), target_variant(s), evidence_block_ids. A new target_variant is
   allowed ONLY when justified by the English evidence context or by a one-clause reason;
   it MUST carry evidence_block_id and variant_reason; do NOT add a VI variant differing
   only by "các"/"những"; at most 2 new target_variants per term per window. NEVER change
   the existing canonical here.
3. conflicts — when a MEMORY_PACK term's existing canonical VI seems wrong, its surface
   is used in a different sense, or it seems too generic to be a term. Declare, never
   silently fix. Fields: source_term, existing_canonical_target_vi, proposed_target_vi
   (or null), conflict_type ∈ {canonical_target_change, polysemy_suspected,
   bad_existing_target, termhood_suspected, plural_only_difference, uncertain},
   reason (one clause), evidence_block_ids[].
4. seen_existing_terms — MEMORY_PACK terms appearing here that need NO change. Fields:
   source_term, evidence_block_ids[].

RECALL RULE (mandatory): Every controlled source term/concept visible in this window must
be represented exactly once across the four buckets; include all evidence block ids where
it appears. Existing MEMORY_PACK terms are not exempt — if one appears and needs no
change, put it in seen_existing_terms. Never omit a visible term because it "already
exists".

Glossary-only: output only glossary entries; do not output entities, relations, or
motifs. Vietnamese targets must be YOUR OWN proposals or prior notes, never a
reference/gold.

Return JSON:
{ "chapter_id":"...", "window_id":"...", "new_terms":[...], "updates_to_existing":[...],
  "conflicts":[...], "seen_existing_terms":[...] }"""


@dataclass(frozen=True)
class RegistryEntry:
    glossary_id: str
    source_term: str
    target_term: str
    term_type: str
    do_not_translate: bool
    allowed_variants: tuple[str, ...]
    evidence_block_ids: tuple[str, ...]
    occurrences_count: int
    concept_key: str
    first_evidence_order: int | None


def load_registry_entries(
    conn: sqlite3.Connection,
    *,
    doc_id: str = "d2l",
) -> list[RegistryEntry]:
    block_orders = _block_orders(conn, doc_id)
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
    entries: list[RegistryEntry] = []
    for row in rows:
        source = str(row["source_term"] or "")
        evidence = tuple(
            item for item in _json_list(row["evidence_span_ids_json"]) if isinstance(item, str)
        )
        orders = [block_orders[item] for item in evidence if item in block_orders]
        entries.append(
            RegistryEntry(
                glossary_id=str(row["glossary_id"] or ""),
                source_term=source,
                target_term=str(row["target_term"] or ""),
                term_type=str(row["term_type"] or "term"),
                do_not_translate=bool(row["do_not_translate"]),
                allowed_variants=tuple(
                    item for item in _json_list(row["allowed_variants_json"]) if isinstance(item, str)
                ),
                evidence_block_ids=evidence,
                occurrences_count=int(row["occurrences_count"] or 0),
                concept_key=concept_key(source),
                first_evidence_order=min(orders) if orders else None,
            )
        )
    return entries


def build_memory_pack(
    entries: list[RegistryEntry],
    window: PrepassWindow,
    *,
    pack_mode: str = "proxy_chronological",
    budget_tokens: int = PACK_TOKEN_CAP,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if pack_mode not in {"proxy_chronological", "proxy_full_registry"}:
        raise ValueError(f"Unknown pack mode: {pack_mode}")
    window_blocks = list(window.blocks)
    window_start_order = min((int(block.get("order_index") or 0) for block in window_blocks), default=0)
    window_text_by_block = {
        str(block["block_id"]): str(block.get("clean_text") or block.get("source_text") or "")
        for block in window_blocks
    }
    candidates: list[dict[str, Any]] = []
    excluded_no_surface: list[str] = []
    surfaces_detected: set[str] = set()

    for entry in entries:
        if pack_mode == "proxy_chronological" and not _has_prior_evidence(entry, window_start_order):
            excluded_no_surface.append(entry.source_term)
            continue
        exact_hits = _find_entry_hits(entry.source_term, window_text_by_block)
        if exact_hits:
            surfaces_detected.add(entry.source_term)
            candidates.append(_candidate(entry, "exact_surface", entry.source_term, exact_hits))
            continue
        concept_surface, concept_hits = _find_concept_variant_hits(entry, window_text_by_block)
        if concept_surface and concept_hits:
            surfaces_detected.add(concept_surface)
            candidates.append(_candidate(entry, "concept_key", concept_surface, concept_hits))
            continue
        excluded_no_surface.append(entry.source_term)

    candidates.sort(key=_candidate_priority_sort)

    matched_existing_terms: list[dict[str, Any]] = []
    near_number_variants: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for candidate in candidates:
        target_list = matched_existing_terms if candidate["match_type"] == "exact_surface" else near_number_variants
        target_list.append(_pack_item(candidate))
        pack = _pack_payload(pack_mode, matched_existing_terms, near_number_variants)
        token_estimate = _json_token_estimate(pack)
        if token_estimate > budget_tokens:
            target_list.pop()
            dropped.append(
                {
                    "glossary_id": candidate["glossary_id"],
                    "source_term": candidate["source_term"],
                    "match_type": candidate["match_type"],
                    "priority": candidate["priority"],
                    "reason": f"would_exceed_PACK_TOKEN_CAP_{budget_tokens}",
                }
            )

    pack = _pack_payload(pack_mode, matched_existing_terms, near_number_variants)
    audit = {
        "included_by_exact_surface": [
            item["source_term"] for item in matched_existing_terms
        ],
        "included_by_concept_key": [
            {
                "source_term": item["source_term"],
                "related_surface_seen": item["related_surface_seen"],
            }
            for item in near_number_variants
        ],
        "excluded_no_surface_match": {
            "count": len(excluded_no_surface),
            "sample": sorted(
                set(excluded_no_surface), key=lambda value: (value.casefold(), value)
            )[:30],
        },
        "dropped_by_budget": dropped,
        "pack_token_estimate": _json_token_estimate(pack),
        "window_term_surfaces_detected": sorted(
            surfaces_detected, key=lambda value: (value.casefold(), value)
        ),
        "pack_source_mode": pack_mode,
        "pack_provenance": PACK_PROVENANCE,
    }
    return pack, audit


def build_builder_v2_messages(
    *,
    pack: dict[str, Any],
    chapter_id: str,
    window_id: str,
    blocks: list[dict[str, Any]],
) -> list[dict[str, str]]:
    user = (
        "MEMORY_PACK\n"
        f"{pack_json_text(pack)}\n\n"
        "CHAPTER_ID\n"
        f"{chapter_id}\n\n"
        "WINDOW_ID\n"
        f"{window_id}\n\n"
        "ENGLISH_SOURCE_WINDOW_WITH_BLOCK_MARKERS\n"
        f"{render_chapter_blocks({'blocks': blocks})}"
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def render_window(
    entries: list[RegistryEntry],
    window: PrepassWindow,
    *,
    pack_mode: str,
) -> dict[str, Any]:
    pack, audit = build_memory_pack(entries, window, pack_mode=pack_mode)
    messages = build_builder_v2_messages(
        pack=pack,
        chapter_id=window.chapter_id,
        window_id=window.window_id,
        blocks=window.blocks,
    )
    prompt_tokens = estimate_prompt_tokens(messages, RESPONSE_FORMAT)
    source_tokens = sum(
        max(1, len(str(block.get("clean_text") or block.get("source_text") or "")) // 4)
        for block in window.blocks
    )
    if audit["pack_token_estimate"] > PACK_TOKEN_CAP:
        raise RuntimeError(
            f"Pack token estimate {audit['pack_token_estimate']} exceeds cap {PACK_TOKEN_CAP}"
        )
    if prompt_tokens > PROMPT_TOKEN_CAP:
        raise RuntimeError(
            f"Prompt token estimate {prompt_tokens} exceeds cap {PROMPT_TOKEN_CAP}"
        )
    return {
        "window_id": window.window_id,
        "chapter_id": window.chapter_id,
        "block_ids": [str(block["block_id"]) for block in window.blocks],
        "messages": messages,
        "pack": pack,
        "audit": audit,
        "token_estimate": {
            "system": max(1, len(SYSTEM_PROMPT) // 4),
            "pack": int(audit["pack_token_estimate"]),
            "source_window": source_tokens,
            "prompt": prompt_tokens,
            "output_cap": OUTPUT_TOKEN_CAP_ESTIMATE,
            "prompt_cap": PROMPT_TOKEN_CAP,
            "pack_cap": PACK_TOKEN_CAP,
        },
    }


def select_representative_windows(
    windows: list[PrepassWindow],
    rendered: dict[str, dict[str, Any]],
) -> list[tuple[str, PrepassWindow]]:
    if not windows:
        return []
    selected: list[tuple[str, PrepassWindow]] = [("chapter_start", windows[0])]
    max_pack = max(
        windows,
        key=lambda window: (
            int(rendered[window.window_id]["audit"]["pack_token_estimate"]),
            window.window_id,
        ),
    )
    selected.append(("max_pack", max_pack))
    fixture = _fixture_window(windows)
    if fixture is not None:
        selected.append(("conflict_fixture", fixture))
    seen: set[str] = set()
    unique: list[tuple[str, PrepassWindow]] = []
    for label, window in selected:
        if window.window_id in seen:
            continue
        seen.add(window.window_id)
        unique.append((label, window))
    for window in windows:
        if len(unique) >= 3:
            break
        if window.window_id not in seen:
            seen.add(window.window_id)
            unique.append(("additional_coverage", window))
    return unique


def build_render_report(
    *,
    db_path: Path,
    doc_id: str,
    chapter_id: str,
    pack_mode: str,
    windows: list[PrepassWindow],
    rendered_by_window: dict[str, dict[str, Any]],
    selected_windows: list[tuple[str, PrepassWindow]],
) -> dict[str, Any]:
    selected_payloads = []
    for label, window in selected_windows:
        rendered = rendered_by_window[window.window_id]
        selected_payloads.append(
            {
                "label": label,
                "window_id": window.window_id,
                "block_ids": rendered["block_ids"],
                "audit": rendered["audit"],
                "token_estimate": rendered["token_estimate"],
                "prompt_file": f"{label}_{window.window_id}.txt",
            }
        )
    prompt_estimates = [
        int(rendered_by_window[window.window_id]["token_estimate"]["prompt"])
        for window in windows
    ]
    pack_estimates = [
        int(rendered_by_window[window.window_id]["audit"]["pack_token_estimate"])
        for window in windows
    ]
    total_prompt = sum(prompt_estimates)
    return {
        "phase": "BUILDER-V2-B",
        "prompt_version": PROMPT_VERSION,
        "db_path": str(db_path),
        "doc_id": doc_id,
        "chapter_id": chapter_id,
        "pack_source_mode": pack_mode,
        "pack_provenance": PACK_PROVENANCE,
        "caps": {
            "PACK_TOKEN_CAP": PACK_TOKEN_CAP,
            "PROMPT_TOKEN_CAP": PROMPT_TOKEN_CAP,
            "OUTPUT_TOKEN_CAP_ESTIMATE": OUTPUT_TOKEN_CAP_ESTIMATE,
        },
        "zero_api": True,
        "zero_db_write": True,
        "windows": {
            "count": len(windows),
            "total_prompt_tokens_est": total_prompt,
            "max_prompt_tokens_est": max(prompt_estimates, default=0),
            "max_pack_tokens_est": max(pack_estimates, default=0),
            "stage_c_upper_bound_tokens_est": total_prompt
            + len(windows) * OUTPUT_TOKEN_CAP_ESTIMATE,
        },
        "context_policy": {
            "include": [
                "matched_existing_terms: registry entry whose source surface appears in the window",
                "near_number_variants: registry entry whose concept_key matches a number variant surface in the window",
            ],
            "exclude": [
                "registry entries with no detected source surface in the window",
                "future entries when pack_mode=proxy_chronological",
                "items dropped by PACK_TOKEN_CAP",
            ],
        },
        "cache_policy": {
            "stable_prefix": "system prompt d2l_terminology_v8",
            "changing_suffix": "MEMORY_PACK + chapter_id + window_id + source window",
            "deterministic_sort": True,
        },
        "halt_conditions": [
            "pack_token_estimate > PACK_TOKEN_CAP",
            "prompt_tokens > PROMPT_TOKEN_CAP",
        ],
        "cost_quality_projection": {
            "note": "Stage B does not spend API tokens; this is a Stage C planning estimate.",
            "prompt_tokens_per_chapter": total_prompt,
            "calls": len(windows),
            "upper_bound_tokens_with_output_cap": total_prompt
            + len(windows) * OUTPUT_TOKEN_CAP_ESTIMATE,
        },
        "selected_windows": selected_payloads,
    }


def prompt_text(messages: list[dict[str, str]]) -> str:
    return "\n\n".join(
        f"{message['role'].upper()}\n{message['content']}" for message in messages
    )


def _pack_payload(
    pack_mode: str,
    matched_existing_terms: list[dict[str, Any]],
    near_number_variants: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "pack_version": PACK_VERSION,
        "pack_source_mode": pack_mode,
        "pack_provenance": PACK_PROVENANCE,
        "matched_existing_terms": matched_existing_terms,
        "near_number_variants": near_number_variants,
    }


def _candidate(
    entry: RegistryEntry,
    match_type: str,
    surface_seen: str,
    evidence_block_ids: list[str],
) -> dict[str, Any]:
    return {
        "glossary_id": entry.glossary_id,
        "source_term": entry.source_term,
        "canonical_target_vi": entry.target_term,
        "allowed_variants": _compact_variants(entry.allowed_variants, entry.target_term),
        "term_type": entry.term_type,
        "do_not_translate": entry.do_not_translate,
        "concept_key": entry.concept_key,
        "match_type": match_type,
        "related_surface_seen": surface_seen if match_type == "concept_key" else None,
        "evidence_block_ids": sorted(set(evidence_block_ids)),
        "status": getattr(entry, "status", None),
        "occurrences_total": int(entry.occurrences_count or 0),
        "priority": None,
    }


def _pack_item(candidate: dict[str, Any]) -> dict[str, Any]:
    item = {
        "source_term": candidate["source_term"],
        "canonical_target_vi": candidate["canonical_target_vi"],
        "allowed_variants": candidate["allowed_variants"][:2],
        "term_type": candidate["term_type"],
        "do_not_translate": candidate["do_not_translate"],
    }
    if candidate.get("status") == "conflict_pending":
        item["status"] = "conflict_pending"
    if candidate["match_type"] == "concept_key":
        item["related_surface_seen"] = candidate["related_surface_seen"]
        item["concept_key"] = candidate["concept_key"]
    return item


def pack_json_text(pack: dict[str, Any]) -> str:
    return _stable_json(pack)


def _candidate_priority_sort(item: dict[str, Any]) -> tuple[Any, ...]:
    source_term = str(item["source_term"])
    return (
        0 if item.get("status") == "conflict_pending" else 1,
        -int(item.get("occurrences_total") or 0),
        0 if " " in source_term.strip() else 1,
        0 if item["match_type"] == "exact_surface" else 1,
        source_term.casefold(),
        str(item["concept_key"]),
        str(item["glossary_id"]),
    )


def _find_entry_hits(source_term: str, text_by_block: dict[str, str]) -> list[str]:
    block_ids: list[str] = []
    for block_id, text in text_by_block.items():
        if _find_word_boundary_matches(text, source_term):
            block_ids.append(block_id)
    return block_ids


def _find_concept_variant_hits(
    entry: RegistryEntry,
    text_by_block: dict[str, str],
) -> tuple[str | None, list[str]]:
    for surface in _number_variant_surfaces(entry.source_term):
        if surface.casefold() == entry.source_term.casefold():
            continue
        if concept_key(surface) != entry.concept_key:
            continue
        hits = _find_entry_hits(surface, text_by_block)
        if hits:
            return surface, hits
    return None, []


def _number_variant_surfaces(source_term: str) -> list[str]:
    tokens = str(source_term).split()
    if not tokens:
        return []
    last = tokens[-1]
    prefix = tokens[:-1]
    variants = {source_term}
    singular = singularize_token(last)
    if singular and singular != last:
        variants.add(" ".join([*prefix, singular]))
    plural = _pluralize_token(singular or last)
    if plural and plural != last:
        variants.add(" ".join([*prefix, plural]))
    return sorted(
        variants,
        key=lambda value: (
            value.casefold() == source_term.casefold(),
            value.casefold(),
            value,
        ),
    )


def _pluralize_token(token: str) -> str:
    irregular = {
        "analysis": "analyses",
        "axis": "axes",
        "hypothesis": "hypotheses",
        "index": "indices",
        "matrix": "matrices",
        "vertex": "vertices",
    }
    lower = token.casefold()
    if lower in irregular:
        return _match_case(irregular[lower], token)
    if re.search(r"[^aeiou]y$", token, flags=re.IGNORECASE):
        return token[:-1] + "ies"
    if re.search(r"(s|x|z|ch|sh)$", token, flags=re.IGNORECASE):
        return token + "es"
    return token + "s"


def _match_case(value: str, original: str) -> str:
    return value.upper() if original.isupper() else value


def _has_prior_evidence(entry: RegistryEntry, window_start_order: int) -> bool:
    return entry.first_evidence_order is not None and entry.first_evidence_order < window_start_order


def _block_orders(conn: sqlite3.Connection, doc_id: str) -> dict[str, int]:
    rows = conn.execute(
        "SELECT block_id, order_index FROM blocks WHERE doc_id = ?",
        (doc_id,),
    ).fetchall()
    return {str(row["block_id"]): int(row["order_index"]) for row in rows}


def _compact_variants(variants: Iterable[str], canonical: str) -> list[str]:
    values = []
    seen = {canonical.casefold()}
    for variant in variants:
        clean = re.sub(r"\s+", " ", str(variant).strip())
        if not clean or clean.casefold() in seen:
            continue
        seen.add(clean.casefold())
        values.append(clean)
        if len(values) >= 2:
            break
    return values


def _fixture_window(windows: list[PrepassWindow]) -> PrepassWindow | None:
    for window in windows:
        text = "\n".join(
            str(block.get("clean_text") or block.get("source_text") or "")
            for block in window.blocks
        )
        if any(_find_word_boundary_matches(text, term) for term in CONFLICT_FIXTURE_TERMS):
            return window
    return None


def _stable_json(value: Any, *, indent: int | None = None) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=indent,
        separators=(",", ":") if indent is None else None,
    )


def _json_token_estimate(value: Any) -> int:
    return max(1, len(_stable_json(value)) // 4)


def _json_list(value: Any) -> list[Any]:
    try:
        parsed = json.loads(str(value or "[]"))
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []
