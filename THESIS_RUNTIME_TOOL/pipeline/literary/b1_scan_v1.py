"""Lightweight chapter entity scan with bounded prior-card continuity hints.

The model observes source evidence and proposes continuity. Code validates
addresses, mints observation ids, and routes uncertainty; it never decides
literary identity.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path
import re
import unicodedata
from typing import Any, Callable, Collection, Iterable, Mapping, Sequence

from pipeline.literary.builder_pilot import load_system_prompt_from_design
from pipeline.literary.chapter_registry_schema_v4 import RenderedRegistryRequestV4
from pipeline.literary.checkpoint import canonical_hash, canonical_json
from pipeline.literary.response_normalization_v1 import (
    attach_response_normalization_notes_v1,
    normalize_code_owned_response_echoes_v1,
)
from pipeline.literary.request_token_preflight_v1 import (
    measure_literary_request_token_preflight_v1,
)


PROMPT_ID = "literary_b1_scan_v1_5"
REQUEST_SCHEMA_VERSION = "literary_b1_scan_request_v1"
OUTPUT_SCHEMA_ID = "LiteraryB1ScanOutputV1"
ARTIFACT_SCHEMA_VERSION = "literary_b1_scan_artifact_v1"
# Structural upper bound for surface-matched packets.  The model-facing
# memory allocator still applies the typed 12k budget before rendering.
MAX_PRIOR_CARDS = 256
MAX_PRIOR_PROFILE_CLAIMS = 256
MAX_MODEL_PROFILE_CLAIMS = 32
MAX_HIT_BLOCK_IDS = 8
# Shared evidence cap for entity, glossary, continuity, and roster rows.  A row
# citing more support than this is trimmed mechanically; it is never dropped,
# because over-citing is a bounding concern, not an error in the judgment.
MAX_OBSERVATION_BLOCK_IDS = 8
MAX_ROSTER_ROWS = 400
MAX_ROSTER_PROPOSALS = 8
MECHANICAL_LEADING_RETRIEVAL_TOKENS = frozenset(
    {"a", "an", "mr", "mrs", "miss", "dr", "the"}
)
NAMED_PRIOR_RECORD_CLASSES = frozenset(
    {"confirmed_entity", "unresolved_named_reference"}
)
OUTER_RETRIEVAL_PUNCTUATION = " \t\r\n.,;:!?\"'()[]{}“”‘’«»‹›"

REFERENT_KINDS = frozenset(
    {
        "person",
        "animal",
        "nonhuman_character",
        "group_reference",
        "place",
        "object",
        "institution",
        "named_text",
        "unknown",
    }
)
RECORD_CLASSES = frozenset(
    {
        "named_entity_candidate",
        "important_unnamed_referent",
        "unresolved_named_reference",
    }
)
PRESENCE_BASES = frozenset(
    {
        "direct_presence",
        "referenced_by_other",
        "reported_only",
        "inscription_or_document",
        "unclear",
    }
)
TERM_CATEGORIES = frozenset(
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
CONTINUITY_VERDICTS = frozenset(
    {"propose_continue", "propose_distinct", "uncertain"}
)
CONTINUITY_REASON_CODES = frozenset(
    {
        "consistent_current_reference",
        "hard_identity_contradiction",
        "same_surface_multiple_referents",
        "prior_reference_not_established_entity",
        "current_reference_not_established_entity",
        "temporal_change_possible",
        "insufficient_evidence",
        "other",
    }
)
PRIOR_RECORD_CLASSES = frozenset(
    {
        "confirmed_entity",
        "important_unnamed_referent",
        "unresolved_named_reference",
    }
)
PRIOR_CLAIM_STATES = frozenset({"confirmed", "provisional", "disputed"})


class B1ScanError(ValueError):
    pass


def b1_scan_response_schema_v1() -> dict[str, Any]:
    string = {"type": "string", "minLength": 1}
    block_ids = {
        "type": "array",
        "items": string,
        "minItems": 1,
        "maxItems": MAX_OBSERVATION_BLOCK_IDS,
        "uniqueItems": True,
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_id",
            "chapter_id",
            "entity_observations",
            "glossary_observations",
            "prior_continuity_proposals",
        ],
        "properties": {
            "schema_id": {"type": "string", "enum": [OUTPUT_SCHEMA_ID]},
            "chapter_id": string,
            "entity_observations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "surface",
                        "source_block_ids",
                        "referent_kind_claim",
                        "record_class",
                        "presence_basis",
                        "scan_note",
                    ],
                    "properties": {
                        "surface": string,
                        "source_block_ids": block_ids,
                        "referent_kind_claim": {
                            "type": "string",
                            "enum": sorted(REFERENT_KINDS),
                        },
                        "record_class": {
                            "type": "string",
                            "enum": sorted(RECORD_CLASSES),
                        },
                        "presence_basis": {
                            "type": "string",
                            "enum": sorted(PRESENCE_BASES),
                        },
                        "scan_note": string,
                    },
                },
            },
            "glossary_observations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "surface",
                        "source_block_ids",
                        "category_hint",
                        "term_category_raw",
                    ],
                    "properties": {
                        "surface": string,
                        "source_block_ids": block_ids,
                        "category_hint": {
                            "type": "string",
                            "enum": sorted(TERM_CATEGORIES),
                        },
                        "term_category_raw": {
                            "anyOf": [
                                {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 120,
                                    "pattern": "^[^\\r\\n]+$",
                                },
                                {"type": "null"},
                            ]
                        },
                    },
                },
            },
            "prior_continuity_proposals": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "prior_card_id",
                        "verdict",
                        "reason_code",
                        "source_block_ids",
                        "reason",
                    ],
                    "properties": {
                        "prior_card_id": string,
                        "verdict": {
                            "type": "string",
                            "enum": sorted(CONTINUITY_VERDICTS),
                        },
                        "reason_code": {
                            "type": "string",
                            "enum": sorted(CONTINUITY_REASON_CODES),
                        },
                        "source_block_ids": block_ids,
                        "reason": string,
                    },
                },
            },
            "roster_recognition_proposals": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "surface",
                        "prior_card_id",
                        "source_block_ids",
                        "reason",
                    ],
                    "properties": {
                        "surface": string,
                        "prior_card_id": string,
                        "source_block_ids": block_ids,
                        "reason": string,
                    },
                },
            },
        },
    }


def render_b1_scan_request_v1(
    *,
    chapter: Mapping[str, Any],
    design_doc: Path,
    prior_cards: Sequence[Mapping[str, Any]] | None = None,
    previous_chapter_summary: str | None = None,
    global_summary: str | None = None,
    model_id: str = "gpt-5.4",
    reasoning_effort: str = "none",
    temperature: float = 1.0,
    seed: int = 20260721,
    max_output_tokens: int = 4096,
    include_registry_roster: bool = True,
    memory_token_budget: int | None = None,
    memory_dormancy_chapters: int = 3,
    chapter_order_by_id: Mapping[str, int] | None = None,
) -> RenderedRegistryRequestV4:
    chapter_id = _required_string(chapter.get("chapter_id"), "chapter_id")
    blocks = _source_blocks(chapter)
    packets = build_prior_candidate_packets_v1(chapter=chapter, prior_cards=prior_cards)
    # An ablation arm may withhold the roster to measure what asking for
    # rename recognition costs the rest of the scan.  Withholding it yields an
    # empty roster, never a missing section, so the request shape is stable.
    roster = build_b1_registry_roster_v1(prior_cards) if include_registry_roster else []
    memory_budget_report = None
    if memory_token_budget is not None:
        packets, roster, memory_budget_report = allocate_b1_scan_memory_v1(
            chapter=chapter,
            prior_cards=prior_cards,
            prior_candidate_packets=packets,
            registry_roster=roster,
            memory_token_budget=memory_token_budget,
            memory_dormancy_chapters=memory_dormancy_chapters,
            chapter_order_by_id=chapter_order_by_id,
        )
    prompt = load_system_prompt_from_design(Path(design_doc), PROMPT_ID)
    prompt_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    schema = b1_scan_response_schema_v1()
    model_sections = {
        "summary_context": {
            "previous_chapter_summary": _optional_string(previous_chapter_summary),
            "global_summary": _optional_string(global_summary),
        },
        "source_blocks": [
            {"block_id": row["block_id"], "text": row["text"]} for row in blocks
        ],
        "prior_candidate_packets": packets,
        "registry_roster": roster,
    }
    sections = deepcopy(model_sections)
    if memory_budget_report is not None:
        # The complete omission ledger belongs to the stored request and
        # validated artifact, not to the model context. Feeding it back to the
        # model would make the prompt grow in proportion to the rows the budget
        # deliberately excluded.
        sections["memory_budget_report"] = memory_budget_report
    payload = {
        "schema_version": REQUEST_SCHEMA_VERSION,
        "role": "b1_scan",
        "chapter_id": chapter_id,
        "allowlisted_sections": model_sections,
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
            "sections_hash": canonical_hash(model_sections),
        }
    )
    return RenderedRegistryRequestV4(
        role="b1_scan",
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


def shared_b1_scan_request_v1(
    rendered: RenderedRegistryRequestV4,
) -> dict[str, Any]:
    schema = b1_scan_response_schema_v1()
    if rendered.response_schema_hash != canonical_hash(schema):
        raise B1ScanError("rendered B1-Scan schema binding differs")
    return {
        "messages": [dict(row) for row in rendered.messages],
        "response_schema": schema,
        "request_fingerprint": rendered.request_fingerprint,
    }


def build_prior_candidate_packets_v1(
    *,
    chapter: Mapping[str, Any],
    prior_cards: Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    blocks = _source_blocks(chapter)
    cards = _validate_prior_cards(prior_cards)
    shared_name_components = _shared_name_component_keys(cards)
    packets: list[dict[str, Any]] = []
    for card in cards:
        hits: list[dict[str, Any]] = []
        for surface in card["stable_surfaces"]:
            retrieval_surface = _retrieval_surface(surface)
            shared_component_exact = (
                _single_surface_component_key(retrieval_surface)
                in shared_name_components
            )
            exact_block_ids = [
                row["block_id"]
                for row in blocks
                if _contains_surface(row["text"], surface)
            ]
            normalized_block_ids = [
                row["block_id"]
                for row in blocks
                if retrieval_surface
                and retrieval_surface != surface
                and not _contains_surface(row["text"], surface)
                and _contains_surface(row["text"], retrieval_surface)
            ]
            block_ids = list(
                dict.fromkeys([*exact_block_ids, *normalized_block_ids])
            )
            if block_ids:
                if exact_block_ids and normalized_block_ids:
                    match_basis = (
                        "shared_name_component_exact_and_outer_punctuation_normalized"
                        if shared_component_exact
                        else "exact_and_outer_punctuation_normalized"
                    )
                elif exact_block_ids:
                    match_basis = (
                        "shared_name_component_exact"
                        if shared_component_exact
                        else "exact"
                    )
                else:
                    match_basis = (
                        "shared_name_component_outer_punctuation_normalized"
                        if shared_component_exact
                        else "outer_punctuation_normalized"
                    )
                hits.append(
                    {
                        "surface": surface,
                        "retrieval_surface": retrieval_surface,
                        "match_basis": match_basis,
                        "current_block_ids": block_ids[:MAX_HIT_BLOCK_IDS],
                        "current_hit_block_count": len(block_ids),
                        "block_ids_truncated": len(block_ids) > MAX_HIT_BLOCK_IDS,
                    }
                )
            for widened_surface, match_basis in _mechanical_retrieval_variants(
                retrieval_surface,
                allow_name_components=(
                    card["record_class"] in NAMED_PRIOR_RECORD_CLASSES
                ),
                shared_name_components=shared_name_components,
            ):
                widened_block_ids = [
                    row["block_id"]
                    for row in blocks
                    if _contains_surface(row["text"], widened_surface)
                ]
                if not widened_block_ids:
                    continue
                hits.append(
                    {
                        "surface": surface,
                        "retrieval_surface": widened_surface,
                        "match_basis": match_basis,
                        "current_block_ids": widened_block_ids[:MAX_HIT_BLOCK_IDS],
                        "current_hit_block_count": len(widened_block_ids),
                        "block_ids_truncated": (
                            len(widened_block_ids) > MAX_HIT_BLOCK_IDS
                        ),
                    }
                )
        if hits:
            packets.append(
                {
                    "prior_card": _prior_card_model_view(card),
                    "current_surface_hits": hits,
                }
            )
    if len(packets) > MAX_PRIOR_CARDS:
        raise B1ScanError(
            "surface-matched prior cards exceed the bounded cap: "
            f"{len(packets)} > {MAX_PRIOR_CARDS}"
        )
    return packets


def build_b1_registry_roster_v1(
    prior_cards: Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Compact model-visible roster of EVERY known prior entity.

    Unlike prior candidate packets, the roster is not filtered by surface
    match: an entity appearing in the current chapter under a different name,
    title, married name, or spelling is invisible to surface retrieval and can
    only be recognized by the model against this roster.  Rows are compact
    retrieval context with zero authority; a full card is still supplied only
    through surface-matched packets.  The cap fails loudly - the roster must
    never be truncated silently.
    """

    cards = _validate_prior_cards(prior_cards)
    if len(cards) > MAX_ROSTER_ROWS:
        raise B1ScanError("registry roster exceeds the bounded cap; raise it explicitly")
    rows = [
        {
            "prior_card_id": card["prior_card_id"],
            "canonical_surface": card["canonical_surface"],
            "stable_surfaces": list(card["stable_surfaces"]),
            "referent_kind": card["referent_kind"],
            "record_class": card["record_class"],
        }
        for card in cards
    ]
    rows.sort(key=lambda row: str(row["prior_card_id"]))
    return rows


