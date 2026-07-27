"""Book-neutral interaction prompt and response schema for Literary B2 V2."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


B2_INTERACTION_PROMPT_ID_V2 = "literary_b2_interaction_window_v2"
B2_INTERACTION_PROMPT_ID_V2_1 = "literary_b2_interaction_window_v2_1"


B2_INTERACTION_SYSTEM_PROMPT_V2 = """\
Prompt version: literary_b2_interaction_window_v2.
Return exactly one JSON object matching the supplied response schema. Return no
Markdown and no prose outside JSON.

You receive ACTIVE_BLOCKS, a short PRECEDING_TAIL that is read-only, the
applicable chapter-frame proposal, and bounded candidate packets. Extract only
rows owned by ACTIVE_BLOCKS. Never emit a speaker turn or interaction event
owned by a tail block. Candidate cards are possible referents, not answers.
Never create, merge, split, rename, or update a card.

speaker_turns owns all direct spoken or explicitly written utterances that
carry translator-relevant speaker or addressee context. Copy utterance_anchor
from the cited active block. Classify the local speech_function and
register_cue. Do not repeat the utterance as an interaction_event.

For every speaker turn, speaker_support points to the exact source span used to
attribute the speaker. support_kind=explicit_reporting_clause is valid only
when the copied support anchor is a reporting or writing clause that actually
attributes that utterance. A nearby reaction, facial expression, movement, or
mere appearance of a name is not an explicit speaker tag. Use turn_sequence,
narrator_frame, nearby_context, or unresolved when attribution is inferred.
The support row is audit evidence, not final identity authority.

interaction_events owns concrete narrated NON-SPEECH actions between local
participants, such as helping, attacking, restraining, giving, physical
contact, meeting, or separation. Copy event_anchor from the cited active block
and summarize only the observed action. Commands, requests, offers, refusals,
threats, proposals, insults, and apologies belong once in speaker_turns through
speech_function, not again in interaction_events.

Endpoint resolution rules:
- resolved_candidate: exactly one supplied candidate id;
- resolved_joint_candidates: at least two supplied ids and EVERY listed card
  jointly occupies the endpoint in the source expression;
- ambiguous_candidates: at least two supplied ids but the source does not
  determine which one occupies the endpoint;
- unresolved or non_entity_voice: no candidate id.
Do not use ambiguous_candidates for a coordinated phrase such as two people
acting together. Do not use resolved_joint_candidates merely because several
cards were retrieved. When uncertain, keep the endpoint unresolved or
ambiguous and open review instead of guessing.

surface may be a proper name, pronoun, descriptor, group expression, or null
for an implicit endpoint. Do not turn a body part, ordinary object, insult, or
address word into a new person/entity. address_terms are forms used to address
the listener, not people merely discussed. register_cue is local to this turn
and does not define a permanent relationship.

This stage does not create entities, aliases, stable claims, relation phases,
long-term emotions, chapter summaries, motifs, translation, or style policy.
Empty output lists are valid only when the active source genuinely contains no
qualifying observation.
"""


B2_INTERACTION_SYSTEM_PROMPT_V2_1 = """\
Prompt version: literary_b2_interaction_window_v2_1.
Return exactly one JSON object matching the supplied response schema. Return no
Markdown and no prose outside JSON.

You receive ACTIVE_BLOCKS, a short PRECEDING_TAIL that is read-only, the
applicable chapter-frame proposal, and bounded candidate packets. Extract only
rows owned by ACTIVE_BLOCKS. Never emit a speaker turn or interaction event
owned by a tail block. Candidate cards are possible referents, not answers.
Never create, merge, split, rename, or update a card.

speaker_turns owns direct spoken or explicitly written utterances that carry
translator-relevant speaker or addressee context. utterance_anchor is a short,
contiguous, verbatim substring of its active source block. Use the shortest
exact excerpt that identifies the utterance; do not copy an entire long
paragraph merely to provide an anchor. Classify the local speech_function and
register_cue. Do not repeat the utterance as an interaction_event.

For every speaker turn, speaker_support points to the exact source span used to
attribute the speaker. support_anchor must always be a contiguous verbatim
substring of source_block_id, including when support_kind is turn_sequence,
narrator_frame, nearby_context, or unresolved. Never put an enum label such as
"turn_sequence" into support_anchor. For an inferred attribution, copy the
shortest exact nearby phrase or utterance excerpt that lets the Auditor inspect
the inference. support_kind=explicit_reporting_clause is valid only when the
copied phrase actually attributes that utterance. A nearby reaction, facial
expression, movement, or mere appearance of a name is not an explicit speaker
tag. The support row is audit evidence, not final identity authority.

interaction_events owns concrete narrated NON-SPEECH actions in which a
person, animal, nonhuman character, or participant group directly acts on or
directly affects such a participant. Qualifying examples include helping,
attacking, restraining, giving something to someone, physical contact, and a
directed meeting or separation. event_anchor is a short, contiguous, verbatim
substring of the cited active block and action_summary covers only the observed
action.

Do not emit ordinary object handling, self-grooming, posture, intransitive
movement, entering or leaving a place, internal reaction, weather inspection,
or an action whose only affected endpoint is an ordinary object or place.
Self-directed bodily action may be emitted only when it is independently
translator-relevant, not merely because actor and target can share a card.
Commands, requests, offers, refusals, threats, proposals, insults, apologies,
and other speech-mediated acts belong once in speaker_turns through
speech_function, not again in interaction_events.

Endpoint resolution rules:
- resolved_candidate: exactly one supplied candidate id;
- resolved_joint_candidates: at least two supplied ids and EVERY listed card
  jointly occupies the endpoint in the source expression;
