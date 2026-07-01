from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from dataclasses import asdict, dataclass, field
from itertools import combinations
from pathlib import Path
from typing import Any

from pipeline.eval.thesis_scoring import normalize_apostrophe
from pipeline.translate.profiles import (
    get_profile,
    injection_role_for_term,
    term_is_injection_eligible,
)
from pipeline.prepass.builder_v2_guards import apply_canonical_collision_soft_fallback_to_rows


@dataclass(frozen=True)
class Anchors:
    doc_id: str
    block_ids: list[str]
    term_block_ids: dict[str, list[str]]
    term_counts: dict[str, int]
    entity_block_ids: dict[str, list[str]]
    entity_counts: dict[str, int]
    has_dialogue: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def count_by_type(self) -> dict[str, int]:
        return {
            "terms": len(self.term_block_ids),
            "entities": len(self.entity_block_ids),
        }


@dataclass(frozen=True)
class DroppedItem:
    item_id: str
    item_type: str
    line: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass
class ContextPack:
    glossary_lines: list[str]
    preserve_lines: list[str]
    context_sensitive_lines: list[str]
    entity_lines: list[str]
    address_lines: list[str]
    token_estimate: int
    anchors: Anchors
    dropped_by_budget: list[DroppedItem] = field(default_factory=list)
    repair_queue: list[dict[str, Any]] = field(default_factory=list)
    low_context: bool = False
    warnings: list[str] = field(default_factory=list)

    def render_hard_constraints(self) -> str:
        sections: list[str] = []
        if self.glossary_lines or self.entity_lines:
            sections.append("MANDATORY TERMINOLOGY & NAMES")
            sections.append(
                "Use the exact Vietnamese target form whenever the concept/name appears; "
                "natural Vietnamese syntax around it is allowed."
            )
            sections.extend([f"- {line}" for line in self.glossary_lines])
            sections.extend([f"- {line}" for line in self.entity_lines])
        if self.preserve_lines:
            sections.append("PRESERVE / DO-NOT-TRANSLATE")
            sections.append("Keep these source tokens unchanged.")
            sections.extend([f"- {line}" for line in self.preserve_lines])
        if self.context_sensitive_lines:
            sections.append("CONTEXT-SENSITIVE TERMINOLOGY HINTS")
            sections.append(
                "These terms are context-dependent. Use the suggested rendering only when "
                "it fits this local context; do not force it as mandatory terminology."
            )
            sections.extend([f"- {line}" for line in self.context_sensitive_lines])
        if self.address_lines:
            sections.append("ADDRESS POLICY (xung ho)")
            sections.append(
                "When these characters address each other, use the following Vietnamese "
                "forms consistently in this window."
            )
            sections.extend([f"- {line}" for line in self.address_lines])
        return "\n".join(sections)

    def to_dict(self) -> dict[str, Any]:
        return {
            "glossary_lines": self.glossary_lines,
            "preserve_lines": self.preserve_lines,
            "context_sensitive_lines": self.context_sensitive_lines,
            "entity_lines": self.entity_lines,
            "address_lines": self.address_lines,
            "token_estimate": self.token_estimate,
            "anchors": self.anchors.to_dict(),
            "anchors_count": {
                **self.anchors.count_by_type,
                "address_policies": len(self.address_lines),
            },
            "dropped_by_budget": [item.to_dict() for item in self.dropped_by_budget],
            "repair_queue": self.repair_queue,
            "low_context": self.low_context,
            "warnings": self.warnings,
        }


@dataclass(frozen=True)
class _ContextItem:
    item_id: str
    item_type: str
    line: str
    token_estimate: int
    sort_key: tuple[Any, ...]
    required: bool = False


PACK_INJECTION_ACTIONS = {"translate", "preserve", "context_sensitive_translate"}
DROP_INJECTION_ACTIONS = {"deprioritize"}
REPAIR_INJECTION_ACTIONS = {"review_only"}
ACTION_SORT_RANK = {
    "translate": 0,
    "preserve": 1,
    "context_sensitive_translate": 2,
}


