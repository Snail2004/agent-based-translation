from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from pipeline.literary.b0_entity_inventory_experiment import (
    GLOSSARY_CATEGORIES,
    NAME_CLASSES,
    REFERENT_KINDS,
    REFERENTIAL_GENDERS,
)
from pipeline.literary.builder_pilot import load_system_prompt_from_design
from pipeline.literary.checkpoint import canonical_hash, canonical_json
from pipeline.literary.chapter_registry_schema_v4 import RenderedRegistryRequestV4


PROMPT_ID = "literary_chapter_entity_inventory_auditor_exp_v1_2"
AUDITOR_SCHEMA_VERSION = "b0_entity_inventory_auditor_exp_v1_1"
ENTITY_ACTIONS = frozenset(
    {
        "confirm_as_is",
        "confirm_with_patch",
        "keep_pending",
        "keep_dormant",
        "reject_candidate",
        "merge_into_candidate",
        "open_split_ticket",
    }
)
GLOSSARY_ACTIONS = frozenset(
    {
        "confirm_as_is",
        "confirm_with_patch",
        "keep_pending",
        "reject_noise",
        "merge_into_candidate",
    }
)
UNRESOLVED_ACTIONS = frozenset(
    {"keep_dormant", "keep_pending", "reject_noise", "open_promotion_ticket"}
)
PATCH_OPERATIONS = frozenset({"keep", "clear", "replace"})
PUBLICATION_SCOPES = frozenset(
    {"global", "block_local", "dormant", "pending", "not_published"}
)
ADDITIONAL_ISSUE_CODES = frozenset(
    {
        "missing_candidate",
        "possible_duplicate",
        "possible_wrong_kind",
        "possible_wrong_gender",
        "possible_wrong_scope",
        "summary_not_durable",
        "glossary_termhood",
        "other",
    }
)
TARGET_TYPES = frozenset({"entity", "glossary", "unresolved", "chapter"})
GENDER_KINDS = frozenset(
    {"person", "animal", "nonhuman_character", "group_reference", "unknown"}
)


def _nullable(enum: Iterable[str] | None = None) -> dict[str, Any]:
    string: dict[str, Any] = {"type": "string", "minLength": 1}
    if enum is not None:
        string["enum"] = sorted(enum)
    return {"anyOf": [string, {"type": "null"}]}


def entity_inventory_auditor_response_schema() -> dict[str, Any]:
    string = {"type": "string", "minLength": 1}
    block_ids = {
        "type": "array",
        "items": string,
        "minItems": 1,
        "maxItems": 6,
        "uniqueItems": True,
    }
    entity_decision = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "candidate_id",
            "action",
            "target_candidate_id",
            "canonical_surface_update",
            "canonical_name_class_operation",
            "canonical_name_class_value",
            "referent_kind_update",
            "referential_gender_operation",
            "referential_gender_value",
            "identity_summary_update",
            "retained_alternative_name_surfaces",
            "publication_scope",
            "source_block_ids",
            "resolution_note",
        ],
        "properties": {
            "candidate_id": string,
            "action": {"type": "string", "enum": sorted(ENTITY_ACTIONS)},
            "target_candidate_id": _nullable(),
            "canonical_surface_update": _nullable(),
            "canonical_name_class_operation": {
                "type": "string",
                "enum": sorted(PATCH_OPERATIONS),
            },
            "canonical_name_class_value": _nullable(NAME_CLASSES),
            "referent_kind_update": _nullable(REFERENT_KINDS),
            "referential_gender_operation": {
                "type": "string",
                "enum": sorted(PATCH_OPERATIONS),
            },
            "referential_gender_value": _nullable(REFERENTIAL_GENDERS),
            "identity_summary_update": _nullable(),
            "retained_alternative_name_surfaces": {
                "type": "array",
                "items": string,
                "uniqueItems": True,
            },
            "publication_scope": {
                "type": "string",
                "enum": sorted(PUBLICATION_SCOPES),
            },
            "source_block_ids": block_ids,
            "resolution_note": string,
        },
    }
    glossary_decision = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "candidate_id",
            "action",
            "target_candidate_id",
            "category_update",
            "short_description_update",
            "source_block_ids",
            "resolution_note",
        ],
        "properties": {
            "candidate_id": string,
            "action": {"type": "string", "enum": sorted(GLOSSARY_ACTIONS)},
            "target_candidate_id": _nullable(),
            "category_update": _nullable(GLOSSARY_CATEGORIES),
            "short_description_update": _nullable(),
            "source_block_ids": block_ids,
            "resolution_note": string,
        },
    }
    unresolved_decision = {
        "type": "object",
        "additionalProperties": False,
        "required": ["candidate_id", "action", "source_block_ids", "resolution_note"],
        "properties": {
            "candidate_id": string,
            "action": {"type": "string", "enum": sorted(UNRESOLVED_ACTIONS)},
            "source_block_ids": block_ids,
            "resolution_note": string,
        },
    }
    issue_ticket = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "target_type",
            "target_candidate_id",
            "issue_code",
            "surface",
            "source_block_ids",
            "note",
        ],
        "properties": {
            "target_type": {"type": "string", "enum": sorted(TARGET_TYPES)},
            "target_candidate_id": _nullable(),
            "issue_code": {
                "type": "string",
                "enum": sorted(ADDITIONAL_ISSUE_CODES),
            },
            "surface": _nullable(),
            "source_block_ids": block_ids,
            "note": string,
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "chapter_id",
            "entity_decisions",
            "glossary_decisions",
            "unresolved_decisions",
            "additional_issue_tickets",
        ],
        "properties": {
            "chapter_id": string,
            "entity_decisions": {"type": "array", "items": entity_decision},
            "glossary_decisions": {"type": "array", "items": glossary_decision},
            "unresolved_decisions": {"type": "array", "items": unresolved_decision},
            "additional_issue_tickets": {"type": "array", "items": issue_ticket},
        },
    }


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def _nullable_string(value: Any, label: str) -> str | None:
    if value is None:
        return None
    return _required_string(value, label)


