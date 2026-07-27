"""Bounded batching for chapter-local B2 registry recovery components."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import hashlib
from typing import Any, Mapping, Sequence

from jsonschema import Draft202012Validator

from pipeline.literary.b2_recovery_prompts_v1 import (
    registry_recovery_response_schema_v1,
)
from pipeline.literary.b2_recovery_v1 import (
    B2RecoveryContractError,
    RenderedB2RecoveryRequestV1,
    render_registry_recovery_request_v1,
    validate_registry_recovery_response_v1,
    verify_b2_recovery_index_v1,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json
from pipeline.literary.response_normalization_v1 import (
    attach_response_normalization_notes_v1,
    normalize_code_owned_response_echoes_v1,
)


REGISTRY_RECOVERY_BATCH_PROMPT_ID_V1 = (
    "literary_b2_registry_recovery_batch_audit_v1_2"
)
REGISTRY_RECOVERY_BATCH_RESPONSE_SCHEMA_VERSION_V1 = (
    "literary_b2_registry_recovery_batch_response_v1_1"
)
REGISTRY_RECOVERY_BATCH_DECISION_SCHEMA_VERSION_V1 = (
    "literary_b2_registry_recovery_batch_decision_v1"
)
MAX_BATCH_COMPONENTS_V1 = 4
MAX_SOURCE_BLOCKS_PER_COMPONENT_V1 = 160


REGISTRY_RECOVERY_BATCH_SYSTEM_PROMPT_V1 = f"""\
Prompt version: {REGISTRY_RECOVERY_BATCH_PROMPT_ID_V1}.
Return exactly one JSON object matching the supplied response schema. Return no
Markdown and no prose outside JSON.

You receive several independent chapter-local registry-recovery components.
The shared candidate-card catalog is a lookup table, not an answer. Each
component lists the only candidate-card ids that may be used for that component
and carries its own tickets and source blocks. Never use evidence, candidate
cards, or provisional groups from one component to decide another component.
Exact-cover every supplied component once and every ticket in that component
once. Keep component_results separate even when two components look similar.

For issue_kind=contextual_speaker_attribution, attach_existing may retain the
supplied candidate or select another candidate listed for that component only
when the source and local turn sequence establish the speaker. A nearby
reaction, movement, facial expression, or mere appearance of a name is not a
speaker tag. Keep the ticket pending when attribution remains uncertain.

Choose attach_existing only when local evidence identifies exactly one listed
candidate. Choose create_chapter_local when local evidence establishes a
translator-relevant participant absent from the listed candidates. Tickets
inside one component may share a provisional_group_key only when local evidence
establishes the same referent. Repeated pronouns or similar descriptions alone
do not prove identity.

A recovery card is chapter-local, not a global entity or alias. Copy
canonical_surface exactly from cited source. identity_summary may contain
stable identifying information only, not a temporary action, mood,
relationship phase, or interpretation. Forms of address, insults, role
phrases, and pronouns may be evidence but are not global names.

Choose keep_pending when evidence is insufficient. Choose reject_non_registry
only when the endpoint is not a translator-relevant chapter referent. Do not
create, merge, split, or rename book-global entities. Do not infer relation
phases, translate, use outside knowledge, or imitate decisions from another
component.

All action fields are required by the JSON shape, but their values depend on
the selected action:
- attach_existing: set target_candidate_card_id to one listed candidate; set
  provisional_group_key, canonical_surface, referent_kind, identity_summary,
  and pending_reason to null.
- create_chapter_local: set target_candidate_card_id and pending_reason to
  null; provide provisional_group_key, canonical_surface, referent_kind, and
  identity_summary.
- keep_pending: provide pending_reason; set target_candidate_card_id,
  provisional_group_key, canonical_surface, referent_kind, and
  identity_summary to null.
- reject_non_registry: set all six fields above to null.
For every action, cite the supplied source_block_ids that support the verdict
and provide a concise resolution_note. These are output-contract rules, not
evidence about which action to choose.

