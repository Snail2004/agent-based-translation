from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import hashlib
from pathlib import Path
import re
import unicodedata
from typing import Any, Iterable, Mapping, Sequence

from pipeline.literary.builder_pilot import load_system_prompt_from_design
from pipeline.literary.b0_chapter_priority_v1 import (
    make_priority_target,
    priority_schema,
    validate_priority_order,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json
from pipeline.literary.chapter_registry_schema_v4 import RenderedRegistryRequestV4


PROMPT_ID = "literary_chapter_entity_inventory_exp_v1_4"
EXPERIMENT_SCHEMA_VERSION = "b0_entity_inventory_exp_v1_4"
PRIORITY_ITEM_CLASSES = frozenset({"new_entity", "new_glossary", "unresolved"})
REFERENT_KINDS = frozenset(
    {
        "person",
        "animal",
        "nonhuman_character",
        "group_reference",
        "place",
        "object",
        "institution",
        "unknown",
    }
)
NAME_CLASSES = frozenset({"proper_name", "stable_nickname", "title_plus_name"})
CLAIM_BASES = frozenset({"explicit", "contextual"})
REFERENTIAL_GENDERS = frozenset({"masculine", "feminine", "neutral", "mixed"})
GLOSSARY_CATEGORIES = frozenset(
    {
        "cultural_term",
        "regional_term",
        "technical_term",
        "place_term",
        "object_term",
        "institution_term",
        "other",
    }
)
UNRESOLVED_ISSUES = frozenset(
    {"identity_ambiguous", "unnamed_but_salient", "surface_scope_uncertain"}
)


def entity_inventory_response_schema() -> dict[str, Any]:
    string = {"type": "string", "minLength": 1}
    block_ids = {
        "type": "array",
        "items": string,
        "minItems": 1,
        "maxItems": 2,
        "uniqueItems": True,
    }
    nullable_name_class = {
        "anyOf": [
            {"type": "string", "enum": sorted(NAME_CLASSES)},
            {"type": "null"},
        ]
    }
    semantic_claim = {
        "type": "object",
        "additionalProperties": False,
        "required": ["value", "basis", "support_block_ids"],
        "properties": {
            "value": {"type": "string", "enum": sorted(REFERENT_KINDS)},
            "basis": {"type": "string", "enum": sorted(CLAIM_BASES)},
            "support_block_ids": block_ids,
        },
    }
    gender_claim = {
        "anyOf": [
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["value", "basis", "support_block_ids"],
                "properties": {
                    "value": {
                        "type": "string",
                        "enum": sorted(REFERENTIAL_GENDERS),
                    },
                    "basis": {"type": "string", "enum": sorted(CLAIM_BASES)},
                    "support_block_ids": block_ids,
                },
            },
            {"type": "null"},
        ]
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "entity_candidates",
            "glossary_candidates",
            "unresolved_referents",
            "chapter_priority_order",
        ],
        "properties": {
            "entity_candidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "canonical_surface",
                        "canonical_surface_support_block_ids",
                        "canonical_name_class",
                        "alternative_names",
                        "referent_kind_claim",
                        "referential_gender_claim",
                        "identity_summary_draft",
                    ],
                    "properties": {
                        "canonical_surface": string,
                        "canonical_surface_support_block_ids": block_ids,
                        "canonical_name_class": nullable_name_class,
                        "alternative_names": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["surface", "name_class", "support_block_ids"],
                                "properties": {
                                    "surface": string,
                                    "name_class": {
                                        "type": "string",
                                        "enum": sorted(NAME_CLASSES),
                                    },
                                    "support_block_ids": block_ids,
                                },
                            },
                        },
                        "referent_kind_claim": semantic_claim,
                        "referential_gender_claim": gender_claim,
                        "identity_summary_draft": string,
                    },
                },
            },
            "glossary_candidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "surface",
                        "category_claim",
                        "short_description",
                        "support_block_ids",
                    ],
                    "properties": {
                        "surface": string,
                        "category_claim": {
                            "type": "string",
                            "enum": sorted(GLOSSARY_CATEGORIES),
                        },
                        "short_description": string,
                        "support_block_ids": block_ids,
                    },
                },
            },
            "unresolved_referents": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "surface",
                        "referent_kind_claim",
                        "short_description",
                        "issue",
                        "support_block_ids",
                    ],
                    "properties": {
                        "surface": string,
                        "referent_kind_claim": {
                            "type": "string",
                            "enum": sorted(REFERENT_KINDS),
                        },
                        "short_description": string,
                        "issue": {
                            "type": "string",
                            "enum": sorted(UNRESOLVED_ISSUES),
                        },
                        "support_block_ids": block_ids,
                    },
                },
            },
            "chapter_priority_order": priority_schema(
                item_classes=PRIORITY_ITEM_CLASSES
            ),
        },
    }


