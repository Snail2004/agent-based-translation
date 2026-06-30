from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from pipeline.agents.llm_client import estimate_prompt_tokens


PROMPT_VERSION = "d2l_term_audit_v1"
SYSTEM_PROMPT = """You are a terminology auditor for an English-to-Vietnamese translation memory of the
deep-learning textbook "Dive into Deep Learning" (D2L). An upstream extractor (the
"Builder") favored recall, so the candidate list mixes real domain terms with generic
words, code tokens, and over-long phrases. Your job is to LABEL each candidate so a later
step can decide which entries to prioritize in the translator's memory. You do NOT
translate, rewrite, or invent terms - you only judge and label what you are given.

Termhood principle (apply it; do NOT use any fixed word list):
- A CONTROLLED TERM names a domain concept (method, object, quantity, model, structure)
  whose inconsistent translation across the book would harm meaning or confuse the reader.
  It deserves a glossary entry.
- A GENERIC WORD is ordinary vocabulary (everyday nouns, verbs, connectives) that a
  competent translator renders correctly from context without a glossary, even inside a
  technical sentence. Judge by the role the word plays in the evidence, not by a fixed list.
- Decide from the evidence sentences and your domain knowledge - not from frequency alone.

Recall-safety (this matters): the memory's value is translation CONSISTENCY, so dropping a
real term is worse than keeping a generic one - a kept generic term may still fall out later
under budget, but a dropped real term is lost. When evidence is thin or you are genuinely
unsure, choose keep_as_translate_term or uncertain_low_conf - never a low-value label on a
hunch.

Reading the fields:
- builder_proposed_vi and builder_target_variants are the SYSTEM'S OWN EARLIER NOTES, NOT
  gold/reference translations; they may be wrong. Use them only as a hint. If the evidence
  shows the proposed translation is context-dependent or incorrect, that itself is a signal
  (often polysemy_or_context_dependent).
- signals (occurrences_total, chapter_spread, has_conflict, do_not_translate,
  n_target_variants, surface_flags, overmerge_suspected) are mechanical HINTS, not verdicts.
  Many conflicting renderings + divergent evidence -> suspect polysemy; surface_flags
  "code_or_symbol_like" or do_not_translate true -> suspect preserve_token;
  overmerge_suspected true means the surface set may mix more than one concept - judge the
  head term, do not let merged fragments mislead you.

Choose exactly one audit_label per entry:
- keep_as_translate_term - a genuine domain term to translate consistently.
- preserve_token - keep verbatim in English / as a symbol (code identifiers, library
  functions, file formats, proper nouns, math symbols).
- generic_low_value - ordinary vocabulary, not worth controlling.
- descriptive_phrase - a compositional/explanatory phrase (several words describing
  something), not a single lexical term to control.
- polysemy_or_context_dependent - two or more valid renderings depending on context;
  forcing one canonical would mislead. Do NOT pick a translation; flag it.
- uncertain_low_conf - genuinely uncertain after weighing the evidence.

Also set for each entry:
- priority_tier: high | medium | low | review
- injection_action: translate | preserve | context_sensitive_translate | deprioritize | review_only
- confidence: high | medium | low
- reason: one short clause (<= 20 words) naming the deciding evidence or signal.

Default label -> tier -> action (you MAY deviate, but say why in reason):
keep_as_translate_term         -> high   / translate
preserve_token                 -> high   / preserve
polysemy_or_context_dependent  -> medium / context_sensitive_translate
generic_low_value              -> low    / deprioritize
descriptive_phrase             -> low    / deprioritize
uncertain_low_conf             -> review / review_only

IMPORTANT: polysemy_or_context_dependent terms are HIGH-VALUE - they are exactly where
consistent, context-aware translation matters most. Never rank them below generic_low_value
or treat them as noise; they must still reach the translator (with their variants), flagged
for context-sensitive handling.

Output: a single JSON array, EXACTLY one object per input entry, keyed by entry_id, in the
same order, no extra entries, no commentary:
[{"entry_id":"...","audit_label":"...","priority_tier":"...","injection_action":"...","confidence":"...","reason":"..."}]

Judge only from each card. Do not request more context. Output nothing except the JSON array."""
USER_PROMPT_PREFIX = "Audit the following candidate term cards. Return the JSON array as specified."

