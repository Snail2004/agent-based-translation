from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any

from pipeline.eval.thesis_scoring import normalize_apostrophe
from pipeline.prepass.registry import PrepassRegistry


@dataclass(frozen=True)
class LiteraryBuilderContextItem:
    item_id: str
    item_type: str
    section: str
    line: str
    reason: str
    matched_by: list[str]
    token_estimate: int
    priority: tuple[Any, ...]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("priority", None)
        return payload


@dataclass
class LiteraryBuilderContextPack:
    chapter_id: str
    budget_tokens: int
    token_estimate: int
    first_person_detected: bool
    included: list[LiteraryBuilderContextItem] = field(default_factory=list)
    excluded: list[dict[str, Any]] = field(default_factory=list)
    dropped_by_budget: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def render_context(self) -> str:
        lines = [
            "REGISTRY_CONTEXT_POLICY",
            (
                "Filtered continuity pack for the literary World Builder. This is NOT "
                "a full registry dump; include only source-visible or continuity-critical "
                "items for the current chapter/window."
            ),
        ]
        if not self.included:
            lines.extend(["REGISTRY_CONTEXT", "(empty - no matched prior continuity item)"])
            return "\n".join(lines)

        section_titles = [
            ("MATCHED_GLOSSARY", "Matched glossary"),
            ("MATCHED_ENTITIES", "Matched entities"),
            ("ACTIVE_RELATIONS", "Active relations"),
            ("NARRATOR_CARD", "Narrator card"),
            ("RECENT_CARRYOVER", "Recent carryover"),
        ]
        for section, title in section_titles:
            section_items = [item for item in self.included if item.section == section]
            if not section_items:
                continue
            lines.append(title.upper())
            lines.extend(f"- {item.line}" for item in section_items)
        if self.dropped_by_budget:
            lines.append("DROPPED_BY_BUDGET")
            lines.extend(
                f"- {item['item_type']}:{item['item_id']} ({item['reason']})"
                for item in self.dropped_by_budget
            )
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "chapter_id": self.chapter_id,
            "budget_tokens": self.budget_tokens,
            "token_estimate": self.token_estimate,
            "first_person_detected": self.first_person_detected,
            "included": [item.to_dict() for item in self.included],
            "excluded": self.excluded,
            "dropped_by_budget": self.dropped_by_budget,
            "warnings": self.warnings,
            "counts": {
                "included": len(self.included),
                "excluded": len(self.excluded),
                "dropped_by_budget": len(self.dropped_by_budget),
            },
        }


def build_literary_builder_context_pack(
    chapter: dict[str, Any],
    registry: PrepassRegistry,
    *,
    budget_tokens: int = 600,
    recent_carryover_limit: int = 2,
) -> LiteraryBuilderContextPack:
    """Build a filtered continuity pack for literary World Builder calls.

    D2L extraction intentionally omits registry. Literary extraction keeps a
    bounded, source-matched continuity pack because entity aliases, narrator
    identity, and address relations can recur across chapters.
    """

    chapter_id = str(chapter.get("chapter_id") or "")
    text = _chapter_text(chapter)
    first_person = _has_first_person_narration(text)
    candidates: list[LiteraryBuilderContextItem] = []
    excluded: list[dict[str, Any]] = []

    matched_entities: set[str] = set()
    visible_entities: set[str] = set()
    for entity_id, entity in sorted(registry.entities.items()):
        surfaces = _entity_surfaces(entity)
        matches = _matched_surfaces(text, surfaces)
        is_narrator = entity_id == "ent_narrator"
        if matches:
            matched_entities.add(entity_id)
            visible_entities.add(entity_id)
            candidates.append(_entity_item(entity_id, entity, matches, "matched_surface"))
        elif is_narrator and first_person:
            matched_entities.add(entity_id)
            visible_entities.add(entity_id)
            candidates.append(_narrator_item(entity_id, entity))
        else:
            excluded.append(
                _excluded(
                    entity_id,
                    "entity",
                    "not_in_current_text",
                    surfaces[:6],
                )
            )

    carryover_added = 0
    for entity_id, entity in _recent_entity_candidates(registry, matched_entities):
        if carryover_added >= recent_carryover_limit:
            break
        candidates.append(_carryover_entity_item(entity_id, entity))
        matched_entities.add(entity_id)
        carryover_added += 1

    for key, term in sorted(registry.glossary.items(), key=lambda item: str(item[0])):
        source = str(term.get("source_term") or "").strip()
        matches = _matched_surfaces(text, [source])
        if matches:
            candidates.append(_glossary_item(str(key), term, matches))
        else:
            excluded.append(_excluded(str(key), "glossary", "not_in_current_text", [source]))

    for key, relation in sorted(registry.relations.items(), key=lambda item: str(item[0])):
        left = str(relation.get("a") or "").strip()
        right = str(relation.get("b") or "").strip()
        endpoints = {left, right}
        if endpoints and endpoints.issubset(visible_entities):
            candidates.append(_relation_item(str(key), relation, "both_entities_in_pack"))
        else:
            excluded.append(
                _excluded(
                    str(key),
                    "relation",
                    "endpoint_not_in_pack",
                    [left, right],
                )
            )

    kept: list[LiteraryBuilderContextItem] = []
    dropped: list[dict[str, Any]] = []
    used = 0
    for item in sorted(candidates, key=lambda value: value.priority):
        if used + item.token_estimate <= budget_tokens:
            kept.append(item)
            used += item.token_estimate
        else:
            dropped.append(
                {
                    **item.to_dict(),
                    "reason": f"budget:{used}+{item.token_estimate}>{budget_tokens}",
                }
            )

    return LiteraryBuilderContextPack(
        chapter_id=chapter_id,
        budget_tokens=budget_tokens,
        token_estimate=used,
        first_person_detected=first_person,
        included=kept,
        excluded=excluded,
        dropped_by_budget=dropped,
    )


