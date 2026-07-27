"""Book-neutral prompts and response schemas for Literary B2 V1."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


B2_FRAME_PROMPT_ID = "literary_b2_chapter_frame_v1"
B2_INTERACTION_PROMPT_ID = "literary_b2_interaction_window_v1"


B2_FRAME_SYSTEM_PROMPT = """\
Prompt version: literary_b2_chapter_frame_v1.
Return exactly one JSON object matching the supplied response schema. Return no
Markdown and no prose outside JSON.

You receive one complete chapter in source order plus a bounded set of
non-authoritative candidate cards retrieved from that chapter. Work only from
the supplied source. Candidate cards are hints, not answers. One candidate may
still be wrong; several candidates may share a surface. Do not create, merge,
split, rename, or update any card.

Your task is narrow:
1. write a short source-grounded chapter gist;
2. classify the broad narrative mode;
3. list a few setting surfaces actually present in the chapter;
4. point to every block where a new narrator or story-time frame begins;
5. open a review request when narrator identity, frame boundary, or story-time
   layer is materially ambiguous.

frame_starts contains START POINTS only. The first non-heading block should be
the first start. Code derives segment ends. Copy start_block_id from a supplied
block marker. narrator_surface is a source expression, or null when the voice
is external/authorial or cannot be named. candidate_card_ids may contain only
ids supplied in CANDIDATE_PACKETS.

Use resolved_candidate only with exactly one candidate id,
ambiguous_candidates with at least two, and external_or_authorial or unknown
with no candidate id. Do not force an identity to avoid uncertainty.

This stage does not extract dialogue turns, events, relation phases, emotions,
motifs, translation, or final chronology. Do not use information from later
chapters. Empty review_requests is valid only when no material ambiguity needs
review.
"""


B2_INTERACTION_SYSTEM_PROMPT = """\
Prompt version: literary_b2_interaction_window_v1.
Return exactly one JSON object matching the supplied response schema. Return no
Markdown and no prose outside JSON.

You receive ACTIVE_BLOCKS, a short PRECEDING_TAIL that is read-only, the
applicable chapter-frame proposal, and bounded candidate packets. Extract only
rows owned by ACTIVE_BLOCKS. Never cite or emit a row for a tail block.

Candidate cards are possible referents, not identity decisions. One supplied
candidate may still be wrong. A bare or shared surface may correctly retrieve
several cards. Point to zero, one, or several supplied candidate ids according
to the local source; never create or update a card.

speaker_turns: record direct spoken or explicitly written utterances that have
translator-relevant speaker/addressee context. utterance_anchor is a short
literal span copied from the cited active block. address_terms are forms used
to address the listener, not people merely discussed. register_cue is local to
this turn and does not define a permanent relationship.

interaction_events: record concrete narrated interactions between locally
referenced participants, such as greeting, helping, ordering, refusing,
attacking, restraining, giving, arriving to meet, or departing from someone.
event_anchor is a short literal span copied from the cited active block.
action_summary states only the observed act. interaction_kind is broad routing,
not a relationship label. Do not output friend, enemy, lover, spouse, hostile
phase, reconciliation, or any validity interval as a final relation state.

Endpoint resolution rules:
- resolved_candidate: exactly one supplied candidate id;
- ambiguous_candidates: at least two supplied candidate ids;
- unresolved: no candidate id;
- non_entity_voice: no candidate id.
surface may be a proper name, pronoun, descriptor, group expression, or null
for an implicit endpoint. Do not turn a body part, ordinary object, insult, or
address word into a new person/entity. When the source is unclear, keep the
endpoint unresolved and open review instead of guessing.

