from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from pipeline.agents.llm_client import estimate_prompt_tokens


PROMPT_VERSION = "d2l_decollision_v1"
SYSTEM_PROMPT = """You resolve naming COLLISIONS in an English-to-Vietnamese translation memory for the
deep-learning textbook "Dive into Deep Learning" (D2L). Upstream code has detected GROUPS:
each group is a set of DISTINCT English source terms that were assigned the SAME Vietnamese
canonical translation. For each member you decide whether that shared translation is correct,
or whether the terms are actually different concepts that must get distinct translations, or
whether a term is context-dependent. You do NOT translate from scratch and you do NOT invent
new Vietnamese wordings - you only choose among the candidates you are given, or flag.

Hard rules:
- Choose a canonical ONLY from the "candidates" list provided for that term (use its "text").
  If none fits, do NOT invent one - use mark_polysemy or uncertain.
- For resolve_distinct, the chosen canonical MUST differ from shared_canonical, AND must differ
  from any sibling you leave at keep_shared - otherwise the collision is NOT removed.
- You are given no reference or gold translation; do not assume one exists. Judge from the
  evidence sentences and your own domain knowledge.
- Within one group, two members you both resolve as distinct must NOT end up with the same
  canonical.
- Never drop or delete a term; you only relabel or re-pick its canonical.

Recall-safety: a wrong forced translation is worse than an honest "context-dependent" flag.
When evidence is thin or the term genuinely has several valid renderings, prefer
mark_polysemy. Assign a distinct canonical only when the evidence clearly shows the terms are
different concepts.

Reading each member:
- source_term: the English term.
- shared_canonical: the Vietnamese translation currently shared with the other members.
- candidates: the ONLY Vietnamese wordings you may choose from. Each has a "text" plus a
  mechanical provenance ("source"/"type"): a candidate from "conflict_ledger" with type
  "bad_existing_target" or "canonical_target_change" is the upstream extractor's OWN flag that
  the shared name is wrong for this term - weigh it as a strong hint, but still confirm from the
  evidence. A "target_variant" candidate is merely another rendering seen for this term.
- evidence: 1-2 source sentences showing how the term is used (use these to tell concepts apart).
- signals: occurrences and an optional upstream note that the translation was flagged
  inconsistent - a hint, not a verdict.

Choose exactly one decision per member:
- keep_shared: the shared_canonical is correct for this term. (If ALL members keep_shared, the
  group was a benign true-synonym group.)
- resolve_distinct: pick from candidates a canonical that differs from the colliding siblings,
  because this is a distinct concept.
- mark_polysemy: the term has two or more valid renderings depending on context; do not force
  one (set chosen_canonical to null).
- uncertain: genuinely unsure after weighing the evidence (set chosen_canonical to null).

Also set:
- chosen_canonical: keep_shared -> the shared_canonical; resolve_distinct -> the "text" of one
  candidate (must differ from shared_canonical); mark_polysemy / uncertain -> null.
- confidence: high | medium | low
- reason: one short clause (<= 20 words) naming the deciding evidence.

Output: a single JSON array, EXACTLY one object per input member, keyed by entry_id, in the
same order, no commentary:
[{"entry_id":"...","decision":"...","chosen_canonical":"... or null","confidence":"...","reason":"..."}]

Judge only from what you are given. Output nothing except the JSON array."""
USER_PROMPT_PREFIX = "Resolve the following collision groups. Return the JSON array as specified."

KEEP_LABELS = {
    "keep_as_translate_term",
    "preserve_token",
    "polysemy_or_context_dependent",
    "uncertain_low_conf",
}
ALLOWED_DECISIONS = {"keep_shared", "resolve_distinct", "mark_polysemy", "uncertain"}
ALLOWED_CONFIDENCE = {"high", "medium", "low"}
CANDIDATE_SOURCE_PRIORITY = {"conflict_ledger": 0, "target_variant": 1}
LEDGER_TYPE_PRIORITY = {
    "bad_existing_target": 0,
    "canonical_target_change": 1,
    "polysemy_suspected": 2,
    "uncertain": 3,
}


@dataclass(frozen=True)
class DecollisionChunk:
    chunk_id: str
    index: int
    groups: list[dict[str, Any]]
    members: list[dict[str, Any]]
    messages: list[dict[str, str]]
    prompt_tokens_est: int


