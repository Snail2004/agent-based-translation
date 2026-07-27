"""Book-neutral prompts and response schemas for the B2 recovery loop."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from pipeline.literary.b0_entity_inventory_experiment import REFERENT_KINDS


REGISTRY_RECOVERY_PROMPT_ID_V1 = "literary_b2_registry_recovery_audit_v1_3"
EVENT_REVIEW_PROMPT_ID_V1 = "literary_b2_event_semantic_audit_v1"


REGISTRY_RECOVERY_SYSTEM_PROMPT_V1 = """\
Prompt version: literary_b2_registry_recovery_audit_v1_3.
Return exactly one JSON object matching the supplied response schema. Return no
Markdown and no prose outside JSON.

You receive one bounded chapter-local component of unresolved B2 endpoints or
single-candidate speaker attributions that lack authoritative exact support,
plus source blocks and candidate cards. Candidate cards are possible referents,
not answers. Exact-cover every ticket once.

For issue_kind=contextual_speaker_attribution, attach_existing may retain the
supplied candidate or replace it with another supplied card only when the
source and local turn sequence establish that speaker. Choose keep_pending when
the attribution remains uncertain. A nearby reaction, movement, facial
expression, or mere appearance of a name is not by itself a speaker tag.

Choose attach_existing only when the evidence identifies exactly one supplied
card. Choose create_chapter_local when the source establishes a
translator-relevant participant that is absent from the supplied cards. Several
tickets may share one provisional_group_key only when the evidence establishes
the same referent. Repeated pronouns or similar descriptions alone do not prove
identity.

A chapter-local recovery card is not a global entity or alias. Its
canonical_surface must be copied exactly from cited source evidence. Its
identity_summary must contain stable identifying information only, not a
temporary action, mood, relationship phase, or interpretation. Forms of
address, insults, role phrases, and pronouns may be evidence but must not be
promoted into a global name.

Choose keep_pending when the evidence is insufficient. Choose
reject_non_registry only when the endpoint is not a translator-relevant
referent that belongs in the chapter registry. Do not create, merge, split, or
rename book-global entities. Do not translate, infer relation phases, or use
knowledge outside the supplied source.

When you answer `keep_pending`, list in `narrowed_candidate_card_ids` every
supplied card that remains possible after weighing the evidence, and omit the
ones you have ruled out. Leave the list empty only if the evidence rules out
nothing. Listing a single card does not mean you have chosen it; use
`attach_existing` only when the evidence settles the question.
"""


EVENT_REVIEW_SYSTEM_PROMPT_V1 = """\
Prompt version: literary_b2_event_semantic_audit_v1.
Return exactly one JSON object matching the supplied response schema. Return no
Markdown and no prose outside JSON.

You receive one bounded component of normalized B2 interaction events, exact
source blocks, and candidate cards. Exact-cover every event case once. Candidate
cards are possible referents, not answers. Never create or modify a card.

One effective event represents one directed, concrete, non-speech action. The
actor performs the action in action_summary. The target receives or is directly
affected by that action. A source span containing actions in opposite
directions must be split into separate events. A retrieved group is not the
target merely because its members are present in the block.

Choose keep only when the event, direction, endpoints, kind, valence, and
summary are source-supported. Choose revise for one corrected event. Choose
split for two or three directed events. Choose pending when the supplied
evidence cannot settle the event. Choose reject only when the row is not a
qualifying non-speech interaction.