def load_notebook_terms(notebook_path: str | Path) -> list[dict[str, Any]]:
    """Load Builder-v2 audited notebook terms for Translator injection."""

    path = Path(notebook_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise ValueError(f"Notebook is missing entries list: {path}")
    return notebook_entries_to_term_rows(entries)


def notebook_entries_to_term_rows(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        audit = _entry_audit(entry)
        surfaces = _entry_source_surfaces(entry)
        source = _entry_source_term(entry, surfaces)
        target = _entry_target_term(entry)
        glossary_id = str(
            entry.get("entry_id")
            or entry.get("concept_key")
            or entry.get("glossary_id")
            or source
            or f"notebook_term_{index}"
        )
        rows.append(
            {
                "glossary_id": glossary_id,
                "source_term": source,
                "target_term": target,
                "term_type": str(entry.get("term_type") or ""),
                "do_not_translate": 1 if audit.get("injection_action") == "preserve" else 0,
                "occurrences_count": int(entry.get("occurrences_total") or entry.get("occurrences_count") or 0),
                "audit": audit,
                "source_surfaces": surfaces,
                "target_variants": _entry_target_variants(entry, target),
            }
        )
    guarded_rows, _ = apply_canonical_collision_soft_fallback_to_rows(rows)
    return guarded_rows


def pack_policy_counts(term_rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = {
        "notebook_total": len(term_rows),
        "hard_translate": 0,
        "preserve": 0,
        "context_sensitive": 0,
        "report_only": 0,
        "repair_queue": 0,
        "pack_total": 0,
    }
    for row in term_rows:
        action = _injection_action(row)
        if action == "translate":
            counts["hard_translate"] += 1
            counts["pack_total"] += 1
        elif action == "preserve":
            counts["preserve"] += 1
            counts["pack_total"] += 1
        elif action == "context_sensitive_translate":
            counts["context_sensitive"] += 1
            counts["pack_total"] += 1
        elif action in DROP_INJECTION_ACTIONS:
            counts["report_only"] += 1
        elif action in REPAIR_INJECTION_ACTIONS:
            counts["repair_queue"] += 1
    return counts


def pack_repair_queue(term_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    queue: list[dict[str, Any]] = []
    for row in term_rows:
        if _injection_action(row) not in REPAIR_INJECTION_ACTIONS:
            continue
        audit = row.get("audit") if isinstance(row.get("audit"), dict) else {}
        queue.append(
            {
                "glossary_id": str(row.get("glossary_id") or ""),
                "source_term": str(row.get("source_term") or ""),
                "target_term": str(row.get("target_term") or ""),
                "audit_label": str(audit.get("audit_label") or ""),
                "reason": str(audit.get("reason") or ""),
            }
        )
    return queue


def plan_anchors(
    conn: sqlite3.Connection,
    window_blocks: list[dict[str, Any]],
    *,
    profile_name: str = "literary_v1",
    term_rows: list[dict[str, Any]] | None = None,
) -> Anchors:
    """Scan a window for glossary/entity anchors without dumping the registry."""

    block_ids = [str(block.get("block_id") or "") for block in window_blocks]
    doc_id = _doc_id_for_blocks(window_blocks)
    term_block_ids: dict[str, list[str]] = {}
    term_counts: dict[str, int] = {}
    entity_block_ids: dict[str, list[str]] = {}
    entity_counts: dict[str, int] = {}
    has_dialogue = any(str(block.get("block_type") or "") == "dialogue" for block in window_blocks)

    if not doc_id:
        return Anchors(
            doc_id="",
            block_ids=block_ids,
            term_block_ids={},
            term_counts={},
            entity_block_ids={},
            entity_counts={},
            has_dialogue=has_dialogue,
        )

    profile = get_profile(profile_name)
    terms = _load_terms(conn, doc_id, profile_name=profile_name, term_rows=term_rows)
    entities = _load_entities(conn, doc_id) if profile.inject_entities else []

    for block in window_blocks:
        block_id = str(block.get("block_id") or "")
        text = str(block.get("clean_text") or block.get("source_text") or block.get("text") or "")
        for term in terms:
            count = _count_entity_matches(text, _term_source_surfaces(term))
            if count:
                glossary_id = str(term["glossary_id"])
                term_block_ids.setdefault(glossary_id, [])
                if block_id not in term_block_ids[glossary_id]:
                    term_block_ids[glossary_id].append(block_id)
                term_counts[glossary_id] = term_counts.get(glossary_id, 0) + count

        for entity in entities:
            surfaces = [str(entity["canonical_source"]), *_json_list(entity["aliases_source_json"])]
            count = _count_entity_matches(text, _dedupe_surfaces(surfaces))
            if count:
                entity_id = str(entity["entity_id"])
                entity_block_ids.setdefault(entity_id, [])
                if block_id not in entity_block_ids[entity_id]:
                    entity_block_ids[entity_id].append(block_id)
                entity_counts[entity_id] = entity_counts.get(entity_id, 0) + count

    return Anchors(
        doc_id=doc_id,
        block_ids=block_ids,
        term_block_ids=term_block_ids,
        term_counts=term_counts,
        entity_block_ids=entity_block_ids,
        entity_counts=entity_counts,
        has_dialogue=has_dialogue,
    )


def build_context_pack(
    conn: sqlite3.Connection,
    window: Any,
    anchors: Anchors,
    budget_tokens: int = 500,
    *,
    term_rows: list[dict[str, Any]] | None = None,
) -> ContextPack:
    pack, included = _build_context_pack_once(conn, window, anchors, budget_tokens, term_rows=term_rows)
    missing = _missing_coverage(anchors, included, pack.dropped_by_budget)
    if missing:
        pack, included = _build_context_pack_once(conn, window, anchors, budget_tokens, term_rows=term_rows)
        missing = _missing_coverage(anchors, included, pack.dropped_by_budget)
    if missing:
        pack.low_context = True
        pack.warnings.append(f"coverage_missing:{','.join(sorted(missing))}")
    return pack


def _build_context_pack_once(
    conn: sqlite3.Connection,
    window: Any,
    anchors: Anchors,
    budget_tokens: int,
    *,
    term_rows: list[dict[str, Any]] | None = None,
) -> tuple[ContextPack, set[str]]:
    glossary_items = _glossary_items(conn, anchors, term_rows=term_rows)
    entity_items = _entity_items(conn, anchors)
    address_items, warnings = _address_items(conn, window, anchors)

    kept: list[_ContextItem] = []
    dropped: list[DroppedItem] = []
    budget_used = 0

    for item in sorted(address_items, key=lambda value: value.sort_key):
        kept.append(item)
        budget_used += item.token_estimate

    for item in sorted(entity_items, key=lambda value: value.sort_key):
        if budget_used + item.token_estimate <= budget_tokens:
            kept.append(item)
            budget_used += item.token_estimate
        else:
            dropped.append(
                DroppedItem(item.item_id, item.item_type, item.line, "budget")
            )

    for item in sorted(glossary_items, key=lambda value: value.sort_key):
        if budget_used + item.token_estimate <= budget_tokens:
            kept.append(item)
            budget_used += item.token_estimate
        else:
            dropped.append(
                DroppedItem(item.item_id, item.item_type, item.line, "budget")
            )

    pack = ContextPack(
        glossary_lines=[item.line for item in kept if item.item_type == "term"],
        preserve_lines=[item.line for item in kept if item.item_type == "preserve"],
        context_sensitive_lines=[item.line for item in kept if item.item_type == "context_sensitive"],
        entity_lines=[item.line for item in kept if item.item_type == "entity"],
        address_lines=[item.line for item in kept if item.item_type == "address"],
        token_estimate=budget_used,
        anchors=anchors,
        dropped_by_budget=dropped,
        repair_queue=pack_repair_queue(term_rows or []),
        low_context=False,
        warnings=warnings,
    )
    included_ids = {item.item_id for item in kept}
    return pack, included_ids


def _glossary_items(
    conn: sqlite3.Connection,
    anchors: Anchors,
    *,
    term_rows: list[dict[str, Any]] | None = None,
) -> list[_ContextItem]:
    if not anchors.term_block_ids:
        return []
    if term_rows is None:
        placeholders = ",".join("?" * len(anchors.term_block_ids))
        rows = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT glossary_id, source_term, target_term, do_not_translate
                FROM glossary_entries
                WHERE glossary_id IN ({placeholders})
                """,
                list(anchors.term_block_ids),
            ).fetchall()
        ]
    else:
        by_id = {str(row.get("glossary_id") or ""): row for row in term_rows}
        rows = [by_id[term_id] for term_id in anchors.term_block_ids if term_id in by_id]
    items: list[_ContextItem] = []
    for row in rows:
        action = _injection_action(row)
        if action in {"deprioritize", "review_only"}:
            continue
        glossary_id = str(row["glossary_id"])
        source = str(row["source_term"] or "")
        target = str(row["target_term"] or "")
        line = _term_line(row, action)
        count = anchors.term_counts.get(glossary_id, 0)
        item_type = {
            "preserve": "preserve",
            "context_sensitive_translate": "context_sensitive",
        }.get(action, "term")
        items.append(
            _ContextItem(
                item_id=f"term:{glossary_id}",
                item_type=item_type,
                line=line,
                token_estimate=_estimate_tokens(line),
                sort_key=(_action_sort_rank(action), -count, source.casefold(), glossary_id),
            )
        )
    return items


def _entity_items(conn: sqlite3.Connection, anchors: Anchors) -> list[_ContextItem]:
    if not anchors.entity_block_ids:
        return []
    placeholders = ",".join("?" * len(anchors.entity_block_ids))
    rows = conn.execute(
        f"""
        SELECT entity_id, canonical_source, canonical_target, aliases_target_json
        FROM entities
        WHERE entity_id IN ({placeholders})
        """,
        list(anchors.entity_block_ids),
    ).fetchall()
    items: list[_ContextItem] = []
    for row in rows:
        entity_id = str(row["entity_id"])
        source = str(row["canonical_source"] or "")
        target = str(row["canonical_target"] or source)
        aliases = [str(item) for item in _json_list(row["aliases_target_json"]) if str(item)]
        alias_part = f" ({', '.join(aliases)})" if aliases else ""
        line = f"{source} -> {target}{alias_part}"
        items.append(
            _ContextItem(
                item_id=f"entity:{entity_id}",
                item_type="entity",
                line=line,
                token_estimate=_estimate_tokens(line),
                sort_key=(source.casefold(), entity_id),
            )
        )
    return items


def _address_items(
    conn: sqlite3.Connection,
    window: Any,
    anchors: Anchors,
) -> tuple[list[_ContextItem], list[str]]:
    entity_ids = sorted(anchors.entity_block_ids)
    if len(entity_ids) < 2:
        return [], []

    start_block_id = _first_block_id(window, anchors)
    start_order = _block_order(conn, start_block_id)
    if start_order is None:
        return [], [f"missing_order:{start_block_id}"]

    entities = _entity_name_map(conn, entity_ids)
    items: list[_ContextItem] = []
    warnings: list[str] = []

    for left, right in combinations(entity_ids, 2):
        rows = _active_relation_rows(conn, anchors.doc_id, left, right, start_order)
        if not rows:
            continue
        if len(rows) > 1:
            warnings.append(f"multiple_active_relations:{left}:{right}")
        row = rows[0]
        source_id = str(row["source_entity_id"])
        target_id = str(row["target_entity_id"])
        policy = _json_dict(row["address_policy_json"])
        source_name = entities.get(source_id, source_id)
        target_name = entities.get(target_id, target_id)
        state = str(row["state_label"] or "")
        line = (
            f"{source_name}->{target_name}: \"{policy.get('a_to_b', '')}\", "
            f"{target_name}->{source_name}: \"{policy.get('b_to_a', '')}\""
        )
        if state:
            line += f" ({state})"
        relation_id = str(row["relation_id"])
        items.append(
            _ContextItem(
                item_id=f"address:{relation_id}",
                item_type="address",
                line=line,
                token_estimate=_estimate_tokens(line),
                sort_key=(source_name.casefold(), target_name.casefold(), relation_id),
                required=True,
            )
        )
    return items, warnings


def _active_relation_rows(
    conn: sqlite3.Connection,
    doc_id: str,
    left: str,
    right: str,
    start_order: int,
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT r.*, from_block.order_index AS from_order, to_block.order_index AS to_order
        FROM entity_relations r
        LEFT JOIN blocks from_block ON from_block.block_id = r.valid_from_block_id
        LEFT JOIN blocks to_block ON to_block.block_id = r.valid_to_block_id
        WHERE r.doc_id = ?
          AND (
            (r.source_entity_id = ? AND r.target_entity_id = ?)
            OR (r.source_entity_id = ? AND r.target_entity_id = ?)
          )
          AND (from_block.order_index IS NULL OR from_block.order_index <= ?)
          AND (to_block.order_index IS NULL OR ? <= to_block.order_index)
        ORDER BY COALESCE(from_block.order_index, -1) DESC, r.relation_id
        """,
        (doc_id, left, right, right, left, start_order, start_order),
    ).fetchall()


def _missing_coverage(
    anchors: Anchors,
    included_ids: set[str],
    dropped_by_budget: list[DroppedItem],
) -> set[str]:
    dropped_ids = {item.item_id for item in dropped_by_budget}
    expected = {
        *(f"term:{term_id}" for term_id in anchors.term_block_ids),
        *(f"entity:{entity_id}" for entity_id in anchors.entity_block_ids),
    }
    return expected - included_ids - dropped_ids


def registry_injection_stats(
    conn: sqlite3.Connection,
    doc_id: str,
    *,
    profile_name: str = "literary_v1",
) -> dict[str, int]:
    profile = get_profile(profile_name)
    rows = conn.execute(
        """
        SELECT glossary_id, source_term, target_term, term_type, do_not_translate,
               occurrences_count
        FROM glossary_entries
        WHERE doc_id = ?
        ORDER BY LENGTH(source_term) DESC, source_term
        """,
        (doc_id,),
    ).fetchall()
    raw = 0
    eligible = 0
    preserve = 0
    hapax_dropped = 0
    for row in rows:
        raw += 1
        item = dict(row)
        role = injection_role_for_term(item)
        if role == "preserve":
            preserve += 1
        if role == "translate" and int(item.get("occurrences_count") or 0) < profile.min_injection_occurrences:
            hapax_dropped += 1
        if term_is_injection_eligible(item, profile):
            eligible += 1
    return {
        "raw_registry": raw,
        "translation_eligible": eligible,
        "preserve_count": preserve,
        "hapax_dropped": hapax_dropped,
    }


def _load_terms(
    conn: sqlite3.Connection,
    doc_id: str,
    *,
    profile_name: str = "literary_v1",
    term_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if term_rows is not None:
        return [
            row
            for row in term_rows
            if _injection_action(row) in PACK_INJECTION_ACTIONS
        ]

    profile = get_profile(profile_name)
    rows = conn.execute(
        """
        SELECT glossary_id, source_term, target_term, term_type, do_not_translate,
               occurrences_count
        FROM glossary_entries
        WHERE doc_id = ?
        ORDER BY LENGTH(source_term) DESC, source_term
        """,
        (doc_id,),
    ).fetchall()
    return [
        dict(row)
        for row in rows
        if term_is_injection_eligible(dict(row), profile)
    ]


def _entry_audit(entry: dict[str, Any]) -> dict[str, Any]:
    audit = entry.get("audit")
    if isinstance(audit, dict):
        return dict(audit)
    return {
        key: entry.get(key)
        for key in ("audit_label", "priority_tier", "injection_action", "confidence", "reason")
        if entry.get(key) is not None
    }


def _entry_source_term(entry: dict[str, Any], surfaces: list[str]) -> str:
    for key in ("source_term", "canonical_source", "canonical_source_term", "canonical"):
        value = str(entry.get(key) or "").strip()
        if value:
            return value
    return surfaces[0] if surfaces else ""


def _entry_source_surfaces(entry: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("source_term", "canonical_source", "canonical_source_term", "canonical"):
        value = str(entry.get(key) or "").strip()
        if value:
            values.append(value)
    raw_variants = entry.get("source_variants")
    if isinstance(raw_variants, list):
        for item in raw_variants:
            if isinstance(item, dict):
                values.append(str(item.get("surface") or item.get("source") or item.get("value") or ""))
            else:
                values.append(str(item or ""))
    return _dedupe_surfaces(values)


def _entry_target_term(entry: dict[str, Any]) -> str:
    for key in ("canonical_target_vi", "canonical_target", "target_term"):
        value = str(entry.get(key) or "").strip()
        if value:
            return value
    variants = _entry_target_variants(entry, "")
    return variants[0] if variants else ""


def _entry_target_variants(entry: dict[str, Any], target: str) -> list[str]:
    values: list[str] = [target] if target else []
    raw_variants = entry.get("target_variants")
    if isinstance(raw_variants, list):
        for item in raw_variants:
            if isinstance(item, dict):
                values.append(str(item.get("surface") or item.get("target") or item.get("value") or ""))
            else:
                values.append(str(item or ""))
    return _dedupe_surfaces(values)


def _term_source_surfaces(term: dict[str, Any]) -> list[str]:
    surfaces = term.get("source_surfaces")
    if isinstance(surfaces, list) and surfaces:
        return _dedupe_surfaces([str(item) for item in surfaces])
    return _dedupe_surfaces([str(term.get("source_term") or "")])


def _injection_action(row: dict[str, Any]) -> str:
    audit = row.get("audit")
    if isinstance(audit, dict):
        action = str(audit.get("injection_action") or "").strip()
        if action:
            return action
    return injection_role_for_term(row)


def _term_line(row: dict[str, Any], action: str) -> str:
    source = str(row.get("source_term") or "")
    target = str(row.get("target_term") or source)
    if action == "preserve":
        return f"{source} (keep unchanged)"
    if action == "context_sensitive_translate":
        variants = [item for item in _jsonish_list(row.get("target_variants")) if item and item != target]
        variant_note = f"; variants: {', '.join(variants[:2])}" if variants else ""
        return f"{source} -> {target} (context-sensitive{variant_note}; do not force)"
    return f"{source} -> {target}"


def _action_sort_rank(action: str) -> int:
    return ACTION_SORT_RANK.get(action, 99)


def _jsonish_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    return [str(item) for item in _json_list(value) if str(item)]


def _load_entities(conn: sqlite3.Connection, doc_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT entity_id, canonical_source, aliases_source_json
        FROM entities
        WHERE doc_id = ?
        ORDER BY LENGTH(canonical_source) DESC, canonical_source
        """,
        (doc_id,),
    ).fetchall()


def _entity_name_map(conn: sqlite3.Connection, entity_ids: list[str]) -> dict[str, str]:
    if not entity_ids:
        return {}
    placeholders = ",".join("?" * len(entity_ids))
    rows = conn.execute(
        f"""
        SELECT entity_id, canonical_source
        FROM entities
        WHERE entity_id IN ({placeholders})
        """,
        entity_ids,
    ).fetchall()
    return {
        str(row["entity_id"]): str(row["canonical_source"] or row["entity_id"])
        for row in rows
    }


def _doc_id_for_blocks(window_blocks: list[dict[str, Any]]) -> str:
    for block in window_blocks:
        doc_id = str(block.get("doc_id") or "")
        if doc_id:
            return doc_id
    return ""


def _first_block_id(window: Any, anchors: Anchors) -> str:
    block_ids = list(getattr(window, "block_ids", None) or anchors.block_ids)
    return str(block_ids[0]) if block_ids else ""


def _block_order(conn: sqlite3.Connection, block_id: str) -> int | None:
    row = conn.execute(
        "SELECT order_index FROM blocks WHERE block_id = ?",
        (block_id,),
    ).fetchone()
    if row is None:
        return None
    return int(row["order_index"])


def _count_matches(text: str, needle: str) -> int:
    normalized_text = _normalize_for_match(text)
    normalized_needle = _normalize_for_match(needle.strip())
    if not normalized_needle:
        return 0
    pattern = rf"(?<!\w){re.escape(normalized_needle)}(?!\w)"
    return len(re.findall(pattern, normalized_text, flags=re.UNICODE))


def _count_entity_matches(text: str, surfaces: list[str]) -> int:
    normalized_text = _normalize_for_match(text)
    candidates: list[tuple[int, int]] = []
    for surface in surfaces:
        normalized_surface = _normalize_for_match(surface)
        if not normalized_surface:
            continue
        pattern = rf"(?<!\w){re.escape(normalized_surface)}(?!\w)"
        candidates.extend(
            (match.start(), match.end())
            for match in re.finditer(pattern, normalized_text, flags=re.UNICODE)
        )

    selected: list[tuple[int, int]] = []
    occupied: set[int] = set()
    for start, end in sorted(candidates, key=lambda item: (item[0], -(item[1] - item[0]))):
        span_positions = set(range(start, end))
        if occupied & span_positions:
            continue
        selected.append((start, end))
        occupied.update(span_positions)
    return len(selected)


def _normalize_for_match(text: str) -> str:
    return unicodedata.normalize("NFC", normalize_apostrophe(str(text))).casefold()


def _dedupe_surfaces(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        surface = unicodedata.normalize("NFC", str(value).strip())
        key = _normalize_for_match(surface)
        if not surface or key in seen:
            continue
        seen.add(key)
        result.append(surface)
    return sorted(result, key=lambda item: (-len(item), item.casefold()))


def _json_list(value: Any) -> list[Any]:
    try:
        parsed = json.loads(str(value or "[]"))
    except Exception:
        return []
    return parsed if isinstance(parsed, list) else []


def _json_dict(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)
