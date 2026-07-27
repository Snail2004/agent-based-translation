"""Second-pass chapter entity enrichment over a sealed B1-Scan artifact.

The model supplies source-grounded dossier proposals. Code validates provenance,
field applicability and exact task coverage; it does not merge identities or
publish registry authority.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path
import re
from typing import Any, Callable, Iterable, Mapping, Sequence

from pipeline.literary.b1_scan_v1 import (
    MAX_OBSERVATION_BLOCK_IDS,
    PRESENCE_BASES,
    REFERENT_KINDS,
    TERM_CATEGORIES,
    _bounded_note,
    _enum,
    _exact_keys,
    _optional_string,
    _normalized_surface,
    _required_string,
    _source_blocks,
    _string_list,
)
from pipeline.literary.builder_pilot import load_system_prompt_from_design
from pipeline.literary.chapter_registry_schema_v4 import (
    REFERENTIAL_GENDERS,
    RenderedRegistryRequestV4,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json
from pipeline.literary.response_normalization_v1 import (
    attach_response_normalization_notes_v1,
    normalize_code_owned_response_echoes_v1,
)


PROMPT_ID = "literary_b1_enrich_v1_5"
REQUEST_SCHEMA_VERSION = "literary_b1_enrich_request_v1"
OUTPUT_SCHEMA_ID = "LiteraryB1EnrichOutputV1"
ARTIFACT_SCHEMA_VERSION = "literary_b1_enrich_artifact_v2"
MAX_SAME_REFERENT_PROPOSALS = 16
SAME_REFERENT_PROPOSAL_BASES = frozenset({"chapter_context_description"})
CONTINUITY_CONTEXT_SCHEMA_VERSION = "literary_b1_enrich_continuity_context_v2"
CONTINUITY_MARKER = (
    "A prior-chapter card shares this surface, but identity linkage is pending. "
    "Build this chapter dossier from current chapter evidence only."
)
PRIOR_CARD_PACKET_ACTIONS = frozenset(
    {"include_prior_card", "withhold_prior_card"}
)
CONTINUITY_CASE_ACTIONS = frozenset(
    {
        "include_prior_card",
        "carry_referenced_prior_card",
        "withhold_prior_card",
    }
)
CONTINUITY_STATES = frozenset(
    {"new_candidate", "continue_prior", "linkage_pending"}
)

CLAIM_FIELDS = frozenset(
    {
        "referent_kind",
        "gender",
        "life_stage",
        "role_or_occupation",
        "species",
        "place_type",
        "group_type",
        "object_type",
        "institution_type",
        "document_type",
    }
)
CLAIM_STATUSES = frozenset({"supported", "unclear", "not_applicable"})
CLAIM_BASES = frozenset(
    {
        "explicit_textual",
        "self_identification",
        "contextual_inference",
        "not_applicable",
    }
)
LIFE_STAGES = frozenset({"child", "youth", "adult", "elder"})
KINSHIP_RELATIONS = frozenset(
    {
        "parent_of",
        "child_of",
        "mother_of",
        "father_of",
        "sibling_of",
        "spouse_of",
        "betrothed_to",
        "widow_of",
        "widower_of",
        "grandparent_of",
        "grandchild_of",
        "other_kin",
    }
)
LINK_RELATIONS = frozenset(
    {
        "resides_at",
        "owned_by",
        "held_by",
        "part_of",
        "member_of",
        "head_of",
        "serves_in",
        "other_link",
    }
)
ADDRESS_MODES = frozenset({"to", "about"})
GLOSSARY_AMBIGUITY = frozenset({"clear", "ambiguous", "unclear"})
TYPE_FIELD_BY_KIND = {
    "animal": "species",
    "place": "place_type",
    "group_reference": "group_type",
    "object": "object_type",
    "institution": "institution_type",
    "named_text": "document_type",
}
GENDER_KINDS = frozenset({"person", "animal", "nonhuman_character"})
APPLICABLE_KINDS_BY_FIELD = {
    "gender": GENDER_KINDS,
    "life_stage": frozenset({"person"}),
    "role_or_occupation": GENDER_KINDS,
    "species": frozenset({"animal"}),
    "place_type": frozenset({"place"}),
    "group_type": frozenset({"group_reference"}),
    "object_type": frozenset({"object"}),
    "institution_type": frozenset({"institution"}),
    "document_type": frozenset({"named_text"}),
}


class B1EnrichError(ValueError):
    pass


def _nullable_string() -> dict[str, Any]:
    return {"anyOf": [{"type": "string", "minLength": 1}, {"type": "null"}]}


def _block_ids(*, minimum: int = 1, maximum: int = 4) -> dict[str, Any]:
    return {
        "type": "array",
        "items": {"type": "string", "minLength": 1},
        "minItems": minimum,
        "maxItems": maximum,
        "uniqueItems": True,
    }


def _claim_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "field",
            "status",
            "value",
            "basis",
            "anchor_block_ids",
            "story_time_note",
        ],
        "properties": {
            "field": {"type": "string", "enum": sorted(CLAIM_FIELDS)},
            "status": {"type": "string", "enum": sorted(CLAIM_STATUSES)},
            "value": _nullable_string(),
            "basis": {
                "anyOf": [
                    {"type": "string", "enum": sorted(CLAIM_BASES)},
                    {"type": "null"},
                ]
            },
            "anchor_block_ids": _block_ids(minimum=0),
            "story_time_note": _nullable_string(),
        },
    }


def _link_schema(relations: Iterable[str], *, note_field: bool) -> dict[str, Any]:
    required = ["relation", "target_ref", "basis", "anchor_block_ids"]
    properties: dict[str, Any] = {
        "relation": {"type": "string", "enum": sorted(relations)},
        "target_ref": {"type": "string", "minLength": 1},
        "basis": {"type": "string", "enum": sorted(CLAIM_BASES - {"not_applicable"})},
        "anchor_block_ids": _block_ids(),
    }
    if note_field:
        required.append("relation_note")
        properties["relation_note"] = _nullable_string()
    return {
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": properties,
    }


def _dossier_properties(*, include_scan_id: bool) -> tuple[list[str], dict[str, Any]]:
    required = [
        "claims",
        "kinship_links",
        "links",
        "address_forms_used",
        "aliases_observed",
        "identity_summary",
        "distinguishing_note",
    ]
    properties: dict[str, Any] = {
        "claims": {"type": "array", "items": _claim_schema(), "maxItems": 16},
        "kinship_links": {
            "type": "array",
            "items": _link_schema(KINSHIP_RELATIONS, note_field=True),
        },
        "links": {
            "type": "array",
            "items": _link_schema(LINK_RELATIONS, note_field=True),
        },
        "address_forms_used": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "counterpart_ref",
                    "mode",
                    "form",
                    "anchor_block_ids",
                ],
                "properties": {
                    "counterpart_ref": {"type": "string", "minLength": 1},
                    "mode": {"type": "string", "enum": sorted(ADDRESS_MODES)},
                    "form": {"type": "string", "minLength": 1},
                    "anchor_block_ids": _block_ids(),
                },
            },
        },
        "aliases_observed": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["surface", "anchor_block_ids"],
                "properties": {
                    "surface": {"type": "string", "minLength": 1},
                    "anchor_block_ids": _block_ids(),
                },
            },
        },
        "identity_summary": {"type": "string", "minLength": 1},
        "distinguishing_note": _nullable_string(),
    }
    if include_scan_id:
        required.insert(0, "scan_observation_id")
        properties["scan_observation_id"] = {"type": "string", "minLength": 1}
    else:
        required[:0] = ["surface", "source_block_ids", "referent_kind_claim"]
        properties.update(
            {
                "surface": {"type": "string", "minLength": 1},
                "source_block_ids": _block_ids(maximum=3),
                "referent_kind_claim": {
                    "type": "string",
                    "enum": sorted(REFERENT_KINDS),
                },
            }
        )
    return required, properties


def _dossier_schema(*, include_scan_id: bool) -> dict[str, Any]:
    required, properties = _dossier_properties(include_scan_id=include_scan_id)
    return {
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": properties,
    }


def b1_enrich_response_schema_v1() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_id",
            "chapter_id",
            "entities",
            "additional_entities",
            "spurious_challenges",
            "same_referent_proposals",
            "conflict_findings",
            "presence_correction_findings",
            "glossary_items",
        ],
        "properties": {
            "schema_id": {"type": "string", "enum": [OUTPUT_SCHEMA_ID]},
            "chapter_id": {"type": "string", "minLength": 1},
            "entities": {"type": "array", "items": _dossier_schema(include_scan_id=True)},
            "additional_entities": {
                "type": "array",
                "items": _dossier_schema(include_scan_id=False),
            },
            "spurious_challenges": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["scan_observation_id", "reason", "source_block_ids"],
                    "properties": {
                        "scan_observation_id": {"type": "string", "minLength": 1},
                        "reason": {"type": "string", "minLength": 1},
                        "source_block_ids": _block_ids(maximum=3),
                    },
                },
            },
            "same_referent_proposals": {
                "type": "array",
                "maxItems": MAX_SAME_REFERENT_PROPOSALS,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "subject_ref",
                        "target_ref",
                        "proposal_basis",
                        "source_block_ids",
                        "reason",
                    ],
                    "properties": {
                        "subject_ref": {
                            "type": "string",
                            "minLength": 1,
                        },
                        "target_ref": {
                            "type": "string",
                            "minLength": 1,
                        },
                        "proposal_basis": {
                            "type": "string",
                            "enum": sorted(SAME_REFERENT_PROPOSAL_BASES),
                        },
                        "source_block_ids": _block_ids(maximum=8),
                        "reason": {"type": "string", "minLength": 1},
                    },
                },
            },
            "conflict_findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "scan_observation_id",
                        "field",
                        "existing_value",
                        "observed_value",
                        "source_block_ids",
                        "reason",
                    ],
                    "properties": {
                        "scan_observation_id": {"type": "string", "minLength": 1},
                        "field": {"type": "string", "enum": sorted(CLAIM_FIELDS)},
                        "existing_value": {"type": "string", "minLength": 1},
                        "observed_value": {"type": "string", "minLength": 1},
                        "source_block_ids": _block_ids(maximum=3),
                        "reason": {"type": "string", "minLength": 1},
                    },
                },
            },
            "presence_correction_findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "scan_observation_id",
                        "proposed_presence_basis",
                        "source_block_ids",
                        "reason",
                    ],
                    "properties": {
                        "scan_observation_id": {"type": "string", "minLength": 1},
                        "proposed_presence_basis": {
                            "type": "string",
                            "enum": sorted(PRESENCE_BASES),
                        },
                        "source_block_ids": _block_ids(maximum=3),
                        "reason": {"type": "string", "minLength": 1},
                    },
                },
            },
            "glossary_items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "term_observation_id",
                        "contextual_sense",
                        "ambiguity_status",
                        "source_block_ids",
                    ],
                    "properties": {
                        "term_observation_id": {"type": "string", "minLength": 1},
                        "contextual_sense": {"type": "string", "minLength": 1},
                        "ambiguity_status": {
                            "type": "string",
                            "enum": sorted(GLOSSARY_AMBIGUITY),
                        },
                        "source_block_ids": _block_ids(maximum=3),
                    },
                },
            },
        },
    }


def render_b1_enrich_request_v1(
    *,
    chapter: Mapping[str, Any],
    scan_artifact: Mapping[str, Any],
    design_doc: Path,
    injected_prior_cards: Sequence[Mapping[str, Any]] | None = None,
    continuity_context: Mapping[str, Any] | None = None,
    previous_chapter_summary: str | None = None,
    global_summary: str | None = None,
    model_id: str = "gpt-5.4",
    reasoning_effort: str = "none",
    temperature: float = 1.0,
    seed: int = 20260721,
    max_output_tokens: int = 8192,
) -> RenderedRegistryRequestV4:
    chapter_id = _required_string(chapter.get("chapter_id"), "chapter_id")
    if continuity_context is None:
        continuity = build_b1_enrich_continuity_context_v1(
            scan_artifact=scan_artifact,
            prior_cards=list(injected_prior_cards or []),
        )
    else:
        if injected_prior_cards is not None:
            raise B1EnrichError(
                "use continuity_context instead of a second prior-card channel"
            )
        continuity = verify_b1_enrich_continuity_context_v1(continuity_context)
        if (
            continuity.get("chapter_id") != chapter_id
            or continuity.get("scan_artifact_hash") != scan_artifact.get("artifact_hash")
        ):
            raise B1EnrichError("continuity context lineage differs")
    tasks = _project_b1_enrich_tasks_for_model_v1(
        continuity["entity_task_packets"]
    )
    glossary_tasks = deepcopy(continuity["glossary_tasks"])
    blocks = _source_blocks(chapter)
    prompt = load_system_prompt_from_design(Path(design_doc), PROMPT_ID)
    prompt_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    schema = b1_enrich_response_schema_v1()
    sections = {
        "summary_context": {
            "previous_chapter_summary": _optional_string(previous_chapter_summary),
            "global_summary": _optional_string(global_summary),
        },
        "entity_tasks": tasks,
        "glossary_tasks": glossary_tasks,
        "source_blocks": [
            {"block_id": row["block_id"], "text": row["text"]} for row in blocks
        ],
    }
    payload = {
        "schema_version": REQUEST_SCHEMA_VERSION,
        "role": "b1_enrich",
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
            "request_schema_version": REQUEST_SCHEMA_VERSION,
            "chapter_id": chapter_id,
            "prompt_id": PROMPT_ID,
            "prompt_sha256": prompt_sha,
            "response_schema_hash": canonical_hash(schema),
            "model_contract": model_contract,
            "sections_hash": canonical_hash(sections),
        }
    )
    return RenderedRegistryRequestV4(
        role="b1_enrich",
        prompt_id=PROMPT_ID,
        prompt_sha256=prompt_sha,
        response_schema_hash=canonical_hash(schema),
        chapter_id=chapter_id,
        window_id=None,
        parent_working_revision_hash=str(scan_artifact.get("artifact_hash") or "") or None,
        sections=sections,
        messages=messages,
        request_fingerprint=fingerprint,
    )


def _project_b1_enrich_tasks_for_model_v1(
    tasks: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Compact only the model-facing prior-card history.

    The sealed continuity context remains the complete lineage source used by
    validation, the Writer, and later hearings. This projection removes only
    repeated claim records and audit-only provenance detail from the request.
    """

    projected: list[dict[str, Any]] = []
    for raw in tasks:
        task = deepcopy(dict(raw))
        continuity = task.get("continuity")
        if isinstance(continuity, Mapping):
            continuity_row = deepcopy(dict(continuity))
            prior_card = continuity_row.get("prior_card")
            if isinstance(prior_card, Mapping):
                continuity_row["prior_card"] = _b1_enrich_prior_card_model_view_v1(
                    prior_card
                )
            task["continuity"] = continuity_row
        projected.append(task)
    return projected