When you answer `keep_pending`, list in `narrowed_candidate_card_ids` every
supplied card that remains possible after weighing the evidence, and omit the
ones you have ruled out. Leave the list empty only if the evidence rules out
nothing. Listing a single card does not mean you have chosen it; use
`attach_existing` only when the evidence settles the question.
"""


def registry_recovery_batch_response_schema_v1(
    *,
    chapter_id: str,
    batch_id: str,
    component_ids: Sequence[str],
) -> dict[str, Any]:
    _required_string(chapter_id, "chapter_id")
    _required_string(batch_id, "batch_id")
    _validated_component_ids(component_ids)
    single = registry_recovery_response_schema_v1()
    identifier = {"type": "string", "minLength": 1, "maxLength": 160}
    result_row = {
        "type": "object",
        "additionalProperties": False,
        "required": ["component_id", "result"],
        "properties": {
            "component_id": deepcopy(identifier),
            "result": single,
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
                "const": REGISTRY_RECOVERY_BATCH_RESPONSE_SCHEMA_VERSION_V1,
            },
            "chapter_id": deepcopy(identifier),
            "batch_id": deepcopy(identifier),
            "component_results": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_BATCH_COMPONENTS_V1,
                "items": result_row,
            },
        },
    }


def render_registry_recovery_batch_request_v1(
    *,
    index: Mapping[str, Any],
    component_ids: Sequence[str],
) -> RenderedB2RecoveryRequestV1:
    verified = verify_b2_recovery_index_v1(index)
    ids = _validated_component_ids(component_ids)
    single_requests = [
        render_registry_recovery_request_v1(
            index=verified,
            component_id=component_id,
        )
        for component_id in ids
    ]
    components: list[dict[str, Any]] = []
    shared_cards: dict[str, dict[str, Any]] = {}
    ticket_ids: list[str] = []
    for request in single_requests:
        payload = request.semantic_payload
        source_blocks = deepcopy(list(payload["source_blocks"]))
        if len(source_blocks) > MAX_SOURCE_BLOCKS_PER_COMPONENT_V1:
            raise B2RecoveryContractError(
                "registry batch component exceeds the source-block cap"
            )
        relevant_card_ids: list[str] = []
        for card in payload["candidate_cards"]:
            card_id = _required_string(
                card.get("candidate_card_id"), "candidate_card_id"
            )
            relevant_card_ids.append(card_id)
            existing = shared_cards.get(card_id)
            if existing is not None and canonical_json(existing) != canonical_json(card):
                raise B2RecoveryContractError(
                    "registry batch candidate card has conflicting payloads"
                )
            shared_cards[card_id] = deepcopy(dict(card))
        component_ticket_ids = [
            _required_string(ticket.get("ticket_id"), "ticket_id")
            for ticket in payload["tickets"]
        ]
        if set(ticket_ids).intersection(component_ticket_ids):
            raise B2RecoveryContractError(
                "registry batch components repeat a ticket"
            )
        ticket_ids.extend(component_ticket_ids)
        components.append(
            {
                "component_id": request.component_id,
                "relevant_candidate_card_ids": relevant_card_ids,
                "tickets": deepcopy(list(payload["tickets"])),
                "source_blocks": source_blocks,
            }
        )
    batch_basis = {
        "recovery_index_hash": verified["recovery_index_hash"],
        "chapter_id": verified["chapter_id"],
        "component_ids": list(ids),
    }
    batch_id = f"b2gapbatch1_{canonical_hash(batch_basis)[:20]}"
    response_schema = registry_recovery_batch_response_schema_v1(
        chapter_id=verified["chapter_id"],
        batch_id=batch_id,
        component_ids=ids,
    )
    payload = {
        "contract_version": "literary_b2_registry_recovery_batch_contract_v1",
        "recovery_index_hash": verified["recovery_index_hash"],
        "chapter_id": verified["chapter_id"],
        "batch_id": batch_id,
        "component_ids": list(ids),
        "shared_candidate_cards": [
            shared_cards[card_id] for card_id in sorted(shared_cards)
        ],
        "components": components,
        "authority_policy": {
            "new_card_scope": "chapter_local_recovery",
            "global_alias_authority": False,
            "book_global_identity_mutation": False,
            "cross_component_evidence_allowed": False,
        },
    }
    fingerprint = canonical_hash(
        {
            "request_kind": "registry_gap_recovery_batch",
            "prompt_id": REGISTRY_RECOVERY_BATCH_PROMPT_ID_V1,
            "prompt_sha256": hashlib.sha256(
                REGISTRY_RECOVERY_BATCH_SYSTEM_PROMPT_V1.encode("utf-8")
            ).hexdigest(),
            "semantic_payload": payload,
            "response_schema": response_schema,
        }
    )
    return RenderedB2RecoveryRequestV1(
        request_kind="registry_gap_recovery_batch",
        component_id=batch_id,
        request_fingerprint=fingerprint,
        messages=(
            {
                "role": "system",
                "content": REGISTRY_RECOVERY_BATCH_SYSTEM_PROMPT_V1,
            },
            {"role": "user", "content": canonical_json(payload)},
        ),
        response_schema=response_schema,
        semantic_payload=payload,
    )


def validate_registry_recovery_batch_response_v1(
    response: Mapping[str, Any],
    *,
    index: Mapping[str, Any],
    component_ids: Sequence[str],
    request_fingerprint: str,
) -> dict[str, Any]:
    verified = verify_b2_recovery_index_v1(index)
    ids = _validated_component_ids(component_ids)
    rendered = render_registry_recovery_batch_request_v1(
        index=verified,
        component_ids=ids,
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
    schema = rendered.response_schema
    errors = sorted(
        Draft202012Validator(schema).iter_errors(normalized_response),
        key=lambda row: tuple(str(part) for part in row.absolute_path),
    )
    if errors:
        first = errors[0]
        pointer = "/" + "/".join(str(part) for part in first.absolute_path)
        raise B2RecoveryContractError(
            f"registry batch response violates schema at {pointer}: {first.message}"
        )
    if rendered.request_fingerprint != request_fingerprint:
        raise B2RecoveryContractError(
            "registry batch request fingerprint differs from rendered contract"
        )
    rows = normalized_response.get("component_results") or []
    observed_ids = [
        _required_string(row.get("component_id"), "batch component_id")
        for row in rows
    ]
    if len(observed_ids) != len(set(observed_ids)) or set(observed_ids) != set(ids):
        raise B2RecoveryContractError(
            "registry batch response does not exact-cover components"
        )
    decisions: list[dict[str, Any]] = []
    quarantined_components: list[dict[str, Any]] = []
    component_errors: list[str] = []
    seen_tickets: set[str] = set()
    contract_normalizations: list[dict[str, Any]] = []
    component_by_id = {
        str(component["component_id"]): component
        for component in verified["registry_components"]
    }
    for row in rows:
        component_id = _required_string(
            row.get("component_id"), "batch component_id"
        )
        nested = row.get("result")
        if not isinstance(nested, Mapping):
            raise B2RecoveryContractError(
                "registry batch component result must be an object"
            )
        if nested.get("component_id") != component_id:
            raise B2RecoveryContractError(
                "registry batch wrapper and nested component differ"
            )
        normalized_nested = deepcopy(dict(nested))
        for action in normalized_nested.get("ticket_actions") or []:
            action_name = action.get("action")
            if action_name not in {"keep_pending", "reject_non_registry"}:
                continue
            forbidden_fields = [
                "target_candidate_card_id",
                "provisional_group_key",
                "canonical_surface",
                "referent_kind",
                "identity_summary",
            ]
            if action_name == "reject_non_registry":
                forbidden_fields.append("pending_reason")
            for field in forbidden_fields:
                if action.get(field) is None:
                    continue
                contract_normalizations.append(
                    {
                        "component_id": component_id,
                        "ticket_id": _required_string(
                            action.get("ticket_id"), "ticket_id"
                        ),
                        "action": action_name,
                        "field": field,
                        "discarded_value_hash": canonical_hash(
                            {"value": action[field]}
                        ),
                        "reason": "non_authoritative_action_cannot_carry_authority",
                    }
                )
                action[field] = None
        try:
            decision = validate_registry_recovery_response_v1(
                normalized_nested,
                index=verified,
                component_id=component_id,
                request_fingerprint=request_fingerprint,
            )
        except B2RecoveryContractError as exc:
            component_errors.append(str(exc))
            component = component_by_id[component_id]
            raw_result = deepcopy(dict(nested))
            raw_result_hash = canonical_hash(raw_result)
            raw_actions = raw_result.get("ticket_actions")
            raw_actions = raw_actions if isinstance(raw_actions, list) else []
            ticket_action_hashes = []
            for ticket_id in component["ticket_ids"]:
                action_hashes = sorted(
                    canonical_hash(dict(action))
                    for action in raw_actions
                    if isinstance(action, Mapping)
                    and action.get("ticket_id") == ticket_id
                )
                ticket_action_hashes.append(
                    {
                        "ticket_id": ticket_id,
                        "action_hashes": action_hashes or [raw_result_hash],
                    }
                )
            quarantined_components.append(
                {
                    "component_id": component_id,
                    "ticket_ids": list(component["ticket_ids"]),
                    "state": "unreviewed",
                    "reason_code": "component_semantic_contract_rejected",
                    "reason": str(exc),
                    "raw_result_hash": raw_result_hash,
                    "ticket_action_hashes": ticket_action_hashes,
                }
            )
            continue
        ticket_ids = {
            _required_string(action.get("ticket_id"), "ticket_id")
            for action in decision["ticket_actions"]
        }
        if seen_tickets.intersection(ticket_ids):
            raise B2RecoveryContractError(
                "registry batch decisions repeat a ticket"
            )
        seen_tickets.update(ticket_ids)
        decisions.append(decision)
    if not decisions:
        raise B2RecoveryContractError(
            "registry batch response has no valid component result; "
            f"first_error={component_errors[0]}"
        )
    expected_tickets = {
        ticket_id
        for component in verified["registry_components"]
        if component["component_id"] in set(ids)
        for ticket_id in component["ticket_ids"]
    }
    quarantined_tickets = {
        ticket_id
        for component in quarantined_components
        for ticket_id in component["ticket_ids"]
    }
    if (
        seen_tickets.intersection(quarantined_tickets)
        or seen_tickets.union(quarantined_tickets) != expected_tickets
    ):
        raise B2RecoveryContractError(
            "registry batch decisions and quarantines do not exact-cover selected tickets"
        )
    body = {
        "schema_version": REGISTRY_RECOVERY_BATCH_DECISION_SCHEMA_VERSION_V1,
        "validator_version": "literary_b2_registry_recovery_batch_validator_v2",
        "recovery_index_hash": verified["recovery_index_hash"],
        "chapter_id": verified["chapter_id"],
        "batch_id": rendered.component_id,
        "request_fingerprint": request_fingerprint,
        "component_decisions": sorted(
            decisions, key=lambda row: row["component_id"]
        ),
        "quarantined_components": sorted(
            quarantined_components, key=lambda row: row["component_id"]
        ),
        "contract_normalizations": sorted(
            contract_normalizations,
            key=lambda row: (
                row["component_id"],
                row["ticket_id"],
                row["field"],
            ),
        ),
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
    ids = tuple(_required_string(value, "component_id") for value in values)
    if (
        not ids
        or len(ids) > MAX_BATCH_COMPONENTS_V1
        or len(ids) != len(set(ids))
    ):
        raise B2RecoveryContractError(
            "registry batch component ids are empty, repeated, or over cap"
        )
    return ids


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise B2RecoveryContractError(f"{label} must be a non-empty string")
    return value


__all__ = [
    "MAX_BATCH_COMPONENTS_V1",
    "MAX_SOURCE_BLOCKS_PER_COMPONENT_V1",
    "REGISTRY_RECOVERY_BATCH_DECISION_SCHEMA_VERSION_V1",
    "REGISTRY_RECOVERY_BATCH_PROMPT_ID_V1",
    "REGISTRY_RECOVERY_BATCH_RESPONSE_SCHEMA_VERSION_V1",
    "REGISTRY_RECOVERY_BATCH_SYSTEM_PROMPT_V1",
    "batch_request_payload_v1",
    "registry_recovery_batch_response_schema_v1",
    "render_registry_recovery_batch_request_v1",
    "validate_registry_recovery_batch_response_v1",
]