def _nfc(value: Any) -> str:
    return unicodedata.normalize("NFC", str(value))


def _normalized_surface(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value)).casefold()
    text = text.replace("\u2018", "'").replace("\u2019", "'")
    text = re.sub(r"\s+", " ", text).strip()
    return text.strip(" \t\r\n.,;:!?\"'()[]{}")


def _block_text(block: Mapping[str, Any]) -> str:
    return _nfc(block.get("clean_text") or block.get("source_text") or block.get("text") or "")


def _chapter_blocks(chapter: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = chapter.get("blocks")
    if not isinstance(raw, list) or not raw:
        raise ValueError("chapter must contain a non-empty blocks list")
    rows = [dict(row) for row in raw if isinstance(row, Mapping)]
    if len(rows) != len(raw):
        raise ValueError("chapter contains a non-object block")
    return sorted(rows, key=lambda row: int(row.get("order_index") or 0))


def _block_view(block: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "block_id": str(block.get("block_id") or ""),
        "block_type": str(block.get("block_type") or "paragraph"),
        "order_index": int(block.get("order_index") or 0),
        "text": _block_text(block),
    }


def render_entity_inventory_request(
    *,
    chapter: Mapping[str, Any],
    design_doc: Path,
    model_id: str = "gpt-5.4",
    reasoning_effort: str = "none",
    temperature: float = 1.0,
    seed: int = 20260715,
    max_output_tokens: int = 4096,
) -> RenderedRegistryRequestV4:
    chapter_id = str(chapter.get("chapter_id") or "").strip()
    if not chapter_id:
        raise ValueError("chapter_id is required")
    prompt = load_system_prompt_from_design(Path(design_doc), PROMPT_ID)
    prompt_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    schema = entity_inventory_response_schema()
    sections = {"source_blocks": [_block_view(row) for row in _chapter_blocks(chapter)]}
    payload = {
        "schema_version": EXPERIMENT_SCHEMA_VERSION,
        "role": "b0_entity_inventory_experiment",
        "chapter_id": chapter_id,
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
            "experiment_schema_version": EXPERIMENT_SCHEMA_VERSION,
            "chapter_id": chapter_id,
            "prompt_id": PROMPT_ID,
            "prompt_sha256": prompt_sha,
            "response_schema_hash": canonical_hash(schema),
            "model_contract": model_contract,
            "sections_hash": canonical_hash(sections),
        }
    )
    return RenderedRegistryRequestV4(
        role="b0",
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


def _exact_keys(row: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(row)
    if actual != expected:
        raise ValueError(f"{label} keys differ: expected {sorted(expected)}, got {sorted(actual)}")


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return _nfc(value.strip())


def _bounded_string_list(
    value: Any, label: str, *, minimum: int, maximum: int
) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    if not minimum <= len(value) <= maximum:
        raise ValueError(f"{label} must contain {minimum} to {maximum} items")
    rows = [_required_string(item, label) for item in value]
    if len(set(rows)) != len(rows):
        raise ValueError(f"{label} contains duplicate values")
    return rows


def _enum(value: Any, allowed: Iterable[str], label: str) -> str:
    row = _required_string(value, label)
    if row not in allowed:
        raise ValueError(f"{label} has unsupported value {row!r}")
    return row


def _validate_supported_claim(
    value: Any,
    *,
    label: str,
    allowed_values: Iterable[str],
    blocks: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    _exact_keys(value, {"value", "basis", "support_block_ids"}, label)
    claim_value = _enum(value.get("value"), allowed_values, f"{label} value")
    basis = _enum(value.get("basis"), CLAIM_BASES, f"{label} basis")
    support_block_ids = _bounded_string_list(
        value.get("support_block_ids"),
        f"{label} support_block_ids",
        minimum=1,
        maximum=2,
    )
    block_by_id = {str(row.get("block_id") or ""): row for row in blocks}
    foreign = [block_id for block_id in support_block_ids if block_id not in block_by_id]
    known = [block_id for block_id in support_block_ids if block_id in block_by_id]
    address_state = "foreign_block_removed" if foreign else "valid"
    return {
        "value": claim_value,
        "basis": basis,
        "support_block_ids": known,
        "proposed_support_block_ids": support_block_ids,
        "address_validation_state": address_state,
        "semantic_status": (
            "unreviewed" if address_state == "valid" else "pending_address_review"
        ),
    }


def _locate_surface(surface: str, blocks: Sequence[Mapping[str, Any]]) -> list[str]:
    return [str(row["block_id"]) for row in blocks if surface in _block_text(row)]


def _surface_support_record(
    value: Any,
    *,
    surface: str,
    blocks: Sequence[Mapping[str, Any]],
    label: str,
    allow_empty: bool = False,
) -> dict[str, Any]:
    if allow_empty and value == []:
        proposed: list[str] = []
    else:
        proposed = _bounded_string_list(value, label, minimum=1, maximum=2)
    block_by_id = {str(row.get("block_id") or ""): row for row in blocks}
    foreign = [block_id for block_id in proposed if block_id not in block_by_id]
    known = [block_id for block_id in proposed if block_id in block_by_id]
    supported = [
        block_id for block_id in known if surface in _block_text(block_by_id[block_id])
    ]
    issues: list[str] = []
    if not proposed:
        issues.append("missing_support")
    if foreign:
        issues.append("foreign_block_removed")
    if len(supported) != len(known):
        issues.append("surface_absent_from_support")
    state = (
        "valid"
        if not issues
        else issues[0]
        if len(issues) == 1
        else "mixed_invalid_support"
    )
    return {
        "source_block_ids": supported,
        "proposed_support_block_ids": proposed,
        "address_validation_state": state,
        "address_issues": issues,
    }


def validate_entity_inventory_response(
    response: Mapping[str, Any],
    chapter: Mapping[str, Any],
    *,
    request_fingerprint: str,
) -> dict[str, Any]:
    if not isinstance(response, Mapping):
        raise ValueError("entity inventory response must be an object")
    _exact_keys(
        response,
        {
            "entity_candidates",
            "glossary_candidates",
            "unresolved_referents",
            "chapter_priority_order",
        },
        "entity inventory response",
    )
    blocks = _chapter_blocks(chapter)
    chapter_id = str(chapter.get("chapter_id") or "")
    accepted_entities: list[dict[str, Any]] = []
    accepted_glossary: list[dict[str, Any]] = []
    accepted_unresolved: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    unlocated_surfaces: list[dict[str, Any]] = []
    claim_issues: list[dict[str, Any]] = []
    alternative_name_issues: list[dict[str, Any]] = []
    canonical_name_class_issues: list[dict[str, Any]] = []
    canonical_surface_support_normalizations: list[dict[str, Any]] = []

    raw_entities = response.get("entity_candidates")
    if not isinstance(raw_entities, list):
        raise ValueError("entity_candidates must be a list")
    surface_claimant_counts: dict[str, int] = defaultdict(int)
    for raw in raw_entities:
        if not isinstance(raw, Mapping):
            continue
        claimed: set[str] = set()
        canonical_value = raw.get("canonical_surface")
        if isinstance(canonical_value, str) and canonical_value.strip():
            claimed.add(_normalized_surface(canonical_value))
        for alternative in raw.get("alternative_names") or []:
            if not isinstance(alternative, Mapping):
                continue
            surface_value = alternative.get("surface")
            if isinstance(surface_value, str) and surface_value.strip():
                claimed.add(_normalized_surface(surface_value))
        for surface_key in claimed:
            if surface_key:
                surface_claimant_counts[surface_key] += 1
    for index, raw in enumerate(raw_entities):
        original_raw = deepcopy(raw)
        canonical_support_normalization: dict[str, Any] | None = None
        try:
            if not isinstance(raw, Mapping):
                raise ValueError("row must be an object")
            raw = deepcopy(dict(raw))
            canonical_support_was_omitted = (
                "canonical_surface_support_block_ids" not in raw
            )
            if canonical_support_was_omitted:
                surface_hint = raw.get("canonical_surface")
                exact_block_ids = (
                    _locate_surface(_nfc(surface_hint.strip()), blocks)[:2]
                    if isinstance(surface_hint, str) and surface_hint.strip()
                    else []
                )
                raw["canonical_surface_support_block_ids"] = exact_block_ids
                canonical_support_normalization = {
                    "row_index": index,
                    "surface": (
                        _nfc(surface_hint.strip())
                        if isinstance(surface_hint, str) and surface_hint.strip()
                        else None
                    ),
                    "normalization_kind": "omitted_canonical_surface_support",
                    "action": (
                        "derived_from_exact_surface_matches"
                        if exact_block_ids
                        else "preserved_pending_source_review"
                    ),
                    "derived_block_ids": exact_block_ids,
                }
            _exact_keys(
                raw,
                {
                    "canonical_surface",
                    "canonical_surface_support_block_ids",
                    "canonical_name_class",
                    "alternative_names",
                    "referent_kind_claim",
                    "referential_gender_claim",
                    "identity_summary_draft",
                },
                "entity candidate",
            )
            canonical = _required_string(raw.get("canonical_surface"), "canonical_surface")
            canonical_block_ids = _locate_surface(canonical, blocks)
            canonical_support = _surface_support_record(
                raw.get("canonical_surface_support_block_ids"),
                surface=canonical,
                blocks=blocks,
                label="canonical_surface_support_block_ids",
                allow_empty=canonical_support_was_omitted,
            )
            surface_status = "located" if canonical_block_ids else "unlocated_pending_repair"
            if not canonical_block_ids:
                unlocated_surfaces.append(
                    {
                        "row_type": "entity_canonical_surface",
                        "row_index": index,
                        "surface": canonical,
                    }
                )
            audit_reasons: list[str] = []
            if not canonical_block_ids:
                audit_reasons.append("canonical_surface_repair_required")
            if canonical_support["address_validation_state"] != "valid":
                audit_reasons.append("canonical_surface_support_review_required")
            canonical_ownership_state = (
                "multi_candidate_claim"
                if surface_claimant_counts[_normalized_surface(canonical)] > 1
                else "single_candidate_claim"
            )
            if canonical_ownership_state == "multi_candidate_claim":
                audit_reasons.append("surface_ownership_review_required")

            canonical_name_class = raw.get("canonical_name_class")
            if canonical_name_class is not None:
                try:
                    canonical_name_class = _enum(
                        canonical_name_class, NAME_CLASSES, "canonical_name_class"
                    )
                except ValueError as exc:
                    canonical_name_class_issues.append(
                        {
                            "row_index": index,
                            "reason": str(exc),
                            "raw_value": deepcopy(raw.get("canonical_name_class")),
                        }
                    )
                    canonical_name_class = None
                    audit_reasons.append("canonical_name_class_review_required")
            if canonical_name_class is None:
                audit_reasons.append("unnamed_candidate_scope_review_required")

            raw_alternatives = raw.get("alternative_names")
            if not isinstance(raw_alternatives, list):
                raise ValueError("alternative_names must be a list")
            alternatives: list[dict[str, Any]] = []
            seen_names = {_normalized_surface(canonical)}
            for alternative_index, alternative in enumerate(raw_alternatives):
                try:
                    if not isinstance(alternative, Mapping):
                        raise ValueError("alternative name must be an object")
                    _exact_keys(
                        alternative,
                        {"surface", "name_class", "support_block_ids"},
                        "alternative name",
                    )
                    surface = _required_string(
                        alternative.get("surface"), "alternative name surface"
                    )
                    normalized = _normalized_surface(surface)
                    if not normalized or normalized in seen_names:
                        raise ValueError("alternative name duplicates another stable name")
                    block_ids = _locate_surface(surface, blocks)
                    if not block_ids:
                        unlocated_surfaces.append(
                            {
                                "row_type": "entity_alternative_name",
                                "row_index": index,
                                "alternative_index": alternative_index,
                                "surface": surface,
                            }
                        )
                    support = _surface_support_record(
                        alternative.get("support_block_ids"),
                        surface=surface,
                        blocks=blocks,
                        label="alternative name support_block_ids",
                    )
                    ownership_state = (
                        "multi_candidate_claim"
                        if surface_claimant_counts[normalized] > 1
                        else "single_candidate_claim"
                    )
                    if support["address_validation_state"] != "valid":
                        audit_reasons.append("alternative_name_support_review_required")
                    if ownership_state == "multi_candidate_claim":
                        audit_reasons.append("surface_ownership_review_required")
                    alternatives.append(
                        {
                            "surface": surface,
                            "name_class": _enum(
                                alternative.get("name_class"),
                                NAME_CLASSES,
                                "alternative name class",
                            ),
                            **support,
                            "surface_match_block_ids": block_ids,
                            "ownership_state": ownership_state,
                        }
                    )
                    seen_names.add(normalized)
                except ValueError as exc:
                    alternative_name_issues.append(
                        {
                            "row_index": index,
                            "alternative_index": alternative_index,
                            "reason": str(exc),
                            "raw_row": deepcopy(alternative),
                        }
                    )
            alternatives.sort(key=lambda row: _normalized_surface(row["surface"]))
            try:
                kind_claim = _validate_supported_claim(
                    raw.get("referent_kind_claim"),
                    label="referent_kind_claim",
                    allowed_values=REFERENT_KINDS,
                    blocks=blocks,
                )
            except ValueError as exc:
                claim_issues.append(
                    {
                        "row_index": index,
                        "claim": "referent_kind_claim",
                        "action": "downgraded_to_unknown",
                        "reason": str(exc),
                        "raw_claim": deepcopy(raw.get("referent_kind_claim")),
                    }
                )
                kind_claim = {
                    "value": "unknown",
                    "basis": None,
                    "support_block_ids": [],
                    "address_validation_state": "invalid",
                    "semantic_status": "unreviewed",
                }
                audit_reasons.append("referent_kind_claim_review_required")

            gender_claim = None
            if raw.get("referential_gender_claim") is not None:
                try:
                    gender_claim = _validate_supported_claim(
                        raw.get("referential_gender_claim"),
                        label="referential_gender_claim",
                        allowed_values=REFERENTIAL_GENDERS,
                        blocks=blocks,
                    )
                except ValueError as exc:
                    claim_issues.append(
                        {
                            "row_index": index,
                            "claim": "referential_gender_claim",
                            "action": "dropped_claim_only",
                            "reason": str(exc),
                            "raw_claim": deepcopy(
                                raw.get("referential_gender_claim")
                            ),
                        }
                    )
                    gender_claim = None
                    audit_reasons.append("referential_gender_claim_review_required")

            if gender_claim is not None and kind_claim["value"] not in {
                "person",
                "animal",
                "nonhuman_character",
                "group_reference",
                "unknown",
            }:
                audit_reasons.append("kind_gender_scope_conflict")

            summary = _required_string(
                raw.get("identity_summary_draft"), "identity_summary_draft"
            )
            name_locations = [
                {
                    "surface": canonical,
                    "name_class": canonical_name_class,
                    **canonical_support,
                    "surface_match_block_ids": canonical_block_ids,
                    "ownership_state": canonical_ownership_state,
                },
                *alternatives,
            ]
            claim_block_ids = [
                *(kind_claim.get("support_block_ids") or []),
                *((gender_claim or {}).get("support_block_ids") or []),
            ]
            source_block_ids = sorted(
                {
                    *(block_id for row in name_locations for block_id in row["source_block_ids"]),
                    *claim_block_ids,
                },
                key=lambda block_id: next(
                    int(block.get("order_index") or 0)
                    for block in blocks
                    if str(block["block_id"]) == block_id
                ),
            )
            address_state = "addressed" if source_block_ids else "pending_source_repair"
            if address_state != "addressed":
                audit_reasons.append("entity_source_address_review_required")
            payload = {
                "canonical_surface": canonical,
                "surface_status": surface_status,
                "canonical_name_class": canonical_name_class,
                "alternative_names": alternatives,
                "name_locations": name_locations,
                "source_block_ids": source_block_ids,
                "address_state": address_state,
                "referent_kind_claim": kind_claim,
                "referential_gender_claim": gender_claim,
                "identity_summary_draft": summary,
                "identity_summary_status": "unreviewed",
                "publication_state": (
                    "pending_auditor"
                    if address_state == "addressed"
                    else "pending_source_repair"
                ),
                "audit_reasons": sorted(set(audit_reasons)),
            }
            accepted_entities.append(
                {
                    "candidate_id": "b0ent_" + canonical_hash(payload)[:20],
                    **payload,
                }
            )
            if canonical_support_normalization is not None:
                canonical_surface_support_normalizations.append(
                    canonical_support_normalization
                )
        except ValueError as exc:
            rejected.append(
                {
                    "row_type": "entity",
                    "row_index": index,
                    "reason": str(exc),
                    "raw_row": original_raw,
                    "lifecycle_state": "quarantined_contract_error",
                }
            )

    raw_glossary = response.get("glossary_candidates")
    if not isinstance(raw_glossary, list):
        raise ValueError("glossary_candidates must be a list")
    for index, raw in enumerate(raw_glossary):
        try:
            if not isinstance(raw, Mapping):
                raise ValueError("row must be an object")
            _exact_keys(
                raw,
                {"surface", "category_claim", "short_description", "support_block_ids"},
                "glossary candidate",
            )
            surface = _required_string(raw.get("surface"), "glossary surface")
            surface_match_block_ids = _locate_surface(surface, blocks)
            support = _surface_support_record(
                raw.get("support_block_ids"),
                surface=surface,
                blocks=blocks,
                label="glossary support_block_ids",
            )
            payload = {
                "surface": surface,
                "category_claim": _enum(
                    raw.get("category_claim"), GLOSSARY_CATEGORIES, "glossary category"
                ),
                "short_description": _required_string(
                    raw.get("short_description"), "short_description"
                ),
                **support,
                "surface_match_block_ids": surface_match_block_ids,
                "publication_state": (
                    "pending_auditor"
                    if support["address_validation_state"] == "valid"
                    else "pending_source_repair"
                ),
            }
            accepted_glossary.append(
                {"candidate_id": "b0gls_" + canonical_hash(payload)[:20], **payload}
            )
        except ValueError as exc:
            rejected.append(
                {
                    "row_type": "glossary",
                    "row_index": index,
                    "reason": str(exc),
                    "raw_row": deepcopy(raw),
                    "lifecycle_state": "quarantined_contract_error",
                }
            )

    raw_unresolved = response.get("unresolved_referents")
    if not isinstance(raw_unresolved, list):
        raise ValueError("unresolved_referents must be a list")
    for index, raw in enumerate(raw_unresolved):
        try:
            if not isinstance(raw, Mapping):
                raise ValueError("row must be an object")
            _exact_keys(
                raw,
                {
                    "surface",
                    "referent_kind_claim",
                    "short_description",
                    "issue",
                    "support_block_ids",
                },
                "unresolved referent",
            )
            surface = _required_string(raw.get("surface"), "unresolved surface")
            surface_match_block_ids = _locate_surface(surface, blocks)
            support = _surface_support_record(
                raw.get("support_block_ids"),
                surface=surface,
                blocks=blocks,
                label="unresolved support_block_ids",
            )
            payload = {
                "surface": surface,
                "referent_kind_claim": _enum(
                    raw.get("referent_kind_claim"), REFERENT_KINDS, "referent kind"
                ),
                "short_description": _required_string(
                    raw.get("short_description"), "short_description"
                ),
                "issue": _enum(raw.get("issue"), UNRESOLVED_ISSUES, "unresolved issue"),
                **support,
                "surface_match_block_ids": surface_match_block_ids,
                "lifecycle_state": "dormant_unresolved",
                "publication_state": "not_published",
            }
            accepted_unresolved.append(
                {"candidate_id": "b0unr_" + canonical_hash(payload)[:20], **payload}
            )
        except ValueError as exc:
            rejected.append(
                {
                    "row_type": "unresolved",
                    "row_index": index,
                    "reason": str(exc),
                    "raw_row": deepcopy(raw),
                    "lifecycle_state": "quarantined_contract_error",
                }
            )

    duplicate_groups: list[dict[str, Any]] = []
    grouped: dict[str, list[str]] = defaultdict(list)
    for row in accepted_entities:
        grouped[_normalized_surface(row["canonical_surface"])].append(row["candidate_id"])
    for surface, candidate_ids in sorted(grouped.items()):
        if len(candidate_ids) > 1:
            duplicate_groups.append(
                {"normalized_canonical_surface": surface, "candidate_ids": candidate_ids}
            )

    priority_targets: list[dict[str, Any]] = []
    for row in accepted_entities:
        for location in row.get("name_locations") or []:
            if not isinstance(location, Mapping):
                continue
            priority_targets.append(
                make_priority_target(
                    item_class="new_entity",
                    ref_id=row["candidate_id"],
                    surface=str(location.get("surface") or ""),
                    block_ids=list(location.get("surface_match_block_ids") or []),
                )
            )
    for row in accepted_glossary:
        priority_targets.append(
            make_priority_target(
                item_class="new_glossary",
                ref_id=row["candidate_id"],
                surface=row["surface"],
                block_ids=list(row.get("surface_match_block_ids") or []),
            )
        )
    for row in accepted_unresolved:
        priority_targets.append(
            make_priority_target(
                item_class="unresolved",
                ref_id=row["candidate_id"],
                surface=row["surface"],
                block_ids=list(row.get("surface_match_block_ids") or []),
            )
        )
    priority_order, priority_issues = validate_priority_order(
        response.get("chapter_priority_order"),
        chapter_blocks=blocks,
        targets=priority_targets,
        allowed_item_classes=PRIORITY_ITEM_CLASSES,
    )

    report = {
        "schema_version": EXPERIMENT_SCHEMA_VERSION,
        "chapter_id": chapter_id,
        "request_fingerprint": request_fingerprint,
        "entity_candidates": accepted_entities,
        "glossary_candidates": accepted_glossary,
        "unresolved_referents": accepted_unresolved,
        "chapter_priority_order": priority_order,
        "validation_report": {
            "raw_entity_count": len(raw_entities),
            "accepted_entity_count": len(accepted_entities),
            "pending_auditor_entity_count": sum(
                row["publication_state"] == "pending_auditor" for row in accepted_entities
            ),
            "pending_source_repair_entity_count": sum(
                row["publication_state"] == "pending_source_repair"
                for row in accepted_entities
            ),
            "pending_surface_repair_count": sum(
                row["surface_status"] == "unlocated_pending_repair"
                for row in accepted_entities
            ),
            "raw_glossary_count": len(raw_glossary),
            "accepted_glossary_count": len(accepted_glossary),
            "pending_source_repair_glossary_count": sum(
                row["publication_state"] == "pending_source_repair"
                for row in accepted_glossary
            ),
            "raw_unresolved_count": len(raw_unresolved),
            "accepted_unresolved_count": len(accepted_unresolved),
            "dormant_unresolved_count": sum(
                row["lifecycle_state"] == "dormant_unresolved"
                for row in accepted_unresolved
            ),
            "accepted_priority_count": len(priority_order),
            "priority_issue_count": len(priority_issues),
            "priority_issues": priority_issues,
            "canonical_name_class_issue_count": len(canonical_name_class_issues),
            "canonical_name_class_issues": canonical_name_class_issues,
            "canonical_surface_support_normalization_count": len(
                canonical_surface_support_normalizations
            ),
            "canonical_surface_support_normalizations": (
                canonical_surface_support_normalizations
            ),
            "alternative_name_issue_count": len(alternative_name_issues),
            "alternative_name_issues": alternative_name_issues,
            "claim_issue_count": len(claim_issues),
            "claim_issues": claim_issues,
            "unlocated_surface_count": len(unlocated_surfaces),
            "unlocated_surfaces": unlocated_surfaces,
            "rejected_row_count": len(rejected),
            "rejected_rows": rejected,
            "duplicate_canonical_group_count": len(duplicate_groups),
            "duplicate_canonical_groups": duplicate_groups,
        },
    }
    return {**report, "inventory_hash": canonical_hash(report)}


def _gold_entity_surfaces(row: Mapping[str, Any]) -> set[str]:
    surfaces = {_normalized_surface(row.get("canonical_surface") or "")}
    for key in ("observed_name_surfaces", "observed_local_references"):
        for item in row.get(key) or []:
            if isinstance(item, Mapping):
                surfaces.add(_normalized_surface(item.get("surface") or ""))
    return {surface for surface in surfaces if surface}


def _prediction_surfaces(row: Mapping[str, Any]) -> set[str]:
    surfaces = {_normalized_surface(row.get("canonical_surface") or row.get("surface") or "")}
    for key in ("source_locations", "name_locations", "alternative_names"):
        for item in row.get(key) or []:
            if isinstance(item, Mapping):
                surfaces.add(_normalized_surface(item.get("surface") or ""))
    return {surface for surface in surfaces if surface}


def _claim_value(value: Any) -> str:
    if isinstance(value, Mapping):
        return str(value.get("value") or "")
    return str(value or "")


def evaluate_inventory_against_gold(
    inventory: Mapping[str, Any], gold: Mapping[str, Any]
) -> dict[str, Any]:
    predicted_entities = list(inventory.get("entity_candidates") or [])
    predicted_unresolved = list(inventory.get("unresolved_referents") or [])
    predicted_glossary = list(inventory.get("glossary_candidates") or [])
    gold_entities = list(gold.get("required_confirmed_entities") or [])
    gold_pending = list(gold.get("required_pending_or_local_referents") or [])
    gold_glossary = list(gold.get("required_glossary_items") or [])

    gold_surface_map = {
        str(row.get("gold_entity_id")): _gold_entity_surfaces(row) for row in gold_entities
    }
    prediction_matches: dict[str, list[str]] = {}
    for row in predicted_entities:
        surfaces = _prediction_surfaces(row)
        prediction_matches[str(row["candidate_id"])] = [
            gold_id
            for gold_id, expected in gold_surface_map.items()
            if surfaces.intersection(expected)
        ]
    unresolved_matches: dict[str, list[str]] = {}
    for row in predicted_unresolved:
        surfaces = _prediction_surfaces(row)
        unresolved_matches[str(row["candidate_id"])] = [
            gold_id
            for gold_id, expected in gold_surface_map.items()
            if surfaces.intersection(expected)
        ]

    entity_rows = []
    matched_entity_ids: set[str] = set()
    detected_anywhere_ids: set[str] = set()
    wrong_kind = []
    for gold_row in gold_entities:
        gold_id = str(gold_row.get("gold_entity_id"))
        entity_hits = sorted(
            candidate_id
            for candidate_id, matches in prediction_matches.items()
            if gold_id in matches
        )
        unresolved_hits = sorted(
            candidate_id
            for candidate_id, matches in unresolved_matches.items()
            if gold_id in matches
        )
        if entity_hits:
            matched_entity_ids.add(gold_id)
        if entity_hits or unresolved_hits:
            detected_anywhere_ids.add(gold_id)
        expected_kind = str(gold_row.get("referent_kind") or "")
        for candidate_id in entity_hits:
            predicted = next(
                row for row in predicted_entities if str(row["candidate_id"]) == candidate_id
            )
            predicted_kind = _claim_value(predicted.get("referent_kind_claim"))
            if predicted_kind != expected_kind:
                wrong_kind.append(
                    {
                        "gold_entity_id": gold_id,
                        "candidate_id": candidate_id,
                        "expected_kind": expected_kind,
                        "predicted_kind": predicted_kind,
                    }
                )
        entity_rows.append(
            {
                "gold_entity_id": gold_id,
                "canonical_surface": gold_row.get("canonical_surface"),
                "entity_candidate_ids": entity_hits,
                "unresolved_candidate_ids": unresolved_hits,
                "confirmed_candidate_match": bool(entity_hits),
                "detected_anywhere": bool(entity_hits or unresolved_hits),
            }
        )

    pending_rows = []
    for gold_row in gold_pending:
        surfaces = {
            _normalized_surface(gold_row.get("canonical_description") or ""),
            *(_normalized_surface(item) for item in gold_row.get("observed_surfaces") or []),
        }
        surfaces.discard("")
        entity_hits = [
            str(row["candidate_id"])
            for row in predicted_entities
            if _prediction_surfaces(row).intersection(surfaces)
        ]
        unresolved_hits = [
            str(row["candidate_id"])
            for row in predicted_unresolved
            if _prediction_surfaces(row).intersection(surfaces)
        ]
        pending_rows.append(
            {
                "gold_referent_id": gold_row.get("gold_referent_id"),
                "entity_candidate_ids": sorted(entity_hits),
                "unresolved_candidate_ids": sorted(unresolved_hits),
                "detected": bool(entity_hits or unresolved_hits),
                "preserved_as_unresolved": bool(unresolved_hits),
            }
        )

    glossary_rows = []
    matched_glossary_ids: set[str] = set()
    for gold_row in gold_glossary:
        expected = _normalized_surface(gold_row.get("surface") or "")
        hits = [
            str(row["candidate_id"])
            for row in predicted_glossary
            if _normalized_surface(row.get("surface") or "") == expected
        ]
        gold_id = str(gold_row.get("gold_glossary_id"))
        if hits:
            matched_glossary_ids.add(gold_id)
        glossary_rows.append(
            {
                "gold_glossary_id": gold_id,
                "surface": gold_row.get("surface"),
                "candidate_ids": sorted(hits),
                "matched": bool(hits),
            }
        )

    wrong_merges = [
        {"candidate_id": candidate_id, "matched_gold_entity_ids": matches}
        for candidate_id, matches in sorted(prediction_matches.items())
        if len(matches) > 1
    ]
    wrong_splits = []
    for gold_id in gold_surface_map:
        hits = [candidate_id for candidate_id, matches in prediction_matches.items() if gold_id in matches]
        if len(hits) > 1:
            wrong_splits.append({"gold_entity_id": gold_id, "candidate_ids": sorted(hits)})

    unmatched_entities = [
        {
            "candidate_id": row["candidate_id"],
            "canonical_surface": row["canonical_surface"],
            "referent_kind_claim": _claim_value(row.get("referent_kind_claim")),
        }
        for row in predicted_entities
        if not prediction_matches.get(str(row["candidate_id"]))
    ]
    total_entities = len(gold_entities)
    total_glossary = len(gold_glossary)
    result = {
        "schema_version": "b0_entity_inventory_gold_eval_v1",
        "gold_id": gold.get("gold_id"),
        "gold_status": gold.get("gold_status"),
        "inventory_hash": inventory.get("inventory_hash"),
        "required_confirmed_entity_count": total_entities,
        "confirmed_entity_match_count": len(matched_entity_ids),
        "confirmed_entity_recall": (
            len(matched_entity_ids) / total_entities if total_entities else None
        ),
        "detected_anywhere_entity_count": len(detected_anywhere_ids),
        "detected_anywhere_entity_recall": (
            len(detected_anywhere_ids) / total_entities if total_entities else None
        ),
        "required_pending_count": len(gold_pending),
        "pending_detected_count": sum(bool(row["detected"]) for row in pending_rows),
        "pending_preserved_as_unresolved_count": sum(
            bool(row["preserved_as_unresolved"]) for row in pending_rows
        ),
        "required_glossary_count": total_glossary,
        "glossary_match_count": len(matched_glossary_ids),
        "glossary_recall": len(matched_glossary_ids) / total_glossary if total_glossary else None,
        "wrong_merge_count": len(wrong_merges),
        "wrong_merges": wrong_merges,
        "wrong_split_count": len(wrong_splits),
        "wrong_splits": wrong_splits,
        "wrong_kind_count": len(wrong_kind),
        "wrong_kind_rows": wrong_kind,
        "unmatched_entity_candidate_count": len(unmatched_entities),
        "unmatched_entity_candidates_for_manual_review": unmatched_entities,
        "entity_rows": entity_rows,
        "pending_rows": pending_rows,
        "glossary_rows": glossary_rows,
        "evaluation_note": (
            "Unmatched predictions require manual review; this evaluator does not label them junk. "
            "Gold data is post-response evaluation only and is forbidden from request construction."
        ),
    }
    return {**result, "evaluation_hash": canonical_hash(result)}


__all__ = [
    "EXPERIMENT_SCHEMA_VERSION",
    "PROMPT_ID",
    "entity_inventory_response_schema",
    "evaluate_inventory_against_gold",
    "render_entity_inventory_request",
    "validate_entity_inventory_response",
]