ALLOWED_AUDIT_LABELS = {
    "keep_as_translate_term",
    "preserve_token",
    "generic_low_value",
    "descriptive_phrase",
    "polysemy_or_context_dependent",
    "uncertain_low_conf",
}
ALLOWED_PRIORITY_TIERS = {"high", "medium", "low", "review"}
ALLOWED_INJECTION_ACTIONS = {
    "translate",
    "preserve",
    "context_sensitive_translate",
    "deprioritize",
    "review_only",
}
ALLOWED_CONFIDENCE = {"high", "medium", "low"}
TIER_RANK = {"high": 0, "medium": 1, "review": 2, "low": 3}
MATH_CODE = re.compile(r"[=${}\\]|\d")


@dataclass(frozen=True)
class AuditChunk:
    chunk_id: str
    index: int
    cards: list[dict[str, Any]]
    messages: list[dict[str, str]]
    prompt_tokens_est: int


def load_notebook_entries(notebook_path: Path) -> list[dict[str, Any]]:
    raw = json.loads(notebook_path.read_text(encoding="utf-8"))
    entries = raw.get("entries")
    if not isinstance(entries, list):
        raise ValueError(f"{notebook_path} does not contain an entries list")
    return [dict(entry) for entry in entries if isinstance(entry, dict)]


def build_term_cards(entries: list[dict[str, Any]], db_path: Path) -> list[dict[str, Any]]:
    conn = _connect_ro(db_path)
    try:
        cur = conn.cursor()
        return [build_card(cur, entry) for entry in entries]
    finally:
        conn.close()


def build_card(cur: sqlite3.Cursor, entry: dict[str, Any]) -> dict[str, Any]:
    source_term = str(entry.get("canonical_source_term") or "").strip()
    evidence_ids = _all_evidence_block_ids(entry)
    chapter_ids = {
        chapter_id
        for block_id in evidence_ids
        for _, chapter_id, _ in [_block(cur, block_id)]
        if chapter_id
    }
    has_conflict = bool(entry.get("conflict_ledger") or [])
    target_variants = _target_variants(entry)
    evidence_count = 2 if (has_conflict or len(target_variants) > 1) else 1
    prose_ids = [
        block_id
        for block_id in evidence_ids
        if _block(cur, block_id)[2] == "prose"
    ]

    evidence: list[str] = []
    missing_reason = None
    if prose_ids:
        for block_id in prose_ids:
            text, _, _ = _block(cur, block_id)
            if text.strip():
                evidence.append(_snippet(text, _source_surfaces(entry)))
            if len(evidence) >= evidence_count:
                break
    else:
        missing_reason = "no prose occurrence"

    flags: list[str] = []
    if re.search(r"[.\d]", source_term) or bool(entry.get("do_not_translate")):
        flags.append("code_or_symbol_like")

    card: dict[str, Any] = {
        "entry_id": str(entry.get("concept_key") or source_term).strip(),
        "source_term": source_term,
        "surface_variants": _source_surfaces(entry)[:8],
        "builder_proposed_vi": str(entry.get("canonical_target_vi") or "").strip(),
        "builder_target_variants": target_variants[:2],
        "_note": "builder_proposed_vi/variants are MODEL-GENERATED notes, NOT gold/reference",
        "signals": {
            "occurrences_total": int(entry.get("occurrences_total") or 0),
            "chapter_spread": len(chapter_ids),
            "is_multiword": " " in source_term.strip(),
            "do_not_translate": bool(entry.get("do_not_translate")),
            "has_conflict": has_conflict,
            "n_target_variants": len(target_variants),
            "surface_flags": flags,
            "overmerge_suspected": _overmerge_suspected(entry),
        },
        "evidence": evidence,
        "evidence_truncated": len(prose_ids) > evidence_count,
    }
    if missing_reason:
        card["evidence_missing_reason"] = missing_reason
    return card


def chunk_cards(
    cards: list[dict[str, Any]],
    chunk_size: int = 40,
    *,
    prompt_token_cap: int | None = None,
) -> list[AuditChunk]:
    if not 1 <= chunk_size <= 50:
        raise ValueError("--chunk-size must be between 1 and 50")
    if prompt_token_cap is None:
        subsets = [cards[start:start + chunk_size] for start in range(0, len(cards), chunk_size)]
    else:
        subsets = _chunk_by_prompt_cap(cards, chunk_size, prompt_token_cap)
    return [_make_chunk(index, subset) for index, subset in enumerate(subsets, start=1)]


