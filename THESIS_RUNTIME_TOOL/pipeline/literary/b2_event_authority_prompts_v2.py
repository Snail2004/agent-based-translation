"""Book-neutral Event Auditor V2 prompt and strict response schema."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from pipeline.literary.b2_recovery_prompts_v1 import (
    _NULLABLE_STRING,
    _replacement_event_schema_v1,
)


EVENT_REVIEW_PROMPT_ID_V2 = "literary_b2_event_semantic_audit_v2_2"


EVENT_REVIEW_SYSTEM_PROMPT_V2 = """\
Prompt version: literary_b2_event_semantic_audit_v2_2.
Return exactly one JSON object matching the supplied response schema. Return no
Markdown and no prose outside JSON.

You receive one bounded component of normalized B2 interaction events, exact
source blocks, and candidate cards. Exact-cover every event case once. Candidate
cards are possible referents, not answers. Never create or modify a card.

One effective event represents one concrete non-speech observation. The actor
performs the action in action_summary. The target receives or is directly
affected by that action. A source span containing actions in opposite
directions must be split into separate events. A retrieved group is not the
target merely because its members are present in the block. An object used in
an action is not automatically the affected counterpart.

Choose keep only when the event, endpoints, kind, valence, and summary are
source-supported. Choose revise for one corrected event. Choose split for two
or three events. Choose pending when the supplied evidence cannot settle the
event. Choose reject only when the row is not a qualifying non-speech
observation.

The action fixes replacement_events cardinality:
- keep, pending, or reject: return an empty replacement_events list;
- revise: return exactly one replacement event;
- split: return exactly two or three replacement events.
For keep, assess the supplied event itself; do not copy it into
replacement_events.

effective_event_assessments must exact-cover only events that remain:
- keep: exactly one assessment for the supplied event;
- revise: exactly one assessment for the replacement event;
- split: exactly one assessment per replacement event, in the same order;
- pending or reject: an empty effective_event_assessments list.

For every event that remains after keep, revise, or split, return one assessment
in the same order as the effective events:

- directionality is one_way when one endpoint acts on another, reciprocal when
  the participants mutually perform the interaction, self_directed only when
  the source explicitly directs the action at the same referent, and unknown
  when direction cannot be settled.
- actuality is occurred only when the supplied narrative asserts the event in
  the represented story frame. Use reported for an attributed report that is
  not independently asserted, hypothetical_or_negated for a hypothetical or
  negated action, and uncertain otherwise.
- endpoint_status is resolved only when actor and target are sufficiently
  identified for this event, partial when exactly one side is settled, and
  pending otherwise.

Actor and target overlap is not automatically an error: a genuine self-directed
action must be kept as self_directed. But an actor merely mentioned inside a
group expression, instrument phrase, or phrase such as "between themselves and
another participant" must not become a self-directed relation. If the supplied
evidence cannot distinguish these cases, choose pending or return a conservative
endpoint_status.

Replacement anchors must be copied exactly from the supplied source block.
Replacement endpoints may reference only supplied candidate ids. Keep
unresolved or ambiguous endpoints when the evidence is insufficient. Reported,
hypothetical, negated, uncertain, self-directed, and endpoint-uncertain events
remain visible in history but code will not grant them ordinary pairwise
relation-edge authority. Do not invent an entity, stable claim, relation phase,
emotion history, translation, or source fact.
"""


def _assessment_schema_v2() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["directionality", "actuality", "endpoint_status"],
        "properties": {
            "directionality": {
                "type": "string",
                "enum": ["one_way", "reciprocal", "self_directed", "unknown"],
            },
            "actuality": {
                "type": "string",
                "enum": [
                    "occurred",
                    "reported",
                    "hypothetical_or_negated",
                    "uncertain",
                ],
            },
            "endpoint_status": {
                "type": "string",
                "enum": ["resolved", "partial", "pending"],
            },
        },
    }


def event_review_response_schema_v2() -> dict[str, Any]:
    action = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "case_id",
            "action",
            "replacement_events",
            "effective_event_assessments",
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
            "effective_event_assessments": {
                "type": "array",
                "maxItems": 3,
                "items": _assessment_schema_v2(),
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
                "const": "literary_b2_event_review_response_v2",
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
    "EVENT_REVIEW_PROMPT_ID_V2",
    "EVENT_REVIEW_SYSTEM_PROMPT_V2",
    "event_review_response_schema_v2",
]