def _b1_enrich_prior_card_model_view_v1(
    prior_card: Mapping[str, Any],
) -> dict[str, Any]:
    view = deepcopy(dict(prior_card))
    claims = view.get("profile_claims")
    if isinstance(claims, list):
        view["profile_claims"] = _compact_b1_enrich_prior_claims_v1(claims)

    provenance = view.get("provenance_refs")
    if isinstance(provenance, list):
        summary = _compact_b1_enrich_provenance_v1(provenance)
        if len(canonical_json(summary)) < len(canonical_json(provenance)):
            view.pop("provenance_refs", None)
            view["provenance_summary"] = summary
    return view


def _compact_b1_enrich_prior_claims_v1(
    claims: Sequence[Any],
) -> list[Any]:
    """Collapse only claims identical outside their historical anchors."""

    groups: dict[str, dict[str, Any]] = {}
    ordered: list[tuple[str, Any]] = []
    for raw in claims:
        if not isinstance(raw, Mapping):
            ordered.append(("raw", deepcopy(raw)))
            continue
        row = deepcopy(dict(raw))
        anchors = row.get("anchor_block_ids")
        if not isinstance(anchors, list) or any(
            not isinstance(value, str) or not value for value in anchors
        ):
            ordered.append(("raw", row))
            continue

        semantic_row = deepcopy(row)
        semantic_row.pop("anchor_block_ids", None)
        key = canonical_json(semantic_row)
        group = groups.get(key)
        if group is None:
            group = {
                "semantic_row": semantic_row,
                "record_count": 0,
                "anchor_block_ids": set(),
                "source_rows": [],
            }
            groups[key] = group
            ordered.append(("group", key))
        group["record_count"] += 1
        group["anchor_block_ids"].update(anchors)
        group["source_rows"].append(row)

    compact: list[Any] = []
    for kind, value in ordered:
        if kind == "raw":
            compact.append(value)
            continue
        group = groups[value]
        if group["record_count"] == 1:
            compact.append(group["source_rows"][0])
            continue
        anchors = sorted(
            group["anchor_block_ids"], key=_natural_identifier_sort_key
        )
        row = deepcopy(group["semantic_row"])
        row.update(
            {
                "anchor_block_ids": anchors,
                "support_record_count": group["record_count"],
            }
        )
        if len(canonical_json(row)) < len(canonical_json(group["source_rows"])):
            compact.append(row)
        else:
            compact.extend(group["source_rows"])
    return compact