def _chunk_by_prompt_cap(
    cards: list[dict[str, Any]],
    max_cards: int,
    prompt_token_cap: int,
) -> list[list[dict[str, Any]]]:
    subsets: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for card in cards:
        trial = [*current, card]
        trial_tokens = estimate_prompt_tokens(build_audit_messages(trial), None)
        if current and (len(trial) > max_cards or trial_tokens > prompt_token_cap):
            subsets.append(current)
            current = [card]
        else:
            current = trial
    if current:
        subsets.append(current)
    return subsets


def _make_chunk(index: int, cards: list[dict[str, Any]]) -> AuditChunk:
    chunk_id = f"chunk_{index:03d}"
    messages = build_audit_messages(cards)
    return AuditChunk(
        chunk_id=chunk_id,
        index=index,
        cards=cards,
        messages=messages,
        prompt_tokens_est=estimate_prompt_tokens(messages, None),
    )


def build_audit_messages(cards: list[dict[str, Any]]) -> list[dict[str, str]]:
    cards_json = json.dumps(cards, ensure_ascii=False, separators=(",", ":"))
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"{USER_PROMPT_PREFIX}\n\n{cards_json}"},
    ]


def prompt_text(messages: list[dict[str, str]]) -> str:
    parts = []
    for message in messages:
        role = str(message.get("role") or "message").upper()
        parts.append(f"## {role}\n{message.get('content') or ''}")
    return "\n\n".join(parts)


def validate_audit_results(
    results: list[dict[str, Any]],
    expected_entry_ids: Iterable[str],
) -> list[dict[str, Any]]:
    expected = [str(item) for item in expected_entry_ids]
    if len(results) != len(expected):
        raise ValueError(f"Expected {len(expected)} audit rows, got {len(results)}")
    validated: list[dict[str, Any]] = []
    for index, (row, expected_id) in enumerate(zip(results, expected), start=1):
        if not isinstance(row, dict):
            raise ValueError(f"Audit row {index} is not an object")
        entry_id = str(row.get("entry_id") or "")
        if entry_id != expected_id:
            raise ValueError(f"Audit row {index} entry_id {entry_id!r} != {expected_id!r}")
        label = _require_choice(row, "audit_label", ALLOWED_AUDIT_LABELS, index)
        tier = _require_choice(row, "priority_tier", ALLOWED_PRIORITY_TIERS, index)
        action = _require_choice(row, "injection_action", ALLOWED_INJECTION_ACTIONS, index)
        confidence = _require_choice(row, "confidence", ALLOWED_CONFIDENCE, index)
        reason = re.sub(r"\s+", " ", str(row.get("reason") or "").strip())
        if not reason:
            raise ValueError(f"Audit row {index} has empty reason")
        if len(reason.split()) > 20:
            raise ValueError(f"Audit row {index} reason is too long: {reason!r}")
        validated.append(
            {
                "entry_id": entry_id,
                "audit_label": label,
                "priority_tier": tier,
                "injection_action": action,
                "confidence": confidence,
                "reason": reason,
            }
        )
    return validated