- ambiguous_candidates: at least two supplied ids but the source does not
  determine which one occupies the endpoint;
- unresolved or non_entity_voice: no candidate id.
Do not use ambiguous_candidates for a coordinated phrase such as two people
acting together. Do not use resolved_joint_candidates merely because several
cards were retrieved. When uncertain, keep the endpoint unresolved or
ambiguous and open review instead of guessing.

surface may be a proper name, pronoun, descriptor, group expression, or null
for an implicit endpoint. Do not turn a body part, ordinary object, insult, or
address word into a new person/entity. address_terms are forms used to address
the listener, not people merely discussed. register_cue is local to this turn
and does not define a permanent relationship.

This stage does not create entities, aliases, stable claims, relation phases,
long-term emotions, chapter summaries, motifs, translation, or style policy.
Empty output lists are valid only when the active source genuinely contains no
qualifying observation.
"""


_ENDPOINT_SCHEMA_V2: dict[str, Any] = {
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
                "resolved_joint_candidates",
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
        "resolution_basis": {
            "type": "string",
            "enum": [
                "explicit_name",
                "pronoun_context",
                "descriptor_context",
                "narrator_frame",
                "group_expression",
                "unknown",
            ],
        },
    },
}


_SPEAKER_SUPPORT_SCHEMA_V2: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["source_block_id", "support_anchor", "support_kind"],
    "properties": {
        "source_block_id": {"type": "string", "minLength": 1, "maxLength": 160},
        "support_anchor": {"type": "string", "minLength": 1, "maxLength": 500},
        "support_kind": {
            "type": "string",
            "enum": [
                "explicit_reporting_clause",
                "turn_sequence",
                "narrator_frame",
                "nearby_context",
                "unresolved",
            ],
        },
    },
}


def b2_interaction_response_schema_v2() -> dict[str, Any]:
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
                "const": "literary_b2_interaction_response_v2",
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
                        "speaker_support",
                        "address_terms",
                        "speech_function",
                        "register_cue",
                    ],
                    "properties": {
                        "block_id": {"type": "string", "minLength": 1, "maxLength": 160},
                        "utterance_anchor": {"type": "string", "minLength": 1, "maxLength": 500},
                        "speaker": deepcopy(_ENDPOINT_SCHEMA_V2),
                        "addressee": deepcopy(_ENDPOINT_SCHEMA_V2),
                        "speaker_support": deepcopy(_SPEAKER_SUPPORT_SCHEMA_V2),
                        "address_terms": {
                            "type": "array",
                            "maxItems": 8,
                            "items": {"type": "string", "minLength": 1, "maxLength": 160},
                        },
                        "speech_function": {
                            "type": "string",
                            "enum": [
                                "statement",
                                "question",
                                "greeting",
                                "request",
                                "command",
                                "refusal",
                                "offer",
                                "promise",
                                "threat",
                                "proposal",
                                "insult",
                                "apology",
                                "other",
                                "unclear",
                            ],
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
                        "actor": deepcopy(_ENDPOINT_SCHEMA_V2),
                        "target": deepcopy(_ENDPOINT_SCHEMA_V2),
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
                "items": _review_request_schema_v2(),
            },
        },
    }


def bind_b2_interaction_response_schema_v2(
    *,
    chapter_id: str,
    window_id: str,
    active_block_ids: list[str],
    support_block_ids: list[str],
    candidate_card_ids: list[str],
) -> dict[str, Any]:
    """Bind transport identifiers without deciding any literary semantics."""

    active = sorted(set(active_block_ids))
    support = sorted(set(support_block_ids))
    candidates = sorted(set(candidate_card_ids))
    if not chapter_id or not window_id or not active or not support:
        raise ValueError("B2 response-schema bindings must be non-empty")
    schema = b2_interaction_response_schema_v2()
    properties = schema["properties"]
    properties["chapter_id"]["enum"] = [chapter_id]
    properties["window_id"]["enum"] = [window_id]

    turn = properties["speaker_turns"]["items"]["properties"]
    event = properties["interaction_events"]["items"]["properties"]
    review = properties["review_requests"]["items"]["properties"]
    turn["block_id"]["enum"] = active
    event["block_id"]["enum"] = active
    turn["speaker_support"]["properties"]["source_block_id"]["enum"] = support
    review["source_block_ids"]["items"]["enum"] = active

    candidate_arrays = [
        turn["speaker"]["properties"]["candidate_card_ids"],
        turn["addressee"]["properties"]["candidate_card_ids"],
        event["actor"]["properties"]["candidate_card_ids"],
        event["target"]["properties"]["candidate_card_ids"],
        review["candidate_card_ids"],
    ]
    for candidate_array in candidate_arrays:
        if candidates:
            candidate_array["items"]["enum"] = candidates
        else:
            candidate_array["maxItems"] = 0
    return schema


def _review_request_schema_v2() -> dict[str, Any]:
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
            "review_kind": {
                "type": "string",
                "enum": [
                    "speaker_identity",
                    "speaker_attribution",
                    "addressee_identity",
                    "event_endpoint",
                    "source_anchor",
                    "frame_context",
                    "turn_event_overlap",
                ],
            },
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
    "B2_INTERACTION_PROMPT_ID_V2",
    "B2_INTERACTION_PROMPT_ID_V2_1",
    "B2_INTERACTION_SYSTEM_PROMPT_V2",
    "B2_INTERACTION_SYSTEM_PROMPT_V2_1",
    "bind_b2_interaction_response_schema_v2",
    "b2_interaction_response_schema_v2",
]