def _chapter_text(chapter: dict[str, Any]) -> str:
    parts: list[str] = []
    for block in chapter.get("blocks") or []:
        parts.append(str(block.get("clean_text") or block.get("source_text") or ""))
    return "\n".join(parts)


def _has_first_person_narration(text: str) -> bool:
    normalized = _normalize_for_match(text)
    return bool(re.search(r"(?<!\w)(i|me|my|mine|myself|we|us|our)(?!\w)", normalized))


def _entity_surfaces(entity: dict[str, Any]) -> list[str]:
    surfaces = [
        str(entity.get("canonical_source") or ""),
        *[str(value) for value in entity.get("aliases_source") or []],
    ]
    return _dedupe_surfaces(surfaces)


def _matched_surfaces(text: str, surfaces: list[str]) -> list[str]:
    result: list[str] = []
    for surface in _dedupe_surfaces(surfaces):
        if _count_matches(text, surface):
            result.append(surface)
    return result


def _entity_item(
    entity_id: str,
    entity: dict[str, Any],
    matches: list[str],
    reason: str,
) -> LiteraryBuilderContextItem:
    source = str(entity.get("canonical_source") or entity_id)
    target = str(entity.get("proposed_target_vi") or entity.get("canonical_target") or source)
    aliases = [str(value) for value in entity.get("aliases_target_vi") or [] if str(value)]
    alias_part = f" ({', '.join(aliases[:4])})" if aliases else ""
    line = f"{entity_id} | {source} -> {target}{alias_part}"
    return LiteraryBuilderContextItem(
        item_id=entity_id,
        item_type="entity",
        section="MATCHED_ENTITIES",
        line=line,
        reason=reason,
        matched_by=matches,
        token_estimate=_estimate_tokens(line),
        priority=(1, source.casefold(), entity_id),
    )


def _narrator_item(entity_id: str, entity: dict[str, Any]) -> LiteraryBuilderContextItem:
    item = _entity_item(entity_id, entity, ["first-person pronoun"], "narrator_first_person")
    return LiteraryBuilderContextItem(
        **{**item.__dict__, "section": "NARRATOR_CARD", "priority": (0, entity_id)}
    )


def _carryover_entity_item(entity_id: str, entity: dict[str, Any]) -> LiteraryBuilderContextItem:
    item = _entity_item(entity_id, entity, ["recent carryover"], "recent_carryover")
    return LiteraryBuilderContextItem(
        **{**item.__dict__, "section": "RECENT_CARRYOVER", "priority": (4, entity_id)}
    )


def _glossary_item(
    key: str,
    term: dict[str, Any],
    matches: list[str],
) -> LiteraryBuilderContextItem:
    source = str(term.get("source_term") or key)
    target = str(term.get("proposed_target_vi") or term.get("target_term") or "")
    keep = " [GIU NGUYEN]" if bool(term.get("do_not_translate")) else ""
    line = f"{source} -> {target}{keep}"
    return LiteraryBuilderContextItem(
        item_id=key,
        item_type="glossary",
        section="MATCHED_GLOSSARY",
        line=line,
        reason="matched_surface",
        matched_by=matches,
        token_estimate=_estimate_tokens(line),
        priority=(2, source.casefold(), key),
    )


def _relation_item(
    key: str,
    relation: dict[str, Any],
    reason: str,
) -> LiteraryBuilderContextItem:
    left = str(relation.get("a") or "")
    right = str(relation.get("b") or "")
    state = str(relation.get("state_label") or "")
    left_to_right = str(relation.get("address_a_to_b_vi") or "")
    right_to_left = str(relation.get("address_b_to_a_vi") or "")
    line = f"{left}<->{right}: {left_to_right} / {right_to_left}"
    if state:
        line += f" ({state})"
    return LiteraryBuilderContextItem(
        item_id=key,
        item_type="relation",
        section="ACTIVE_RELATIONS",
        line=line,
        reason=reason,
        matched_by=[left, right],
        token_estimate=_estimate_tokens(line),
        priority=(0, left, right, key),
    )


def _recent_entity_candidates(
    registry: PrepassRegistry,
    already_included: set[str],
) -> list[tuple[str, dict[str, Any]]]:
    items = [
        (entity_id, entity)
        for entity_id, entity in registry.entities.items()
        if entity_id not in already_included
    ]
    return sorted(
        items,
        key=lambda item: (
            -len(registry.entity_chapters.get(item[0], set())),
            item[0],
        ),
    )


def _excluded(
    item_id: str,
    item_type: str,
    reason: str,
    surfaces: list[str],
) -> dict[str, Any]:
    return {
        "item_id": item_id,
        "item_type": item_type,
        "reason": reason,
        "matched_by": [],
        "surfaces_checked": [surface for surface in surfaces if surface],
    }


def _count_matches(text: str, needle: str) -> int:
    normalized_text = _normalize_for_match(text)
    normalized_needle = _normalize_for_match(needle.strip())
    if not normalized_needle:
        return 0
    pattern = rf"(?<!\w){re.escape(normalized_needle)}(?!\w)"
    return len(re.findall(pattern, normalized_text, flags=re.UNICODE))


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


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)
