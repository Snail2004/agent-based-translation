"""Book-neutral slim frame and durable-observation prompts for Literary B2 V3."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


B2_FRAME_PROMPT_ID_V2 = "literary_b2_chapter_frame_v2"
B2_FRAME_PROMPT_ID_V3 = "literary_b2_chapter_frame_v3"
B2_FRAME_PROMPT_ID_V4 = "literary_b2_chapter_frame_v4"
B2_FRAME_PROMPT_ID_V5 = "literary_b2_chapter_frame_v5"
B2_SLIM_INTERACTION_PROMPT_ID_V3 = "literary_b2_slim_interaction_window_v3"
B2_SLIM_INTERACTION_PROMPT_ID_V4 = "literary_b2_slim_interaction_window_v4"
B2_SLIM_INTERACTION_PROMPT_ID_V5 = "literary_b2_slim_interaction_window_v5"
B2_SLIM_INTERACTION_PROMPT_ID_V6 = "literary_b2_slim_interaction_window_v6"
B2_SLIM_INTERACTION_PROMPT_ID_V7 = "literary_b2_slim_interaction_window_v7"
B2_SLIM_INTERACTION_PROMPT_ID_V8 = "literary_b2_slim_interaction_window_v8"
B2_SLIM_INTERACTION_PROMPT_ID_V9 = "literary_b2_slim_interaction_window_v9"
B2_SLIM_INTERACTION_PROMPT_ID_V10 = "literary_b2_slim_interaction_window_v10"
B2_SLIM_INTERACTION_PROMPT_ID_V11 = "literary_b2_slim_interaction_window_v11"


B2_FRAME_SYSTEM_PROMPT_V2 = """\
Prompt version: literary_b2_chapter_frame_v2.
Return exactly one JSON object matching the supplied response schema. Return no
Markdown and no prose outside JSON.

You receive one complete chapter in source order plus bounded,
non-authoritative candidate cards. Work only from the supplied source.
Candidate cards are possible referents, not answers. One candidate may still
be wrong, and several candidates may share a surface. Never create, merge,
split, rename, or update a card.

Your only task is to point to the first active block and every later block
where the narrator or narrative time/layer changes materially. Emit START
POINTS only. Code derives segment ends and exact chapter coverage.

For each start:
- copy start_block_id from a supplied block marker;
- copy a short boundary_cue_anchor exactly from that block when a useful cue
  exists, otherwise return null;
- identify the narrator only when the chapter supports it;
- classify the local narrative mode.

Use resolved_candidate with exactly one supplied candidate id,
ambiguous_candidates with at least two, and external_or_authorial or unknown
with none. Do not force identity to avoid uncertainty. If the first active
block has no reliable narrator, emit an unknown start there.

Do not write a chapter gist, settings list, dialogue turns, events, relation
states, motifs, translation, or style policy. Empty review_requests is valid
when no material frame ambiguity needs review.
"""

B2_FRAME_SYSTEM_PROMPT_V3 = (
    B2_FRAME_SYSTEM_PROMPT_V2.replace(
        "Prompt version: literary_b2_chapter_frame_v2.",
        "Prompt version: literary_b2_chapter_frame_v3.",
        1,
    )
    + """\

