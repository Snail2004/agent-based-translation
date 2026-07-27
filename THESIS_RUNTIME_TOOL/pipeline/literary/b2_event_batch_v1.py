"""Bounded same-chapter batching for B2 Event Auditor V2 components."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import hashlib
from typing import Any, Mapping, Sequence

from jsonschema import Draft202012Validator

from pipeline.literary.b2_event_authority_prompts_v2 import (
    event_review_response_schema_v2,
)
from pipeline.literary.b2_recovery_v1 import (
    B2RecoveryContractError,
    RenderedB2RecoveryRequestV1,
    render_event_review_request_v2,
    validate_event_review_response_v2,
    verify_b2_recovery_index_v1,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json
from pipeline.literary.response_normalization_v1 import (
    attach_response_normalization_notes_v1,
    normalize_code_owned_response_echoes_v1,
)


EVENT_REVIEW_BATCH_PROMPT_ID_V1 = "literary_b2_event_semantic_batch_audit_v1_2"
EVENT_REVIEW_BATCH_RESPONSE_SCHEMA_VERSION_V1 = (
    "literary_b2_event_review_batch_response_v1_1"
)
EVENT_REVIEW_BATCH_DECISION_SCHEMA_VERSION_V1 = (
    "literary_b2_event_review_batch_decision_v1_1"
)
MAX_EVENT_BATCH_COMPONENTS_V1 = 2
MAX_EVENT_CASES_PER_COMPONENT_V1 = 12
MAX_EVENT_SOURCE_BLOCKS_PER_COMPONENT_V1 = 12


EVENT_REVIEW_BATCH_SYSTEM_PROMPT_V1 = f"""\
Prompt version: {EVENT_REVIEW_BATCH_PROMPT_ID_V1}.
Return exactly one JSON object matching the supplied response schema. Return no
Markdown and no prose outside JSON.

You receive several independent Event Auditor components from one chapter.
Shared candidate cards are possible referents, not answers. For each component,
use only that component's event cases, source blocks, and relevant candidate
card ids. Never use another component's evidence to revise, split, keep, reject,
or assess an event. Exact-cover every component and every event case once.

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

A command, request, instruction, question, or reply is speech, even when its
words name a concrete action. Do not keep the speech act as a non-speech event.
Only a separately asserted performance of the named action can qualify.

For every event case, separately classify observation_channel:
- non_speech_observation: the reviewed interaction is performed through a
  concrete non-verbal action;
- communication_or_speech: the reviewed interaction is performed by speaking,
  writing, asking, ordering, greeting, replying, or otherwise communicating;
- mixed_or_uncertain: the supplied evidence mixes those channels or does not
  settle them.
Exact-cover case_channels for each component. This classification does not drop
the source observation. Code withholds ordinary relation-edge authority for
communication_or_speech and mixed_or_uncertain while retaining provenance.

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
  when direction cannot be settled;
- actuality is occurred only when the supplied narrative asserts the event in
  the represented story frame. Use reported for an attributed report that is
  not independently asserted, hypothetical_or_negated for a hypothetical or
  negated action, and uncertain otherwise;
- endpoint_status is resolved only when actor and target are sufficiently
  identified for this event, partial when exactly one side is settled, and
  pending otherwise.

Actor and target overlap is not automatically an error: a genuine self-directed
action must be kept as self_directed. But an actor merely mentioned inside a
group expression, instrument phrase, or phrase such as "between themselves and
another participant" must not become a self-directed relation. If the supplied
evidence cannot distinguish these cases, choose pending or return a conservative
endpoint_status.

