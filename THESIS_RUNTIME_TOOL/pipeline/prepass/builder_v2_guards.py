from __future__ import annotations

import copy
import re
import unicodedata
from collections import defaultdict
from typing import Any

from pipeline.prepass.concept_key import concept_key


PACK_SOFT_FALLBACK_VERSION = "c5_canonical_collision_soft_v1"
SURFACE_OWNERSHIP_VERSION = "c5_surface_ownership_v1"


def apply_surface_ownership_guard(notebook: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Detach source surfaces that are owned by another headword entry.

    This is intentionally mechanical: ownership is decided by concept_key over
    source surfaces and canonical source headwords. Zero-occurrence surfaces are
    quarantined instead of silently deleted.
    """

    guarded = copy.deepcopy(notebook)
    entries = guarded.get("entries")
    if not isinstance(entries, list):
        raise ValueError("Notebook is missing entries list.")

    owner_by_key: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        entry_id = _entry_id(entry)
        headword_key = concept_key(str(entry.get("canonical_source_term") or entry.get("source_term") or entry_id))
        if headword_key and headword_key not in owner_by_key:
            owner_by_key[headword_key] = entry_id

    quarantine: list[dict[str, Any]] = []
    detached: list[dict[str, Any]] = []

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        entry_id = _entry_id(entry)
        kept_variants: list[dict[str, Any]] = []
        for variant in entry.get("source_variants") or []:
            if not isinstance(variant, dict):
                continue
            surface = str(variant.get("surface") or "").strip()
            if not surface:
                continue
            occurrence_count = int(variant.get("occurrence_count") or 0)
            surface_key = concept_key(surface)
            owner_id = owner_by_key.get(surface_key)
            if occurrence_count == 0:
                quarantine.append(
                    {
                        "entry_id": entry_id,
                        "surface": surface,
                        "surface_key": surface_key,
                        "reason": "zero_occurrence_surface",
                        "variant": copy.deepcopy(variant),
                    }
                )
                continue
            if owner_id and owner_id != entry_id:
                detached.append(
                    {
                        "entry_id": entry_id,
                        "owner_entry_id": owner_id,
                        "surface": surface,
                        "surface_key": surface_key,
                        "reason": "surface_owned_by_headword_entry",
                        "variant": copy.deepcopy(variant),
                    }
                )
                continue
            kept_variants.append(variant)
        entry["source_variants"] = kept_variants
        entry["occurrences_total"] = sum(int(item.get("occurrence_count") or 0) for item in kept_variants)

    report = {
        "version": SURFACE_OWNERSHIP_VERSION,
        "entries": len(entries),
        "detached_count": len(detached),
        "quarantined_count": len(quarantine),
        "detached_surfaces": detached,
        "surface_quarantine": quarantine,
    }
    guarded["surface_ownership_guard"] = {
        "version": SURFACE_OWNERSHIP_VERSION,
        "detached_count": len(detached),
        "quarantined_count": len(quarantine),
    }
    return guarded, report


def apply_canonical_collision_soft_fallback_to_rows(
    term_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Downgrade unresolved hard canonical collisions to context-sensitive hints."""

    rows = [copy.deepcopy(row) for row in term_rows]
    hard_by_target: dict[str, list[int]] = defaultdict(list)
    for index, row in enumerate(rows):
        if _injection_action(row) != "translate":
            continue
        target_key = _target_key(str(row.get("target_term") or ""))
        if target_key:
            hard_by_target[target_key].append(index)

    softened: list[dict[str, Any]] = []
    kept_mechanical: list[dict[str, Any]] = []
    for target_key, indexes in hard_by_target.items():
        if len(indexes) < 2:
            continue
        group = [rows[index] for index in indexes]
        if _is_mechanical_source_group(group):
            kept_mechanical.append(_group_record(target_key, group, "mechanical_source_group"))
            continue
        record = _group_record(target_key, group, "unresolved_canonical_collision")
        softened.append(record)
        for row in group:
            audit = dict(row.get("audit") or {})
            audit.setdefault("audit_label", "keep_as_translate_term")
            audit["injection_action"] = "context_sensitive_translate"
            audit["priority_tier"] = "medium"
            audit["collision_soft_fallback"] = {
                "version": PACK_SOFT_FALLBACK_VERSION,
                "target_key": target_key,
                "reason": "unresolved shared canonical among distinct source terms",
            }
            row["audit"] = audit

    report = {
        "version": PACK_SOFT_FALLBACK_VERSION,
        "softened_groups": softened,
        "softened_entries": sum(len(group["members"]) for group in softened),
        "kept_mechanical_groups": kept_mechanical,
    }
    return rows, report


def _entry_id(entry: dict[str, Any]) -> str:
    return str(
        entry.get("entry_id")
        or entry.get("concept_key")
        or entry.get("canonical_source_term")
        or entry.get("source_term")
        or ""
    )


def _injection_action(row: dict[str, Any]) -> str:
    audit = row.get("audit")
    if isinstance(audit, dict):
        action = str(audit.get("injection_action") or "").strip()
        if action:
            return action
    return str(row.get("injection_action") or "").strip() or "translate"


def _is_mechanical_source_group(rows: list[dict[str, Any]]) -> bool:
    sources = [str(row.get("source_term") or "").strip() for row in rows if str(row.get("source_term") or "").strip()]
    if len(sources) < 2:
        return False
    if len({concept_key(source) for source in sources}) == 1:
        return True
    return len({_source_shape_key(source) for source in sources}) == 1


def _source_shape_key(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value).casefold()
    return re.sub(r"[^0-9a-z]+", "", normalized)


def _target_key(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value).casefold().strip()
    return re.sub(r"\s+", " ", normalized)


def _group_record(target_key: str, rows: list[dict[str, Any]], reason: str) -> dict[str, Any]:
    return {
        "target_key": target_key,
        "reason": reason,
        "members": [
            {
                "glossary_id": str(row.get("glossary_id") or ""),
                "source_term": str(row.get("source_term") or ""),
                "target_term": str(row.get("target_term") or ""),
            }
            for row in rows
        ],
    }
