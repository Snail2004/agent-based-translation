from __future__ import annotations

import re
from typing import Any


ROOT_FIELDS = {
    "chapter_id": str,
    "glossary_candidates": list,
    "entities": list,
    "relations": list,
    "mention_surfaces": list,
    "chapter_summary_vi": str,
    "motifs": list,
}
TERM_CATEGORIES = {"nautical", "cultural", "object", "place", "other"}
ENTITY_TYPES = {"person", "place", "object", "other"}
ENTITY_ID_RE = re.compile(r"^ent_[a-z0-9_]+$")


def validate_chapter_output(
    obj: Any,
    *,
    expected_chapter_id: str | None = None,
    known_entity_ids: set[str] | None = None,
    valid_block_ids: set[str] | None = None,
) -> list[str]:
    """Return human-readable validation errors; empty list means valid."""

    errors: list[str] = []
    if not isinstance(obj, dict):
        return ["root must be a JSON object"]

    for field, expected_type in ROOT_FIELDS.items():
        if field not in obj:
            errors.append(f"missing required field: {field}")
        elif not isinstance(obj[field], expected_type):
            errors.append(f"{field} must be {expected_type.__name__}")
    if errors:
        return errors

    if expected_chapter_id and obj["chapter_id"] != expected_chapter_id:
        errors.append(
            f"chapter_id must be {expected_chapter_id}, got {obj['chapter_id']}"
        )

    chapter_entity_ids: set[str] = set()
    for index, entity in enumerate(obj["entities"]):
        if not isinstance(entity, dict):
            errors.append(f"entities[{index}] must be object")
            continue
        _require(entity, "entities", index, "entity_id", str, errors)
        _require(entity, "entities", index, "canonical_source", str, errors)
        _require(entity, "entities", index, "aliases_source", list, errors)
        _require(entity, "entities", index, "entity_type", str, errors)
        _require(entity, "entities", index, "proposed_target_vi", str, errors)
        _require(entity, "entities", index, "aliases_target_vi", list, errors)
        entity_id = str(entity.get("entity_id") or "")
        if entity_id and not ENTITY_ID_RE.match(entity_id):
            errors.append(f"entities[{index}].entity_id must match ent_[a-z0-9_]+")
        if entity.get("entity_type") not in ENTITY_TYPES:
            errors.append(f"entities[{index}].entity_type is invalid")
        if entity_id:
            chapter_entity_ids.add(entity_id)
        _require_str_list(entity, "entities", index, "aliases_source", errors)
        _require_str_list(entity, "entities", index, "aliases_target_vi", errors)

    allowed_entity_ids = set(known_entity_ids or set()) | chapter_entity_ids

    for index, term in enumerate(obj["glossary_candidates"]):
        if not isinstance(term, dict):
            errors.append(f"glossary_candidates[{index}] must be object")
            continue
        _require(term, "glossary_candidates", index, "source_term", str, errors)
        _require(term, "glossary_candidates", index, "proposed_target_vi", str, errors)
        _require(term, "glossary_candidates", index, "do_not_translate", bool, errors)
        _require(term, "glossary_candidates", index, "category", str, errors)
        _require(term, "glossary_candidates", index, "block_ids", list, errors)
        if term.get("category") not in TERM_CATEGORIES:
            errors.append(f"glossary_candidates[{index}].category is invalid")
        _require_str_list(term, "glossary_candidates", index, "block_ids", errors)
        _validate_block_ids(
            term.get("block_ids") or [],
            f"glossary_candidates[{index}].block_ids",
            valid_block_ids,
            errors,
        )

    for index, relation in enumerate(obj["relations"]):
        if not isinstance(relation, dict):
            errors.append(f"relations[{index}] must be object")
            continue
        for field in [
            "a",
            "b",
            "relation",
            "address_a_to_b_vi",
            "address_b_to_a_vi",
            "state_label",
            "notes",
        ]:
            _require(relation, "relations", index, field, str, errors)
        if "trigger_block_id" not in relation:
            errors.append(f"relations[{index}].trigger_block_id is required")
        elif relation["trigger_block_id"] is not None and not isinstance(
            relation["trigger_block_id"], str
        ):
            errors.append(f"relations[{index}].trigger_block_id must be string or null")
        for field in ["a", "b"]:
            entity_id = relation.get(field)
            if isinstance(entity_id, str) and entity_id not in allowed_entity_ids:
                errors.append(f"relations[{index}].{field} references unknown entity_id")
        trigger = relation.get("trigger_block_id")
        if trigger is not None:
            _validate_block_ids(
                [trigger], f"relations[{index}].trigger_block_id", valid_block_ids, errors
            )

    for index, mention in enumerate(obj["mention_surfaces"]):
        if not isinstance(mention, dict):
            errors.append(f"mention_surfaces[{index}] must be object")
            continue
        _require(mention, "mention_surfaces", index, "entity_id", str, errors)
        _require(mention, "mention_surfaces", index, "surfaces", list, errors)
        entity_id = mention.get("entity_id")
        if isinstance(entity_id, str) and entity_id not in allowed_entity_ids:
            errors.append(
                f"mention_surfaces[{index}].entity_id references unknown entity_id"
            )
        _require_str_list(mention, "mention_surfaces", index, "surfaces", errors)

    for index, motif in enumerate(obj["motifs"]):
        if not isinstance(motif, dict):
            errors.append(f"motifs[{index}] must be object")
            continue
        _require(motif, "motifs", index, "note", str, errors)
        _require(motif, "motifs", index, "block_ids", list, errors)
        _require_str_list(motif, "motifs", index, "block_ids", errors)
        _validate_block_ids(
            motif.get("block_ids") or [],
            f"motifs[{index}].block_ids",
            valid_block_ids,
            errors,
        )

    return errors


def _require(
    obj: dict[str, Any],
    collection: str,
    index: int,
    field: str,
    expected_type: type,
    errors: list[str],
) -> None:
    if field not in obj:
        errors.append(f"{collection}[{index}].{field} is required")
    elif not isinstance(obj[field], expected_type):
        errors.append(
            f"{collection}[{index}].{field} must be {expected_type.__name__}"
        )


def _require_str_list(
    obj: dict[str, Any],
    collection: str,
    index: int,
    field: str,
    errors: list[str],
) -> None:
    value = obj.get(field)
    if isinstance(value, list) and any(not isinstance(item, str) for item in value):
        errors.append(f"{collection}[{index}].{field} must contain only strings")


def _validate_block_ids(
    block_ids: list[Any],
    label: str,
    valid_block_ids: set[str] | None,
    errors: list[str],
) -> None:
    if valid_block_ids is None:
        return
    for block_id in block_ids:
        if isinstance(block_id, str) and block_id not in valid_block_ids:
            errors.append(f"{label} contains unknown block_id: {block_id}")
