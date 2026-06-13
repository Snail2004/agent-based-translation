from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from pipeline.prepass.schemas import is_plain_pronoun


@dataclass(frozen=True)
class TermOccurrence:
    source_term: str
    block_id: str
    char_start: int
    char_end: int

    def to_json_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EntityMention:
    entity_id: str
    block_id: str
    surface: str
    char_start: int
    char_end: int

    def to_json_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ResolvedSpans:
    term_occurrences: list[TermOccurrence]
    entity_mentions: list[EntityMention]
    coverage: dict[str, list[str]]

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "term_occurrences": [item.to_json_dict() for item in self.term_occurrences],
            "entity_mentions": [item.to_json_dict() for item in self.entity_mentions],
            "coverage": self.coverage,
        }


def resolve_spans(
    document_json_path: str | Path,
    artifact_paths: list[str | Path],
) -> ResolvedSpans:
    document = json.loads(Path(document_json_path).read_text(encoding="utf-8"))
    return resolve_spans_for_document(document, artifact_paths)


def resolve_spans_for_document(
    document: dict[str, Any],
    artifact_paths: list[str | Path],
) -> ResolvedSpans:
    artifacts = [json.loads(Path(path).read_text(encoding="utf-8")) for path in artifact_paths]
    chapter_ids = {str(artifact.get("chapter_id") or "") for artifact in artifacts}
    blocks = _blocks_for_chapters(document, chapter_ids)
    block_order = {block_id: index for index, block_id in enumerate(blocks)}

    terms = _collect_terms(artifacts)
    entity_surfaces = _collect_entity_surfaces(artifacts)

    term_occurrences: list[TermOccurrence] = []
    for source_term in sorted(terms.values(), key=lambda value: value.casefold()):
        for block_id, text in blocks.items():
            for start, end in _find_word_boundary_matches(text, source_term):
                term_occurrences.append(
                    TermOccurrence(
                        source_term=source_term,
                        block_id=block_id,
                        char_start=start,
                        char_end=end,
                    )
                )

    entity_mentions = _resolve_entity_mentions(blocks, entity_surfaces)
    term_keys_seen = {item.source_term.casefold() for item in term_occurrences}
    entities_seen = {item.entity_id for item in entity_mentions}

    return ResolvedSpans(
        term_occurrences=sorted(
            term_occurrences,
            key=lambda item: (
                item.source_term.casefold(),
                block_order.get(item.block_id, 10**9),
                item.char_start,
                item.char_end,
            ),
        ),
        entity_mentions=sorted(
            entity_mentions,
            key=lambda item: (
                block_order.get(item.block_id, 10**9),
                item.char_start,
                item.char_end,
                item.entity_id,
            ),
        ),
        coverage={
            "terms_zero_occurrence": [
                term
                for key, term in sorted(terms.items())
                if key not in term_keys_seen
            ],
            "entities_zero_mention": [
                entity_id
                for entity_id in sorted(entity_surfaces)
                if entity_id not in entities_seen
            ],
        },
    )


def _blocks_for_chapters(
    document: dict[str, Any],
    chapter_ids: set[str],
) -> dict[str, str]:
    result: dict[str, str] = {}
    for chapter in document.get("chapters") or []:
        chapter_id = str(chapter.get("chapter_id") or "")
        if chapter_id not in chapter_ids:
            continue
        for block in chapter.get("blocks") or []:
            block_id = str(block.get("block_id") or "")
            if not block_id:
                continue
            text = str(block.get("clean_text") or block.get("source_text") or "")
            result[block_id] = unicodedata.normalize("NFC", text)
    return result


def _collect_terms(artifacts: list[dict[str, Any]]) -> dict[str, str]:
    terms: dict[str, str] = {}
    for artifact in artifacts:
        for term in artifact.get("glossary_candidates") or []:
            if not isinstance(term, dict):
                continue
            source_term = str(term.get("source_term") or "").strip()
            if source_term:
                terms.setdefault(source_term.casefold(), source_term)
    return terms


def _collect_entity_surfaces(
    artifacts: list[dict[str, Any]],
) -> dict[str, list[str]]:
    surfaces_by_entity: dict[str, list[str]] = {}
    entity_aliases: dict[str, list[str]] = {}

    for artifact in artifacts:
        for entity in artifact.get("entities") or []:
            if not isinstance(entity, dict):
                continue
            entity_id = str(entity.get("entity_id") or "").strip()
            if not entity_id:
                continue
            values = [
                str(entity.get("canonical_source") or ""),
                *[str(item) for item in entity.get("aliases_source") or []],
            ]
            entity_aliases.setdefault(entity_id, []).extend(values)

        for mention in artifact.get("mention_surfaces") or []:
            if not isinstance(mention, dict):
                continue
            entity_id = str(mention.get("entity_id") or "").strip()
            if not entity_id:
                continue
            values = [str(item) for item in mention.get("surfaces") or []]
            surfaces_by_entity.setdefault(entity_id, []).extend(values)

    for entity_id, aliases in entity_aliases.items():
        surfaces_by_entity.setdefault(entity_id, []).extend(aliases)

    return {
        entity_id: _dedupe_surfaces(values)
        for entity_id, values in surfaces_by_entity.items()
    }


def _resolve_entity_mentions(
    blocks: dict[str, str],
    surfaces_by_entity: dict[str, list[str]],
) -> list[EntityMention]:
    all_candidates: list[EntityMention] = []
    for entity_id, surfaces in surfaces_by_entity.items():
        for surface in surfaces:
            for block_id, text in blocks.items():
                for start, end in _find_word_boundary_matches(text, surface):
                    all_candidates.append(
                        EntityMention(
                            entity_id=entity_id,
                            block_id=block_id,
                            surface=text[start:end],
                            char_start=start,
                            char_end=end,
                        )
                    )

    selected: dict[tuple[str, int], EntityMention] = {}
    for candidate in sorted(
        all_candidates,
        key=lambda item: (
            item.block_id,
            item.char_start,
            -(item.char_end - item.char_start),
            item.entity_id,
        ),
    ):
        key = (candidate.block_id, candidate.char_start)
        if key not in selected:
            selected[key] = candidate
    return list(selected.values())


def _find_word_boundary_matches(text: str, needle: str) -> list[tuple[int, int]]:
    normalized_needle = unicodedata.normalize("NFC", needle.strip())
    if not normalized_needle:
        return []
    pattern = rf"(?<!\w){re.escape(normalized_needle)}(?!\w)"
    return [
        (match.start(), match.end())
        for match in re.finditer(pattern, text, flags=re.IGNORECASE | re.UNICODE)
    ]


def _dedupe_surfaces(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        surface = unicodedata.normalize("NFC", value.strip())
        key = surface.casefold()
        if not surface or is_plain_pronoun(surface) or key in seen:
            continue
        seen.add(key)
        result.append(surface)
    return sorted(result, key=lambda item: (-len(item), item.casefold()))