This stage does not create entities, aliases, stable claims, relation phases,
long-term emotions, chapter summaries, motifs, translation, or style policy.
Empty output lists are valid only when the active source genuinely contains no
qualifying observation.
"""


_ENDPOINT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "surface",
        "reference_form",
        "resolution_status",
        "candidate_card_ids",
        "attribution_method",
    ],
    "properties": {
        "surface": {"type": ["string", "null"], "maxLength": 160},
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
                "ambiguous_candidates",
                "unresolved",
                "non_entity_voice",
            ],
        },
        "candidate_card_ids": {
            "type": "array",
            "maxItems": 12,
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1, "maxLength": 160},
        },
        "attribution_method": {
            "type": "string",
            "enum": [
                "explicit_tag",
                "turn_sequence",
                "vocative",
                "frame_context",
                "nearby_context",
                "unknown",
            ],
        },
    },
}


def b2_frame_response_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "chapter_id",
            "chapter_orientation",
            "frame_starts",
            "review_requests",
        ],
        "properties": {
            "schema_version": {
                "type": "string",
                "const": "literary_b2_frame_response_v1",
            },
            "chapter_id": {"type": "string", "minLength": 1, "maxLength": 160},
            "chapter_orientation": {
                "type": "object",
                "additionalProperties": False,
                "required": ["chapter_gist", "narrative_mode", "setting_surfaces"],
                "properties": {
                    "chapter_gist": {"type": "string", "minLength": 1, "maxLength": 1600},
                    "narrative_mode": {
                        "type": "string",
                        "enum": [
                            "first_person_character",
                            "third_person_external",
                            "mixed_or_embedded",
                            "unknown",
                        ],
                    },
                    "setting_surfaces": {
                        "type": "array",
                        "maxItems": 12,
                        "uniqueItems": True,
                        "items": {"type": "string", "minLength": 1, "maxLength": 160},
                    },
                },
            },
            "frame_starts": {
                "type": "array",
                "maxItems": 32,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "start_block_id",
                        "narrator_surface",
                        "narrator_status",
                        "candidate_card_ids",
                        "story_time_label",
                        "boundary_reason",
                    ],
                    "properties": {
                        "start_block_id": {"type": "string", "minLength": 1, "maxLength": 160},
                        "narrator_surface": {"type": ["string", "null"], "maxLength": 160},
                        "narrator_status": {
                            "type": "string",
                            "enum": [
                                "resolved_candidate",
                                "ambiguous_candidates",
                                "external_or_authorial",
                                "unknown",
                            ],
                        },
                        "candidate_card_ids": {
                            "type": "array",
                            "maxItems": 12,
                            "uniqueItems": True,
                            "items": {"type": "string", "minLength": 1, "maxLength": 160},
                        },
                        "story_time_label": {
                            "type": "string",
                            "enum": [
                                "frame_present",
                                "retrospective_past",
                                "embedded_document",
                                "dream_or_vision",
                                "tale_told_aloud",
                                "unclear",
                            ],
                        },
                        "boundary_reason": {"type": "string", "minLength": 1, "maxLength": 320},
                    },
                },
            },
            "review_requests": {
                "type": "array",
                "maxItems": 32,
                "items": _review_request_schema(
                    ["narrator_identity", "frame_boundary", "story_time"]
                ),
            },
        },
    }


def b2_interaction_response_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "chapter_id",
            "window_id",
            "speaker_turns",
            "interaction_events",
            "review_requests",
        ],
        "properties": {
            "schema_version": {
                "type": "string",
                "const": "literary_b2_interaction_response_v1",
            },
            "chapter_id": {"type": "string", "minLength": 1, "maxLength": 160},
            "window_id": {"type": "string", "minLength": 1, "maxLength": 200},
            "speaker_turns": {
                "type": "array",
                "maxItems": 256,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "block_id",
                        "utterance_anchor",
                        "speaker",
                        "addressee",
                        "address_terms",
                        "register_cue",
                    ],
                    "properties": {
                        "block_id": {"type": "string", "minLength": 1, "maxLength": 160},
                        "utterance_anchor": {"type": "string", "minLength": 1, "maxLength": 500},
                        "speaker": deepcopy(_ENDPOINT_SCHEMA),
                        "addressee": {
                            "anyOf": [deepcopy(_ENDPOINT_SCHEMA), {"type": "null"}]
                        },
                        "address_terms": {
                            "type": "array",
                            "maxItems": 8,
                            "items": {"type": "string", "minLength": 1, "maxLength": 160},
                        },
                        "register_cue": {
                            "type": "string",
                            "enum": [
                                "neutral",
                                "formal",
                                "intimate",
                                "deferential",
                                "paternal",
                                "hostile",
                                "mocking",
                                "unclear",
                            ],
                        },
                    },
                },
            },
            "interaction_events": {
                "type": "array",
                "maxItems": 256,
                "items": {
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
                        "actor": deepcopy(_ENDPOINT_SCHEMA),
                        "target": {
                            "anyOf": [deepcopy(_ENDPOINT_SCHEMA), {"type": "null"}]
                        },
                        "interaction_kind": {
                            "type": "string",
                            "enum": [
                                "speech_act",
                                "affiliation_or_care",
                                "service_or_obedience",
                                "conflict_or_hostility",
                                "coercion_or_control",
                                "physical_contact",
                                "exchange_or_transfer",
                                "meeting_or_separation",
                                "other_interaction",
                            ],
                        },
                        "action_summary": {"type": "string", "minLength": 1, "maxLength": 400},
                        "observed_valence": {
                            "type": "string",
                            "enum": ["positive", "negative", "mixed", "neutral", "unclear"],
                        },
                    },
                },
            },
            "review_requests": {
                "type": "array",
                "maxItems": 64,
                "items": _review_request_schema(
                    [
                        "speaker_identity",
                        "addressee_identity",
                        "event_endpoint",
                        "source_anchor",
                        "frame_context",
                    ]
                ),
            },
        },
    }


def _review_request_schema(kinds: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "review_kind",
            "source_block_ids",
            "candidate_card_ids",
            "reason",
        ],
        "properties": {
            "review_kind": {"type": "string", "enum": list(kinds)},
            "source_block_ids": {
                "type": "array",
                "minItems": 1,
                "maxItems": 8,
                "uniqueItems": True,
                "items": {"type": "string", "minLength": 1, "maxLength": 160},
            },
            "candidate_card_ids": {
                "type": "array",
                "maxItems": 12,
                "uniqueItems": True,
                "items": {"type": "string", "minLength": 1, "maxLength": 160},
            },
            "reason": {"type": "string", "minLength": 1, "maxLength": 500},
        },
    }


__all__ = [
    "B2_FRAME_PROMPT_ID",
    "B2_FRAME_SYSTEM_PROMPT",
    "B2_INTERACTION_PROMPT_ID",
    "B2_INTERACTION_SYSTEM_PROMPT",
    "b2_frame_response_schema",
    "b2_interaction_response_schema",
]