For every review request you raise, set `blocking_kind` to exactly one value.
Use `unresolved_entity` when you know who is meant in the scene but not which
supplied card that person is; list every card you are choosing between in
`competing_card_ids`. Use `scene_ambiguity` when the supplied blocks do not
establish which present participant is meant, regardless of which cards exist.
Use `anchor_defect` when the quoted span cannot be located uniquely in its
block. Use `timeline_pending` when the blocker is whether the event happened,
whether it should be recorded, or that a participant in it has no supplied
card. `competing_card_ids` must be empty unless `blocking_kind` is
`unresolved_entity`.
"""
)

B2_FRAME_SYSTEM_PROMPT_V4 = (
    B2_FRAME_SYSTEM_PROMPT_V3.replace(
        "Prompt version: literary_b2_chapter_frame_v3.",
        "Prompt version: literary_b2_chapter_frame_v4.",
        1,
    ).replace(
        "list every card you are choosing between in\n"
        "`competing_card_ids`.",
        "list every card you are choosing between in `competing_card_ids`, giving each\n"
        "one as the exact `candidate_card_id` string copied from the supplied\n"
        "candidate cards - never a surface name, never an abbreviation, never a\n"
        "position or index.",
        1,
    )
)

B2_FRAME_SYSTEM_PROMPT_V5 = (
    B2_FRAME_SYSTEM_PROMPT_V4.replace(
        "Prompt version: literary_b2_chapter_frame_v4.",
        "Prompt version: literary_b2_chapter_frame_v5.",
        1,
    ).replace(
        "card. `competing_card_ids` must be empty unless `blocking_kind` is\n"
        "`unresolved_entity`.",
        "card. Use `frame_structure` when the blocker is where a scene boundary "
        "falls or\n"
        "which narrator frames the passage - a question about the frame itself, "
        "not\n"
        "about who speaks or is addressed within it. `competing_card_ids` must be "
        "empty\n"
        "unless `blocking_kind` is `unresolved_entity`.",
        1,
    )
)


B2_SLIM_INTERACTION_SYSTEM_PROMPT_V3 = """\
Prompt version: literary_b2_slim_interaction_window_v3.
Return exactly one JSON object matching the supplied response schema. Return no
Markdown and no prose outside JSON.

You receive ACTIVE_BLOCKS, a short PRECEDING_TAIL that is read-only, the
applicable chapter-frame context, and bounded candidate packets. Extract only
rows owned by ACTIVE_BLOCKS. Never emit a row owned by a tail block.

Candidate cards are possible referents, not identity decisions. One supplied
candidate may still be wrong. Several cards may legitimately share a surface.
Point only to supplied candidate ids, or leave an endpoint unresolved. Never
create, merge, split, rename, or update a card.

speaker_turns records direct spoken or explicitly written utterances whose
speaker or addressee context helps translation. Copy a short exact
utterance_anchor from the active block. A short utterance may be copied in
full; for a long utterance copy only a distinctive exact span. Do not copy a
reporting clause or explain attribution. address_terms are forms used to
address the listener, not people merely discussed. register_cue is local to
the turn and is not a permanent relationship label.

salient_events is NOT an inventory of actions. Emit an event only when it is:
1. meaningful evidence for a relationship that may matter later;
2. a durable state change likely to remain relevant after this passage; or
3. a larger world-state change affecting the story environment.

Ordinary movement, object handling, looking, sitting, opening a door, and
other momentary action remain in the source and should normally be omitted.
Do not promote an event merely because it has a verb. A spoken proposal,
promise, report, denial, or announcement may be both a speaker turn and a
salient event only when its content has durable narrative significance.

For each salient event:
- cite all active source_block_ids needed for the observation;
- choose one anchor_block_id among them and copy event_anchor exactly from it;
- use generic participant roles so people, animals, groups, places, objects,
  and institutions can participate;
- summarize only what the supplied source supports;
- distinguish occurred, planned, cancelled, denied, and uncertain;
- distinguish directly narrated, reported by a character, and inferred;
- set pending_review when participant, actuality, significance, or grounding
  remains materially uncertain.

Endpoint rules:
- resolved_candidate: exactly one supplied candidate id;
- resolved_joint_candidates: at least two supplied ids and all jointly occupy
  the endpoint;
- ambiguous_candidates: at least two supplied ids and the source does not
  select one;
- unresolved or non_entity_voice: no candidate id.

