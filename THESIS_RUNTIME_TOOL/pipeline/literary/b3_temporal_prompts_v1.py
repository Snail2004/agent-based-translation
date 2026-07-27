"""Book-neutral prompt and response schema for Literary B3 temporal memory."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Sequence


B3_TEMPORAL_PROMPT_ID_V1 = "literary_b3_temporal_state_batch_v1"


B3_TEMPORAL_SYSTEM_PROMPT_V1 = """\
Prompt version: literary_b3_temporal_state_batch_v1.
Return exactly one JSON object matching the supplied response schema. Return no
Markdown and no prose outside JSON.

You receive one or more independent temporal components from one chapter.
All components are intentionally packed into one request when they fit. Treat
each component independently and return exactly one component_result for every
supplied component_id. Do not compare entities that the supplied evidence does
not connect.

Candidate cards and referent refs are possible referents under the stated
identity scope. They are not permission to merge, split, rename, or update an
entity. Use only supplied referent refs. If the evidence may concern a different
referent, choose pending_review with identity_review.

Your task is to preserve only temporal information that can matter beyond the
immediate sentence or action:
- durable relationship or durable interpersonal disposition;
- life status, residence, ownership, role, or stable name usage;
- durable physical state of an important referent;
- larger world state affecting the story environment.

Do not inventory ordinary movement, object handling, momentary emotion, every
verb, every topic of dialogue, or every speaker turn. The source already retains
those details. A component with no durable temporal information should be
no_durable_change; this is a normal useful result.

Distinguish operations carefully:
- open_state: the source supports a state beginning here;
- change_state: one supplied prior open state changes to a new value here;
- close_state: one supplied prior open state ends here;
- reinforce_state: new evidence supports the same supplied open state;
- reveal_only: the passage reveals a pre-existing state but does not establish
  when it began.

Never turn planned, cancelled, denied, or uncertain content into an occurred
state. Preserve the supplied event_status. Distinguish current progression,
earlier story time, prospective content, nonactual content, and unknown time.
A dream, vision, hypothetical, false report, or character belief does not
silently mutate current story-world state. A reported fact may remain pending
when the report is not enough to grant authority.

Dialogue is evidence, not automatically a state. Address terms and local
register may support a relation, but an insult, courtesy, or temporary mood is
not automatically a durable relationship. A statement that merely reveals an
existing relation is reveal_only rather than a change.

For every proposed action cite only supplied event ids, turn ids, source block
ids, and frame segment ids. At least one event or turn id is required. Do not
invent state IDs, predecessor IDs, dates, identity decisions, chapter summaries,
motifs, style rules, or translations. Code computes state lineage after local
validation.