def load_notebook(notebook_path: Path) -> dict[str, Any]:
    raw = json.loads(notebook_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("entries"), list):
        raise ValueError(f"{notebook_path} must contain an object with entries[]")
    return raw


def build_collision_groups(notebook: dict[str, Any], db_path: Path) -> list[dict[str, Any]]:
    entries = [entry for entry in notebook.get("entries") or [] if isinstance(entry, dict)]
    buckets: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        if _audit_label(entry) not in KEEP_LABELS:
            continue
        canonical = _clean_text(entry.get("canonical_target_vi"))
        if not canonical:
            continue
        buckets.setdefault(normalize_target_key(canonical), []).append(entry)

    conn = _connect_ro(db_path)
    try:
        cur = conn.cursor()
        groups: list[dict[str, Any]] = []
        for key, members in sorted(buckets.items(), key=lambda item: item[0]):
            source_keys = {_entry_id(member) for member in members}
            if len(source_keys) < 2:
                continue
            shared = _clean_text(members[0].get("canonical_target_vi"))
            groups.append(
                {
                    "group_id": f"collision_{len(groups) + 1:03d}",
                    "shared_canonical": shared,
                    "normalized_shared_canonical": key,
                    "members": [_build_member_card(cur, member, shared) for member in members],
                }
            )
        return groups
    finally:
        conn.close()


def chunk_groups(
    groups: list[dict[str, Any]],
    *,
    max_groups: int = 8,
    prompt_token_cap: int | None = None,
) -> list[DecollisionChunk]:
    if max_groups <= 0:
        raise ValueError("max_groups must be positive")
    subsets: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for group in groups:
        trial = [*current, group]
        too_many = len(trial) > max_groups
        too_large = (
            prompt_token_cap is not None
            and estimate_prompt_tokens(build_decollision_messages(trial), None) > prompt_token_cap
        )
        if current and (too_many or too_large):
            subsets.append(current)
            current = [group]
        else:
            current = trial
    if current:
        subsets.append(current)
    return [_make_chunk(index, subset) for index, subset in enumerate(subsets, start=1)]


def build_decollision_messages(groups: list[dict[str, Any]]) -> list[dict[str, str]]:
    payload = {"groups": groups}
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"{USER_PROMPT_PREFIX}\n{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}",
        },
    ]


def prompt_text(messages: list[dict[str, str]]) -> str:
    return "\n\n".join(
        f"## {str(message.get('role') or 'message').upper()}\n{message.get('content') or ''}"
        for message in messages
    )