Replacement anchors must be copied exactly from source blocks belonging to the
same component. Replacement endpoints may reference only candidate ids listed
for that component. Keep unresolved or ambiguous endpoints when evidence is
insufficient. Reported, hypothetical, negated, uncertain, self-directed, and
endpoint-uncertain events remain visible in history but code will not grant
them ordinary pairwise relation-edge authority. Do not invent an entity, stable
claim, relation phase, emotion history, translation, or source fact.
"""


def event_review_batch_response_schema_v1(
    *,
    chapter_id: str,
    batch_id: str,
    component_ids: Sequence[str],
    case_ids: Sequence[str],
) -> dict[str, Any]:
    ids = _validated_component_ids(component_ids)
    cases = tuple(sorted(_required_string(value, "case_id") for value in case_ids))
    if not cases or len(cases) != len(set(cases)):
        raise B2RecoveryContractError("event batch case ids are empty or repeated")
    nested = event_review_response_schema_v2()
    nested["properties"]["chapter_id"]["enum"] = [chapter_id]
    nested["properties"]["component_id"]["enum"] = list(ids)
    row_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["component_id", "result", "case_channels"],
        "properties": {
            "component_id": {"type": "string", "enum": list(ids)},
            "result": nested,
            "case_channels": {
                "type": "array",
                "minItems": 1,
                "maxItems": len(cases),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["case_id", "observation_channel"],
                    "properties": {
                        "case_id": {"type": "string", "enum": list(cases)},
                        "observation_channel": {
                            "type": "string",
                            "enum": [
                                "non_speech_observation",
                                "communication_or_speech",
                                "mixed_or_uncertain",
                            ],
                        },
                    },
                },
            },
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "chapter_id",
            "batch_id",
            "component_results",
        ],
        "properties": {
            "schema_version": {
                "type": "string",
                "const": EVENT_REVIEW_BATCH_RESPONSE_SCHEMA_VERSION_V1,
            },
            "chapter_id": {"type": "string", "const": chapter_id},
            "batch_id": {"type": "string", "const": batch_id},
            "component_results": {
                "type": "array",
                "minItems": len(ids),
                "maxItems": len(ids),
                "items": row_schema,
            },
        },
    }


def render_event_review_batch_request_v1(
    *,
    index: Mapping[str, Any],
    component_ids: Sequence[str],
    chapter_artifact: Mapping[str, Any],
    registry_ledger: Mapping[str, Any] | None = None,
) -> RenderedB2RecoveryRequestV1:
    verified = verify_b2_recovery_index_v1(index)
    ids = _validated_component_ids(component_ids)
    single_requests = [
        render_event_review_request_v2(
            index=verified,
            component_id=component_id,
            chapter_artifact=chapter_artifact,
            registry_ledger=registry_ledger,
        )
        for component_id in ids
    ]

    shared_cards: dict[str, dict[str, Any]] = {}
    components: list[dict[str, Any]] = []
    seen_cases: set[str] = set()
    seen_blocks: set[str] = set()
    authority_policy: dict[str, Any] | None = None
    registry_ledger_hash: str | None = None

    for request in single_requests:
        payload = request.semantic_payload
        component_cases = deepcopy(list(payload.get("event_cases") or []))
        component_blocks = deepcopy(list(payload.get("source_blocks") or []))
        if not component_cases or len(component_cases) > MAX_EVENT_CASES_PER_COMPONENT_V1:
            raise B2RecoveryContractError(
                "event batch component is empty or exceeds the event-case cap"
            )
        if (
            not component_blocks
            or len(component_blocks) > MAX_EVENT_SOURCE_BLOCKS_PER_COMPONENT_V1
        ):
            raise B2RecoveryContractError(
                "event batch component is empty or exceeds the source-block cap"
            )

        case_ids = {
            _required_string(case.get("case_id"), "event case_id")
            for case in component_cases
        }
        block_ids = {
            _required_string(block.get("block_id"), "event block_id")
            for block in component_blocks
        }
        if seen_cases.intersection(case_ids):
            raise B2RecoveryContractError(
                "event batch components repeat an event case"
            )
        if seen_blocks.intersection(block_ids):
            raise B2RecoveryContractError(
                "event batch requires disjoint component source blocks"
            )
        seen_cases.update(case_ids)
        seen_blocks.update(block_ids)

        relevant_card_ids: list[str] = []
        for raw_card in payload.get("candidate_cards") or []:
            card = deepcopy(dict(raw_card))
            card_id = _required_string(
                card.get("candidate_card_id"), "candidate_card_id"
            )
            existing = shared_cards.get(card_id)
            if existing is not None and canonical_json(existing) != canonical_json(card):
                raise B2RecoveryContractError(
                    "event batch candidate card has conflicting payloads"
                )
            shared_cards[card_id] = card
            relevant_card_ids.append(card_id)

        current_policy = deepcopy(dict(payload.get("authority_policy") or {}))
        if authority_policy is None:
            authority_policy = current_policy
        elif canonical_json(authority_policy) != canonical_json(current_policy):
            raise B2RecoveryContractError(
                "event batch components carry different authority policies"
            )
        current_ledger_hash = payload.get("registry_recovery_ledger_hash")
        if registry_ledger_hash is None:
            registry_ledger_hash = current_ledger_hash
        elif registry_ledger_hash != current_ledger_hash:
            raise B2RecoveryContractError(
                "event batch components cite different registry ledgers"
            )

        components.append(
            {
                "component_id": request.component_id,
                "component_contract_fingerprint": request.request_fingerprint,
                "relevant_candidate_card_ids": sorted(relevant_card_ids),
                "event_cases": component_cases,
                "source_blocks": component_blocks,
            }
        )

    batch_basis = {
        "recovery_index_hash": verified["recovery_index_hash"],
        "registry_recovery_ledger_hash": registry_ledger_hash,
        "source_b2_artifact_hash": verified["source_b2_artifact_hash"],
        "chapter_id": verified["chapter_id"],
        "component_ids": list(ids),
    }
    batch_id = f"b2evtbatch1_{canonical_hash(batch_basis)[:20]}"
    response_schema = event_review_batch_response_schema_v1(
        chapter_id=verified["chapter_id"],
        batch_id=batch_id,
        component_ids=ids,
        case_ids=sorted(seen_cases),
    )
    payload = {
        "contract_version": "literary_b2_event_review_batch_contract_v1",
        "recovery_index_hash": verified["recovery_index_hash"],
        "registry_recovery_ledger_hash": registry_ledger_hash,
        "source_b2_artifact_hash": verified["source_b2_artifact_hash"],
        "chapter_id": verified["chapter_id"],
        "batch_id": batch_id,
        "component_ids": list(ids),
        "shared_candidate_cards": [
            shared_cards[card_id] for card_id in sorted(shared_cards)
        ],
        "components": components,
        "authority_policy": authority_policy or {},
    }
    fingerprint = canonical_hash(
        {
            "request_kind": "event_semantic_review_batch",
            "prompt_id": EVENT_REVIEW_BATCH_PROMPT_ID_V1,
            "prompt_sha256": hashlib.sha256(
                EVENT_REVIEW_BATCH_SYSTEM_PROMPT_V1.encode("utf-8")
            ).hexdigest(),
            "semantic_payload": payload,
            "response_schema": response_schema,
        }
    )
    return RenderedB2RecoveryRequestV1(
        request_kind="event_semantic_review_batch",
        component_id=batch_id,
        request_fingerprint=fingerprint,
        messages=(
            {"role": "system", "content": EVENT_REVIEW_BATCH_SYSTEM_PROMPT_V1},
            {"role": "user", "content": canonical_json(payload)},
        ),
        response_schema=response_schema,
        semantic_payload=payload,
    )


def validate_event_review_batch_response_v1(
    response: Mapping[str, Any],
    *,
    index: Mapping[str, Any],
    component_ids: Sequence[str],
    chapter_artifact: Mapping[str, Any],
    registry_ledger: Mapping[str, Any] | None,
    request_fingerprint: str,
) -> dict[str, Any]:
    verified = verify_b2_recovery_index_v1(index)
    ids = _validated_component_ids(component_ids)
    rendered = render_event_review_batch_request_v1(
        index=verified,
        component_ids=ids,
        chapter_artifact=chapter_artifact,
        registry_ledger=registry_ledger,
    )
    if request_fingerprint != rendered.request_fingerprint:
        raise B2RecoveryContractError(
            "event batch request fingerprint differs from rendered contract"
        )
    normalized_response, response_normalization_notes = (
        normalize_code_owned_response_echoes_v1(
            response,
            expected={
                "chapter_id": verified["chapter_id"],
                "batch_id": rendered.component_id,
            },
        )
    )
    raw_component_results = normalized_response.get("component_results")
    if isinstance(raw_component_results, list):
        for index, raw_row in enumerate(raw_component_results):
            if not isinstance(raw_row, Mapping):
                continue
            nested = raw_row.get("result")
            if not isinstance(nested, Mapping):
                continue
            normalized_nested, nested_notes = (
                normalize_code_owned_response_echoes_v1(
                    nested,
                    expected={"chapter_id": verified["chapter_id"]},
                )
            )
            raw_row["result"] = normalized_nested
            for note in nested_notes:
                note["field_path"] = (
                    f"/component_results/{index}/result/{note['field']}"
                )
            response_normalization_notes.extend(nested_notes)
    errors = sorted(
        Draft202012Validator(rendered.response_schema).iter_errors(
            normalized_response
        ),
        key=lambda row: tuple(str(part) for part in row.absolute_path),
    )
    if errors:
        first = errors[0]
        pointer = "/" + "/".join(str(part) for part in first.absolute_path)
        raise B2RecoveryContractError(
            f"event batch response violates schema at {pointer}: {first.message}"
        )

    rows = normalized_response.get("component_results") or []
    observed_ids = [
        _required_string(row.get("component_id"), "batch component_id")
        for row in rows
    ]
    if len(observed_ids) != len(set(observed_ids)) or set(observed_ids) != set(ids):
        raise B2RecoveryContractError(
            "event batch response does not exact-cover components"
        )

    component_fingerprints = {
        row["component_id"]: row["component_contract_fingerprint"]
        for row in rendered.semantic_payload["components"]
    }
    decisions: list[dict[str, Any]] = []
    channel_rows: list[dict[str, str]] = []
    seen_cases: set[str] = set()
    for row in rows:
        component_id = _required_string(
            row.get("component_id"), "batch component_id"
        )
        nested = row.get("result")
        if not isinstance(nested, Mapping):
            raise B2RecoveryContractError(
                "event batch component result must be an object"
            )
        if nested.get("component_id") != component_id:
            raise B2RecoveryContractError(
                "event batch wrapper and nested component differ"
            )
        expected_component_cases = {
            _required_string(case.get("case_id"), "component event case_id")
            for component in rendered.semantic_payload["components"]
            if component["component_id"] == component_id
            for case in component["event_cases"]
        }
        raw_channels = row.get("case_channels") or []
        observed_component_channels: dict[str, str] = {}
        for raw_channel in raw_channels:
            channel_case_id = _required_string(
                raw_channel.get("case_id"), "channel case_id"
            )
            if channel_case_id in observed_component_channels:
                raise B2RecoveryContractError(
                    "event batch repeats a case-channel classification"
                )
            observed_component_channels[channel_case_id] = _required_string(
                raw_channel.get("observation_channel"), "observation_channel"
            )
        if set(observed_component_channels) != expected_component_cases:
            raise B2RecoveryContractError(
                "event batch channels do not exact-cover their component"
            )
        channel_rows.extend(
            {
                "component_id": component_id,
                "case_id": case_id,
                "observation_channel": observed_component_channels[case_id],
            }
            for case_id in sorted(observed_component_channels)
        )
        decision = validate_event_review_response_v2(
            nested,
            index=verified,
            component_id=component_id,
            chapter_artifact=chapter_artifact,
            registry_ledger=registry_ledger,
            request_fingerprint=component_fingerprints[component_id],
        )
        case_ids = {
            _required_string(action.get("case_id"), "event case_id")
            for action in decision["event_actions"]
        }
        if seen_cases.intersection(case_ids):
            raise B2RecoveryContractError(
                "event batch decisions repeat an event case"
            )
        seen_cases.update(case_ids)
        decisions.append(decision)

    expected_cases = {
        case_id
        for component in verified["event_components"]
        if component["component_id"] in set(ids)
        for case_id in component["case_ids"]
    }
    if seen_cases != expected_cases:
        raise B2RecoveryContractError(
            "event batch decisions do not exact-cover selected cases"
        )
    body = {
        "schema_version": EVENT_REVIEW_BATCH_DECISION_SCHEMA_VERSION_V1,
        "validator_version": "literary_b2_event_review_batch_validator_v1",
        "recovery_index_hash": verified["recovery_index_hash"],
        "registry_recovery_ledger_hash": (
            registry_ledger.get("registry_recovery_ledger_hash")
            if registry_ledger is not None
            else None
        ),
        "source_b2_artifact_hash": verified["source_b2_artifact_hash"],
        "chapter_id": verified["chapter_id"],
        "batch_id": rendered.component_id,
        "batch_request_fingerprint": request_fingerprint,
        "component_contract_fingerprints": {
            component_id: component_fingerprints[component_id]
            for component_id in sorted(component_fingerprints)
        },
        "component_decisions": sorted(
            decisions, key=lambda row: row["component_id"]
        ),
        "case_channels": sorted(channel_rows, key=lambda row: row["case_id"]),
        "relation_authority_holds": [
            {
                "case_id": row["case_id"],
                "observation_channel": row["observation_channel"],
                "reason_code": "communication_or_uncertain_channel_holds_pairwise_authority",
            }
            for row in sorted(channel_rows, key=lambda row: row["case_id"])
            if row["observation_channel"] != "non_speech_observation"
        ],
    }
    body = attach_response_normalization_notes_v1(
        body, response_normalization_notes
    )
    return {**body, "batch_decision_hash": canonical_hash(body)}


def batch_request_payload_v1(
    request: RenderedB2RecoveryRequestV1,
) -> dict[str, Any]:
    payload = asdict(request)
    payload["messages"] = list(payload["messages"])
    return payload


def _validated_component_ids(values: Sequence[str]) -> tuple[str, ...]:
    ids = tuple(sorted(_required_string(value, "component_id") for value in values))
    if (
        len(ids) < 2
        or len(ids) > MAX_EVENT_BATCH_COMPONENTS_V1
        or len(ids) != len(set(ids))
    ):
        raise B2RecoveryContractError(
            "event batch requires two distinct component ids within cap"
        )
    return ids


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise B2RecoveryContractError(f"{label} must be a non-empty string")
    return value


__all__ = [
    "EVENT_REVIEW_BATCH_DECISION_SCHEMA_VERSION_V1",
    "EVENT_REVIEW_BATCH_PROMPT_ID_V1",
    "EVENT_REVIEW_BATCH_RESPONSE_SCHEMA_VERSION_V1",
    "EVENT_REVIEW_BATCH_SYSTEM_PROMPT_V1",
    "MAX_EVENT_BATCH_COMPONENTS_V1",
    "MAX_EVENT_CASES_PER_COMPONENT_V1",
    "MAX_EVENT_SOURCE_BLOCKS_PER_COMPONENT_V1",
    "batch_request_payload_v1",
    "event_review_batch_response_schema_v1",
    "render_event_review_batch_request_v1",
    "validate_event_review_batch_response_v1",
]