Do not infer final relationship phases, validity intervals, long-term emotion,
or durable effect records. B3 owns temporal state materialization. Do not write
a chapter summary, motif profile, translation, or style policy. Empty lists are
valid when the active source genuinely has no qualifying row.
"""


B2_SLIM_INTERACTION_SYSTEM_PROMPT_V4 = """\
Prompt version: literary_b2_slim_interaction_window_v4.
Return exactly one JSON object matching the supplied response schema. Return no
Markdown and no prose outside JSON.

You receive ACTIVE_BLOCKS, a short PRECEDING_TAIL that is read-only, the
applicable chapter-frame context, and bounded candidate packets. Extract only
rows owned by ACTIVE_BLOCKS. Never emit a row owned by a tail block.

Candidate cards are possible referents, not identity decisions. One supplied
candidate may still be wrong. Several cards may legitimately share a surface.
Point only to supplied candidate ids, or leave an endpoint unresolved. Never
create, merge, split, rename, or update a card.

speaker_turns records direct spoken or explicitly written utterances whose
speaker or addressee context helps translation. Include a direct soliloquy or
brief command even when it has no listener. Copy a short exact
utterance_anchor from the active block. A short utterance may be copied in
full; for a long utterance copy only a distinctive exact span. Do not copy a
reporting clause into utterance_anchor. address_terms are forms used to
address the listener, not people merely discussed. register_cue is local to
the turn and is not a permanent relationship label.

Attribute speech from the source grammar before using turn-taking. Prefer an
explicit speech verb or reporting clause attached to the utterance, then a
clearly continued utterance by the same speaker, then local turn sequence. A
nearby name in an action, facial-expression, or reaction clause is not a
speech attribution unless the grammar explicitly links that subject to a
speech act. If the source does not securely identify the speaker or listener,
leave that endpoint unresolved and open the corresponding review request;
never reverse endpoints merely to fill both fields.

salient_events is NOT an inventory of actions. Emit an event only when it is:
1. meaningful evidence for a relationship that may matter later;
2. a durable state change likely to remain relevant after this passage; or
3. a larger world-state change affecting the story environment.

Ordinary movement, object handling, looking, sitting, opening a door, and
other momentary action remain in the source and should normally be omitted.
Do not promote an event merely because it has a verb. A pre-existing state
that is merely stated, explained, or revealed in this passage is not a state
change. A past anecdote with no likely effect on later relationship, status,
identity, or world state should remain in chapter-level interpretation rather
than durable event memory. A spoken proposal, promise, report, denial, or
announcement may be both a speaker turn and a salient event only when its
content has durable narrative significance.

For each salient event:
- cite all active source_block_ids needed for the observation;
- choose one anchor_block_id among them and copy event_anchor exactly from it;
- use generic participant roles so people, animals, groups, places, objects,
  and institutions can participate;
- assign initiator to the grammatical agent of the qualifying act and affected
  to what the act changes; a possessor, companion, or nearby noun is not an
  initiator merely because it is mentioned;
- when participant roles are not secure, use a generic participant role or
  pending_review instead of inventing causal direction;
- summarize only what the supplied source supports;
- distinguish occurred, planned, cancelled, denied, and uncertain;
- distinguish directly narrated, reported by a character, and inferred;
- set pending_review when participant, actuality, significance, or grounding
  remains materially uncertain.

Endpoint rules:
- resolved_candidate: exactly one supplied candidate id;
- resolved_joint_candidates: at least two supplied ids and all jointly occupy
  the endpoint;
- ambiguous_candidates: at least two supplied ids and the source does not
  select one;
- unresolved or non_entity_voice: no candidate id.

Do not infer final relationship phases, validity intervals, long-term emotion,
or durable effect records. B3 owns temporal state materialization. Do not write
a chapter summary, motif profile, translation, or style policy. Empty lists are
valid when the active source genuinely has no qualifying row.
"""


B2_SLIM_INTERACTION_SYSTEM_PROMPT_V5 = (
    B2_SLIM_INTERACTION_SYSTEM_PROMPT_V4.replace(
        "Prompt version: literary_b2_slim_interaction_window_v4.",
        "Prompt version: literary_b2_slim_interaction_window_v5.",
        1,
    )
    + """\

