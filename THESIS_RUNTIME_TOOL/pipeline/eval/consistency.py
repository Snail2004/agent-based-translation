from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any


METRIC_VERSION = "consistency_v1"
ECS_LIMITATION = (
    "ECS v1 measures approved-form coverage only. It does not detect unregistered "
    "wrong forms directly; a suspicious entity is surfaced when no approved form "
    "appears in a target block that has a source name mention."
)


def normalize_text(text: str) -> str:
    """Normalize by Unicode NFC + casefold; Vietnamese diacritics are preserved."""

    return unicodedata.normalize("NFC", text).casefold()


def count_term_matches(text: str, term: str) -> int:
    """Count Unicode word-boundary matches using (?<!\\w)<term>(?!\\w)."""

    if not term:
        return 0
    normalized_text = normalize_text(text)
    normalized_term = normalize_text(term)
    pattern = rf"(?<!\w){re.escape(normalized_term)}(?!\w)"
    return len(re.findall(pattern, normalized_text, flags=re.UNICODE))


def has_term(text: str, term: str) -> bool:
    return count_term_matches(text, term) > 0


def score_consistency(
    *,
    project: str,
    terms: dict[str, dict[str, Any]],
    entities: dict[str, dict[str, Any]],
    term_occurrences_by_block: dict[str, list[Any]],
    entity_mentions_by_block: dict[str, list[Any]],
    translations_by_block: dict[str, str],
    block_chapters: dict[str, str],
    source: str = "oracle_gpt55_preview",
) -> dict[str, Any]:
    """Score TAR/FVR/ECS for already prepared registries and per-block outputs.

    ECS v1 intentionally scores only approved-form coverage for source name mentions.
    It excludes pronouns/ellipsis from the denominator and cannot directly identify
    unregistered wrong target names; that limitation is repeated in the report.
    """

    tar = _score_tar(
        terms=terms,
        term_occurrences_by_block=term_occurrences_by_block,
        translations_by_block=translations_by_block,
        block_chapters=block_chapters,
    )
    fvr = _score_fvr(
        terms=terms,
        term_occurrences_by_block=term_occurrences_by_block,
        translations_by_block=translations_by_block,
        total_pairs=tar["pairs"],
    )
    ecs = _score_ecs(
        entities=entities,
        entity_mentions_by_block=entity_mentions_by_block,
        translations_by_block=translations_by_block,
    )
    return {
        "project": project,
        "scored_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "metric_version": METRIC_VERSION,
        "tar": tar,
        "fvr": fvr,
        "ecs": ecs,
        "inspection": {
            "lowest_tar_blocks": tar.pop("_lowest_tar_blocks"),
            "lowest_ecs_entities": ecs.pop("_lowest_ecs_entities"),
        },
    }


def _score_tar(
    *,
    terms: dict[str, dict[str, Any]],
    term_occurrences_by_block: dict[str, list[Any]],
    translations_by_block: dict[str, str],
    block_chapters: dict[str, str],
) -> dict[str, Any]:
    pair_counts = _pair_counts(term_occurrences_by_block, terms)
    total_pairs = len(pair_counts)
    adherent_pairs = 0
    weighted_total = 0
    weighted_adherent = 0
    chapter_total: Counter[str] = Counter()
    chapter_adherent: Counter[str] = Counter()
    term_total: Counter[str] = Counter()
    term_adherent: Counter[str] = Counter()
    block_total: Counter[str] = Counter()
    block_adherent: Counter[str] = Counter()

    for (block_id, term_id), weight in sorted(pair_counts.items()):
        term = terms.get(term_id, {})
        target_text = translations_by_block.get(block_id, "")
        adherent = _term_adherent(term, target_text)
        chapter = block_chapters.get(block_id, _chapter_label_from_block(block_id))
        term_label = _term_label(term_id, term)

        chapter_total[chapter] += 1
        term_total[term_label] += 1
        block_total[block_id] += 1
        weighted_total += weight
        if adherent:
            adherent_pairs += 1
            chapter_adherent[chapter] += 1
            term_adherent[term_label] += 1
            block_adherent[block_id] += 1
            weighted_adherent += weight

    all_chapters = set(block_chapters.values()) | set(chapter_total)
    per_chapter = {
        chapter: _safe_ratio(chapter_adherent[chapter], chapter_total[chapter])
        for chapter in sorted(all_chapters)
    }
    worst_terms = []
    for term_label, total in term_total.items():
        worst_terms.append(
            {
                "term": term_label,
                "rate": _safe_ratio(term_adherent[term_label], total),
                "pairs": total,
            }
        )
    worst_terms.sort(key=lambda item: (item["rate"], -item["pairs"], item["term"]))

    block_rates = []
    for block_id, total in block_total.items():
        block_rates.append(
            (
                _safe_ratio(block_adherent[block_id], total),
                -total,
                block_id,
            )
        )
    block_rates.sort()

    return {
        "overall": _safe_ratio(adherent_pairs, total_pairs),
        "pairs": total_pairs,
        "occurrence_weighted": _safe_ratio(weighted_adherent, weighted_total),
        "per_chapter": per_chapter,
        "worst_terms": worst_terms[:15],
        "_lowest_tar_blocks": [item[2] for item in block_rates[:10]],
    }