Use stable_claim_review for a disputed stable role/name/life claim,
identity_review for same-versus-different referent uncertainty, and
temporal_review for unresolved state timing or relation meaning. Pending is
preferable to a guessed authoritative transition.
"""


_STATE_ACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "operation",
        "state_domain",
        "subject_referent_refs",
        "counterpart_referent_refs",
        "state_value",
        "event_status",
        "temporal_position",
        "source_event_ids",
        "source_turn_ids",
        "source_block_ids",
        "frame_segment_ids",
        "reason",
    ],
    "properties": {
        "operation": {
            "type": "string",
            "enum": [
                "open_state",
                "change_state",
                "close_state",
                "reinforce_state",
                "reveal_only",
            ],
        },
        "state_domain": {
            "type": "string",
            "enum": [
                "relationship",
                "durable_disposition",
                "life_status",
                "residence",
                "ownership",
                "role",
                "name_usage",
                "durable_physical_state",
                "world_state",
                "other_durable_state",
            ],
        },
        "subject_referent_refs": {
            "type": "array",
            "minItems": 1,
            "maxItems": 8,
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1, "maxLength": 160},
        },
        "counterpart_referent_refs": {
            "type": "array",
            "maxItems": 8,
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1, "maxLength": 160},
        },
        "state_value": {"type": "string", "minLength": 1, "maxLength": 320},
        "event_status": {
            "type": "string",
            "enum": ["occurred", "planned", "cancelled", "denied", "uncertain"],
        },
        "temporal_position": {
            "type": "string",
            "enum": [
                "current_progression",
                "prior_story_time",
                "prospective",
                "nonactual",
                "unknown",
            ],
        },
        "source_event_ids": {
            "type": "array",
            "maxItems": 16,
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1, "maxLength": 160},
        },
        "source_turn_ids": {
            "type": "array",
            "maxItems": 48,
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1, "maxLength": 160},
        },
        "source_block_ids": {
            "type": "array",
            "minItems": 1,
            "maxItems": 24,
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1, "maxLength": 160},
        },
        "frame_segment_ids": {
            "type": "array",
            "minItems": 1,
            "maxItems": 12,
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1, "maxLength": 160},
        },
        "reason": {"type": "string", "minLength": 1, "maxLength": 500},
    },
}


def b3_temporal_response_schema_v1() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "chapter_id", "batch_id", "component_results"],
        "properties": {
            "schema_version": {
                "type": "string",
                "const": "literary_b3_temporal_response_v1",
            },
            "chapter_id": {"type": "string", "minLength": 1, "maxLength": 160},
            "batch_id": {"type": "string", "minLength": 1, "maxLength": 200},
            "component_results": {
                "type": "array",
                "maxItems": 64,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "component_id",
                        "disposition",
                        "state_actions",
                        "pending_route",
                        "pending_reason",
                    ],
                    "properties": {
                        "component_id": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 200,
                        },
                        "disposition": {
                            "type": "string",
                            "enum": [
                                "state_actions_proposed",
                                "no_durable_change",
                                "pending_review",
                            ],
                        },
                        "state_actions": {
                            "type": "array",
                            "maxItems": 12,
                            "items": deepcopy(_STATE_ACTION_SCHEMA),
                        },
                        "pending_route": {
                            "type": "string",
                            "enum": [
                                "none",
                                "temporal_review",
                                "stable_claim_review",
                                "identity_review",
                            ],
                        },
                        "pending_reason": {
                            "type": ["string", "null"],
                            "maxLength": 500,
                        },
                    },
                },
            },
        },
    }


def bind_b3_temporal_response_schema_v1(
    *,
    chapter_id: str,
    batch_id: str,
    component_ids: Sequence[str],
    referent_refs: Sequence[str],
    event_ids: Sequence[str],
    turn_ids: Sequence[str],
    block_ids: Sequence[str],
    frame_segment_ids: Sequence[str],
) -> dict[str, Any]:
    if not chapter_id or not batch_id or not component_ids:
        raise ValueError("B3 response-schema bindings must be non-empty")
    schema = b3_temporal_response_schema_v1()
    root = schema["properties"]
    root["chapter_id"]["enum"] = [chapter_id]
    root["batch_id"]["enum"] = [batch_id]
    result = root["component_results"]["items"]["properties"]
    result["component_id"]["enum"] = sorted(set(component_ids))
    action = result["state_actions"]["items"]["properties"]
    bindings = [
        (action["subject_referent_refs"], referent_refs),
        (action["counterpart_referent_refs"], referent_refs),
        (action["source_event_ids"], event_ids),
        (action["source_turn_ids"], turn_ids),
        (action["source_block_ids"], block_ids),
        (action["frame_segment_ids"], frame_segment_ids),
    ]
    for target, values in bindings:
        normalized = sorted(set(values))
        if normalized:
            target["items"]["enum"] = normalized
        else:
            target["maxItems"] = 0
    return schema


__all__ = [
    "B3_TEMPORAL_PROMPT_ID_V1",
    "B3_TEMPORAL_SYSTEM_PROMPT_V1",
    "b3_temporal_response_schema_v1",
    "bind_b3_temporal_response_schema_v1",
]