def validate_decollision_results(
    results: list[dict[str, Any]],
    groups: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    expected_members = _flatten_members(groups)
    if len(results) != len(expected_members):
        raise ValueError(f"Expected {len(expected_members)} decollision rows, got {len(results)}")

    rows: list[dict[str, Any]] = []
    for index, (row, expected) in enumerate(zip(results, expected_members), start=1):
        if not isinstance(row, dict):
            raise ValueError(f"Decollision row {index} is not an object")
        entry_id = str(row.get("entry_id") or "")
        if entry_id != expected["entry_id"]:
            raise ValueError(f"Decollision row {index} entry_id {entry_id!r} != {expected['entry_id']!r}")
        decision = str(row.get("decision") or "")
        if decision not in ALLOWED_DECISIONS:
            raise ValueError(f"Decollision row {index} invalid decision: {decision!r}")
        confidence = str(row.get("confidence") or "")
        if confidence not in ALLOWED_CONFIDENCE:
            raise ValueError(f"Decollision row {index} invalid confidence: {confidence!r}")
        reason = re.sub(r"\s+", " ", str(row.get("reason") or "").strip())
        if not reason:
            raise ValueError(f"Decollision row {index} has empty reason")
        if len(reason.split()) > 20:
            raise ValueError(f"Decollision row {index} reason is too long: {reason!r}")
        chosen_raw = row.get("chosen_canonical")
        chosen = _clean_text(chosen_raw) if chosen_raw is not None else None
        shared = expected["shared_canonical"]
        candidate_texts = set(expected["candidate_texts"])
        if decision == "keep_shared":
            if chosen != shared:
                raise ValueError(f"Decollision row {index} keep_shared must choose shared_canonical")
        elif decision == "resolve_distinct":
            if not chosen:
                raise ValueError(f"Decollision row {index} resolve_distinct needs chosen_canonical")
            if normalize_target_key(chosen) == normalize_target_key(shared):
                raise ValueError(f"Decollision row {index} resolve_distinct chose shared canonical")
            if chosen not in candidate_texts:
                raise ValueError(f"Decollision row {index} chosen canonical is not in candidates: {chosen!r}")
        else:
            if chosen is not None:
                raise ValueError(f"Decollision row {index} {decision} must use chosen_canonical=null")
        rows.append(
            {
                "group_id": expected["group_id"],
                "entry_id": entry_id,
                "decision": decision,
                "chosen_canonical": chosen,
                "confidence": confidence,
                "reason": reason,
                "shared_canonical": shared,
                "candidate_texts": sorted(candidate_texts, key=str.casefold),
            }
        )

    rows_by_group: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        rows_by_group.setdefault(row["group_id"], []).append(row)
    for group_id, group_rows in rows_by_group.items():
        keep_shared_canonicals = {
            normalize_target_key(row["chosen_canonical"] or "")
            for row in group_rows
            if row["decision"] == "keep_shared"
        }
        resolved: set[str] = set()
        for row in group_rows:
            if row["decision"] != "resolve_distinct":
                continue
            normalized = normalize_target_key(row["chosen_canonical"] or "")
            if normalized in keep_shared_canonicals:
                raise ValueError(
                    f"Group {group_id} resolve_distinct chose canonical kept by a sibling"
                )
            if normalized in resolved:
                raise ValueError(f"Group {group_id} has duplicate resolved canonical")
            resolved.add(normalized)
    return rows


def apply_decollision_to_notebook(
    notebook: dict[str, Any],
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    by_id = {row["entry_id"]: row for row in rows}
    entries: list[dict[str, Any]] = []
    for entry in notebook.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        updated = dict(entry)
        entry_id = _entry_id(updated)
        row = by_id.get(entry_id)
        if row is not None:
            decision = row["decision"]
            updated["decollision"] = {
                "prompt_version": PROMPT_VERSION,
                "group_id": row["group_id"],
                "decision": decision,
                "previous_canonical_target_vi": str(entry.get("canonical_target_vi") or ""),
                "chosen_canonical": row["chosen_canonical"],
                "confidence": row["confidence"],
                "reason": row["reason"],
                "candidate_texts": row["candidate_texts"],
            }
            if decision == "resolve_distinct":
                updated["canonical_target_vi"] = row["chosen_canonical"]
            elif decision == "mark_polysemy":
                audit = dict(updated.get("audit") or {})
                audit.update(
                    {
                        "audit_label": "polysemy_or_context_dependent",
                        "priority_tier": "medium",
                        "injection_action": "context_sensitive_translate",
                    }
                )
                updated["audit"] = audit
                updated["inject_as_hard_canonical"] = False
                updated["canonical_unresolved"] = str(entry.get("canonical_target_vi") or "")
            elif decision == "uncertain":
                audit = dict(updated.get("audit") or {})
                audit.update(
                    {
                        "audit_label": "uncertain_low_conf",
                        "priority_tier": "review",
                        "injection_action": "review_only",
                    }
                )
                updated["audit"] = audit
                updated["inject_as_hard_canonical"] = False
                updated["canonical_unresolved"] = str(entry.get("canonical_target_vi") or "")
        entries.append(updated)
    result = dict(notebook)
    result["entries"] = entries
    result["decollision_prompt_version"] = PROMPT_VERSION
    result["decollision_trail"] = rows
    return result


def normalize_target_key(value: str) -> str:
    value = unicodedata.normalize("NFC", str(value or ""))
    value = re.sub(r"\s+", " ", value.strip())
    return value.casefold()


def _make_chunk(index: int, groups: list[dict[str, Any]]) -> DecollisionChunk:
    chunk_id = f"decollision_{index:03d}"
    messages = build_decollision_messages(groups)
    members = _flatten_members(groups)
    return DecollisionChunk(
        chunk_id=chunk_id,
        index=index,
        groups=groups,
        members=members,
        messages=messages,
        prompt_tokens_est=estimate_prompt_tokens(messages, None),
    )


def _build_member_card(cur: sqlite3.Cursor, entry: dict[str, Any], shared: str) -> dict[str, Any]:
    evidence_ids = _all_evidence_block_ids(entry)
    candidates = _candidate_objects(entry, shared)
    surfaces = _source_surfaces(entry)
    evidence: list[str] = []
    for block_id in evidence_ids:
        text, _, block_type = _block(cur, block_id)
        if block_type != "prose" or not text.strip():
            continue
        evidence.append(_snippet(text, surfaces))
        if len(evidence) >= 2:
            break
    return {
        "entry_id": _entry_id(entry),
        "source_term": _source_term(entry),
        "shared_canonical": shared,
        "candidates": candidates[:6],
        "evidence": evidence,
        "signals": {
            "occurrences_total": int(entry.get("occurrences_total") or 0),
            "builder_conflict_note": bool(entry.get("conflict_ledger") or []),
        },
    }


def _candidate_objects(entry: dict[str, Any], shared: str) -> list[dict[str, Any]]:
    rows: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    order = 0
    for conflict in entry.get("conflict_ledger") or []:
        if not isinstance(conflict, dict):
            continue
        text = _clean_text(conflict.get("proposed_target"))
        if not text:
            continue
        ctype = str(conflict.get("type") or "")
        rows.append(
            (
                (
                    CANDIDATE_SOURCE_PRIORITY["conflict_ledger"],
                    LEDGER_TYPE_PRIORITY.get(ctype, 99),
                    order,
                ),
                {"text": text, "source": "conflict_ledger", "type": ctype or None},
            )
        )
        order += 1
    for variant in entry.get("target_variants") or []:
        if not isinstance(variant, dict):
            continue
        text = _clean_text(variant.get("text"))
        if not text:
            continue
        rows.append(
            (
                (
                    CANDIDATE_SOURCE_PRIORITY["target_variant"],
                    0 if normalize_target_key(text) != normalize_target_key(shared) else 1,
                    order,
                ),
                {"text": text, "source": "target_variant", "type": None},
            )
        )
        order += 1
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _, candidate in sorted(rows, key=lambda item: item[0]):
        key = normalize_target_key(str(candidate["text"]))
        if key in seen:
            continue
        seen.add(key)
        results.append(candidate)
    return results


def _flatten_members(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for group in groups:
        for member in group.get("members") or []:
            rows.append(
                {
                    "group_id": str(group["group_id"]),
                    "entry_id": str(member["entry_id"]),
                    "shared_canonical": str(group["shared_canonical"]),
                    "candidate_texts": [
                        str(candidate.get("text") or "")
                        for candidate in member.get("candidates") or []
                        if str(candidate.get("text") or "").strip()
                    ],
                }
            )
    return rows


def _audit_label(entry: dict[str, Any]) -> str:
    return str(((entry.get("audit") or {}).get("audit_label") or "missing"))


def _entry_id(entry: dict[str, Any]) -> str:
    return str(entry.get("concept_key") or _source_term(entry)).strip()


def _source_term(entry: dict[str, Any]) -> str:
    canonical = str(entry.get("canonical_source_term") or "").strip()
    if canonical:
        return canonical
    surfaces = _source_surfaces(entry)
    return surfaces[0] if surfaces else _entry_id(entry)


def _source_surfaces(entry: dict[str, Any]) -> list[str]:
    surfaces = [
        str(variant.get("surface") or "").strip()
        for variant in entry.get("source_variants") or []
        if isinstance(variant, dict)
    ]
    canonical = str(entry.get("canonical_source_term") or "").strip()
    if canonical:
        surfaces.append(canonical)
    return _stable_unique(surfaces)


def _all_evidence_block_ids(entry: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for conflict in entry.get("conflict_ledger") or []:
        if isinstance(conflict, dict):
            ids.extend(str(item) for item in conflict.get("evidence_block_ids") or [])
    for variant in entry.get("source_variants") or []:
        if isinstance(variant, dict):
            ids.extend(str(item) for item in variant.get("evidence_block_ids") or [])
    for variant in entry.get("target_variants") or []:
        if isinstance(variant, dict) and variant.get("evidence_block_id"):
            ids.append(str(variant.get("evidence_block_id")))
    for decision in entry.get("decision_log") or []:
        if isinstance(decision, dict):
            ids.extend(str(item) for item in decision.get("evidence_block_ids") or [])
    return _stable_unique(ids)


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


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _stable_unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    results: list[str] = []
    for value in values:
        clean = _clean_text(value)
        key = clean.casefold()
        if not clean or key in seen:
            continue
        seen.add(key)
        results.append(clean)
    return results