def apply_audit_to_notebook(
    notebook: dict[str, Any],
    audit_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    entries = [dict(entry) for entry in notebook.get("entries", []) if isinstance(entry, dict)]
    by_id = {str(row["entry_id"]): row for row in audit_rows}
    audited: list[dict[str, Any]] = []
    for entry in entries:
        entry_id = str(entry.get("concept_key") or entry.get("canonical_source_term") or "")
        row = by_id.get(entry_id)
        updated = dict(entry)
        if row is not None:
            updated["audit"] = dict(row)
            if row["audit_label"] == "preserve_token" or row["injection_action"] == "preserve":
                updated["do_not_translate"] = True
        audited.append(updated)
    result = dict(notebook)
    result["entries"] = audited
    result["audit_trail"] = audit_rows
    result["audit_prompt_version"] = PROMPT_VERSION
    return result


def simulate_injection_order(
    entries: list[dict[str, Any]],
    *,
    min_injection_occurrences: int = 0,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for entry in entries:
        occurrences = int(entry.get("occurrences_total") or 0)
        if occurrences < min_injection_occurrences:
            continue
        audit = entry.get("audit") or {}
        tier = str(audit.get("priority_tier") or "high")
        action = str(audit.get("injection_action") or "translate")
        candidates.append(
            {
                "entry_id": str(entry.get("concept_key") or ""),
                "source_term": str(entry.get("canonical_source_term") or ""),
                "canonical_target_vi": str(entry.get("canonical_target_vi") or ""),
                "occurrences_total": occurrences,
                "priority_tier": tier,
                "injection_action": action,
                "do_not_translate": bool(entry.get("do_not_translate")),
                "sort_key": [
                    TIER_RANK.get(tier, TIER_RANK["review"]),
                    -occurrences,
                    str(entry.get("canonical_source_term") or "").casefold(),
                    str(entry.get("concept_key") or ""),
                ],
            }
        )
    candidates.sort(key=lambda item: tuple(item["sort_key"]))
    return candidates


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)


def _block(cur: sqlite3.Cursor, block_id: str) -> tuple[str, str, str]:
    row = cur.execute(
        "SELECT text, chapter_id, block_type FROM blocks WHERE block_id=?",
        (block_id,),
    ).fetchone()
    if row is None:
        return "", "", ""
    return str(row[0] or ""), str(row[1] or ""), str(row[2] or "")


def _snippet(text: str, surfaces: list[str], win: int = 45) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    low = compact.casefold()
    pos = -1
    for surface in surfaces:
        pos = low.find(surface.casefold())
        if pos >= 0:
            break
    words = compact.split()
    if pos < 0:
        return " ".join(words[:win]) + (" ..." if len(words) > win else "")
    before = len(compact[:pos].split())
    start = max(0, before - win // 2)
    end = min(len(words), start + win)
    return ("... " if start > 0 else "") + " ".join(words[start:end]) + (" ..." if end < len(words) else "")


def _all_evidence_block_ids(entry: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for conflict in entry.get("conflict_ledger") or []:
        if isinstance(conflict, dict):
            ids.extend(str(item) for item in conflict.get("evidence_block_ids") or [])
    for variant in sorted(
        [item for item in entry.get("source_variants") or [] if isinstance(item, dict)],
        key=lambda item: -int(item.get("occurrence_count") or 0),
    ):
        ids.extend(str(item) for item in variant.get("evidence_block_ids") or [])
    for decision in entry.get("decision_log") or []:
        if isinstance(decision, dict):
            ids.extend(str(item) for item in decision.get("evidence_block_ids") or [])
    return _stable_unique(ids)


def _source_surfaces(entry: dict[str, Any]) -> list[str]:
    surfaces = [
        str(variant.get("surface") or "").strip()
        for variant in entry.get("source_variants") or []
        if isinstance(variant, dict)
    ]
    canonical = str(entry.get("canonical_source_term") or "").strip()
    if canonical:
        surfaces.append(canonical)
    return sorted(
        _stable_unique(surfaces),
        key=lambda value: (value.casefold() != canonical.casefold(), value.casefold(), value),
    )


def _target_variants(entry: dict[str, Any]) -> list[str]:
    seen = {str(entry.get("canonical_target_vi") or "").casefold()}
    values: list[str] = []
    for variant in entry.get("target_variants") or []:
        if not isinstance(variant, dict):
            continue
        text = str(variant.get("text") or "").strip()
        marker = text.casefold()
        if not text or marker in seen:
            continue
        seen.add(marker)
        values.append(text)
    return values


def _overmerge_suspected(entry: dict[str, Any]) -> bool:
    canonical = str(entry.get("canonical_source_term") or "").strip()
    if not re.fullmatch(r"[A-Za-z][A-Za-z .\-]*", canonical):
        return False
    canonical_low = canonical.casefold()
    for surface in _source_surfaces(entry):
        surface_low = surface.casefold()
        if "=" in surface or "$" in surface:
            return True
        if MATH_CODE.search(surface) and canonical_low not in surface_low:
            return True
    return False


def _stable_unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    results: list[str] = []
    for value in values:
        clean = re.sub(r"\s+", " ", str(value or "").strip())
        key = clean.casefold()
        if not clean or key in seen:
            continue
        seen.add(key)
        results.append(clean)
    return results


def _require_choice(row: dict[str, Any], key: str, allowed: set[str], index: int) -> str:
    value = str(row.get(key) or "")
    if value not in allowed:
        raise ValueError(f"Audit row {index} has invalid {key}: {value!r}")
    return value