def _compact_b1_enrich_provenance_v1(
    provenance_refs: Sequence[Any],
) -> dict[str, Any]:
    standard: dict[str, dict[str, str]] = {}
    nonstandard: list[Any] = []
    for raw in provenance_refs:
        if (
            isinstance(raw, Mapping)
            and set(raw) == {"chapter_id", "block_id"}
            and isinstance(raw.get("chapter_id"), str)
            and raw.get("chapter_id")
            and isinstance(raw.get("block_id"), str)
            and raw.get("block_id")
        ):
            row = {
                "chapter_id": str(raw["chapter_id"]),
                "block_id": str(raw["block_id"]),
            }
            standard.setdefault(canonical_json(row), row)
        else:
            nonstandard.append(deepcopy(raw))

    rows = sorted(
        standard.values(),
        key=lambda row: (
            _natural_identifier_sort_key(row["chapter_id"]),
            _natural_identifier_sort_key(row["block_id"]),
        ),
    )
    chapter_ids = sorted(
        {row["chapter_id"] for row in rows},
        key=_natural_identifier_sort_key,
    )
    summary: dict[str, Any] = {
        "source_ref_count": len(rows),
        "support_chapter_ids": chapter_ids,
        "first_block_id": rows[0]["block_id"] if rows else None,
        "last_block_id": rows[-1]["block_id"] if rows else None,
    }
    if nonstandard:
        # Unknown provenance shapes remain visible rather than being discarded.
        summary["nonstandard_refs"] = nonstandard
    return summary


def _natural_identifier_sort_key(value: str) -> tuple[tuple[int, Any], ...]:
    return tuple(
        (1, int(part)) if part.isdigit() else (0, part.casefold())
        for part in re.split(r"(\d+)", value)
        if part
    )


def shared_b1_enrich_request_v1(rendered: RenderedRegistryRequestV4) -> dict[str, Any]:
    schema = b1_enrich_response_schema_v1()
    if rendered.response_schema_hash != canonical_hash(schema):
        raise B1EnrichError("rendered B1-Enrich schema binding differs")
    return {
        "messages": [dict(row) for row in rendered.messages],
        "response_schema": schema,
        "request_fingerprint": rendered.request_fingerprint,
    }