def allocate_b1_scan_memory_v1(
    *,
    chapter: Mapping[str, Any],
    prior_cards: Sequence[Mapping[str, Any]] | None,
    prior_candidate_packets: Sequence[Mapping[str, Any]],
    registry_roster: Sequence[Mapping[str, Any]],
    memory_token_budget: int,
    memory_dormancy_chapters: int = 3,
    chapter_order_by_id: Mapping[str, int] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Allocate whole packet/roster rows using typed provenance only."""

    if (
        not isinstance(memory_token_budget, int)
        or isinstance(memory_token_budget, bool)
        or memory_token_budget <= 0
    ):
        raise B1ScanError("memory_token_budget must be a positive integer")
    if (
        not isinstance(memory_dormancy_chapters, int)
        or isinstance(memory_dormancy_chapters, bool)
        or memory_dormancy_chapters < 0
    ):
        raise B1ScanError(
            "memory_dormancy_chapters must be a nonnegative integer"
        )
    chapter_id = _required_string(chapter.get("chapter_id"), "chapter_id")
    order_by_id = _validated_chapter_order_by_id(
        chapter_order_by_id, current_chapter_id=chapter_id
    )
    current_order = order_by_id[chapter_id]
    cards = _validate_prior_cards(prior_cards)
    card_by_id = {row["prior_card_id"]: row for row in cards}
    packets = [deepcopy(dict(row)) for row in prior_candidate_packets]
    roster = [deepcopy(dict(row)) for row in registry_roster]

    candidates: list[dict[str, Any]] = []
    for row_kind, rows in (("packet", packets), ("roster_row", roster)):
        seen_ids: set[str] = set()
        for payload in rows:
            card_id = (
                payload.get("prior_card", {}).get("prior_card_id")
                if row_kind == "packet"
                and isinstance(payload.get("prior_card"), Mapping)
                else payload.get("prior_card_id")
            )
            card_id = _required_string(card_id, f"{row_kind} prior_card_id")
            if card_id in seen_ids:
                raise B1ScanError(f"{row_kind} repeats prior_card_id")
            seen_ids.add(card_id)
            card = card_by_id.get(card_id)
            if card is None:
                raise B1ScanError(f"{row_kind} cites a foreign prior card")
            tier, dormancy_rank, member_chapters = _memory_priority_v1(
                card=card,
                current_order=current_order,
                order_by_id=order_by_id,
                memory_dormancy_chapters=memory_dormancy_chapters,
            )
            candidates.append(
                {
                    "row_kind": row_kind,
                    "prior_card_id": card_id,
                    "payload": payload,
                    "tier": tier,
                    "dormancy_rank": dormancy_rank,
                    "member_chapters": member_chapters,
                    "record_class": card["record_class"],
                    "referent_kind": card["referent_kind"],
                    "estimated_tokens": _estimate_memory_row_tokens_v1(payload),
                }
            )

    record_rank = {
        "confirmed_entity": 0,
        "unresolved_named_reference": 1,
        "important_unnamed_referent": 2,
    }
    candidates.sort(
        key=lambda row: (
            row["tier"],
            0 if row["row_kind"] == "packet" else 1,
            row["dormancy_rank"],
            record_rank[row["record_class"]],
            row["prior_card_id"],
        )
    )

    admitted_keys: set[tuple[str, str]] = set()
    admitted_packet_ids: set[str] = set()
    selected_packets: list[dict[str, Any]] = []
    selected_roster: list[dict[str, Any]] = []
    omitted: list[dict[str, Any]] = []
    used = 0
    exhausted = False
    for row in candidates:
        if (
            row["row_kind"] == "roster_row"
            and row["prior_card_id"] in admitted_packet_ids
        ):
            omitted.append(
                {
                    "row_kind": row["row_kind"],
                    "prior_card_id": row["prior_card_id"],
                    "tier": row["tier"],
                    "dormancy_rank": row["dormancy_rank"],
                    "member_chapters": row["member_chapters"],
                    "referent_kind": row["referent_kind"],
                    "estimated_tokens": row["estimated_tokens"],
                    "reason": "covered_by_admitted_packet",
                }
            )
            continue
        proposed_packets = (
            [*selected_packets, row["payload"]]
            if row["row_kind"] == "packet"
            else selected_packets
        )
        proposed_roster = (
            [*selected_roster, row["payload"]]
            if row["row_kind"] == "roster_row"
            else selected_roster
        )
        proposed_tokens = _measure_model_visible_memory_tokens_v1(
            packets=proposed_packets,
            roster=proposed_roster,
        )
        if not exhausted and proposed_tokens <= memory_token_budget:
            used = proposed_tokens
            admitted_keys.add((row["row_kind"], row["prior_card_id"]))
            if row["row_kind"] == "packet":
                admitted_packet_ids.add(row["prior_card_id"])
                selected_packets.append(row["payload"])
            else:
                selected_roster.append(row["payload"])
            continue
        exhausted = True
        omitted.append(
            {
                "row_kind": row["row_kind"],
                "prior_card_id": row["prior_card_id"],
                "tier": row["tier"],
                "dormancy_rank": row["dormancy_rank"],
                "member_chapters": row["member_chapters"],
                "referent_kind": row["referent_kind"],
                "estimated_tokens": row["estimated_tokens"],
                "reason": "memory_budget_exhausted",
            }
        )

    admitted_packets = [
        row
        for row in packets
        if (
            "packet",
            row["prior_card"]["prior_card_id"],
        )
        in admitted_keys
    ]
    admitted_roster = [
        row
        for row in roster
        if ("roster_row", row["prior_card_id"]) in admitted_keys
    ]
    review_issues = [
        {
            "row_type": "memory_budget",
            "row_index": None,
            "reason": "memory_budget_evicted_identity_row",
            "raw_row": deepcopy(row),
        }
        for row in omitted
        if row["tier"] == 1 and row["reason"] == "memory_budget_exhausted"
    ]
    packet_tokens_used = _measure_model_visible_memory_tokens_v1(
        packets=admitted_packets,
        roster=[],
    )
    report = {
        "memory_token_budget": memory_token_budget,
        "memory_dormancy_chapters": memory_dormancy_chapters,
        "memory_tokens_used": used,
        "packet_tokens_used": packet_tokens_used,
        "roster_tokens_used": used - packet_tokens_used,
        "built": {
            "packets": len(packets),
            "roster_rows": len(roster),
        },
        "admitted": {
            "packets": len(admitted_packets),
            "roster_rows": len(admitted_roster),
        },
        "omitted": omitted,
        "omitted_counts": {
            "packets": sum(1 for row in omitted if row["row_kind"] == "packet"),
            "roster_rows": sum(
                1 for row in omitted if row["row_kind"] == "roster_row"
            ),
        },
        "review_issues": review_issues,
    }
    return admitted_packets, admitted_roster, report


def validate_b1_scan_response_v1(
    response: Mapping[str, Any],
    *,
    chapter: Mapping[str, Any],
    prior_candidate_packets: Sequence[Mapping[str, Any]],
    request_fingerprint: str,
    registry_roster: Sequence[Mapping[str, Any]] | None = None,
    memory_budget_report: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(response, Mapping):
        raise B1ScanError("B1-Scan response must be an object")
    expected_keys = {
        "schema_id",
        "chapter_id",
        "entity_observations",
        "glossary_observations",
        "prior_continuity_proposals",
    }
    # roster recognition is an optional channel: absent in historical
    # responses, legal when present, never silently invented
    if "roster_recognition_proposals" in response:
        expected_keys = expected_keys | {"roster_recognition_proposals"}
    _exact_keys(
        response,
        expected_keys,
        "B1-Scan response",
    )
    if response.get("schema_id") != OUTPUT_SCHEMA_ID:
        raise B1ScanError("B1-Scan response schema_id differs")
    chapter_id = _required_string(chapter.get("chapter_id"), "chapter_id")
    response, normalization_notes = normalize_code_owned_response_echoes_v1(
        response,
        expected={"chapter_id": chapter_id},
    )
    blocks = _source_blocks(chapter)
    block_by_id = {row["block_id"]: row for row in blocks}
    issues: list[dict[str, Any]] = []
    content_field_quarantines: list[dict[str, Any]] = []

    entities = _validate_entity_rows(
        response.get("entity_observations"),
        chapter_id=chapter_id,
        block_by_id=block_by_id,
        issues=issues,
    )
    glossary = _validate_glossary_rows(
        response.get("glossary_observations"),
        chapter_id=chapter_id,
        block_by_id=block_by_id,
        issues=issues,
        content_field_quarantines=content_field_quarantines,
    )
    routes = _validate_continuity_rows(
        response.get("prior_continuity_proposals"),
        packets=prior_candidate_packets,
        block_by_id=block_by_id,
        issues=issues,
    )
    roster_proposals = _validate_roster_proposal_rows(
        response.get("roster_recognition_proposals"),
        registry_roster=registry_roster,
        block_by_id=block_by_id,
        issues=issues,
    )
    if memory_budget_report is not None:
        budget_issues = memory_budget_report.get("review_issues")
        if not isinstance(budget_issues, list) or not all(
            isinstance(row, Mapping) for row in budget_issues
        ):
            raise B1ScanError("memory budget review issues are malformed")
        issues.extend(deepcopy(list(budget_issues)))
    body = attach_response_normalization_notes_v1(
        {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "chapter_id": chapter_id,
        "request_fingerprint": _required_string(
            request_fingerprint, "request_fingerprint"
        ),
        "entity_observations": entities,
        "glossary_observations": glossary,
        "continuity_routes": routes,
        "roster_recognition_proposals": roster_proposals,
        "review_issues": issues,
        "content_field_quarantines": content_field_quarantines,
        "metrics": {
            "entity_observation_count": len(entities),
            "glossary_observation_count": len(glossary),
            "prior_packet_count": len(prior_candidate_packets),
            "hearing_required_count": sum(
                1 for row in routes if row["hearing_required"]
            ),
            "roster_recognition_count": len(roster_proposals),
            "review_issue_count": len(issues),
            "content_field_quarantine_count": len(content_field_quarantines),
        },
        "identity_authority_granted": False,
        "registry_mutation_performed": False,
        },
        normalization_notes,
    )
    if memory_budget_report is not None:
        body["memory_budget_report"] = deepcopy(dict(memory_budget_report))
    return {**body, "artifact_hash": canonical_hash(body)}


def make_b1_scan_semantic_validator_v1(
    *, chapter: Mapping[str, Any], rendered: RenderedRegistryRequestV4
) -> Callable[[Mapping[str, Any]], Mapping[str, Any]]:
    packets = rendered.sections.get("prior_candidate_packets")
    if not isinstance(packets, list):
        raise B1ScanError("rendered prior candidate packets are malformed")
    roster = rendered.sections.get("registry_roster")
    if roster is not None and not isinstance(roster, list):
        raise B1ScanError("rendered registry roster is malformed")
    memory_budget_report = rendered.sections.get("memory_budget_report")
    if memory_budget_report is not None and not isinstance(
        memory_budget_report, Mapping
    ):
        raise B1ScanError("rendered memory budget report is malformed")

    def validate(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return validate_b1_scan_response_v1(
            payload,
            chapter=chapter,
            prior_candidate_packets=packets,
            request_fingerprint=rendered.request_fingerprint,
            registry_roster=roster,
            memory_budget_report=memory_budget_report,
        )

    return validate


def _validate_entity_rows(
    value: Any,
    *,
    chapter_id: str,
    block_by_id: Mapping[str, Mapping[str, Any]],
    issues: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise B1ScanError("entity_observations must be a list")
    accepted: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(value):
        try:
            bounded_raw, all_source_block_ids = _bound_entity_evidence_blocks(
                raw,
                row_index=index,
                block_by_id=block_by_id,
                issues=issues,
            )
            row = _validated_entity_row(bounded_raw, block_by_id=block_by_id)
            row["all_source_block_ids"] = (
                all_source_block_ids or list(row["source_block_ids"])
            )
            row["source_block_count"] = len(row["all_source_block_ids"])
            key = canonical_hash(row)
            if key in seen:
                raise B1ScanError("duplicate entity observation")
            seen.add(key)
            observation_body = {"chapter_id": chapter_id, **row}
            accepted.append(
                {
                    "observation_id": f"b1obs_{canonical_hash(observation_body)[:16]}",
                    **row,
                    "authority_scope": "chapter_provisional",
                }
            )
        except B1ScanError as exc:
            issues.append(
                {
                    "row_type": "entity_observation",
                    "row_index": index,
                    "reason": str(exc),
                    "raw_row": deepcopy(raw),
                }
            )
    return accepted


def _validate_roster_proposal_rows(
    value: Any,
    *,
    registry_roster: Sequence[Mapping[str, Any]] | None,
    block_by_id: Mapping[str, Mapping[str, Any]],
    issues: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Row-level validation for roster recognition proposals.

    Every rule here is mechanical: roster-id membership, verbatim surface
    support, channel overlap with already-known stable surfaces, duplicates,
    and a loud bounded cap.  Whether the link is TRUE stays a hearing question;
    code never accepts or rejects the identity itself.
    """

    if value is None:
        return []
    if not isinstance(value, list):
        raise B1ScanError("roster_recognition_proposals must be a list")
    roster_by_id: dict[str, Mapping[str, Any]] = {}
    for row in registry_roster or []:
        if isinstance(row, Mapping) and isinstance(row.get("prior_card_id"), str):
            roster_by_id[row["prior_card_id"]] = row
    accepted: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for index, raw in enumerate(value):
        try:
            if not isinstance(raw, Mapping):
                raise B1ScanError("roster proposal must be an object")
            _exact_keys(
                raw,
                {"surface", "prior_card_id", "source_block_ids", "reason"},
                "roster proposal",
            )
            surface = _evidence_surface(raw.get("surface"), "roster proposal surface")
            prior_card_id = _required_string(
                raw.get("prior_card_id"), "roster proposal prior_card_id"
            )
            roster_row = roster_by_id.get(prior_card_id)
            if roster_row is None:
                raise B1ScanError(
                    "roster proposal targets an id outside the supplied roster"
                )
            block_ids = _string_list(
                _trim_evidence_block_ids(
                    raw.get("source_block_ids"),
                    row_type="roster_recognition_proposal",
                    row_index=index,
                    block_by_id=block_by_id,
                    issues=issues,
                ),
                "roster proposal source_block_ids",
                minimum=1,
                maximum=MAX_OBSERVATION_BLOCK_IDS,
            )
            if len(block_ids) != len(set(block_ids)):
                raise B1ScanError("roster proposal source_block_ids contains duplicates")
            for block_id in block_ids:
                if block_id not in block_by_id:
                    raise B1ScanError("roster proposal cites a foreign block")
            known_surfaces = {
                _normalized_surface(row)
                for row in roster_row.get("stable_surfaces") or []
                if isinstance(row, str)
            }
            known_surfaces.add(
                _normalized_surface(roster_row.get("canonical_surface") or "")
            )
            if _normalized_surface(surface) in known_surfaces:
                raise B1ScanError(
                    "roster proposal repeats a known stable surface; the"
                    " continuity channel owns same-surface cases"
                )
            key = (_normalized_surface(surface), prior_card_id)
            if key in seen:
                raise B1ScanError("duplicate roster proposal")
            seen.add(key)
            if len(accepted) >= MAX_ROSTER_PROPOSALS:
                raise B1ScanError(
                    "roster proposals exceed the bounded cap; extra rows are recorded"
                )
            proposal_body = {
                "surface": surface,
                "prior_card_id": prior_card_id,
                "source_block_ids": list(block_ids),
                "reason": _bounded_note(raw.get("reason"), "roster proposal reason"),
            }
            accepted.append(
                {
                    "proposal_id": "b1rrp_" + canonical_hash(proposal_body)[:16],
                    **proposal_body,
                    "roster_card": deepcopy(dict(roster_row)),
                    "authority_scope": "proposal_only",
                    "identity_authority_granted": False,
                }
            )
        except B1ScanError as exc:
            issues.append(
                {
                    "row_type": "roster_recognition_proposal",
                    "row_index": index,
                    "reason": str(exc),
                    "raw_row": deepcopy(raw),
                }
            )
    return accepted


def _bound_entity_evidence_blocks(
    raw: Any,
    *,
    row_index: int,
    block_by_id: Mapping[str, Mapping[str, Any]],
    issues: list[dict[str, Any]],
) -> tuple[Any, list[str] | None]:
    if not isinstance(raw, Mapping):
        return raw, None
    source_block_ids = raw.get("source_block_ids")
    if (
        not isinstance(source_block_ids, list)
        or len(source_block_ids) <= MAX_OBSERVATION_BLOCK_IDS
    ):
        return raw, None
    block_ids = [_required_string(value, "entity source_block_ids") for value in source_block_ids]
    if len(block_ids) != len(set(block_ids)):
        raise B1ScanError("entity source_block_ids contains duplicates")
    if any(block_id not in block_by_id for block_id in block_ids):
        raise B1ScanError("entity source_block_ids cites a foreign block")
    ordered = sorted(
        block_ids,
        key=lambda block_id: (
            int(block_by_id[block_id]["order_index"]),
            block_id,
        ),
    )
    retained = ordered[:MAX_OBSERVATION_BLOCK_IDS]
    issues.append(
        {
            "row_type": "entity_observation_support_overflow",
            "row_index": row_index,
            "reason": (
                "valid source evidence exceeded the bounded context cap; "
                "the earliest source blocks were retained mechanically"
            ),
            "retained_source_block_ids": retained,
            "omitted_source_block_count": len(ordered) - len(retained),
            "raw_row": deepcopy(raw),
        }
    )
    bounded = deepcopy(dict(raw))
    bounded["source_block_ids"] = retained
    return bounded, ordered


def _validated_entity_row(
    raw: Any, *, block_by_id: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise B1ScanError("entity observation must be an object")
    _exact_keys(
        raw,
        {
            "surface",
            "source_block_ids",
            "referent_kind_claim",
            "record_class",
            "presence_basis",
            "scan_note",
        },
        "entity observation",
    )
    surface = _evidence_surface(raw.get("surface"), "entity surface")
    block_ids = _validated_surface_blocks(
        raw.get("source_block_ids"),
        block_by_id=block_by_id,
        label="entity source_block_ids",
    )
    return {
        "surface": surface,
        "source_block_ids": block_ids,
        "referent_kind_claim": _enum(
            raw.get("referent_kind_claim"), REFERENT_KINDS, "referent kind"
        ),
        "record_class": _enum(
            raw.get("record_class"), RECORD_CLASSES, "record class"
        ),
        "presence_basis": _enum(
            raw.get("presence_basis"), PRESENCE_BASES, "presence basis"
        ),
        "scan_note": _bounded_note(raw.get("scan_note"), "scan_note"),
    }


def _validate_glossary_rows(
    value: Any,
    *,
    chapter_id: str,
    block_by_id: Mapping[str, Mapping[str, Any]],
    issues: list[dict[str, Any]],
    content_field_quarantines: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise B1ScanError("glossary_observations must be a list")
    accepted: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(value):
        try:
            if not isinstance(raw, Mapping):
                raise B1ScanError("glossary observation must be an object")
            _exact_keys(
                raw,
                {
                    "surface",
                    "source_block_ids",
                    "category_hint",
                    "term_category_raw",
                },
                "glossary observation",
            )
            surface = _evidence_surface(raw.get("surface"), "glossary surface")
            source_block_ids = _validated_surface_blocks(
                raw.get("source_block_ids"),
                block_by_id=block_by_id,
                label="glossary source_block_ids",
            )
            (
                category_hint,
                term_category_raw,
                term_category_status,
                quarantine_reason,
            ) = _normalized_term_category(
                raw.get("category_hint"),
                raw.get("term_category_raw"),
            )
            row = {
                "surface": surface,
                "source_block_ids": source_block_ids,
                "category_hint": category_hint,
                "term_category_raw": term_category_raw,
                "term_category_status": term_category_status,
            }
            if quarantine_reason is not None:
                content_field_quarantines.append(
                    {
                        "row_type": "glossary_observation",
                        "field": "category_hint",
                        "quarantine_reason": quarantine_reason,
                        "raw_value": raw.get("category_hint"),
                        "source_block_ids": source_block_ids,
                        "raw_row_sha256": canonical_hash(raw),
                    }
                )
            key = canonical_hash(row)
            if key in seen:
                raise B1ScanError("duplicate glossary observation")
            seen.add(key)
            accepted.append(
                {
                    "term_observation_id": (
                        f"b1term_{canonical_hash({'chapter_id': chapter_id, **row})[:16]}"
                    ),
                    **row,
                    "authority_scope": "chapter_provisional",
                }
            )
        except B1ScanError as exc:
            issues.append(
                {
                    "row_type": "glossary_observation",
                    "row_index": index,
                    "reason": str(exc),
                    "raw_row": deepcopy(raw),
                }
            )
    return accepted


def _normalized_term_category(
    value: Any, raw_value: Any
) -> tuple[str, str | None, str, str | None]:
    category = _required_string(value, "category hint")
    raw = None
    if raw_value is not None:
        if (
            isinstance(raw_value, str)
            and raw_value.strip()
            and len(raw_value) <= 120
            and "\n" not in raw_value
            and "\r" not in raw_value
        ):
            raw = raw_value
    if category in TERM_CATEGORIES - {"other"} and raw is None:
        return category, None, "in_vocabulary", None
    if category == "other" and raw is not None:
        return category, raw, "model_other", None
    if category == "other":
        return (
            "other",
            None,
            "quarantined_invalid_enum",
            "other_term_category_missing_raw",
        )
    if category in TERM_CATEGORIES:
        return (
            "other",
            raw,
            "quarantined_invalid_enum",
            "known_term_category_has_raw",
        )
    return (
        "other",
        category,
        "quarantined_invalid_enum",
        "unsupported_term_category",
    )


def _validate_continuity_rows(
    value: Any,
    *,
    packets: Sequence[Mapping[str, Any]],
    block_by_id: Mapping[str, Mapping[str, Any]],
    issues: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise B1ScanError("prior_continuity_proposals must be a list")
    packet_by_id = {
        str(packet["prior_card"]["prior_card_id"]): packet for packet in packets
    }
    accepted: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(value):
        try:
            if not isinstance(raw, Mapping):
                raise B1ScanError("continuity proposal must be an object")
            _exact_keys(
                raw,
                {
                    "prior_card_id",
                    "verdict",
                    "reason_code",
                    "source_block_ids",
                    "reason",
                },
                "continuity proposal",
            )
            card_id = _required_string(raw.get("prior_card_id"), "prior_card_id")
            packet = packet_by_id.get(card_id)
            if packet is None:
                raise B1ScanError("continuity proposal cites a foreign prior card")
            if card_id in accepted:
                raise B1ScanError("continuity proposal duplicates a prior card")
            allowed_blocks = {
                block_id
                for hit in packet["current_surface_hits"]
                for block_id in hit["current_block_ids"]
            }
            source_block_ids = _string_list(
                _trim_evidence_block_ids(
                    raw.get("source_block_ids"),
                    row_type="prior_continuity_proposal",
                    row_index=index,
                    block_by_id=block_by_id,
                    issues=issues,
                    preferred=allowed_blocks,
                ),
                "continuity source_block_ids",
                minimum=1,
                maximum=MAX_OBSERVATION_BLOCK_IDS,
            )
            if any(block_id not in block_by_id for block_id in source_block_ids):
                raise B1ScanError("continuity proposal cites a foreign block")
            if not set(source_block_ids).intersection(allowed_blocks):
                raise B1ScanError(
                    "continuity evidence does not intersect a supplied surface hit"
                )
            verdict = _enum(
                raw.get("verdict"), CONTINUITY_VERDICTS, "continuity verdict"
            )
            accepted[card_id] = {
                "prior_card_id": card_id,
                "verdict": verdict,
                "reason_code": _enum(
                    raw.get("reason_code"),
                    CONTINUITY_REASON_CODES,
                    "continuity reason_code",
                ),
                "source_block_ids": source_block_ids,
                "reason": _bounded_note(raw.get("reason"), "continuity reason"),
            }
        except B1ScanError as exc:
            issues.append(
                {
                    "row_type": "prior_continuity_proposal",
                    "row_index": index,
                    "reason": str(exc),
                    "raw_row": deepcopy(raw),
                }
            )

    collisions = _prior_surface_collisions(packets)
    routes: list[dict[str, Any]] = []
    for card_id in sorted(packet_by_id):
        proposal = accepted.get(card_id)
        if proposal is None:
            packet = packet_by_id[card_id]
            source_block_ids = sorted(
                {
                    block_id
                    for hit in packet["current_surface_hits"]
                    for block_id in hit["current_block_ids"]
                }
            )[:3]
            proposal = {
                "prior_card_id": card_id,
                "verdict": "uncertain",
                "reason_code": "insufficient_evidence",
                "source_block_ids": source_block_ids,
                "reason": "The model did not provide one valid exact-cover proposal.",
            }
            issues.append(
                {
                    "row_type": "prior_continuity_exact_cover",
                    "row_index": None,
                    "reason": f"missing valid proposal for {card_id}",
                    "raw_row": None,
                }
            )
        mechanical_risks: list[str] = []
        if card_id in collisions:
            mechanical_risks.append("same_surface_matches_multiple_prior_cards")
        card = packet_by_id[card_id]["prior_card"]
        if card["record_class"] != "confirmed_entity":
            mechanical_risks.append("prior_record_is_not_confirmed_entity")
        if card["claim_state"] != "confirmed":
            mechanical_risks.append("prior_claim_state_is_not_confirmed")
        # These flags describe evidence already visible in the Scan request.
        # Keep them for audit, but do not let code overrule the model's explicit
        # continuity verdict.
        hearing_required = proposal["verdict"] != "propose_continue"
        routes.append(
            {
                **proposal,
                "packet_action": (
                    "withhold_prior_card" if hearing_required else "include_prior_card"
                ),
                "hearing_required": hearing_required,
                "mechanical_risk_codes": mechanical_risks,
                "identity_authority_granted": False,
            }
        )
    return routes


def _prior_surface_collisions(
    packets: Sequence[Mapping[str, Any]],
) -> set[str]:
    owners: dict[str, set[str]] = {}
    for packet in packets:
        card_id = str(packet["prior_card"]["prior_card_id"])
        for hit in packet["current_surface_hits"]:
            owners.setdefault(_normalized_surface(hit["surface"]), set()).add(card_id)
    return {
        card_id
        for card_ids in owners.values()
        if len(card_ids) > 1
        for card_id in card_ids
    }


def _validate_prior_cards(
    prior_cards: Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    if prior_cards is None:
        return []
    if isinstance(prior_cards, (str, bytes)) or not isinstance(prior_cards, Sequence):
        raise B1ScanError("prior_cards must be a sequence")
    rows: list[dict[str, Any]] = []
    for raw in prior_cards:
        if not isinstance(raw, Mapping):
            raise B1ScanError("prior card must be an object")
        expected = {
            "prior_card_id",
            "canonical_surface",
            "stable_surfaces",
            "referent_kind",
            "identity_summary",
            "record_class",
            "presence_basis",
            "claim_state",
            "first_supported_block_id",
            "provenance_refs",
        }
        optional_profile = {"profile_claims", "distinguishing_note"}
        if frozenset(raw) not in {
            frozenset(expected),
            frozenset(expected | optional_profile),
        }:
            raise B1ScanError("prior card fields differ from a supported projection")
        canonical_surface = _required_string(
            raw.get("canonical_surface"), "canonical_surface"
        )
        stable_surfaces = _string_list(
            raw.get("stable_surfaces"), "stable_surfaces", minimum=1, maximum=8
        )
        normalized = {_normalized_surface(surface) for surface in stable_surfaces}
        if not all(normalized) or _normalized_surface(canonical_surface) not in normalized:
            raise B1ScanError("canonical_surface is absent from stable_surfaces")
        refs = raw.get("provenance_refs")
        if not isinstance(refs, list) or not refs:
            raise B1ScanError("prior provenance_refs must be a non-empty list")
        checked_refs: list[dict[str, str]] = []
        for ref in refs:
            if not isinstance(ref, Mapping):
                raise B1ScanError("prior provenance ref must be an object")
            _exact_keys(ref, {"chapter_id", "block_id"}, "prior provenance ref")
            checked_refs.append(
                {
                    "chapter_id": _required_string(
                        ref.get("chapter_id"), "prior provenance chapter_id"
                    ),
                    "block_id": _required_string(
                        ref.get("block_id"), "prior provenance block_id"
                    ),
                }
            )
        first_block = _required_string(
            raw.get("first_supported_block_id"), "first_supported_block_id"
        )
        if first_block not in {ref["block_id"] for ref in checked_refs}:
            raise B1ScanError("first_supported_block_id is absent from provenance")
        row = {
                "prior_card_id": _required_string(
                    raw.get("prior_card_id"), "prior_card_id"
                ),
                "canonical_surface": canonical_surface,
                "stable_surfaces": stable_surfaces,
                "referent_kind": _enum(
                    raw.get("referent_kind"), REFERENT_KINDS, "prior referent_kind"
                ),
                "identity_summary": _required_string(
                    raw.get("identity_summary"), "prior identity_summary"
                ),
                "record_class": _enum(
                    raw.get("record_class"),
                    PRIOR_RECORD_CLASSES,
                    "prior record_class",
                ),
                "presence_basis": _enum(
                    raw.get("presence_basis"), PRESENCE_BASES, "prior presence_basis"
                ),
                "claim_state": _enum(
                    raw.get("claim_state"), PRIOR_CLAIM_STATES, "prior claim_state"
                ),
                "first_supported_block_id": first_block,
                "provenance_refs": checked_refs,
            }
        if optional_profile <= set(raw):
            row["profile_claims"] = _validate_prior_profile_claims(
                raw.get("profile_claims")
            )
            note = raw.get("distinguishing_note")
            row["distinguishing_note"] = (
                None
                if note is None
                else _bounded_note(note, "prior distinguishing_note")
            )
        rows.append(row)
    rows.sort(key=lambda row: row["prior_card_id"])
    if len(rows) != len({row["prior_card_id"] for row in rows}):
        raise B1ScanError("prior cards contain duplicate ids")
    return rows


def _validated_chapter_order_by_id(
    value: Mapping[str, int] | None,
    *,
    current_chapter_id: str,
) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise B1ScanError(
            "chapter_order_by_id is required when memory budgeting is active"
        )
    rows: dict[str, int] = {}
    for raw_id, raw_order in value.items():
        chapter_id = _required_string(raw_id, "chapter order id")
        if (
            not isinstance(raw_order, int)
            or isinstance(raw_order, bool)
            or raw_order <= 0
        ):
            raise B1ScanError("chapter order must be a positive integer")
        if chapter_id in rows:
            raise B1ScanError("chapter order repeats a chapter id")
        rows[chapter_id] = raw_order
    if len(rows) != len(set(rows.values())):
        raise B1ScanError("chapter order repeats an ordinal")
    if current_chapter_id not in rows:
        raise B1ScanError("current chapter is absent from chapter order")
    return rows


def _memory_priority_v1(
    *,
    card: Mapping[str, Any],
    current_order: int,
    order_by_id: Mapping[str, int],
    memory_dormancy_chapters: int,
) -> tuple[int, int, int]:
    member_ids = {
        _required_string(ref.get("chapter_id"), "prior provenance chapter_id")
        for ref in card["provenance_refs"]
    }
    try:
        member_orders = [order_by_id[chapter_id] for chapter_id in member_ids]
    except KeyError as exc:
        raise B1ScanError(
            f"prior provenance chapter is absent from chapter order: {exc.args[0]}"
        ) from exc
    if any(order >= current_order for order in member_orders):
        raise B1ScanError("prior card provenance is not earlier than current chapter")
    member_chapters = len(member_ids)
    dormancy_rank = current_order - 1 - max(member_orders)
    if member_chapters >= 2 or dormancy_rank < memory_dormancy_chapters:
        return 1, dormancy_rank, member_chapters
    if card["referent_kind"] in {"person", "nonhuman_character", "unknown"}:
        return 2, dormancy_rank, member_chapters
    return 3, dormancy_rank, member_chapters


def _estimate_memory_row_tokens_v1(value: Mapping[str, Any]) -> int:
    is_packet = isinstance(value.get("prior_card"), Mapping)
    return _measure_model_visible_memory_tokens_v1(
        packets=[value] if is_packet else [],
        roster=[] if is_packet else [value],
    )


def _measure_model_visible_memory_tokens_v1(
    *,
    packets: Sequence[Mapping[str, Any]],
    roster: Sequence[Mapping[str, Any]],
) -> int:
    def estimate(
        packet_rows: Sequence[Mapping[str, Any]],
        roster_rows: Sequence[Mapping[str, Any]],
    ) -> int:
        payload = {
            "schema_version": REQUEST_SCHEMA_VERSION,
            "role": "b1_scan",
            "chapter_id": "memory_budget_measurement",
            "allowlisted_sections": {
                "prior_candidate_packets": deepcopy(list(packet_rows)),
                "registry_roster": deepcopy(list(roster_rows)),
            },
        }
        request = {
            "messages": [
                {"role": "system", "content": ""},
                {"role": "user", "content": canonical_json(payload)},
            ],
            "response_schema": b1_scan_response_schema_v1(),
            "request_fingerprint": "0" * 64,
        }
        return measure_literary_request_token_preflight_v1(
            request,
            prompt_token_cap=1_000_000,
            output_token_cap=0,
        ).message_token_estimate

    baseline = estimate([], [])
    return max(0, estimate(packets, roster) - baseline)


def _prior_card_model_view(card: Mapping[str, Any]) -> dict[str, Any]:
    view = {
        key: deepcopy(card[key])
        for key in (
            "prior_card_id",
            "canonical_surface",
            "stable_surfaces",
            "referent_kind",
            "identity_summary",
            "record_class",
            "presence_basis",
            "claim_state",
            "first_supported_block_id",
        )
    }
    if "profile_claims" in card:
        view["profile_claims"] = _compact_prior_profile_claims_for_model(
            card["profile_claims"]
        )
        view["distinguishing_note"] = deepcopy(card["distinguishing_note"])
    return view


def _compact_prior_profile_claims_for_model(
    claims: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    by_field: dict[str, list[Mapping[str, Any]]] = {}
    for claim in claims:
        by_field.setdefault(str(claim["field"]), []).append(claim)

    compact: list[dict[str, Any]] = []
    for field in sorted(by_field):
        rows = by_field[field]
        effective_rows = [row for row in rows if row["effective"] is True]
        selected = effective_rows or rows
        grouped: dict[tuple[str, str | None], list[Mapping[str, Any]]] = {}
        for row in selected:
            grouped.setdefault((str(row["status"]), row.get("value")), []).append(row)
        for (status, value), support in sorted(
            grouped.items(),
            key=lambda item: (item[0][0], "" if item[0][1] is None else item[0][1]),
        ):
            basis_values = sorted(
                {
                    str(row["basis"])
                    for row in support
                    if isinstance(row.get("basis"), str) and row["basis"]
                }
            )
            compact.append(
                {
                    "field": field,
                    "status": status,
                    "value": value,
                    "effective": bool(effective_rows),
                    "basis_values": basis_values,
                    "support_count": len(support),
                }
            )
    if len(compact) > MAX_MODEL_PROFILE_CLAIMS:
        raise B1ScanError(
            "compacted prior profile_claims exceed the model-facing cap"
        )
    return compact


def _validate_prior_profile_claims(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > MAX_PRIOR_PROFILE_CLAIMS:
        raise B1ScanError("prior profile_claims must be a bounded list")
    rows: list[dict[str, Any]] = []
    expected = {
        "field",
        "status",
        "value",
        "basis",
        "effective",
        "anchor_block_ids",
        "story_time_note",
        "validity",
        "semantic_status",
    }
    for raw in value:
        if not isinstance(raw, Mapping):
            raise B1ScanError("prior profile claim must be an object")
        _exact_keys(raw, expected, "prior profile claim")
        claim_value = raw.get("value")
        if claim_value is not None and not isinstance(claim_value, str):
            raise B1ScanError("prior profile claim value must be string or null")
        basis = raw.get("basis")
        if basis is not None and not isinstance(basis, str):
            raise B1ScanError("prior profile claim basis must be string or null")
        story_time_note = raw.get("story_time_note")
        if story_time_note is not None and not isinstance(story_time_note, str):
            raise B1ScanError("prior story_time_note must be string or null")
        validity = raw.get("validity")
        if not isinstance(validity, Mapping):
            raise B1ScanError("prior profile claim validity must be an object")
        _exact_keys(validity, {"from_block", "to_block"}, "prior claim validity")
        for endpoint in ("from_block", "to_block"):
            point = validity.get(endpoint)
            if point is not None and not isinstance(point, str):
                raise B1ScanError("prior claim validity endpoint is malformed")
        effective = raw.get("effective")
        if not isinstance(effective, bool):
            raise B1ScanError("prior profile claim effective must be boolean")
        rows.append(
            {
                "field": _required_string(raw.get("field"), "prior claim field"),
                "status": _required_string(raw.get("status"), "prior claim status"),
                "value": claim_value,
                "basis": basis,
                "effective": effective,
                "anchor_block_ids": _string_list(
                    raw.get("anchor_block_ids"),
                    "prior claim anchor_block_ids",
                    minimum=0,
                    maximum=8,
                ),
                "story_time_note": story_time_note,
                "validity": {
                    "from_block": validity.get("from_block"),
                    "to_block": validity.get("to_block"),
                },
                "semantic_status": _required_string(
                    raw.get("semantic_status"), "prior claim semantic_status"
                ),
            }
        )
    return rows


def _source_blocks(chapter: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = chapter.get("blocks")
    if not isinstance(raw, list) or not raw:
        raise B1ScanError("chapter must contain source blocks")
    rows: list[dict[str, Any]] = []
    for block in raw:
        if not isinstance(block, Mapping):
            raise B1ScanError("chapter contains a non-object block")
        rows.append(
            {
                "block_id": _required_string(block.get("block_id"), "block_id"),
                "order_index": int(block.get("order_index") or 0),
                "text": unicodedata.normalize(
                    "NFC",
                    str(
                        block.get("clean_text")
                        or block.get("source_text")
                        or block.get("text")
                        or ""
                    ),
                ),
            }
        )
    rows.sort(key=lambda row: (row["order_index"], row["block_id"]))
    if len(rows) != len({row["block_id"] for row in rows}):
        raise B1ScanError("chapter contains duplicate block ids")
    return rows


def _trim_evidence_block_ids(
    value: Any,
    *,
    row_type: str,
    row_index: int,
    block_by_id: Mapping[str, Mapping[str, Any]],
    issues: list[dict[str, Any]],
    preferred: Collection[str] = (),
) -> Any:
    """Trim over-cited evidence to the cap instead of dropping the whole row.

    Citing more support than the cap is a bounding concern, never a defect in
    the judgment being made, so the row survives and the omission is recorded.
    ``preferred`` blocks are retained first: a continuity proposal must keep the
    supplied surface hit that entitles it to speak about that prior card.
    Anything the caller's own validator should reject - a wrong shape, a foreign
    block - is passed through untouched so that error is reported truthfully.
    """

    if not isinstance(value, list) or len(value) <= MAX_OBSERVATION_BLOCK_IDS:
        return value
    if not all(isinstance(item, str) and item.strip() for item in value):
        return value
    block_ids = [unicodedata.normalize("NFC", item.strip()) for item in value]
    if len(block_ids) != len(set(block_ids)):
        return value
    if any(block_id not in block_by_id for block_id in block_ids):
        return value
    preferred_ids = set(preferred)
    ordered = sorted(
        block_ids,
        key=lambda block_id: (
            block_id not in preferred_ids,
            int(block_by_id[block_id]["order_index"]),
            block_id,
        ),
    )
    retained = ordered[:MAX_OBSERVATION_BLOCK_IDS]
    issues.append(
        {
            "row_type": f"{row_type}_support_overflow",
            "row_index": row_index,
            "reason": (
                "valid source evidence exceeded the bounded context cap; "
                "the earliest source blocks were retained mechanically"
            ),
            "retained_source_block_ids": list(retained),
            "omitted_source_block_count": len(ordered) - len(retained),
            "raw_row": list(value),
        }
    )
    return sorted(
        retained,
        key=lambda block_id: (int(block_by_id[block_id]["order_index"]), block_id),
    )


def _evidence_surface(value: Any, label: str) -> str:
    """Strip only the punctuation wrapping a surface, never anything inside it.

    A model routinely copies a surface as it was typeset - ``“Hareton
    Earnshaw.”`` - while the prose carries the bare name.  Removing the wrapper
    is mechanical.  Honorifics, particles, and internal punctuation stay: what
    counts as a removable title is a language judgment (``Mrs. Heathcliff``).
    """

    text = _required_string(value, label)
    stripped = text.strip(OUTER_RETRIEVAL_PUNCTUATION).strip()
    if not stripped:
        raise B1ScanError(f"{label} carries no surface beyond punctuation")
    return stripped


def _validated_surface_blocks(
    value: Any,
    *,
    block_by_id: Mapping[str, Mapping[str, Any]],
    label: str,
) -> list[str]:
    block_ids = _string_list(
        value,
        label,
        minimum=1,
        maximum=MAX_OBSERVATION_BLOCK_IDS,
    )
    if any(block_id not in block_by_id for block_id in block_ids):
        raise B1ScanError(f"{label} cites a foreign block")
    # Deliberately no verbatim-surface check.  A block can carry a referent
    # through a pronoun, an epithet, or unattributed dialogue, and deciding
    # whether it does is language work that belongs to the model, not here.
    # Code verifies only that the cited block exists in this chapter.
    return block_ids


def _contains_surface(text: str, surface: str) -> bool:
    start_guard = r"(?<!\w)" if surface[0].isalnum() else ""
    end_guard = r"(?!\w)" if surface[-1].isalnum() else ""
    return (
        re.search(
            start_guard + re.escape(surface) + end_guard,
            text,
            flags=re.IGNORECASE | re.UNICODE,
        )
        is not None
    )


def _mechanical_retrieval_variants(
    surface: str,
    *,
    allow_name_components: bool,
    shared_name_components: Collection[str] = (),
) -> list[tuple[str, str]]:
    """Widen candidate invitations without deciding referential identity."""

    words = re.findall(r"[^\W_]+(?:['’][^\W_]+)*", surface, flags=re.UNICODE)
    if not words:
        return []
    variants: list[tuple[str, str]] = []
    seen = {_normalized_surface(surface)}

    def add(value: str, basis: str) -> None:
        normalized = _normalized_surface(value)
        if not normalized or normalized in seen:
            return
        seen.add(normalized)
        variants.append((value, basis))

    if (
        len(words) > 1
        and words[0].casefold() in MECHANICAL_LEADING_RETRIEVAL_TOKENS
    ):
        add(" ".join(words[1:]), "leading_wrapper_omitted")
    if allow_name_components and len(words) > 1:
        components = [
            word
            for word in words
            if word.casefold() not in MECHANICAL_LEADING_RETRIEVAL_TOKENS
        ]
        for word in dict.fromkeys(
            components[:1] + components[-1:] if components else []
        ):
            basis = (
                "shared_name_component"
                if _normalized_surface(word) in shared_name_components
                else "name_component"
            )
            add(word, basis)
    return variants


def _shared_name_component_keys(
    cards: Sequence[Mapping[str, Any]],
) -> frozenset[str]:
    owners: dict[str, set[str]] = {}
    for card in cards:
        card_id = str(card["prior_card_id"])
        for surface in card["stable_surfaces"]:
            words = re.findall(
                r"[^\W_]+(?:['â€™][^\W_]+)*",
                _retrieval_surface(surface),
                flags=re.UNICODE,
            )
            components = [
                word
                for word in words
                if word.casefold() not in MECHANICAL_LEADING_RETRIEVAL_TOKENS
            ]
            if len(components) <= 1:
                continue
            for word in dict.fromkeys(components[:1] + components[-1:]):
                owners.setdefault(_normalized_surface(word), set()).add(card_id)
    return frozenset(
        component for component, card_ids in owners.items() if len(card_ids) >= 2
    )


def _single_surface_component_key(surface: str) -> str | None:
    words = re.findall(
        r"[^\W_]+(?:['â€™][^\W_]+)*",
        surface,
        flags=re.UNICODE,
    )
    if len(words) != 1:
        return None
    return _normalized_surface(words[0])


def _normalized_surface(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value)).casefold()
    text = re.sub(r"\s+", " ", text).strip()
    return text.strip(OUTER_RETRIEVAL_PUNCTUATION)


def _retrieval_surface(value: Any) -> str:
    text = unicodedata.normalize("NFC", str(value)).strip()
    return text.strip(OUTER_RETRIEVAL_PUNCTUATION).strip()


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise B1ScanError(f"{label} must be a non-empty string")
    return unicodedata.normalize("NFC", value.strip())


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    return _required_string(value, "optional summary")


def _bounded_note(value: Any, label: str) -> str:
    result = _required_string(value, label)
    if len(result) > 320:
        raise B1ScanError(f"{label} exceeds 320 characters")
    return result


def _string_list(
    value: Any,
    label: str,
    *,
    minimum: int,
    maximum: int,
) -> list[str]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise B1ScanError(f"{label} violates cardinality bounds")
    rows = [_required_string(item, label) for item in value]
    if len(rows) != len(set(rows)):
        raise B1ScanError(f"{label} contains duplicates")
    return rows


def _enum(value: Any, allowed: Iterable[str], label: str) -> str:
    result = _required_string(value, label)
    if result not in allowed:
        raise B1ScanError(f"{label} has unsupported value {result!r}")
    return result


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        raise B1ScanError(
            f"{label} field set differs; missing={sorted(expected - actual)}, "
            f"foreign={sorted(actual - expected)}"
        )


__all__ = [
    "ARTIFACT_SCHEMA_VERSION",
    "B1ScanError",
    "MAX_ROSTER_PROPOSALS",
    "MAX_ROSTER_ROWS",
    "allocate_b1_scan_memory_v1",
    "OUTPUT_SCHEMA_ID",
    "PROMPT_ID",
    "b1_scan_response_schema_v1",
    "build_b1_registry_roster_v1",
    "build_prior_candidate_packets_v1",
    "make_b1_scan_semantic_validator_v1",
    "render_b1_scan_request_v1",
    "shared_b1_scan_request_v1",
    "validate_b1_scan_response_v1",
]