def _score_fvr(
    *,
    terms: dict[str, dict[str, Any]],
    term_occurrences_by_block: dict[str, list[Any]],
    translations_by_block: dict[str, str],
    total_pairs: int,
) -> dict[str, Any]:
    pair_counts = _pair_counts(term_occurrences_by_block, terms)
    violating_pairs: set[tuple[str, str]] = set()
    violations: list[dict[str, Any]] = []
    for block_id, term_id in sorted(pair_counts):
        term = terms.get(term_id, {})
        target_text = translations_by_block.get(block_id, "")
        for variant in term.get("forbidden_variants") or []:
            count = count_term_matches(target_text, str(variant))
            if count:
                violating_pairs.add((block_id, term_id))
                violations.append(
                    {
                        "block_id": block_id,
                        "term": _term_label(term_id, term),
                        "variant": str(variant),
                        "count": count,
                    }
                )
    return {
        "overall": _safe_ratio(len(violating_pairs), total_pairs),
        "violations": violations,
    }


def _score_ecs(
    *,
    entities: dict[str, dict[str, Any]],
    entity_mentions_by_block: dict[str, list[Any]],
    translations_by_block: dict[str, str],
) -> dict[str, Any]:
    name_blocks_by_entity: dict[str, set[str]] = defaultdict(set)
    for block_id, mentions in entity_mentions_by_block.items():
        for mention in mentions:
            entity_id = _mention_entity_id(mention)
            if not entity_id:
                continue
            entity = entities.get(entity_id, {})
            if _is_name_mention(mention, entity, block_id):
                name_blocks_by_entity[entity_id].add(block_id)

    per_entity: list[dict[str, Any]] = []
    entities_skipped = 0
    weighted_numerator = 0
    weighted_denominator = 0
    for entity_id, entity in sorted(entities.items()):
        canonical_target = str(entity.get("canonical_target") or "").strip()
        approved_forms = _approved_entity_forms(entity)
        name_blocks = name_blocks_by_entity.get(entity_id, set())
        if not canonical_target:
            entities_skipped += 1
            continue
        if not name_blocks:
            continue

        covered_blocks = 0
        forms_used: Counter[str] = Counter()
        for block_id in sorted(name_blocks):
            target_text = translations_by_block.get(block_id, "")
            block_has_form = False
            for form in approved_forms:
                count = count_term_matches(target_text, form)
                if count:
                    block_has_form = True
                    forms_used[form] += count
            if block_has_form:
                covered_blocks += 1

        denominator = len(name_blocks)
        weighted_numerator += covered_blocks
        weighted_denominator += denominator
        per_entity.append(
            {
                "entity": _entity_label(entity_id, entity),
                "entity_id": entity_id,
                "coverage": _safe_ratio(covered_blocks, denominator),
                "name_mention_blocks": denominator,
                "forms_used": dict(sorted(forms_used.items())),
            }
        )

    lowest_coverage = sorted(
        per_entity,
        key=lambda item: (item["coverage"], -item["name_mention_blocks"], item["entity"]),
    )
    top_by_mentions = sorted(
        per_entity,
        key=lambda item: (-item["name_mention_blocks"], item["entity"]),
    )
    return {
        "overall": _safe_ratio(weighted_numerator, weighted_denominator),
        "entities_scored": len(per_entity),
        "entities_skipped": entities_skipped,
        "per_entity": top_by_mentions[:15],
        "lowest_coverage": lowest_coverage[:15],
        "limitation": ECS_LIMITATION,
        "_lowest_ecs_entities": [item["entity"] for item in lowest_coverage[:10]],
    }