def select_b1_enrich_prior_cards_v1(
    *,
    scan_artifact: Mapping[str, Any],
    prior_cards: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Project only B1-Scan-approved prior cards into the Enrich packet."""

    if isinstance(prior_cards, (str, bytes)) or not isinstance(
        prior_cards, Sequence
    ):
        raise B1EnrichError("prior_cards must be a sequence")
    card_by_id: dict[str, Mapping[str, Any]] = {}
    for raw in prior_cards:
        if not isinstance(raw, Mapping):
            raise B1EnrichError("prior card must be an object")
        card_id = _required_string(raw.get("prior_card_id"), "prior_card_id")
        if card_id in card_by_id:
            raise B1EnrichError("prior card ids are duplicated")
        card_by_id[card_id] = raw

    routes = scan_artifact.get("continuity_routes")
    if not isinstance(routes, list):
        raise B1EnrichError("scan continuity routes are absent or malformed")
    selected: list[dict[str, Any]] = []
    seen_route_ids: set[str] = set()
    for raw in routes:
        if not isinstance(raw, Mapping):
            raise B1EnrichError("scan continuity route must be an object")
        card_id = _required_string(raw.get("prior_card_id"), "prior_card_id")
        if card_id in seen_route_ids:
            raise B1EnrichError("scan continuity route ids are duplicated")
        seen_route_ids.add(card_id)
        if card_id not in card_by_id:
            raise B1EnrichError("scan continuity route cites a foreign prior card")
        action = _enum(
            raw.get("packet_action"),
            PRIOR_CARD_PACKET_ACTIONS,
            "packet_action",
        )
        hearing_required = raw.get("hearing_required")
        if not isinstance(hearing_required, bool):
            raise B1EnrichError("continuity route hearing_required must be boolean")
        if (action == "include_prior_card") == hearing_required:
            raise B1EnrichError(
                "continuity route packet action conflicts with hearing state"
            )
        if action == "include_prior_card":
            selected.append(deepcopy(dict(card_by_id[card_id])))
    return selected


def _continuity_surface_key(value: Any) -> str:
    return _normalized_surface(value)


def _prior_surface_keys(card: Mapping[str, Any]) -> set[str]:
    values = [card.get("canonical_surface"), *(card.get("stable_surfaces") or [])]
    return {
        key
        for value in values
        if value is not None and (key := _continuity_surface_key(value))
    }


def build_b1_enrich_continuity_context_v1(
    *,
    scan_artifact: Mapping[str, Any],
    prior_cards: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Prejoin Scan tasks with continuity without deciding literary identity.

    Exact normalized surface and source-block overlap are retrieval constraints,
    not identity authority. A Scan roster proposal may recover the current-side
    observation when its surface differs from the prior card's stable surfaces.
    """

    chapter_id = _required_string(scan_artifact.get("chapter_id"), "chapter_id")
    scan_hash = _required_string(scan_artifact.get("artifact_hash"), "artifact_hash")
    tasks, glossary_tasks = _scan_tasks(scan_artifact, chapter_id=chapter_id)
    raw_by_id = {
        _required_string(row.get("observation_id"), "observation_id"): row
        for row in scan_artifact.get("entity_observations") or []
        if isinstance(row, Mapping)
    }
    if len(raw_by_id) != len(tasks):
        raise B1EnrichError("scan task projection is not an exact cover")

    card_by_id: dict[str, dict[str, Any]] = {}
    for raw in prior_cards:
        if not isinstance(raw, Mapping):
            raise B1EnrichError("prior card must be an object")
        card = deepcopy(dict(raw))
        card_id = _required_string(card.get("prior_card_id"), "prior_card_id")
        if card_id in card_by_id:
            raise B1EnrichError("prior card ids are duplicated")
        if not _prior_surface_keys(card):
            raise B1EnrichError("prior card has no usable stable surface")
        card_by_id[card_id] = card

    routes = scan_artifact.get("continuity_routes")
    if routes is None:
        routes = []
    if not isinstance(routes, list):
        raise B1EnrichError("scan continuity routes are absent or malformed")

    roster_candidates_by_card: dict[str, set[str]] = {}
    for raw in scan_artifact.get("roster_recognition_proposals") or []:
        if not isinstance(raw, Mapping):
            raise B1EnrichError("scan roster proposal must be an object")
        card_id = _required_string(raw.get("prior_card_id"), "prior_card_id")
        if card_id not in card_by_id:
            raise B1EnrichError("scan roster proposal cites a foreign prior card")
        surface_key = _continuity_surface_key(raw.get("surface"))
        source_ids = set(
            _string_list(
                raw.get("source_block_ids"),
                "roster proposal source_block_ids",
                minimum=1,
                maximum=MAX_OBSERVATION_BLOCK_IDS,
            )
        )
        for task in tasks:
            observation_id = task["scan_observation_id"]
            observation = raw_by_id[observation_id]
            observation_blocks = set(
                observation.get("all_source_block_ids")
                or observation.get("source_block_ids")
                or []
            )
            if (
                _continuity_surface_key(task["surface"]) == surface_key
                and observation_blocks.intersection(source_ids)
            ):
                roster_candidates_by_card.setdefault(card_id, set()).add(
                    observation_id
                )

    route_rows: list[dict[str, Any]] = []
    seen_card_ids: set[str] = set()
    candidate_ids_by_card: dict[str, list[str]] = {}
    exact_candidate_ids_by_card: dict[str, list[str]] = {}
    for raw in routes:
        if not isinstance(raw, Mapping):
            raise B1EnrichError("scan continuity route must be an object")
        card_id = _required_string(raw.get("prior_card_id"), "prior_card_id")
        if card_id in seen_card_ids:
            raise B1EnrichError("scan continuity route ids are duplicated")
        seen_card_ids.add(card_id)
        card = card_by_id.get(card_id)
        if card is None:
            raise B1EnrichError("scan continuity route cites a foreign prior card")
        action = _enum(
            raw.get("packet_action"), PRIOR_CARD_PACKET_ACTIONS, "packet_action"
        )
        hearing = raw.get("hearing_required")
        if not isinstance(hearing, bool):
            raise B1EnrichError("continuity route hearing_required must be boolean")
        if (action == "include_prior_card") == hearing:
            raise B1EnrichError(
                "continuity route packet action conflicts with hearing state"
            )
        source_ids = _string_list(
            raw.get("source_block_ids"),
            "continuity source_block_ids",
            minimum=1,
            maximum=MAX_OBSERVATION_BLOCK_IDS,
        )
        stable_keys = _prior_surface_keys(card)
        exact_candidates: list[str] = []
        for task in tasks:
            observation_id = task["scan_observation_id"]
            observation = raw_by_id[observation_id]
            observation_blocks = set(
                observation.get("all_source_block_ids")
                or observation.get("source_block_ids")
                or []
            )
            if (
                _continuity_surface_key(task["surface"]) in stable_keys
                and observation_blocks.intersection(source_ids)
            ):
                exact_candidates.append(observation_id)
        exact_candidate_ids_by_card[card_id] = sorted(exact_candidates)
        candidates = exact_candidates or sorted(
            roster_candidates_by_card.get(card_id, set())
        )
        candidate_ids_by_card[card_id] = sorted(candidates)
        route = deepcopy(dict(raw))
        route["source_block_ids"] = source_ids
        route_rows.append(route)

    cards_by_observation: dict[str, list[str]] = {}
    for card_id, observation_ids in candidate_ids_by_card.items():
        for observation_id in observation_ids:
            cards_by_observation.setdefault(observation_id, []).append(card_id)

    cases: list[dict[str, Any]] = []
    case_by_id: dict[str, dict[str, Any]] = {}
    selected_prior_cards: list[dict[str, Any]] = []
    for route in sorted(route_rows, key=lambda row: str(row["prior_card_id"])):
        card_id = str(route["prior_card_id"])
        current_ids = candidate_ids_by_card[card_id]
        risks = set(route.get("mechanical_risk_codes") or [])
        if not exact_candidate_ids_by_card[card_id]:
            risks.add("no_exact_current_scan_observation")
        if len(current_ids) > 1:
            risks.add("multiple_current_scan_observations")
        if any(len(cards_by_observation.get(row, [])) > 1 for row in current_ids):
            risks.add("current_observation_matches_multiple_prior_cards")
        # Scan owns the continuity judgment. A referenced prior card with no
        # current observation is still carried, but cannot be represented as a
        # continuation onto an observation that does not exist.
        route_allows_continuity = (
            route.get("packet_action") == "include_prior_card"
            and route.get("hearing_required") is False
        )
        can_continue = route_allows_continuity and bool(current_ids)
        carry_referenced = route_allows_continuity and not current_ids
        if carry_referenced:
            risks.add("referenced_prior_without_current_observation")
        effective_action = (
            "include_prior_card"
            if can_continue
            else (
                "carry_referenced_prior_card"
                if carry_referenced
                else "withhold_prior_card"
            )
        )
        case_identity = {
            "chapter_id": chapter_id,
            "scan_artifact_hash": scan_hash,
            "prior_card_id": card_id,
            "current_scan_observation_ids": current_ids,
            "scan_verdict": _required_string(route.get("verdict"), "scan verdict"),
            "reason_code": _required_string(
                route.get("reason_code"), "continuity reason_code"
            ),
            "source_block_ids": list(route["source_block_ids"]),
        }
        case_id = "b1cont_" + canonical_hash(case_identity)[:20]
        case_body = {
            "continuity_case_id": case_id,
            **case_identity,
            "reason": _bounded_note(route.get("reason"), "continuity reason"),
            "packet_action": effective_action,
            "hearing_required": not (can_continue or carry_referenced),
            "mechanical_risk_codes": sorted(risks),
            "prior_card_snapshot": deepcopy(card_by_id[card_id]),
            "identity_authority_granted": False,
        }
        case_body["evidence_manifest_hash"] = canonical_hash(
            {
                "case_identity": case_identity,
                "prior_card_sha256": canonical_hash(card_by_id[card_id]),
                "reason": case_body["reason"],
                "mechanical_risk_codes": case_body["mechanical_risk_codes"],
            }
        )
        cases.append(case_body)
        case_by_id[case_id] = case_body
        if can_continue or carry_referenced:
            selected_prior_cards.append(deepcopy(card_by_id[card_id]))

    case_ids_by_observation: dict[str, list[str]] = {}
    for case in cases:
        for observation_id in case["current_scan_observation_ids"]:
            case_ids_by_observation.setdefault(observation_id, []).append(
                case["continuity_case_id"]
            )

    task_packets: list[dict[str, Any]] = []
    for task in tasks:
        observation_id = task["scan_observation_id"]
        case_ids = sorted(case_ids_by_observation.get(observation_id, []))
        linked_cases = [case_by_id[case_id] for case_id in case_ids]
        continued = [
            row for row in linked_cases if row["packet_action"] == "include_prior_card"
        ]
        if len(continued) == 1 and len(linked_cases) == 1:
            continuity = {
                "state": "continue_prior",
                "continuity_case_ids": case_ids,
                "continued_prior_card_id": continued[0]["prior_card_id"],
                "prior_card": deepcopy(continued[0]["prior_card_snapshot"]),
                "withheld_prior_card_ids": [],
                "marker": None,
            }
        elif linked_cases:
            continuity = {
                "state": "linkage_pending",
                "continuity_case_ids": case_ids,
                "continued_prior_card_id": None,
                "prior_card": None,
                "withheld_prior_card_ids": sorted(
                    {str(row["prior_card_id"]) for row in linked_cases}
                ),
                "marker": CONTINUITY_MARKER,
            }
        else:
            continuity = {
                "state": "new_candidate",
                "continuity_case_ids": [],
                "continued_prior_card_id": None,
                "prior_card": None,
                "withheld_prior_card_ids": [],
                "marker": None,
            }
        task_packets.append({**deepcopy(task), "continuity": continuity})

    body = {
        "schema_version": CONTINUITY_CONTEXT_SCHEMA_VERSION,
        "chapter_id": chapter_id,
        "scan_artifact_hash": scan_hash,
        "entity_task_packets": task_packets,
        "glossary_tasks": glossary_tasks,
        "continuity_cases": cases,
        "selected_prior_cards": sorted(
            selected_prior_cards, key=lambda row: str(row["prior_card_id"])
        ),
        "identity_authority_granted": False,
    }
    return {**body, "context_hash": canonical_hash(body)}


def verify_b1_enrich_continuity_context_v1(
    context: Mapping[str, Any],
) -> dict[str, Any]:
    if context.get("schema_version") != CONTINUITY_CONTEXT_SCHEMA_VERSION:
        raise B1EnrichError("foreign B1-Enrich continuity context schema")
    body = deepcopy(dict(context))
    observed = _required_string(body.pop("context_hash", None), "context_hash")
    if canonical_hash(body) != observed:
        raise B1EnrichError("B1-Enrich continuity context hash mismatch")
    if context.get("identity_authority_granted") is not False:
        raise B1EnrichError("continuity context grants identity authority")
    tasks = context.get("entity_task_packets")
    cases = context.get("continuity_cases")
    selected = context.get("selected_prior_cards")
    if (
        not isinstance(tasks, list)
        or not isinstance(cases, list)
        or not isinstance(selected, list)
    ):
        raise B1EnrichError("continuity context lists are malformed")
    task_ids = [
        _required_string(row.get("scan_observation_id"), "scan_observation_id")
        for row in tasks
        if isinstance(row, Mapping)
    ]
    if len(task_ids) != len(tasks) or len(task_ids) != len(set(task_ids)):
        raise B1EnrichError("continuity task ids are malformed or duplicated")
    case_ids = [
        _required_string(row.get("continuity_case_id"), "continuity_case_id")
        for row in cases
        if isinstance(row, Mapping)
    ]
    if len(case_ids) != len(cases) or len(case_ids) != len(set(case_ids)):
        raise B1EnrichError("continuity case ids are malformed or duplicated")
    known_cases = set(case_ids)
    known_tasks = set(task_ids)
    selected_by_id: dict[str, Mapping[str, Any]] = {}
    for row in selected:
        if not isinstance(row, Mapping):
            raise B1EnrichError("selected prior card is malformed")
        prior_id = _required_string(row.get("prior_card_id"), "prior_card_id")
        if prior_id in selected_by_id:
            raise B1EnrichError("selected prior cards duplicate prior_card_id")
        selected_by_id[prior_id] = row

    expected_selected: set[str] = set()
    for case in cases:
        if not isinstance(case, Mapping):
            raise B1EnrichError("continuity case is malformed")
        action = _enum(
            case.get("packet_action"),
            CONTINUITY_CASE_ACTIONS,
            "continuity packet_action",
        )
        current_ids = _string_list(
            case.get("current_scan_observation_ids"),
            "current_scan_observation_ids",
            minimum=0,
            maximum=max(1, len(task_ids)),
        )
        if not set(current_ids).issubset(known_tasks):
            raise B1EnrichError("continuity case cites a foreign scan observation")
        hearing_required = case.get("hearing_required")
        if not isinstance(hearing_required, bool):
            raise B1EnrichError("continuity hearing_required is malformed")
        prior_id = _required_string(case.get("prior_card_id"), "prior_card_id")
        prior_snapshot = case.get("prior_card_snapshot")
        if (
            not isinstance(prior_snapshot, Mapping)
            or prior_snapshot.get("prior_card_id") != prior_id
        ):
            raise B1EnrichError("continuity prior-card snapshot identity differs")
        if action == "include_prior_card":
            if not current_ids or hearing_required:
                raise B1EnrichError(
                    "included prior card lacks a current observation or still needs hearing"
                )
            expected_selected.add(prior_id)
        elif action == "carry_referenced_prior_card":
            if current_ids or hearing_required:
                raise B1EnrichError(
                    "referenced prior carry has an observation or still needs hearing"
                )
            expected_selected.add(prior_id)
        elif hearing_required is not True:
            raise B1EnrichError("withheld prior card does not require hearing")
    if set(selected_by_id) != expected_selected:
        raise B1EnrichError("selected prior cards differ from continuity cases")

    for task in tasks:
        continuity = task.get("continuity")
        if not isinstance(continuity, Mapping):
            raise B1EnrichError("entity task lacks continuity packet")
        state = _enum(
            continuity.get("state"), CONTINUITY_STATES, "continuity state"
        )
        cited = _string_list(
            continuity.get("continuity_case_ids"),
            "continuity_case_ids",
            minimum=0,
            maximum=max(1, len(case_ids)),
        )
        if not set(cited).issubset(known_cases):
            raise B1EnrichError("entity task cites a foreign continuity case")
        has_card = isinstance(continuity.get("prior_card"), Mapping)
        if (state == "continue_prior") != has_card:
            raise B1EnrichError("prior card exposure conflicts with continuity state")
        if state == "linkage_pending" and continuity.get("marker") != CONTINUITY_MARKER:
            raise B1EnrichError("pending continuity marker differs")
    return deepcopy(dict(context))


def validate_b1_enrich_response_v1(
    response: Mapping[str, Any],
    *,
    chapter: Mapping[str, Any],
    scan_artifact: Mapping[str, Any],
    request_fingerprint: str,
    continuity_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(response, Mapping):
        raise B1EnrichError("B1-Enrich response must be an object")
    expected = {
        "schema_id",
        "chapter_id",
        "entities",
        "additional_entities",
        "spurious_challenges",
        "same_referent_proposals",
        "conflict_findings",
        "presence_correction_findings",
        "glossary_items",
    }
    _exact_keys(response, expected, "B1-Enrich response")
    if response.get("schema_id") != OUTPUT_SCHEMA_ID:
        raise B1EnrichError("B1-Enrich schema_id differs")
    chapter_id = _required_string(chapter.get("chapter_id"), "chapter_id")
    response, normalization_notes = normalize_code_owned_response_echoes_v1(
        response,
        expected={"chapter_id": chapter_id},
    )
    if continuity_context is None:
        tasks, glossary_tasks = _scan_tasks(scan_artifact, chapter_id=chapter_id)
        continuity = build_b1_enrich_continuity_context_v1(
            scan_artifact=scan_artifact, prior_cards=[]
        )
    else:
        continuity = verify_b1_enrich_continuity_context_v1(continuity_context)
        if (
            continuity.get("chapter_id") != chapter_id
            or continuity.get("scan_artifact_hash") != scan_artifact.get("artifact_hash")
        ):
            raise B1EnrichError("continuity context lineage differs")
        tasks = deepcopy(continuity["entity_task_packets"])
        glossary_tasks = deepcopy(continuity["glossary_tasks"])
    task_by_id = {row["scan_observation_id"]: row for row in tasks}
    glossary_by_id = {row["term_observation_id"]: row for row in glossary_tasks}
    blocks = {row["block_id"]: row for row in _source_blocks(chapter)}
    issues: list[dict[str, Any]] = []
    content_field_quarantines: list[dict[str, Any]] = []

    entities = _validate_entity_rows(
        response.get("entities"),
        task_by_id=task_by_id,
        blocks=blocks,
        issues=issues,
        content_field_quarantines=content_field_quarantines,
    )
    challenges = _validate_challenges(
        response.get("spurious_challenges"), task_by_id=task_by_id, blocks=blocks, issues=issues
    )
    quarantined: list[dict[str, Any]] = []
    for task_id in sorted(task_by_id):
        if task_id in entities and task_id in challenges:
            entities.pop(task_id)
            challenges.pop(task_id)
            quarantined.append({"task_id": task_id, "reason": "both_enriched_and_challenged"})
        elif task_id not in entities and task_id not in challenges:
            quarantined.append({"task_id": task_id, "reason": "missing_valid_disposition"})

    additional = _validate_additional_entities(
        response.get("additional_entities"),
        blocks=blocks,
        issues=issues,
        content_field_quarantines=content_field_quarantines,
    )
    same_referent = _validate_same_referent_proposals(
        response.get("same_referent_proposals"),
        task_by_id=task_by_id,
        accepted_dossier_ids=set(entities),
        blocks=blocks,
        issues=issues,
    )
    glossary, glossary_quarantine = _validate_glossary_items(
        response.get("glossary_items"), glossary_by_id=glossary_by_id, blocks=blocks, issues=issues
    )
    conflicts = _validate_findings(
        response.get("conflict_findings"),
        kind="conflict",
        task_by_id=task_by_id,
        blocks=blocks,
        issues=issues,
    )
    presence = _validate_findings(
        response.get("presence_correction_findings"),
        kind="presence",
        task_by_id=task_by_id,
        blocks=blocks,
        issues=issues,
    )
    body = attach_response_normalization_notes_v1(
        {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "chapter_id": chapter_id,
        "request_fingerprint": _required_string(request_fingerprint, "request_fingerprint"),
        "scan_artifact_hash": _required_string(scan_artifact.get("artifact_hash"), "scan artifact_hash"),
        "continuity_context_hash": continuity["context_hash"],
        "continuity_cases": deepcopy(continuity["continuity_cases"]),
        "entity_dossiers": [entities[key] for key in sorted(entities)],
        "additional_entity_dossiers": additional,
        "spurious_challenges": [challenges[key] for key in sorted(challenges)],
        "same_referent_proposals": same_referent,
        "conflict_findings": conflicts,
        "presence_correction_findings": presence,
        "glossary_items": glossary,
        "quarantined_tasks": quarantined + glossary_quarantine,
        "review_issues": issues,
        "content_field_quarantines": content_field_quarantines,
        "identity_authority_granted": False,
        "registry_mutation_performed": False,
        },
        normalization_notes,
    )
    body["metrics"] = {
        "enriched_entity_count": len(body["entity_dossiers"]),
        "additional_entity_count": len(additional),
        "spurious_challenge_count": len(body["spurious_challenges"]),
        "same_referent_proposal_count": len(same_referent),
        "glossary_item_count": len(glossary),
        "finding_count": len(conflicts) + len(presence),
        "quarantined_task_count": len(body["quarantined_tasks"]),
        "review_issue_count": len(issues),
        "content_field_quarantine_count": len(content_field_quarantines),
    }
    return {**body, "artifact_hash": canonical_hash(body)}


def make_b1_enrich_semantic_validator_v1(
    *,
    chapter: Mapping[str, Any],
    scan_artifact: Mapping[str, Any],
    rendered: RenderedRegistryRequestV4,
    continuity_context: Mapping[str, Any] | None = None,
) -> Callable[[Mapping[str, Any]], Mapping[str, Any]]:
    def validate(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return validate_b1_enrich_response_v1(
            payload,
            chapter=chapter,
            scan_artifact=scan_artifact,
            request_fingerprint=rendered.request_fingerprint,
            continuity_context=continuity_context,
        )

    return validate


def validate_b1_enrich_capability_payload_v1(
    payload: Mapping[str, Any],
    *,
    chapter: Mapping[str, Any],
    scan_artifact: Mapping[str, Any],
    request_fingerprint: str,
) -> Mapping[str, Any]:
    """Require a fully clean synthetic result before qualifying a source."""

    artifact = validate_b1_enrich_response_v1(
        payload,
        chapter=chapter,
        scan_artifact=scan_artifact,
        request_fingerprint=request_fingerprint,
    )
    if artifact["review_issues"] or artifact["quarantined_tasks"]:
        raise B1EnrichError("capability probe payload contains semantic quarantine")
    if len(artifact["same_referent_proposals"]) != 1:
        raise B1EnrichError(
            "capability probe payload must exercise one same-referent proposal"
        )
    return artifact


def _scan_tasks(
    scan_artifact: Mapping[str, Any], *, chapter_id: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if scan_artifact.get("chapter_id") != chapter_id:
        raise B1EnrichError("scan artifact chapter differs")
    if not isinstance(scan_artifact.get("artifact_hash"), str):
        raise B1EnrichError("scan artifact hash is absent")
    entities = scan_artifact.get("entity_observations")
    glossary = scan_artifact.get("glossary_observations")
    if not isinstance(entities, list) or not isinstance(glossary, list):
        raise B1EnrichError("scan artifact task lists are malformed")
    tasks: list[dict[str, Any]] = []
    for raw in entities:
        if not isinstance(raw, Mapping):
            raise B1EnrichError("scan entity task is malformed")
        task = {
            "task_ref": f"scan:{_required_string(raw.get('observation_id'), 'observation_id')}",
            "scan_observation_id": _required_string(raw.get("observation_id"), "observation_id"),
            "surface": _required_string(raw.get("surface"), "surface"),
            "source_block_ids": _string_list(
                raw.get("source_block_ids"),
                "source_block_ids",
                minimum=1,
                maximum=MAX_OBSERVATION_BLOCK_IDS,
            ),
            "referent_kind_claim": _enum(raw.get("referent_kind_claim"), REFERENT_KINDS, "referent_kind_claim"),
            "record_class": _required_string(raw.get("record_class"), "record_class"),
            "presence_basis": _enum(raw.get("presence_basis"), PRESENCE_BASES, "presence_basis"),
            "scan_note": _bounded_note(raw.get("scan_note"), "scan_note"),
        }
        tasks.append(task)
    terms: list[dict[str, Any]] = []
    for raw in glossary:
        if not isinstance(raw, Mapping):
            raise B1EnrichError("scan glossary task is malformed")
        category_hint = _enum(
            raw.get("category_hint"),
            TERM_CATEGORIES,
            "category_hint",
        )
        term_category_raw = _nullable_note(raw.get("term_category_raw"))
        term_category_status = raw.get("term_category_status")
        if term_category_status not in {
            "in_vocabulary",
            "model_other",
            "quarantined_invalid_enum",
        }:
            term_category_status = (
                "model_other"
                if category_hint == "other" and term_category_raw is not None
                else (
                    "quarantined_invalid_enum"
                    if category_hint == "other"
                    else "in_vocabulary"
                )
            )
        terms.append(
            {
                "term_observation_id": _required_string(raw.get("term_observation_id"), "term_observation_id"),
                "surface": _required_string(raw.get("surface"), "term surface"),
                "source_block_ids": _string_list(
                    raw.get("source_block_ids"),
                    "term blocks",
                    minimum=1,
                    maximum=MAX_OBSERVATION_BLOCK_IDS,
                ),
                "category_hint": category_hint,
                "term_category_raw": term_category_raw,
                "term_category_status": term_category_status,
            }
        )
    if len(tasks) != len({row["scan_observation_id"] for row in tasks}):
        raise B1EnrichError("scan observation ids are duplicated")
    if len(terms) != len({row["term_observation_id"] for row in terms}):
        raise B1EnrichError("scan term ids are duplicated")
    return tasks, terms


def _validate_entity_rows(
    value: Any,
    *,
    task_by_id,
    blocks,
    issues,
    content_field_quarantines,
) -> dict[str, dict[str, Any]]:
    if not isinstance(value, list):
        raise B1EnrichError("entities must be a list")
    accepted: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(value):
        try:
            if not isinstance(raw, Mapping):
                raise B1EnrichError("entity dossier must be an object")
            scan_id = _required_string(raw.get("scan_observation_id"), "scan_observation_id")
            if scan_id not in task_by_id or scan_id in accepted:
                raise B1EnrichError("entity dossier cites a foreign or duplicate scan id")
            accepted[scan_id] = _validated_dossier(
                raw,
                task=task_by_id[scan_id],
                blocks=blocks,
                include_scan_id=True,
                issues=issues,
                content_field_quarantines=content_field_quarantines,
                issue_prefix=f"entity:{scan_id}",
            )
        except (B1EnrichError, ValueError) as exc:
            issues.append(_issue("entity", index, exc, raw))
    return accepted


def _validated_dossier(
    raw: Mapping[str, Any],
    *,
    task,
    blocks,
    include_scan_id: bool,
    issues,
    content_field_quarantines,
    issue_prefix: str,
) -> dict[str, Any]:
    required, _properties = _dossier_properties(include_scan_id=include_scan_id)
    _exact_keys(raw, set(required), "entity dossier")
    kind = task["referent_kind_claim"]
    claims = _validate_claims(raw.get("claims"), kind=kind, blocks=blocks)
    result = {
        "claims": claims,
        "kinship_links": _validate_links(
            raw.get("kinship_links"),
            KINSHIP_RELATIONS,
            blocks,
            note_field=True,
            open_relation=None,
            issues=issues,
            content_field_quarantines=content_field_quarantines,
            issue_prefix=f"{issue_prefix}.kinship_link",
        ),
        "links": _validate_links(
            raw.get("links"),
            LINK_RELATIONS,
            blocks,
            note_field=True,
            open_relation="other_link",
            issues=issues,
            content_field_quarantines=content_field_quarantines,
            issue_prefix=f"{issue_prefix}.link",
        ),
        "address_forms_used": _validate_address_forms(
            raw.get("address_forms_used"),
            blocks,
            issues=issues,
            issue_prefix=f"{issue_prefix}.address_form",
        ),
        "aliases_observed": _validate_aliases(
            raw.get("aliases_observed"),
            blocks,
            issues=issues,
            issue_prefix=f"{issue_prefix}.alias",
        ),
        "identity_summary": _summary(raw.get("identity_summary")),
        "distinguishing_note": _nullable_note(raw.get("distinguishing_note")),
        "authority_scope": "chapter_provisional",
    }
    if include_scan_id:
        result["scan_observation_id"] = task["scan_observation_id"]
        result["task_ref"] = task["task_ref"]
        result["surface"] = task["surface"]
        result["referent_kind_claim"] = kind
        result["continuity"] = deepcopy(
            task.get("continuity")
            or {
                "state": "new_candidate",
                "continuity_case_ids": [],
                "continued_prior_card_id": None,
                "prior_card": None,
                "withheld_prior_card_ids": [],
                "marker": None,
            }
        )
    return result


def _validate_claims(value: Any, *, kind: str, blocks) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise B1EnrichError("claims must be a list")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, Mapping):
            raise B1EnrichError("claim must be an object")
        _exact_keys(raw, {"field", "status", "value", "basis", "anchor_block_ids", "story_time_note"}, "claim")
        field = _enum(raw.get("field"), CLAIM_FIELDS, "claim field")
        if field in seen:
            raise B1EnrichError("claim field is duplicated")
        seen.add(field)
        status = _enum(raw.get("status"), CLAIM_STATUSES, "claim status")
        value_text = raw.get("value")
        basis = raw.get("basis")
        anchors = raw.get("anchor_block_ids")
        if status == "supported":
            value_text = _required_string(value_text, "claim value")
            basis = _enum(basis, CLAIM_BASES - {"not_applicable"}, "claim basis")
            anchors = _known_blocks(anchors, blocks, minimum=1)
        elif status == "unclear":
            if value_text is not None or basis is not None:
                raise B1EnrichError("unclear claim must have null value and basis")
            anchors = _known_blocks(anchors, blocks, minimum=1)
        else:
            if value_text is not None or basis != "not_applicable" or anchors != []:
                raise B1EnrichError("not_applicable claim payload is inconsistent")
        applicable_kinds = APPLICABLE_KINDS_BY_FIELD.get(field)
        if (
            applicable_kinds is not None
            and kind not in applicable_kinds
            and status != "not_applicable"
        ):
            raise B1EnrichError(f"{field} is inapplicable to this referent kind")
        if field == "gender":
            if status == "supported" and value_text not in REFERENTIAL_GENDERS:
                raise B1EnrichError("gender value is unsupported")
        if field == "life_stage" and status == "supported" and value_text not in LIFE_STAGES:
            raise B1EnrichError("life_stage value is unsupported")
        if field == "referent_kind" and status == "supported" and value_text not in REFERENT_KINDS:
            raise B1EnrichError("referent_kind value is unsupported")
        rows.append(
            {
                "field": field,
                "status": status,
                "value": value_text,
                "basis": basis,
                "anchor_block_ids": anchors,
                "story_time_note": _nullable_note(raw.get("story_time_note")),
                "semantic_status": "unreviewed",
            }
        )
    required = {"gender", "life_stage"} if kind == "person" else set()
    type_field = TYPE_FIELD_BY_KIND.get(kind)
    if type_field:
        required.add(type_field)
    if not required.issubset(seen):
        raise B1EnrichError(f"required field checks are missing: {sorted(required - seen)}")
    return rows


def _validate_links(
    value,
    relations,
    blocks,
    *,
    note_field,
    open_relation,
    issues,
    content_field_quarantines,
    issue_prefix,
):
    if not isinstance(value, list):
        raise B1EnrichError("links must be a list")
    rows = []
    for index, raw in enumerate(value):
        try:
            if not isinstance(raw, Mapping):
                raise B1EnrichError("link must be an object")
            keys = {"relation", "target_ref", "basis", "anchor_block_ids"}
            if note_field:
                keys.add("relation_note")
            _exact_keys(raw, keys, "link")
            raw_relation = _required_string(raw.get("relation"), "link relation")
            note = _nullable_note(raw.get("relation_note")) if note_field else None
            target_ref = _required_string(raw.get("target_ref"), "target_ref")
            basis = _enum(
                raw.get("basis"),
                CLAIM_BASES - {"not_applicable"},
                "link basis",
            )
            anchor_block_ids = _known_blocks(
                raw.get("anchor_block_ids"),
                blocks,
            )

            relation = raw_relation
            relation_raw = None
            relation_status = "in_vocabulary"
            quarantine_reason = None
            if open_relation is None:
                relation = _enum(raw_relation, relations, "link relation")
                if note_field and relation == "other_kin" and note is None:
                    raise B1EnrichError("other_kin requires relation_note")
            elif raw_relation == open_relation:
                relation_raw = note
                if note is None:
                    relation_status = "quarantined_invalid_enum"
                    quarantine_reason = f"{open_relation}_missing_relation_note"
                else:
                    relation_status = "model_other"
            elif raw_relation not in relations:
                relation = open_relation
                relation_raw = raw_relation
                relation_status = "quarantined_invalid_enum"
                quarantine_reason = "unsupported_link_relation"

            if quarantine_reason is not None:
                content_field_quarantines.append(
                    {
                        "row_type": issue_prefix,
                        "row_index": index,
                        "field": "relation",
                        "quarantine_reason": quarantine_reason,
                        "raw_value": raw_relation,
                        "target_ref": target_ref,
                        "anchor_block_ids": anchor_block_ids,
                        "raw_row_sha256": canonical_hash(raw),
                    }
                )
            rows.append(
                {
                    "relation": relation,
                    "target_ref": target_ref,
                    "basis": basis,
                    "anchor_block_ids": anchor_block_ids,
                    **({"relation_note": note} if note_field else {}),
                    **(
                        {
                            "relation_raw": relation_raw,
                            "relation_status": relation_status,
                        }
                        if open_relation is not None
                        else {}
                    ),
                    "validity_scope": "as_of_chapter",
                    "semantic_status": "unreviewed",
                }
            )
        except (B1EnrichError, ValueError) as exc:
            issues.append(_issue(issue_prefix, index, exc, raw))
    return rows


def _validate_address_forms(value, blocks, *, issues, issue_prefix):
    if not isinstance(value, list):
        raise B1EnrichError("address_forms_used must be a list")
    rows = []
    for index, raw in enumerate(value):
        try:
            if not isinstance(raw, Mapping):
                raise B1EnrichError("address form must be an object")
            _exact_keys(raw, {"counterpart_ref", "mode", "form", "anchor_block_ids"}, "address form")
            form = _required_string(raw.get("form"), "address form")
            anchors = _known_blocks(raw.get("anchor_block_ids"), blocks)
            anchors = _retain_verbatim_anchors(
                anchors,
                surface=form,
                blocks=blocks,
                issues=issues,
                issue_prefix=issue_prefix,
                row_index=index,
                raw=raw,
            )
            rows.append(
                {
                    "counterpart_ref": _required_string(raw.get("counterpart_ref"), "counterpart_ref"),
                    "mode": _enum(raw.get("mode"), ADDRESS_MODES, "address mode"),
                    "form": form,
                    "anchor_block_ids": anchors,
                    "alias_authority": False,
                }
            )
        except (B1EnrichError, ValueError) as exc:
            issues.append(_issue(issue_prefix, index, exc, raw))
    return rows


def _validate_aliases(value, blocks, *, issues, issue_prefix):
    if not isinstance(value, list):
        raise B1EnrichError("aliases_observed must be a list")
    rows = []
    for index, raw in enumerate(value):
        try:
            if not isinstance(raw, Mapping):
                raise B1EnrichError("alias observation must be an object")
            _exact_keys(raw, {"surface", "anchor_block_ids"}, "alias observation")
            surface = _required_string(raw.get("surface"), "alias surface")
            anchors = _known_blocks(raw.get("anchor_block_ids"), blocks)
            anchors = _retain_verbatim_anchors(
                anchors,
                surface=surface,
                blocks=blocks,
                issues=issues,
                issue_prefix=issue_prefix,
                row_index=index,
                raw=raw,
            )
            rows.append({"surface": surface, "anchor_block_ids": anchors, "status": "proposed"})
        except (B1EnrichError, ValueError) as exc:
            issues.append(_issue(issue_prefix, index, exc, raw))
    return rows


def _validate_challenges(value, *, task_by_id, blocks, issues):
    if not isinstance(value, list):
        raise B1EnrichError("spurious_challenges must be a list")
    rows = {}
    for index, raw in enumerate(value):
        try:
            if not isinstance(raw, Mapping):
                raise B1EnrichError("spurious challenge must be an object")
            _exact_keys(raw, {"scan_observation_id", "reason", "source_block_ids"}, "spurious challenge")
            scan_id = _required_string(raw.get("scan_observation_id"), "scan_observation_id")
            if scan_id not in task_by_id or scan_id in rows:
                raise B1EnrichError("spurious challenge cites a foreign or duplicate scan id")
            rows[scan_id] = {
                "scan_observation_id": scan_id,
                "reason": _bounded_note(raw.get("reason"), "challenge reason"),
                "source_block_ids": _known_blocks(raw.get("source_block_ids"), blocks),
                "requires_local_auditor": True,
            }
        except (B1EnrichError, ValueError) as exc:
            issues.append(_issue("spurious_challenge", index, exc, raw))
    return rows


def _validate_additional_entities(
    value, *, blocks, issues, content_field_quarantines
):
    if not isinstance(value, list):
        raise B1EnrichError("additional_entities must be a list")
    rows = []
    for index, raw in enumerate(value):
        try:
            if not isinstance(raw, Mapping):
                raise B1EnrichError("additional entity must be an object")
            required, _properties = _dossier_properties(include_scan_id=False)
            _exact_keys(raw, set(required), "additional entity")
            surface = _required_string(raw.get("surface"), "additional surface")
            declared_source_ids = _known_blocks(raw.get("source_block_ids"), blocks)
            source_ids = _retain_verbatim_anchors(
                declared_source_ids,
                surface=surface,
                blocks=blocks,
                issues=issues,
                issue_prefix="additional_entity.source_block_ids",
                row_index=index,
                raw=raw,
            )
            kind = _enum(raw.get("referent_kind_claim"), REFERENT_KINDS, "additional referent_kind")
            task = {"referent_kind_claim": kind}
            dossier = _validated_dossier(
                raw,
                task=task,
                blocks=blocks,
                include_scan_id=False,
                issues=issues,
                content_field_quarantines=content_field_quarantines,
                issue_prefix=f"additional_entity:{index}",
            )
            body = {
                "surface": surface,
                "source_block_ids": source_ids,
                "referent_kind_claim": kind,
                **dossier,
            }
            body["additional_entity_id"] = f"b1add_{canonical_hash(body)[:16]}"
            rows.append(body)
        except (B1EnrichError, ValueError) as exc:
            issues.append(_issue("additional_entity", index, exc, raw))
    return rows


def _validate_same_referent_proposals(
    value, *, task_by_id, accepted_dossier_ids, blocks, issues
):
    if not isinstance(value, list):
        raise B1EnrichError("same_referent_proposals must be a list")
    if len(value) > MAX_SAME_REFERENT_PROPOSALS:
        raise B1EnrichError("same_referent_proposals exceeds the chapter cap")
    rows = []
    seen_pairs: set[tuple[str, str]] = set()
    for index, raw in enumerate(value):
        try:
            if not isinstance(raw, Mapping):
                raise B1EnrichError("same-referent proposal must be an object")
            _exact_keys(
                raw,
                {
                    "subject_ref",
                    "target_ref",
                    "proposal_basis",
                    "source_block_ids",
                    "reason",
                },
                "same-referent proposal",
            )
            subject_ref = _required_string(raw.get("subject_ref"), "subject_ref")
            target_ref = _required_string(raw.get("target_ref"), "target_ref")
            if not subject_ref.startswith("scan:") or not target_ref.startswith("scan:"):
                raise B1EnrichError(
                    "same-referent proposal refs must be scan task refs"
                )
            subject_id = subject_ref.removeprefix("scan:")
            target_id = target_ref.removeprefix("scan:")
            if subject_id == target_id:
                raise B1EnrichError("same-referent proposal is reflexive")
            if subject_id not in task_by_id or target_id not in task_by_id:
                raise B1EnrichError("same-referent proposal cites a foreign scan id")
            if (
                subject_id not in accepted_dossier_ids
                or target_id not in accepted_dossier_ids
            ):
                raise B1EnrichError(
                    "same-referent proposal requires accepted subject and target dossiers"
                )
            pair = tuple(sorted((subject_id, target_id)))
            if pair in seen_pairs:
                raise B1EnrichError("same-referent proposal duplicates a pair")
            subject = task_by_id[subject_id]
            target = task_by_id[target_id]
            if subject.get("record_class") != "important_unnamed_referent":
                raise B1EnrichError(
                    "Enrich same-referent subject must be an unnamed observation"
                )
            if target.get("record_class") != "named_entity_candidate":
                raise B1EnrichError(
                    "Enrich same-referent target must be a named observation"
                )
            subject_kind = subject.get("referent_kind_claim")
            target_kind = target.get("referent_kind_claim")
            if (
                subject_kind != target_kind
                and "unknown" not in {subject_kind, target_kind}
            ):
                raise B1EnrichError("same-referent proposal crosses referent kinds")
            source_ids = _known_blocks(
                raw.get("source_block_ids"), blocks, maximum=8
            )
            if not set(source_ids).intersection(subject["source_block_ids"]):
                raise B1EnrichError(
                    "same-referent proposal cites no subject evidence"
                )
            if not set(source_ids).intersection(target["source_block_ids"]):
                raise B1EnrichError(
                    "same-referent proposal cites no target evidence"
                )
            seen_pairs.add(pair)
            rows.append(
                {
                    "subject_ref": subject_ref,
                    "target_ref": target_ref,
                    "proposal_basis": _enum(
                        raw.get("proposal_basis"),
                        SAME_REFERENT_PROPOSAL_BASES,
                        "same-referent proposal basis",
                    ),
                    "source_block_ids": source_ids,
                    "reason": _bounded_note(
                        raw.get("reason"), "same-referent proposal reason"
                    ),
                    "retrieval_surface_policy": "subject_evidence_only",
                    "requires_local_auditor": True,
                    "identity_authority_granted": False,
                }
            )
        except (B1EnrichError, ValueError) as exc:
            issues.append(_issue("same_referent_proposal", index, exc, raw))
    return rows


def _validate_glossary_items(value, *, glossary_by_id, blocks, issues):
    if not isinstance(value, list):
        raise B1EnrichError("glossary_items must be a list")
    rows = {}
    for index, raw in enumerate(value):
        try:
            if not isinstance(raw, Mapping):
                raise B1EnrichError("glossary item must be an object")
            _exact_keys(raw, {"term_observation_id", "contextual_sense", "ambiguity_status", "source_block_ids"}, "glossary item")
            term_id = _required_string(raw.get("term_observation_id"), "term_observation_id")
            if term_id not in glossary_by_id or term_id in rows:
                raise B1EnrichError("glossary item cites a foreign or duplicate term id")
            rows[term_id] = {
                "term_observation_id": term_id,
                "surface": glossary_by_id[term_id]["surface"],
                "contextual_sense": _bounded_note(raw.get("contextual_sense"), "contextual_sense"),
                "ambiguity_status": _enum(raw.get("ambiguity_status"), GLOSSARY_AMBIGUITY, "ambiguity_status"),
                "source_block_ids": _known_blocks(raw.get("source_block_ids"), blocks),
                "translation_authority_granted": False,
            }
        except (B1EnrichError, ValueError) as exc:
            issues.append(_issue("glossary_item", index, exc, raw))
    quarantine = [
        {"task_id": term_id, "reason": "missing_valid_glossary_disposition"}
        for term_id in sorted(set(glossary_by_id) - set(rows))
    ]
    return [rows[key] for key in sorted(rows)], quarantine


def _validate_findings(value, *, kind, task_by_id, blocks, issues):
    if not isinstance(value, list):
        raise B1EnrichError(f"{kind} findings must be a list")
    rows = []
    for index, raw in enumerate(value):
        try:
            if not isinstance(raw, Mapping):
                raise B1EnrichError("finding must be an object")
            scan_id = _required_string(raw.get("scan_observation_id"), "scan_observation_id")
            if scan_id not in task_by_id:
                raise B1EnrichError("finding cites a foreign scan id")
            if kind == "conflict":
                _exact_keys(raw, {"scan_observation_id", "field", "existing_value", "observed_value", "source_block_ids", "reason"}, "conflict finding")
                row = {
                    "scan_observation_id": scan_id,
                    "field": _enum(raw.get("field"), CLAIM_FIELDS, "conflict field"),
                    "existing_value": _required_string(raw.get("existing_value"), "existing_value"),
                    "observed_value": _required_string(raw.get("observed_value"), "observed_value"),
                    "source_block_ids": _known_blocks(raw.get("source_block_ids"), blocks),
                    "reason": _bounded_note(raw.get("reason"), "conflict reason"),
                    "requires_hearing": True,
                }
                continuity = task_by_id[scan_id].get("continuity") or {}
                row["prior_card_id"] = continuity.get(
                    "continued_prior_card_id"
                )
                row["continuity_case_ids"] = list(
                    continuity.get("continuity_case_ids") or []
                )
            else:
                _exact_keys(raw, {"scan_observation_id", "proposed_presence_basis", "source_block_ids", "reason"}, "presence finding")
                row = {
                    "scan_observation_id": scan_id,
                    "proposed_presence_basis": _enum(raw.get("proposed_presence_basis"), PRESENCE_BASES, "proposed presence"),
                    "source_block_ids": _known_blocks(raw.get("source_block_ids"), blocks),
                    "reason": _bounded_note(raw.get("reason"), "presence reason"),
                    "requires_local_auditor": True,
                }
            rows.append(row)
        except (B1EnrichError, ValueError) as exc:
            issues.append(_issue(f"{kind}_finding", index, exc, raw))
    return rows


def _known_blocks(value, blocks, *, minimum=1, maximum=4):
    rows = _string_list(
        value, "anchor_block_ids", minimum=minimum, maximum=maximum
    )
    if any(block_id not in blocks for block_id in rows):
        raise B1EnrichError("anchor_block_ids cites a foreign block")
    return rows


def _retain_verbatim_anchors(
    anchors, *, surface, blocks, issues, issue_prefix, row_index, raw
):
    retained = [block_id for block_id in anchors if surface in blocks[block_id]["text"]]
    dropped = [block_id for block_id in anchors if block_id not in retained]
    if dropped:
        issues.append(
            _issue(
                issue_prefix,
                row_index,
                B1EnrichError(
                    f"removed non-verbatim anchor blocks: {', '.join(dropped)}"
                ),
                raw,
            )
        )
    if not retained:
        raise B1EnrichError("no declared anchor block contains the exact surface")
    return retained


def _summary(value: Any) -> str:
    text = _required_string(value, "identity_summary")
    if len(text) > 420 or "\n" in text:
        raise B1EnrichError("identity_summary exceeds the compact limit")
    return text


def _nullable_note(value: Any) -> str | None:
    if value is None:
        return None
    return _bounded_note(value, "optional note")


def _issue(row_type: str, index: int, exc: Exception, raw: Any) -> dict[str, Any]:
    return {
        "row_type": row_type,
        "row_index": index,
        "reason": str(exc),
        "raw_row": deepcopy(raw),
    }


__all__ = [
    "ARTIFACT_SCHEMA_VERSION",
    "B1EnrichError",
    "OUTPUT_SCHEMA_ID",
    "PROMPT_ID",
    "b1_enrich_response_schema_v1",
    "build_b1_enrich_continuity_context_v1",
    "make_b1_enrich_semantic_validator_v1",
    "render_b1_enrich_request_v1",
    "select_b1_enrich_prior_cards_v1",
    "shared_b1_enrich_request_v1",
    "validate_b1_enrich_capability_payload_v1",
    "validate_b1_enrich_response_v1",
    "verify_b1_enrich_continuity_context_v1",
]