Apply these attribution and event-admission rules strictly:
- A quoted reply immediately following another participant's question or
  prompt normally belongs to the respondent established by local turn
  sequence. Do not assign it to a person named only in narration after the
  closing quotation mark.
- A clause describing a person's countenance, look, gesture, posture,
  movement, or emotional reaction is not an explicit speech tag. It cannot by
  itself make that person the speaker of the preceding quotation.
- Text explicitly presented as a reflection, thought, imagined paraphrase, or
  unspoken sentiment is not a spoken turn.
- Use ownership_or_residence_change only when the source explicitly describes
  acquisition, transfer, dispossession, moving residence, or beginning or
  ending occupancy. A statement of an already-existing ownership or residence
  is background state, not a change event, and should be omitted here.
"""
)


B2_SLIM_INTERACTION_SYSTEM_PROMPT_V6 = (
    B2_SLIM_INTERACTION_SYSTEM_PROMPT_V5.replace(
        "Prompt version: literary_b2_slim_interaction_window_v5.",
        "Prompt version: literary_b2_slim_interaction_window_v6.",
        1,
    )
    + """\

An addressee has two possibilities a speaker does not. Use them instead of
unresolved, which asserts that a listener exists and has not been identified:
- no_addressee: the utterance is not directed at anyone. A soliloquy, an
  exclamation uttered to oneself, or a remark the source says was not aimed at
  the person present belongs here, even when someone is standing nearby.
- addressee_outside_scene: the utterance is directed at someone or something
  that is not a participant in this scene - a deity, an absent person, the
  dead, an animal, or an object addressed directly.
Both take no candidate id. Where the source states who a remark was or was not
aimed at, follow the source rather than proximity.
"""
)

B2_SLIM_INTERACTION_SYSTEM_PROMPT_V7 = (
    B2_SLIM_INTERACTION_SYSTEM_PROMPT_V6.replace(
        "Prompt version: literary_b2_slim_interaction_window_v6.",
        "Prompt version: literary_b2_slim_interaction_window_v7.",
        1,
    )
    + """\

For every review request you raise, set `blocking_kind` to exactly one value.
Use `unresolved_entity` when you know who is meant in the scene but not which
supplied card that person is; list every card you are choosing between in
`competing_card_ids`. Use `scene_ambiguity` when the supplied blocks do not
establish which present participant is meant, regardless of which cards exist.
Use `anchor_defect` when the quoted span cannot be located uniquely in its
block. Use `timeline_pending` when the blocker is whether the event happened,
whether it should be recorded, or that a participant in it has no supplied
card. `competing_card_ids` must be empty unless `blocking_kind` is
`unresolved_entity`.
"""
)

B2_SLIM_INTERACTION_SYSTEM_PROMPT_V8 = (
    B2_SLIM_INTERACTION_SYSTEM_PROMPT_V7.replace(
        "Prompt version: literary_b2_slim_interaction_window_v7.",
        "Prompt version: literary_b2_slim_interaction_window_v8.",
        1,
    ).replace(
        "list every card you are choosing between in\n"
        "`competing_card_ids`.",
        "list every card you are choosing between in `competing_card_ids`, giving each\n"
        "one as the exact `candidate_card_id` string copied from the supplied\n"
        "candidate cards - never a surface name, never an abbreviation, never a\n"
        "position or index.",
        1,
    )
)

B2_SLIM_INTERACTION_SYSTEM_PROMPT_V9 = (
    B2_SLIM_INTERACTION_SYSTEM_PROMPT_V8.replace(
        "Prompt version: literary_b2_slim_interaction_window_v8.",
        "Prompt version: literary_b2_slim_interaction_window_v9.",
        1,
    ).replace(
        "card. `competing_card_ids` must be empty unless `blocking_kind` is\n"
        "`unresolved_entity`.",
        "card. Use `frame_structure` when the blocker is where a scene boundary "
        "falls or\n"
        "which narrator frames the passage - a question about the frame itself, "
        "not\n"
        "about who speaks or is addressed within it. `competing_card_ids` must be "
        "empty\n"
        "unless `blocking_kind` is `unresolved_entity`.",
        1,
    )
)

B2_SLIM_INTERACTION_SYSTEM_PROMPT_V10 = (
    B2_SLIM_INTERACTION_SYSTEM_PROMPT_V9.replace(
        "Prompt version: literary_b2_slim_interaction_window_v9.",
        "Prompt version: literary_b2_slim_interaction_window_v10.",
        1,
    )
    + """\