def _enum(value: Any, allowed: Iterable[str], label: str) -> str:
    row = _required_string(value, label)
    if row not in allowed:
        raise ValueError(f"{label} has unsupported value {row!r}")
    return row


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        raise ValueError(
            f"{label} keys differ: expected {sorted(expected)}, got {sorted(actual)}"
        )


def _chapter_blocks(chapter: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = chapter.get("blocks")
    if not isinstance(raw, list) or not raw:
        raise ValueError("chapter must contain non-empty blocks")
    rows = [dict(row) for row in raw if isinstance(row, Mapping)]
    if len(rows) != len(raw):
        raise ValueError("chapter contains a non-object block")
    return sorted(rows, key=lambda row: int(row.get("order_index") or 0))


def _block_text(block: Mapping[str, Any]) -> str:
    return str(block.get("clean_text") or block.get("source_text") or block.get("text") or "")


def _block_view(block: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "block_id": str(block.get("block_id") or ""),
        "block_type": str(block.get("block_type") or "paragraph"),
        "order_index": int(block.get("order_index") or 0),
        "text": _block_text(block),
    }


def _allowed_support(
    source_ids: Sequence[str], blocks: Sequence[Mapping[str, Any]], *, neighbor_count: int = 1
) -> list[str]:
    index_by_id = {str(row.get("block_id") or ""): index for index, row in enumerate(blocks)}
    indexes: set[int] = set()
    for block_id in source_ids:
        if block_id not in index_by_id:
            raise ValueError(f"candidate cites foreign source block {block_id!r}")
        center = index_by_id[block_id]
        indexes.update(
            range(max(0, center - neighbor_count), min(len(blocks), center + neighbor_count + 1))
        )
    return [str(blocks[index].get("block_id") or "") for index in sorted(indexes)]


def _entity_roster_row(
    row: Mapping[str, Any], blocks: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    source_ids = [str(value) for value in row.get("source_block_ids") or []]
    return {
        "candidate_id": str(row.get("candidate_id") or ""),
        "canonical_surface": row.get("canonical_surface"),
        "canonical_name_class": row.get("canonical_name_class"),
        "alternative_names": [
            {"surface": value.get("surface"), "name_class": value.get("name_class")}
            for value in row.get("alternative_names") or []
            if isinstance(value, Mapping)
        ],
        "referent_kind_claim": row.get("referent_kind_claim"),
        "referential_gender_claim": row.get("referential_gender_claim"),
        "identity_summary_draft": row.get("identity_summary_draft"),
        "publication_state": row.get("publication_state"),
        "audit_reasons": list(row.get("audit_reasons") or []),
        "source_block_ids": source_ids,
        "suggested_support_block_ids": _allowed_support(source_ids, blocks),
    }


def _simple_roster_row(
    row: Mapping[str, Any], blocks: Sequence[Mapping[str, Any]], *, row_type: str
) -> dict[str, Any]:
    source_ids = [str(value) for value in row.get("source_block_ids") or []]
    common = {
        "candidate_id": str(row.get("candidate_id") or ""),
        "surface": row.get("surface"),
        "source_block_ids": source_ids,
        "suggested_support_block_ids": _allowed_support(source_ids, blocks),
    }
    if row_type == "glossary":
        common.update(
            {
                "category_claim": row.get("category_claim"),
                "short_description": row.get("short_description"),
            }
        )
    else:
        common.update(
            {
                "referent_kind_claim": row.get("referent_kind_claim"),
                "short_description": row.get("short_description"),
                "issue": row.get("issue"),
                "lifecycle_state": row.get("lifecycle_state"),
                "publication_state": row.get("publication_state"),
            }
        )
    return common


def build_audit_case_manifest(
    inventory: Mapping[str, Any], chapter: Mapping[str, Any]
) -> list[dict[str, Any]]:
    blocks = _chapter_blocks(chapter)
    cases: list[dict[str, Any]] = []
    for raw in inventory.get("entity_candidates") or []:
        if not isinstance(raw, Mapping):
            continue
        issues = set(str(value) for value in raw.get("audit_reasons") or [])
        kind = raw.get("referent_kind_claim") or {}
        gender = raw.get("referential_gender_claim") or {}
        if isinstance(kind, Mapping) and kind.get("basis") == "contextual":
            issues.add("contextual_identity_check")
        if isinstance(gender, Mapping) and gender.get("basis") == "contextual":
            issues.add("contextual_gender_check")
        if issues:
            source_ids = [str(value) for value in raw.get("source_block_ids") or []]
            payload = {
                "target_type": "entity",
                "target_candidate_id": str(raw.get("candidate_id") or ""),
                "issue_codes": sorted(issues),
                "review_scope": "entity_identity_and_profile",
                "suggested_support_block_ids": _allowed_support(source_ids, blocks),
            }
            cases.append({"case_id": "audcase_" + canonical_hash(payload)[:20], **payload})
    for raw in inventory.get("glossary_candidates") or []:
        if not isinstance(raw, Mapping):
            continue
        source_ids = [str(value) for value in raw.get("source_block_ids") or []]
        payload = {
            "target_type": "glossary",
            "target_candidate_id": str(raw.get("candidate_id") or ""),
            "issue_codes": ["glossary_termhood_check"],
            "review_scope": "termhood_and_category",
            "suggested_support_block_ids": _allowed_support(source_ids, blocks),
        }
        cases.append({"case_id": "audcase_" + canonical_hash(payload)[:20], **payload})
    return sorted(cases, key=lambda row: (row["target_type"], row["target_candidate_id"]))


def render_inventory_auditor_request(
    *,
    chapter: Mapping[str, Any],
    inventory: Mapping[str, Any],
    design_doc: Path,
    model_id: str = "gpt-5.4",
    reasoning_effort: str = "none",
    temperature: float = 1.0,
    seed: int = 20260715,
    max_output_tokens: int = 8192,
) -> RenderedRegistryRequestV4:
    chapter_id = _required_string(chapter.get("chapter_id"), "chapter_id")
    if inventory.get("chapter_id") != chapter_id:
        raise ValueError("inventory chapter differs from source chapter")
    blocks = _chapter_blocks(chapter)
    prompt = load_system_prompt_from_design(Path(design_doc), PROMPT_ID)
    prompt_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    schema = entity_inventory_auditor_response_schema()
    sections = {
        "source_blocks": [_block_view(row) for row in blocks],
        "entity_roster": [
            _entity_roster_row(row, blocks)
            for row in inventory.get("entity_candidates") or []
            if isinstance(row, Mapping)
        ],
        "glossary_roster": [
            _simple_roster_row(row, blocks, row_type="glossary")
            for row in inventory.get("glossary_candidates") or []
            if isinstance(row, Mapping)
        ],
        "unresolved_roster": [
            _simple_roster_row(row, blocks, row_type="unresolved")
            for row in inventory.get("unresolved_referents") or []
            if isinstance(row, Mapping)
        ],
        "audit_case_manifest": build_audit_case_manifest(inventory, chapter),
        "routine_checklist": [
            "entity_existence_and_scope",
            "referent_kind",
            "referential_gender_scope",
            "stable_name_and_alias",
            "identity_summary_durability",
            "cross_row_duplicate_or_split",
            "glossary_termhood",
        ],
    }
    payload = {
        "schema_version": AUDITOR_SCHEMA_VERSION,
        "role": "b0_entity_inventory_auditor_experiment",
        "chapter_id": chapter_id,
        "source_inventory_hash": inventory.get("inventory_hash"),
        "allowlisted_sections": sections,
    }
    messages = (
        {"role": "system", "content": prompt},
        {"role": "user", "content": canonical_json(payload)},
    )
    model_contract = {
        "model_id": model_id,
        "reasoning_effort": reasoning_effort,
        "temperature": temperature,
        "seed": seed,
        "max_output_tokens": max_output_tokens,
    }
    fingerprint = canonical_hash(
        {
            "auditor_schema_version": AUDITOR_SCHEMA_VERSION,
            "chapter_id": chapter_id,
            "source_inventory_hash": inventory.get("inventory_hash"),
            "prompt_id": PROMPT_ID,
            "prompt_sha256": prompt_sha,
            "response_schema_hash": canonical_hash(schema),
            "model_contract": model_contract,
            "sections_hash": canonical_hash(sections),
        }
    )
    return RenderedRegistryRequestV4(
        role="auditor",
        prompt_id=PROMPT_ID,
        prompt_sha256=prompt_sha,
        response_schema_hash=canonical_hash(schema),
        chapter_id=chapter_id,
        window_id=None,
        parent_working_revision_hash=None,
        sections=sections,
        messages=messages,
        request_fingerprint=fingerprint,
    )


def _string_list(value: Any, label: str, *, maximum: int | None = None) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    rows = [_required_string(item, label) for item in value]
    if len(rows) != len(set(rows)):
        raise ValueError(f"{label} contains duplicates")
    if maximum is not None and len(rows) > maximum:
        raise ValueError(f"{label} exceeds {maximum} items")
    return rows


def _validate_source_ids(
    value: Any, *, label: str, allowed: set[str]
) -> list[str]:
    rows = _string_list(value, label, maximum=6)
    if not rows:
        raise ValueError(f"{label} must not be empty")
    foreign = sorted(set(rows) - allowed)
    if foreign:
        raise ValueError(f"{label} cites blocks outside the supplied chapter: {foreign}")
    return rows


def _assert_exact_cover(rows: Sequence[Mapping[str, Any]], expected: set[str], label: str) -> None:
    actual = [str(row.get("candidate_id") or "") for row in rows]
    if len(actual) != len(set(actual)):
        raise ValueError(f"{label} contains duplicate candidate ids")
    if set(actual) != expected:
        raise ValueError(
            f"{label} must exact-cover supplied ids; missing={sorted(expected-set(actual))}, "
            f"foreign={sorted(set(actual)-expected)}"
        )


def _normalize_entity_decisions(
    value: Any,
    *,
    entity_by_id: Mapping[str, Mapping[str, Any]],
    suggested_support: Mapping[str, set[str]],
    chapter_block_ids: set[str],
    chapter_blocks: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("entity_decisions must be a list")
    rows = [row for row in value if isinstance(row, Mapping)]
    if len(rows) != len(value):
        raise ValueError("entity_decisions contains a non-object")
    _assert_exact_cover(rows, set(entity_by_id), "entity_decisions")
    normalized: list[dict[str, Any]] = []
    block_ids = {str(row.get("block_id") or "") for row in chapter_blocks}
    for raw in rows:
        _exact_keys(
            raw,
            {
                "candidate_id",
                "action",
                "target_candidate_id",
                "canonical_surface_update",
                "canonical_name_class_operation",
                "canonical_name_class_value",
                "referent_kind_update",
                "referential_gender_operation",
                "referential_gender_value",
                "identity_summary_update",
                "retained_alternative_name_surfaces",
                "publication_scope",
                "source_block_ids",
                "resolution_note",
            },
            "entity decision",
        )
        candidate_id = _required_string(raw.get("candidate_id"), "candidate_id")
        source = entity_by_id[candidate_id]
        action = _enum(raw.get("action"), ENTITY_ACTIONS, "entity action")
        target = _nullable_string(raw.get("target_candidate_id"), "target_candidate_id")
        canonical_update = _nullable_string(
            raw.get("canonical_surface_update"), "canonical_surface_update"
        )
        name_operation = _enum(
            raw.get("canonical_name_class_operation"),
            PATCH_OPERATIONS,
            "canonical_name_class_operation",
        )
        name_value = raw.get("canonical_name_class_value")
        if name_value is not None:
            name_value = _enum(name_value, NAME_CLASSES, "canonical_name_class_value")
        kind_update = raw.get("referent_kind_update")
        if kind_update is not None:
            kind_update = _enum(kind_update, REFERENT_KINDS, "referent_kind_update")
        gender_operation = _enum(
            raw.get("referential_gender_operation"),
            PATCH_OPERATIONS,
            "referential_gender_operation",
        )
        gender_value = raw.get("referential_gender_value")
        if gender_value is not None:
            gender_value = _enum(
                gender_value, REFERENTIAL_GENDERS, "referential_gender_value"
            )
        summary_update = _nullable_string(
            raw.get("identity_summary_update"), "identity_summary_update"
        )
        retained = _string_list(
            raw.get("retained_alternative_name_surfaces"),
            "retained_alternative_name_surfaces",
        )
        supplied_alternatives = {
            str(row.get("surface") or "")
            for row in source.get("alternative_names") or []
            if isinstance(row, Mapping)
        }
        if not set(retained) <= supplied_alternatives:
            raise ValueError("retained alternatives contain an unsupplied surface")
        scope = _enum(raw.get("publication_scope"), PUBLICATION_SCOPES, "publication_scope")
        sources = _validate_source_ids(
            raw.get("source_block_ids"),
            label="entity decision source_block_ids",
            allowed=chapter_block_ids,
        )
        extended_sources = sorted(set(sources) - suggested_support[candidate_id])
        note = _required_string(raw.get("resolution_note"), "resolution_note")

        if name_operation in {"keep", "clear"} and name_value is not None:
            raise ValueError("name class keep/clear requires null value")
        if name_operation == "replace" and name_value is None:
            raise ValueError("name class replace requires a value")
        if gender_operation in {"keep", "clear"} and gender_value is not None:
            raise ValueError("gender keep/clear requires null value")
        if gender_operation == "replace" and gender_value is None:
            raise ValueError("gender replace requires a value")
        if canonical_update is not None and not any(
            canonical_update in _block_text(block) for block in chapter_blocks
        ):
            raise ValueError("canonical surface update is not located in the chapter")

        has_patch = any(
            (
                canonical_update is not None,
                name_operation != "keep",
                kind_update is not None,
                gender_operation != "keep",
                summary_update is not None,
                set(retained) != supplied_alternatives,
            )
        )
        if action == "confirm_as_is":
            if has_patch or target is not None or scope not in {"global", "block_local"}:
                raise ValueError("confirm_as_is cannot patch, target, or remain unpublished")
            if source.get("audit_reasons"):
                raise ValueError("a flagged entity cannot be confirmed as-is")
        elif action == "confirm_with_patch":
            if target is not None or scope not in {"global", "block_local"}:
                raise ValueError("confirm_with_patch cannot target or remain unpublished")
            if not has_patch and not source.get("audit_reasons"):
                raise ValueError("confirm_with_patch requires a patch or flagged scope resolution")
        elif action == "merge_into_candidate":
            if target not in entity_by_id or target == candidate_id:
                raise ValueError("merge target must be another supplied entity")
            if has_patch or scope != "not_published":
                raise ValueError("merge source cannot carry patches or remain published")
        else:
            expected_scope = {
                "keep_pending": "pending",
                "keep_dormant": "dormant",
                "reject_candidate": "not_published",
                "open_split_ticket": "pending",
            }[action]
            if target is not None or has_patch or scope != expected_scope:
                raise ValueError(f"{action} has incompatible target, patch, or scope")

        current_name_class = source.get("canonical_name_class")
        final_name_class = (
            current_name_class
            if name_operation == "keep"
            else (None if name_operation == "clear" else name_value)
        )
        current_kind = (source.get("referent_kind_claim") or {}).get("value")
        final_kind = kind_update or current_kind
        current_gender = (source.get("referential_gender_claim") or {}).get("value")
        final_gender = (
            current_gender
            if gender_operation == "keep"
            else (None if gender_operation == "clear" else gender_value)
        )
        if scope == "global" and final_name_class is None:
            raise ValueError("global publication requires a stable canonical name class")
        if final_gender is not None and final_kind not in GENDER_KINDS:
            raise ValueError("final referent kind cannot carry referential gender")
        normalized.append(
            {
                "candidate_id": candidate_id,
                "action": action,
                "target_candidate_id": target,
                "canonical_surface_update": canonical_update,
                "canonical_name_class_operation": name_operation,
                "canonical_name_class_value": name_value,
                "referent_kind_update": kind_update,
                "referential_gender_operation": gender_operation,
                "referential_gender_value": gender_value,
                "identity_summary_update": summary_update,
                "retained_alternative_name_surfaces": retained,
                "publication_scope": scope,
                "source_block_ids": sources,
                "used_extended_chapter_support": bool(extended_sources),
                "extended_source_block_ids": extended_sources,
                "resolution_note": note,
            }
        )
    decisions = {row["candidate_id"]: row for row in normalized}
    for row in normalized:
        if row["action"] == "merge_into_candidate":
            target_action = decisions[str(row["target_candidate_id"])]["action"]
            if target_action not in {"confirm_as_is", "confirm_with_patch"}:
                raise ValueError("merge target must be confirmed in the same response")
    return sorted(normalized, key=lambda row: row["candidate_id"])


def _normalize_glossary_decisions(
    value: Any,
    *,
    glossary_by_id: Mapping[str, Mapping[str, Any]],
    suggested_support: Mapping[str, set[str]],
    chapter_block_ids: set[str],
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("glossary_decisions must be a list")
    rows = [row for row in value if isinstance(row, Mapping)]
    if len(rows) != len(value):
        raise ValueError("glossary_decisions contains a non-object")
    _assert_exact_cover(rows, set(glossary_by_id), "glossary_decisions")
    normalized: list[dict[str, Any]] = []
    for raw in rows:
        _exact_keys(
            raw,
            {
                "candidate_id",
                "action",
                "target_candidate_id",
                "category_update",
                "short_description_update",
                "source_block_ids",
                "resolution_note",
            },
            "glossary decision",
        )
        candidate_id = _required_string(raw.get("candidate_id"), "candidate_id")
        action = _enum(raw.get("action"), GLOSSARY_ACTIONS, "glossary action")
        target = _nullable_string(raw.get("target_candidate_id"), "target_candidate_id")
        category = raw.get("category_update")
        if category is not None:
            category = _enum(category, GLOSSARY_CATEGORIES, "category_update")
        description = _nullable_string(
            raw.get("short_description_update"), "short_description_update"
        )
        sources = _validate_source_ids(
            raw.get("source_block_ids"),
            label="glossary decision source_block_ids",
            allowed=chapter_block_ids,
        )
        extended_sources = sorted(set(sources) - suggested_support[candidate_id])
        note = _required_string(raw.get("resolution_note"), "resolution_note")
        if action == "merge_into_candidate":
            if target not in glossary_by_id or target == candidate_id:
                raise ValueError("glossary merge target must be another supplied row")
            if category is not None or description is not None:
                raise ValueError("glossary merge source cannot carry patches")
        elif target is not None:
            raise ValueError("only glossary merge may target another candidate")
        if action == "confirm_as_is" and (category is not None or description is not None):
            raise ValueError("confirm_as_is glossary cannot carry patches")
        if action == "confirm_with_patch" and category is None and description is None:
            raise ValueError("confirm_with_patch glossary requires a patch")
        if action in {"keep_pending", "reject_noise"} and (
            category is not None or description is not None
        ):
            raise ValueError(f"{action} glossary cannot carry patches")
        normalized.append(
            {
                "candidate_id": candidate_id,
                "action": action,
                "target_candidate_id": target,
                "category_update": category,
                "short_description_update": description,
                "source_block_ids": sources,
                "used_extended_chapter_support": bool(extended_sources),
                "extended_source_block_ids": extended_sources,
                "resolution_note": note,
            }
        )
    return sorted(normalized, key=lambda row: row["candidate_id"])


def _normalize_unresolved_decisions(
    value: Any,
    *,
    unresolved_by_id: Mapping[str, Mapping[str, Any]],
    suggested_support: Mapping[str, set[str]],
    chapter_block_ids: set[str],
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("unresolved_decisions must be a list")
    rows = [row for row in value if isinstance(row, Mapping)]
    if len(rows) != len(value):
        raise ValueError("unresolved_decisions contains a non-object")
    _assert_exact_cover(rows, set(unresolved_by_id), "unresolved_decisions")
    normalized: list[dict[str, Any]] = []
    for raw in rows:
        _exact_keys(
            raw,
            {"candidate_id", "action", "source_block_ids", "resolution_note"},
            "unresolved decision",
        )
        candidate_id = _required_string(raw.get("candidate_id"), "candidate_id")
        sources = _validate_source_ids(
            raw.get("source_block_ids"),
            label="unresolved decision source_block_ids",
            allowed=chapter_block_ids,
        )
        extended_sources = sorted(set(sources) - suggested_support[candidate_id])
        normalized.append(
            {
                "candidate_id": candidate_id,
                "action": _enum(raw.get("action"), UNRESOLVED_ACTIONS, "unresolved action"),
                "source_block_ids": sources,
                "used_extended_chapter_support": bool(extended_sources),
                "extended_source_block_ids": extended_sources,
                "resolution_note": _required_string(
                    raw.get("resolution_note"), "resolution_note"
                ),
            }
        )
    return sorted(normalized, key=lambda row: row["candidate_id"])


def _normalize_additional_tickets(
    value: Any,
    *,
    entity_ids: set[str],
    glossary_ids: set[str],
    unresolved_ids: set[str],
    chapter_block_ids: set[str],
    chapter_blocks: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("additional_issue_tickets must be a list")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    valid_ids = {
        "entity": entity_ids,
        "glossary": glossary_ids,
        "unresolved": unresolved_ids,
    }
    for raw in value:
        if not isinstance(raw, Mapping):
            raise ValueError("additional issue ticket must be an object")
        _exact_keys(
            raw,
            {
                "target_type",
                "target_candidate_id",
                "issue_code",
                "surface",
                "source_block_ids",
                "note",
            },
            "additional issue ticket",
        )
        target_type = _enum(raw.get("target_type"), TARGET_TYPES, "target_type")
        target_id = _nullable_string(raw.get("target_candidate_id"), "target_candidate_id")
        issue_code = _enum(raw.get("issue_code"), ADDITIONAL_ISSUE_CODES, "issue_code")
        surface = _nullable_string(raw.get("surface"), "surface")
        sources = _validate_source_ids(
            raw.get("source_block_ids"),
            label="additional issue source_block_ids",
            allowed=chapter_block_ids,
        )
        note = _required_string(raw.get("note"), "ticket note")
        if target_type == "chapter":
            if target_id is not None:
                raise ValueError("chapter issue cannot target a candidate")
        elif target_id not in valid_ids[target_type]:
            raise ValueError("additional issue targets a foreign candidate")
        if issue_code == "missing_candidate":
            if target_type != "chapter" or surface is None:
                raise ValueError("missing candidate ticket requires chapter target and surface")
            if not any(surface in _block_text(row) for row in chapter_blocks):
                raise ValueError("missing candidate surface is not located in the chapter")
        payload = {
            "target_type": target_type,
            "target_candidate_id": target_id,
            "issue_code": issue_code,
            "surface": surface,
            "source_block_ids": sources,
            "note": note,
        }
        ticket_id = "auditissue_" + canonical_hash(payload)[:20]
        if ticket_id in seen:
            raise ValueError("additional issue ticket is duplicated")
        seen.add(ticket_id)
        normalized.append({"ticket_id": ticket_id, **payload})
    return sorted(normalized, key=lambda row: row["ticket_id"])


def _apply_entity_decisions(
    inventory: Mapping[str, Any], decisions: Sequence[Mapping[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    by_id = {
        str(row["candidate_id"]): deepcopy(dict(row))
        for row in inventory.get("entity_candidates") or []
    }
    confirmed: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    closed: list[dict[str, Any]] = []
    for decision in decisions:
        candidate_id = str(decision["candidate_id"])
        row = by_id[candidate_id]
        action = str(decision["action"])
        audit = {
            "action": action,
            "publication_scope": decision["publication_scope"],
            "source_block_ids": list(decision["source_block_ids"]),
            "resolution_note": decision["resolution_note"],
        }
        if action in {"confirm_as_is", "confirm_with_patch"}:
            if decision["canonical_surface_update"] is not None:
                row["canonical_surface"] = decision["canonical_surface_update"]
            name_operation = decision["canonical_name_class_operation"]
            if name_operation == "clear":
                row["canonical_name_class"] = None
            elif name_operation == "replace":
                row["canonical_name_class"] = decision["canonical_name_class_value"]
            if decision["referent_kind_update"] is not None:
                row["referent_kind_claim"]["value"] = decision["referent_kind_update"]
                row["referent_kind_claim"]["support_block_ids"] = list(
                    decision["source_block_ids"]
                )
            row["referent_kind_claim"]["semantic_status"] = "auditor_reviewed"
            gender_operation = decision["referential_gender_operation"]
            if gender_operation == "clear":
                row["referential_gender_claim"] = None
            elif gender_operation == "replace":
                row["referential_gender_claim"] = {
                    "value": decision["referential_gender_value"],
                    "basis": "contextual",
                    "support_block_ids": list(decision["source_block_ids"]),
                    "address_validation_state": "valid",
                    "semantic_status": "auditor_reviewed",
                }
            elif row.get("referential_gender_claim") is not None:
                row["referential_gender_claim"]["semantic_status"] = "auditor_reviewed"
            if decision["identity_summary_update"] is not None:
                row["identity_summary_draft"] = decision["identity_summary_update"]
            row["identity_summary_status"] = "auditor_reviewed"
            retained = set(decision["retained_alternative_name_surfaces"])
            row["alternative_names"] = [
                value
                for value in row.get("alternative_names") or []
                if value.get("surface") in retained
            ]
            row["name_locations"] = [
                value
                for value in row.get("name_locations") or []
                if value.get("surface") == row["canonical_surface"]
                or value.get("surface") in retained
            ]
            row["publication_state"] = "auditor_confirmed"
            row["publication_scope"] = decision["publication_scope"]
            row["audit_reasons"] = []
            row["auditor_disposition"] = audit
            confirmed.append(row)
        elif action in {"keep_pending", "keep_dormant", "open_split_ticket"}:
            row["publication_state"] = (
                "dormant" if action == "keep_dormant" else "pending_auditor"
            )
            row["publication_scope"] = decision["publication_scope"]
            row["auditor_disposition"] = audit
            pending.append(row)
        else:
            row["publication_state"] = "not_published"
            row["publication_scope"] = "not_published"
            row["auditor_disposition"] = {
                **audit,
                "target_candidate_id": decision["target_candidate_id"],
            }
            closed.append(row)
    return confirmed, pending, closed


def validate_and_apply_auditor_response(
    response: Mapping[str, Any],
    *,
    chapter: Mapping[str, Any],
    inventory: Mapping[str, Any],
    request_fingerprint: str,
) -> dict[str, Any]:
    if not isinstance(response, Mapping):
        raise ValueError("auditor response must be an object")
    _exact_keys(
        response,
        {
            "chapter_id",
            "entity_decisions",
            "glossary_decisions",
            "unresolved_decisions",
            "additional_issue_tickets",
        },
        "auditor response",
    )
    chapter_id = _required_string(chapter.get("chapter_id"), "chapter_id")
    if response.get("chapter_id") != chapter_id or inventory.get("chapter_id") != chapter_id:
        raise ValueError("auditor response, inventory, and source chapter must agree")
    blocks = _chapter_blocks(chapter)
    chapter_block_ids = {str(row.get("block_id") or "") for row in blocks}
    entity_by_id = {
        str(row["candidate_id"]): row for row in inventory.get("entity_candidates") or []
    }
    glossary_by_id = {
        str(row["candidate_id"]): row for row in inventory.get("glossary_candidates") or []
    }
    unresolved_by_id = {
        str(row["candidate_id"]): row for row in inventory.get("unresolved_referents") or []
    }
    entity_allowed = {
        candidate_id: set(_allowed_support(row.get("source_block_ids") or [], blocks))
        for candidate_id, row in entity_by_id.items()
    }
    glossary_allowed = {
        candidate_id: set(_allowed_support(row.get("source_block_ids") or [], blocks))
        for candidate_id, row in glossary_by_id.items()
    }
    unresolved_allowed = {
        candidate_id: set(_allowed_support(row.get("source_block_ids") or [], blocks))
        for candidate_id, row in unresolved_by_id.items()
    }
    entity_decisions = _normalize_entity_decisions(
        response.get("entity_decisions"),
        entity_by_id=entity_by_id,
        suggested_support=entity_allowed,
        chapter_block_ids=chapter_block_ids,
        chapter_blocks=blocks,
    )
    glossary_decisions = _normalize_glossary_decisions(
        response.get("glossary_decisions"),
        glossary_by_id=glossary_by_id,
        suggested_support=glossary_allowed,
        chapter_block_ids=chapter_block_ids,
    )
    unresolved_decisions = _normalize_unresolved_decisions(
        response.get("unresolved_decisions"),
        unresolved_by_id=unresolved_by_id,
        suggested_support=unresolved_allowed,
        chapter_block_ids=chapter_block_ids,
    )
    additional_tickets = _normalize_additional_tickets(
        response.get("additional_issue_tickets"),
        entity_ids=set(entity_by_id),
        glossary_ids=set(glossary_by_id),
        unresolved_ids=set(unresolved_by_id),
        chapter_block_ids=chapter_block_ids,
        chapter_blocks=blocks,
    )

    confirmed_entities, pending_entities, closed_entities = _apply_entity_decisions(
        inventory, entity_decisions
    )
    glossary_source = {
        str(row["candidate_id"]): deepcopy(dict(row))
        for row in inventory.get("glossary_candidates") or []
    }
    confirmed_glossary: list[dict[str, Any]] = []
    pending_glossary: list[dict[str, Any]] = []
    closed_glossary: list[dict[str, Any]] = []
    for decision in glossary_decisions:
        row = glossary_source[str(decision["candidate_id"])]
        action = str(decision["action"])
        if decision["category_update"] is not None:
            row["category_claim"] = decision["category_update"]
        if decision["short_description_update"] is not None:
            row["short_description"] = decision["short_description_update"]
        row["auditor_disposition"] = dict(decision)
        if action in {"confirm_as_is", "confirm_with_patch"}:
            row["publication_state"] = "auditor_confirmed"
            confirmed_glossary.append(row)
        elif action == "keep_pending":
            row["publication_state"] = "pending_auditor"
            pending_glossary.append(row)
        else:
            row["publication_state"] = "not_published"
            closed_glossary.append(row)

    unresolved_source = {
        str(row["candidate_id"]): deepcopy(dict(row))
        for row in inventory.get("unresolved_referents") or []
    }
    retained_unresolved: list[dict[str, Any]] = []
    closed_unresolved: list[dict[str, Any]] = []
    for decision in unresolved_decisions:
        row = unresolved_source[str(decision["candidate_id"])]
        action = str(decision["action"])
        row["auditor_disposition"] = dict(decision)
        if action == "keep_dormant":
            row["lifecycle_state"] = "dormant_unresolved"
            retained_unresolved.append(row)
        elif action == "keep_pending":
            row["lifecycle_state"] = "pending_auditor"
            retained_unresolved.append(row)
        elif action == "open_promotion_ticket":
            row["lifecycle_state"] = "promotion_ticket_open"
            retained_unresolved.append(row)
        else:
            row["lifecycle_state"] = "rejected_noise"
            row["publication_state"] = "not_published"
            closed_unresolved.append(row)

    report = {
        "schema_version": AUDITOR_SCHEMA_VERSION,
        "chapter_id": chapter_id,
        "source_inventory_hash": inventory.get("inventory_hash"),
        "request_fingerprint": request_fingerprint,
        "entity_candidates": confirmed_entities,
        "pending_entity_candidates": pending_entities,
        "closed_entity_candidates": closed_entities,
        "glossary_candidates": confirmed_glossary,
        "pending_glossary_candidates": pending_glossary,
        "closed_glossary_candidates": closed_glossary,
        "unresolved_referents": retained_unresolved,
        "closed_unresolved_referents": closed_unresolved,
        "entity_decisions": entity_decisions,
        "glossary_decisions": glossary_decisions,
        "unresolved_decisions": unresolved_decisions,
        "additional_issue_tickets": additional_tickets,
        "audit_summary": {
            "confirmed_entity_count": len(confirmed_entities),
            "pending_entity_count": len(pending_entities),
            "closed_entity_count": len(closed_entities),
            "confirmed_glossary_count": len(confirmed_glossary),
            "pending_glossary_count": len(pending_glossary),
            "closed_glossary_count": len(closed_glossary),
            "retained_unresolved_count": len(retained_unresolved),
            "closed_unresolved_count": len(closed_unresolved),
            "additional_issue_count": len(additional_tickets),
            "extended_support_decision_count": sum(
                bool(row.get("used_extended_chapter_support"))
                for row in [*entity_decisions, *glossary_decisions, *unresolved_decisions]
            ),
            "extended_support_block_count": len(
                {
                    block_id
                    for row in [
                        *entity_decisions,
                        *glossary_decisions,
                        *unresolved_decisions,
                    ]
                    for block_id in row.get("extended_source_block_ids") or []
                }
            ),
        },
        "production_publish_performed": False,
    }
    return {**report, "audited_inventory_hash": canonical_hash(report)}


__all__ = [
    "AUDITOR_SCHEMA_VERSION",
    "PROMPT_ID",
    "build_audit_case_manifest",
    "entity_inventory_auditor_response_schema",
    "render_inventory_auditor_request",
    "validate_and_apply_auditor_response",
]