Replacement anchors must be copied exactly from the supplied source block.
Replacement endpoints may reference only supplied candidate ids. Keep
unresolved or ambiguous endpoints when the evidence is insufficient. Do not
invent an entity, stable claim, relation phase, emotion history, translation,
or source fact.
"""


_NULLABLE_STRING: dict[str, Any] = {
    "anyOf": [{"type": "string", "minLength": 1}, {"type": "null"}]
}


def registry_recovery_response_schema_v1() -> dict[str, Any]:
    action = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "ticket_id",
            "action",
            "target_candidate_card_id",
            "provisional_group_key",
            "canonical_surface",
            "referent_kind",
            "identity_summary",
            "source_block_ids",
            "pending_reason",
            "resolution_note",
        ],
        "properties": {
            "ticket_id": {"type": "string", "minLength": 1, "maxLength": 160},
            "action": {
                "type": "string",
                "enum": [
                    "attach_existing",
                    "create_chapter_local",
                    "keep_pending",
                    "reject_non_registry",
                ],
            },
            "target_candidate_card_id": deepcopy(_NULLABLE_STRING),
            "provisional_group_key": deepcopy(_NULLABLE_STRING),
            "canonical_surface": deepcopy(_NULLABLE_STRING),
            "referent_kind": {
                "anyOf": [
                    {"type": "string", "enum": sorted(REFERENT_KINDS)},
                    {"type": "null"},
                ]
            },
            "identity_summary": deepcopy(_NULLABLE_STRING),
            "source_block_ids": {
                "type": "array",
                "minItems": 1,
                "maxItems": 12,
                "uniqueItems": True,
                "items": {"type": "string", "minLength": 1, "maxLength": 160},
            },
            "pending_reason": deepcopy(_NULLABLE_STRING),
            "resolution_note": {
                "type": "string",
                "minLength": 1,
                "maxLength": 600,
            },
            "narrowed_candidate_card_ids": {
                "type": "array",
                "maxItems": 32,
                "uniqueItems": True,
                "items": {"type": "string", "minLength": 1, "maxLength": 160},
            },
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "chapter_id",
            "component_id",
            "ticket_actions",
        ],
        "properties": {
            "schema_version": {
                "type": "string",
                "const": "literary_b2_registry_recovery_response_v1",
            },
            "chapter_id": {"type": "string", "minLength": 1, "maxLength": 160},
            "component_id": {"type": "string", "minLength": 1, "maxLength": 160},
            "ticket_actions": {
                "type": "array",
                "maxItems": 12,
                "items": action,
            },
        },
    }


_RECOVERY_ENDPOINT_SCHEMA_V1: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "surface",
        "reference_form",
        "resolution_status",
        "candidate_card_ids",
        "resolution_basis",
    ],
    "properties": {
        "surface": {
            "anyOf": [
                {"type": "string", "minLength": 1, "maxLength": 160},
                {"type": "null"},
            ]
        },
        "reference_form": {
            "type": "string",
            "enum": [
                "proper_name",
                "pronoun",
                "descriptor",
                "implicit",
                "group",
                "unknown",
            ],
        },
        "resolution_status": {
            "type": "string",
            "enum": [
                "resolved_candidate",
                "resolved_joint_candidates",
                "ambiguous_candidates",
                "unresolved",
            ],
        },
        "candidate_card_ids": {
            "type": "array",
            "maxItems": 12,
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1, "maxLength": 160},
        },
        "resolution_basis": {
            "type": "string",
            "enum": [
                "explicit_name",
                "pronoun_context",
                "descriptor_context",
                "narrator_frame",
                "group_expression",
                "registry_recovery",
                "unknown",
            ],
        },
    },
}


def _replacement_event_schema_v1() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "block_id",
            "event_anchor",
            "actor",
            "target",
            "interaction_kind",
            "action_summary",
            "observed_valence",
        ],
        "properties": {
            "block_id": {"type": "string", "minLength": 1, "maxLength": 160},
            "event_anchor": {"type": "string", "minLength": 1, "maxLength": 500},
            "actor": deepcopy(_RECOVERY_ENDPOINT_SCHEMA_V1),
            "target": deepcopy(_RECOVERY_ENDPOINT_SCHEMA_V1),
            "interaction_kind": {
                "type": "string",
                "enum": [
                    "affiliation_or_care",
                    "service_or_obedience",
                    "conflict_or_hostility",
                    "coercion_or_control",
                    "physical_contact",
                    "exchange_or_transfer",
                    "meeting_or_separation",
                    "other_nonspeech_interaction",
                ],
            },
            "action_summary": {
                "type": "string",
                "minLength": 1,
                "maxLength": 400,
            },
            "observed_valence": {
                "type": "string",
                "enum": ["positive", "negative", "mixed", "neutral", "unclear"],
            },
        },
    }


def event_review_response_schema_v1() -> dict[str, Any]:
    action = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "case_id",
            "action",
            "replacement_events",
            "source_block_ids",
            "pending_reason",
            "resolution_note",
        ],
        "properties": {
            "case_id": {"type": "string", "minLength": 1, "maxLength": 160},
            "action": {
                "type": "string",
                "enum": ["keep", "revise", "split", "pending", "reject"],
            },
            "replacement_events": {
                "type": "array",
                "maxItems": 3,
                "items": _replacement_event_schema_v1(),
            },
            "source_block_ids": {
                "type": "array",
                "minItems": 1,
                "maxItems": 12,
                "uniqueItems": True,
                "items": {"type": "string", "minLength": 1, "maxLength": 160},
            },
            "pending_reason": deepcopy(_NULLABLE_STRING),
            "resolution_note": {
                "type": "string",
                "minLength": 1,
                "maxLength": 600,
            },
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "chapter_id",
            "component_id",
            "event_actions",
        ],
        "properties": {
            "schema_version": {
                "type": "string",
                "const": "literary_b2_event_review_response_v1",
            },
            "chapter_id": {"type": "string", "minLength": 1, "maxLength": 160},
            "component_id": {"type": "string", "minLength": 1, "maxLength": 160},
            "event_actions": {
                "type": "array",
                "maxItems": 12,
                "items": action,
            },
        },
    }


__all__ = [
    "EVENT_REVIEW_PROMPT_ID_V1",
    "EVENT_REVIEW_SYSTEM_PROMPT_V1",
    "REGISTRY_RECOVERY_PROMPT_ID_V1",
    "REGISTRY_RECOVERY_SYSTEM_PROMPT_V1",
    "event_review_response_schema_v1",
    "registry_recovery_response_schema_v1",
]