Review kinds about an event belong to the temporal route. When `review_kind`
is `event_participant`, `event_significance`, or `event_actuality`, set
`blocking_kind` to `timeline_pending`, including when the uncertainty is which
scene participant took part. Do not classify those event review kinds as
`scene_ambiguity`; that route is reserved for speaker and addressee endpoint
questions.
"""
)

B2_SLIM_INTERACTION_SYSTEM_PROMPT_V11 = (
    B2_SLIM_INTERACTION_SYSTEM_PROMPT_V10.replace(
        "Prompt version: literary_b2_slim_interaction_window_v10.",
        "Prompt version: literary_b2_slim_interaction_window_v11.",
        1,
    )
    + """\

`register_cue` records only the social stance of the turn. Use one supplied
vocabulary value, `unclear` when the stance cannot be determined, or `other`
when the stance is clear but unlisted. When and only when it is `other`, put a
short exact description in `register_cue_raw`; otherwise set that field to
null. `delivery_tone` is a separate short phrase for audible or emotional
delivery, such as mournful, trembling, pleading, or weary; set it to null when
the source supplies no useful tone. Never put delivery tone in `register_cue`.
"""
)


_SLIM_ENDPOINT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["surface", "resolution_status", "candidate_card_ids"],
    "properties": {
        "surface": {"type": ["string", "null"], "maxLength": 160},
        "resolution_status": {
            "type": "string",
            # no_addressee and addressee_outside_scene exist because an
            # addressee may be absent in a way a speaker never is: a soliloquy
            # has no listener, and an invocation is aimed outside the scene.
            # Both used to collapse into "unresolved", which asserts a listener
            # exists and was not identified - the opposite of the truth, and it
            # sends a settled case to a recovery pass that can never close it.
            # They are carried on the shared endpoint enum rather than a
            # separate addressee schema: the transport contract admits one
            # canonical endpoint shape, so the restriction to addressee is
            # stated in the prompt and enforced downstream, not by shape.
            "enum": [
                "resolved_candidate",
                "resolved_joint_candidates",
                "ambiguous_candidates",
                "unresolved",
                "non_entity_voice",
                "no_addressee",
                "addressee_outside_scene",
            ],
        },
        "candidate_card_ids": {
            "type": "array",
            "maxItems": 12,
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1, "maxLength": 160},
        },
    },
}

# Keep the response able to repeat every candidate the bounded interaction
# profile may supply; b2_context_v1 rejects profile caps above 128.
_INTERACTION_CANDIDATE_CARD_MAX_ITEMS = 128
_INTERACTION_ENDPOINT_SCHEMA = deepcopy(_SLIM_ENDPOINT_SCHEMA)
_INTERACTION_ENDPOINT_SCHEMA["properties"]["candidate_card_ids"]["maxItems"] = (
    _INTERACTION_CANDIDATE_CARD_MAX_ITEMS
)


_PARTICIPANT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["role", "surface", "resolution_status", "candidate_card_ids"],
    "properties": {
        "role": {
            "type": "string",
            "enum": [
                "initiator",
                "affected",
                "participant",
                "counterpart",
                "source_of_report",
                "witness",
                "location",
                "cause",
                "beneficiary",
                "object",
            ],
        },
        "surface": {"type": ["string", "null"], "maxLength": 160},
        "resolution_status": deepcopy(
            _SLIM_ENDPOINT_SCHEMA["properties"]["resolution_status"]
        ),
        "candidate_card_ids": deepcopy(
            _INTERACTION_ENDPOINT_SCHEMA["properties"]["candidate_card_ids"]
        ),
    },
}


def b2_frame_response_schema_v2() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "chapter_id",
            "frame_starts",
            "review_requests",
        ],
        "properties": {
            "schema_version": {
                "type": "string",
                "const": "literary_b2_frame_response_v2",
            },
            "chapter_id": {"type": "string", "minLength": 1, "maxLength": 160},
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
                        "narrative_mode",
                        "boundary_cue_anchor",
                    ],
                    "properties": {
                        "start_block_id": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 160,
                        },
                        "narrator_surface": {
                            "type": ["string", "null"],
                            "maxLength": 160,
                        },
                        "narrator_status": {
                            "type": "string",
                            "enum": [
                                "resolved_candidate",
                                "ambiguous_candidates",
                                "external_or_authorial",
                                "unknown",
                            ],
                        },
                        "candidate_card_ids": deepcopy(
                            _SLIM_ENDPOINT_SCHEMA["properties"]["candidate_card_ids"]
                        ),
                        "narrative_mode": {
                            "type": "string",
                            "enum": [
                                "direct_current",
                                "recollected",
                                "embedded_story",
                                "quoted_document",
                                "dream_or_vision",
                                "external_narration",
                                "unclear",
                            ],
                        },
                        "boundary_cue_anchor": {
                            "type": ["string", "null"],
                            "maxLength": 300,
                        },
                    },
                },
            },
            "review_requests": {
                "type": "array",
                "maxItems": 32,
                "items": _review_request_schema(
                    ["narrator_identity", "frame_boundary", "frame_context"],
                    max_source_block_ids=8,
                ),
            },
        },
    }


def bind_b2_frame_response_schema_v2(
    *, chapter_id: str, active_block_ids: list[str], candidate_card_ids: list[str]
) -> dict[str, Any]:
    active = sorted(set(active_block_ids))
    candidates = sorted(set(candidate_card_ids))
    if not chapter_id or not active:
        raise ValueError("B2 frame response-schema bindings must be non-empty")
    schema = b2_frame_response_schema_v2()
    properties = schema["properties"]
    properties["chapter_id"]["enum"] = [chapter_id]
    start = properties["frame_starts"]["items"]["properties"]
    start["start_block_id"]["enum"] = active
    review = properties["review_requests"]["items"]["properties"]
    review["source_block_ids"]["items"]["enum"] = active
    _bind_candidate_array(start["candidate_card_ids"], candidates)
    _bind_candidate_array(review["candidate_card_ids"], candidates)
    _bind_candidate_array(review["competing_card_ids"], candidates)
    return schema


def b2_interaction_response_schema_v3() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "chapter_id",
            "window_id",
            "speaker_turns",
            "salient_events",
            "review_requests",
        ],
        "properties": {
            "schema_version": {
                "type": "string",
                "const": "literary_b2_interaction_response_v3",
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
                        "register_cue_raw",
                        "delivery_tone",
                    ],
                    "properties": {
                        "block_id": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 160,
                        },
                        "utterance_anchor": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 500,
                        },
                        "speaker": deepcopy(_INTERACTION_ENDPOINT_SCHEMA),
                        "addressee": deepcopy(_INTERACTION_ENDPOINT_SCHEMA),
                        "address_terms": {
                            "type": "array",
                            "maxItems": 8,
                            "uniqueItems": True,
                            "items": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 160,
                            },
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
                                "other",
                            ],
                        },
                        "register_cue_raw": {
                            "anyOf": [
                                {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 60,
                                    "pattern": "^[^\\r\\n]+$",
                                },
                                {"type": "null"},
                            ]
                        },
                        "delivery_tone": {
                            "anyOf": [
                                {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 60,
                                    "pattern": "^[^\\r\\n]+$",
                                },
                                {"type": "null"},
                            ]
                        },
                    },
                },
            },
            "salient_events": {
                "type": "array",
                "maxItems": 96,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "source_block_ids",
                        "anchor_block_id",
                        "event_anchor",
                        "event_kind",
                        "event_scope",
                        "participants",
                        "summary",
                        "memory_role",
                        "event_status",
                        "evidence_mode",
                        "review_status",
                    ],
                    "properties": {
                        "source_block_ids": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 28,
                            "uniqueItems": True,
                            "items": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 160,
                            },
                        },
                        "anchor_block_id": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 160,
                        },
                        "event_anchor": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 500,
                        },
                        "event_kind": {
                            "type": "string",
                            "enum": [
                                "relationship_bearing_interaction",
                                "commitment_or_separation",
                                "life_status_change",
                                "identity_or_role_change",
                                "ownership_or_residence_change",
                                "durable_physical_change",
                                "world_state_change",
                                "other_salient_event",
                            ],
                        },
                        "event_scope": {
                            "type": "string",
                            "enum": [
                                "interpersonal",
                                "household",
                                "institutional",
                                "local",
                                "regional",
                                "societal",
                                "environmental",
                                "unknown",
                            ],
                        },
                        "participants": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 12,
                            "items": deepcopy(_PARTICIPANT_SCHEMA),
                        },
                        "summary": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 500,
                        },
                        "memory_role": {
                            "type": "string",
                            "enum": [
                                "relationship_evidence",
                                "durable_state_change",
                                "world_state_change",
                            ],
                        },
                        "event_status": {
                            "type": "string",
                            "enum": [
                                "occurred",
                                "planned",
                                "cancelled",
                                "denied",
                                "uncertain",
                            ],
                        },
                        "evidence_mode": {
                            "type": "string",
                            "enum": [
                                "directly_narrated",
                                "reported_by_character",
                                "inferred",
                            ],
                        },
                        "review_status": {
                            "type": "string",
                            "enum": ["resolved", "pending_review"],
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
                        "speaker_attribution",
                        "addressee_identity",
                        "event_participant",
                        "event_actuality",
                        "event_significance",
                        "source_anchor",
                        "frame_context",
                    ],
                    max_source_block_ids=28,
                    max_candidate_card_ids=_INTERACTION_CANDIDATE_CARD_MAX_ITEMS,
                ),
            },
        },
    }


def bind_b2_interaction_response_schema_v3(
    *,
    chapter_id: str,
    window_id: str,
    active_block_ids: list[str],
    candidate_card_ids: list[str],
) -> dict[str, Any]:
    active = sorted(set(active_block_ids))
    candidates = sorted(set(candidate_card_ids))
    if not chapter_id or not window_id or not active:
        raise ValueError("B2 interaction response-schema bindings must be non-empty")
    schema = b2_interaction_response_schema_v3()
    properties = schema["properties"]
    properties["chapter_id"]["enum"] = [chapter_id]
    properties["window_id"]["enum"] = [window_id]

    turn = properties["speaker_turns"]["items"]["properties"]
    event = properties["salient_events"]["items"]["properties"]
    review = properties["review_requests"]["items"]["properties"]
    turn["block_id"]["enum"] = active
    event["source_block_ids"]["items"]["enum"] = active
    event["anchor_block_id"]["enum"] = active
    review["source_block_ids"]["items"]["enum"] = active

    candidate_arrays = [
        turn["speaker"]["properties"]["candidate_card_ids"],
        turn["addressee"]["properties"]["candidate_card_ids"],
        event["participants"]["items"]["properties"]["candidate_card_ids"],
        review["candidate_card_ids"],
        review["competing_card_ids"],
    ]
    for candidate_array in candidate_arrays:
        _bind_candidate_array(candidate_array, candidates)
    return schema


def _review_request_schema(
    kinds: list[str],
    *,
    max_source_block_ids: int,
    max_candidate_card_ids: int = 12,
) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "review_kind",
            "blocking_kind",
            "source_block_ids",
            "candidate_card_ids",
            "competing_card_ids",
            "reason",
        ],
        "properties": {
            "review_kind": {"type": "string", "enum": list(kinds)},
            "blocking_kind": {
                "type": "string",
                "enum": [
                    "scene_ambiguity",
                    "unresolved_entity",
                    "anchor_defect",
                    "timeline_pending",
                    "frame_structure",
                ],
            },
            "source_block_ids": {
                "type": "array",
                "minItems": 1,
                "maxItems": max_source_block_ids,
                "uniqueItems": True,
                "items": {"type": "string", "minLength": 1, "maxLength": 160},
            },
            "candidate_card_ids": {
                **deepcopy(
                    _SLIM_ENDPOINT_SCHEMA["properties"]["candidate_card_ids"]
                ),
                "maxItems": max_candidate_card_ids,
            },
            "competing_card_ids": {
                **deepcopy(
                    _SLIM_ENDPOINT_SCHEMA["properties"]["candidate_card_ids"]
                ),
                "maxItems": max_candidate_card_ids,
            },
            "reason": {"type": "string", "minLength": 1, "maxLength": 500},
        },
    }


def _bind_candidate_array(candidate_array: dict[str, Any], candidates: list[str]) -> None:
    if candidates:
        candidate_array["items"]["enum"] = candidates
    else:
        candidate_array["maxItems"] = 0


__all__ = [
    "B2_FRAME_PROMPT_ID_V2",
    "B2_FRAME_PROMPT_ID_V3",
    "B2_FRAME_PROMPT_ID_V4",
    "B2_FRAME_PROMPT_ID_V5",
    "B2_FRAME_SYSTEM_PROMPT_V2",
    "B2_FRAME_SYSTEM_PROMPT_V3",
    "B2_FRAME_SYSTEM_PROMPT_V4",
    "B2_FRAME_SYSTEM_PROMPT_V5",
    "B2_SLIM_INTERACTION_PROMPT_ID_V3",
    "B2_SLIM_INTERACTION_PROMPT_ID_V4",
    "B2_SLIM_INTERACTION_PROMPT_ID_V5",
    "B2_SLIM_INTERACTION_PROMPT_ID_V6",
    "B2_SLIM_INTERACTION_PROMPT_ID_V7",
    "B2_SLIM_INTERACTION_PROMPT_ID_V8",
    "B2_SLIM_INTERACTION_PROMPT_ID_V9",
    "B2_SLIM_INTERACTION_PROMPT_ID_V10",
    "B2_SLIM_INTERACTION_PROMPT_ID_V11",
    "B2_SLIM_INTERACTION_SYSTEM_PROMPT_V3",
    "B2_SLIM_INTERACTION_SYSTEM_PROMPT_V4",
    "B2_SLIM_INTERACTION_SYSTEM_PROMPT_V5",
    "B2_SLIM_INTERACTION_SYSTEM_PROMPT_V6",
    "B2_SLIM_INTERACTION_SYSTEM_PROMPT_V7",
    "B2_SLIM_INTERACTION_SYSTEM_PROMPT_V8",
    "B2_SLIM_INTERACTION_SYSTEM_PROMPT_V9",
    "B2_SLIM_INTERACTION_SYSTEM_PROMPT_V10",
    "B2_SLIM_INTERACTION_SYSTEM_PROMPT_V11",
    "b2_frame_response_schema_v2",
    "b2_interaction_response_schema_v3",
    "bind_b2_frame_response_schema_v2",
    "bind_b2_interaction_response_schema_v3",
]
