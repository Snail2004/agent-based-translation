from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PrepassRegistry:
    glossary: dict[str, dict[str, Any]] = field(default_factory=dict)
    entities: dict[str, dict[str, Any]] = field(default_factory=dict)
    entity_chapters: dict[str, set[str]] = field(default_factory=dict)
    relations: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)

    @property
    def entity_ids(self) -> set[str]:
        return set(self.entities)

    def merge(self, chapter_output: dict[str, Any]) -> None:
        chapter_id = str(chapter_output.get("chapter_id") or "")
        for term in chapter_output.get("glossary_candidates") or []:
            source_term = str(term.get("source_term") or "").strip()
            if not source_term:
                continue
            key = source_term.casefold()
            if key not in self.glossary:
                self.glossary[key] = dict(term)
            else:
                existing = self.glossary[key]
                existing_blocks = set(existing.get("block_ids") or [])
                existing_blocks.update(term.get("block_ids") or [])
                existing["block_ids"] = sorted(existing_blocks)

        for entity in chapter_output.get("entities") or []:
            entity_id = str(entity.get("entity_id") or "").strip()
            if not entity_id:
                continue
            self.entities[entity_id] = dict(entity)
            self.entity_chapters.setdefault(entity_id, set()).add(chapter_id)

        for relation in chapter_output.get("relations") or []:
            a = str(relation.get("a") or "").strip()
            b = str(relation.get("b") or "").strip()
            if not a or not b:
                continue
            key = tuple(sorted((a, b)))
            self.relations[key] = dict(relation)

    def compress(self, max_tokens: int = 600) -> str:
        max_chars = max_tokens * 4
        if not self.glossary and not self.entities and not self.relations:
            return "(registry empty - first chapter)"

        glossary_lines = ["Glossary:"]
        for term in sorted(self.glossary.values(), key=lambda item: str(item.get("source_term", "")).casefold()):
            glossary_lines.append(
                f"- {term.get('source_term', '')} -> {term.get('proposed_target_vi', '')}"
            )

        relation_lines = ["Relations:"]
        for relation in sorted(
            self.relations.values(),
            key=lambda item: (str(item.get("a", "")), str(item.get("b", ""))),
        ):
            relation_lines.append(
                "- "
                f"{relation.get('a', '')} <-> {relation.get('b', '')}: "
                f"{relation.get('state_label', '')}, "
                f"{relation.get('address_a_to_b_vi', '')} / "
                f"{relation.get('address_b_to_a_vi', '')}"
            )

        required = "\n".join([*glossary_lines, *relation_lines]).strip()
        entity_items = sorted(
            self.entities.items(),
            key=lambda item: (-len(self.entity_chapters.get(item[0], set())), item[0]),
        )
        entity_lines = ["Entities:"]
        for entity_id, entity in entity_items:
            aliases = ", ".join(entity.get("aliases_source") or [])
            line = (
                f"- {entity_id} | {entity.get('canonical_source', '')}"
                f" ({aliases}) -> {entity.get('proposed_target_vi', '')}"
            )
            candidate = "\n".join([required, *entity_lines, line]).strip()
            if len(candidate) > max_chars and len(entity_lines) > 1:
                continue
            entity_lines.append(line)

        compressed = "\n".join([required, *entity_lines]).strip()
        if len(compressed) <= max_chars:
            return compressed
        return required