def _pair_counts(
    occurrences_by_block: dict[str, list[Any]],
    terms: dict[str, dict[str, Any]],
) -> Counter[tuple[str, str]]:
    counts: Counter[tuple[str, str]] = Counter()
    registry_counts = _registry_occurrence_counts(terms)
    for block_id, occurrences in occurrences_by_block.items():
        block_terms = [_occurrence_term_id(item) for item in occurrences]
        block_terms = [term_id for term_id in block_terms if term_id]
        for term_id in sorted(set(block_terms)):
            weight = registry_counts.get((block_id, term_id), block_terms.count(term_id))
            counts[(block_id, term_id)] = max(1, weight)
    return counts


def _registry_occurrence_counts(
    terms: dict[str, dict[str, Any]]
) -> Counter[tuple[str, str]]:
    counts: Counter[tuple[str, str]] = Counter()
    for term_id, term in terms.items():
        for occurrence in term.get("occurrences") or []:
            block_id = occurrence.get("block_id") if isinstance(occurrence, dict) else None
            if block_id:
                counts[(str(block_id), str(term_id))] += 1
    return counts


def _term_adherent(term: dict[str, Any], target_text: str) -> bool:
    if term.get("do_not_translate"):
        return has_term(target_text, str(term.get("source_term") or ""))
    expected = str(term.get("expected_target") or "").strip()
    allowed = [str(item) for item in term.get("allowed_variants") or [] if str(item).strip()]
    return any(has_term(target_text, form) for form in [expected, *allowed] if form)


def _mention_entity_id(mention: Any) -> str | None:
    if isinstance(mention, str):
        return mention
    if isinstance(mention, dict):
        value = mention.get("entity_id") or mention.get("id")
        if value:
            return str(value)
    return None


def _occurrence_term_id(occurrence: Any) -> str | None:
    if isinstance(occurrence, str):
        return occurrence
    if isinstance(occurrence, dict):
        value = occurrence.get("term_id") or occurrence.get("id")
        if value:
            return str(value)
    return None


def _is_name_mention(mention: Any, entity: dict[str, Any], block_id: str) -> bool:
    if isinstance(mention, dict):
        mention_type = str(mention.get("mention_type") or mention.get("type") or "").casefold()
        if mention_type:
            return mention_type in {"name", "proper_name", "alias"}
        surface = mention.get("surface")
        if surface:
            return _is_source_name_surface(str(surface), entity)

    surfaces = [
        str(item.get("surface") or "")
        for item in entity.get("mentions") or []
        if isinstance(item, dict) and str(item.get("block_id") or "") == block_id
    ]
    return any(_is_source_name_surface(surface, entity) for surface in surfaces)


def _is_source_name_surface(surface: str, entity: dict[str, Any]) -> bool:
    approved_sources = [
        str(entity.get("canonical_source") or ""),
        *[str(item) for item in entity.get("aliases_source") or []],
    ]
    normalized_surface = normalize_text(surface)
    return normalized_surface in {
        normalize_text(item) for item in approved_sources if item.strip()
    }


def _approved_entity_forms(entity: dict[str, Any]) -> list[str]:
    forms = [
        str(entity.get("canonical_target") or ""),
        *[str(item) for item in entity.get("aliases_target") or []],
    ]
    seen: set[str] = set()
    result: list[str] = []
    for form in forms:
        normalized = normalize_text(form.strip())
        if form.strip() and normalized not in seen:
            seen.add(normalized)
            result.append(form.strip())
    return result


def _safe_ratio(numerator: int | float, denominator: int | float) -> float:
    if not denominator:
        return 0.0
    return numerator / denominator


def _term_label(term_id: str, term: dict[str, Any]) -> str:
    source = str(term.get("source_term") or "").strip()
    return source or term_id


def _entity_label(entity_id: str, entity: dict[str, Any]) -> str:
    source = str(entity.get("canonical_source") or "").strip()
    return source or entity_id


def _chapter_label_from_block(block_id: str) -> str:
    match = re.search(r"_ch(\d+)_", block_id)
    if match:
        return f"ch{int(match.group(1)):02d}"
    return "unknown"
