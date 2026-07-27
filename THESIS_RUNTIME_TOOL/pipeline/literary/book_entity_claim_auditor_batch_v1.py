"""Bounded transport batching for independently validated claim components."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from pipeline.literary.book_entity_claim_auditor_batch_prompts_v1 import (
    PROMPT_ID as BATCH_PROMPT_ID,
    PROMPT_SHA256 as BATCH_PROMPT_SHA256,
    load_book_entity_claim_batch_prompt_v1,
)
from pipeline.literary.book_entity_claim_auditor_prompts_v1 import (
    PROMPT_ID as COMPONENT_PROMPT_ID,
    PROMPT_SHA256 as COMPONENT_PROMPT_SHA256,
    load_book_entity_claim_prompt_v1,
)
from pipeline.literary.book_entity_claim_auditor_v1 import (
    BookEntityClaimContractError,
    prior_claim_response_schema_v1,
    validate_prior_claim_response_v1,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json
from pipeline.literary.response_normalization_v1 import (
    attach_response_normalization_notes_v1,
    normalize_code_owned_response_echoes_v1,
)


BATCH_REQUEST_SCHEMA_VERSION = "prior_claim_transport_batch_request_v2"
BATCH_DECISION_SCHEMA_VERSION = "prior_claim_transport_batch_decision_v2"
BATCH_VALIDATOR_VERSION = "book_entity_claim_auditor_batch_validator_v2"
DEFAULT_MAX_COMPONENTS = 3
DEFAULT_MAX_SOURCE_BLOCKS = 24
DEFAULT_MAX_INVOLVED_CHAPTERS = 3


@dataclass(frozen=True)
class RenderedPriorClaimBatchRequestV1:
    batch_id: str
    component_ids: tuple[str, ...]
    request_fingerprint: str
    messages: tuple[dict[str, str], ...]
    response_schema: dict[str, Any]
    semantic_payload: dict[str, Any]


def _clone(value: Any) -> Any:
    return deepcopy(value)


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BookEntityClaimContractError(f"{label} must be a non-empty string")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        raise BookEntityClaimContractError(
            f"{label} field set differs; missing={sorted(expected-actual)}, "
            f"foreign={sorted(actual-expected)}"
        )


def _verify_component_request(
    request: Mapping[str, Any], *, component_prompt: str
) -> dict[str, Any]:
    if not isinstance(request, Mapping):
        raise BookEntityClaimContractError("component request must be an object")
    _exact_keys(
        request,
        {
            "component_id",
            "request_fingerprint",
            "messages",
            "response_schema",
            "semantic_payload",
        },
        "rendered component request",
    )
    component_id = _required_string(request.get("component_id"), "component_id")
    payload = request.get("semantic_payload")
    if not isinstance(payload, Mapping):
        raise BookEntityClaimContractError("component semantic payload must be an object")
    if payload.get("component_id") != component_id:
        raise BookEntityClaimContractError("component request and payload ids differ")
    raw_tickets = payload.get("tickets")
    if not isinstance(raw_tickets, list) or not raw_tickets:
        raise BookEntityClaimContractError("component payload tickets must be a list")
    ticket_ids = [
        _required_string(row.get("ticket_id"), "component ticket_id")
        for row in raw_tickets
        if isinstance(row, Mapping)
    ]
    if len(ticket_ids) != len(raw_tickets) or len(ticket_ids) != len(set(ticket_ids)):
        raise BookEntityClaimContractError("component payload tickets are invalid")
    schema = prior_claim_response_schema_v1()
    schema["properties"]["component_id"]["enum"] = [component_id]
    schema["properties"]["ticket_actions"]["items"]["properties"]["ticket_id"][
        "enum"
    ] = ticket_ids
    if canonical_json(request.get("response_schema")) != canonical_json(schema):
        raise BookEntityClaimContractError("component response schema differs")
    messages = request.get("messages")
    if not isinstance(messages, (list, tuple)) or len(messages) != 2:
        raise BookEntityClaimContractError("component messages must contain two rows")
    if messages[0] != {"role": "system", "content": component_prompt}:
        raise BookEntityClaimContractError("component system prompt differs")
    if messages[1] != {"role": "user", "content": canonical_json(payload)}:
        raise BookEntityClaimContractError("component user payload differs")
    expected_fingerprint = canonical_hash(
        {
            "prompt_id": COMPONENT_PROMPT_ID,
            "prompt_sha256": COMPONENT_PROMPT_SHA256,
            "semantic_payload": payload,
            "response_schema": schema,
        }
    )
    if request.get("request_fingerprint") != expected_fingerprint:
        raise BookEntityClaimContractError("component request fingerprint differs")
    required_payload = {
        "contract_version",
        "ticket_index_hash",
        "registry_generation_hash",
        "component_id",
        "tickets",
        "prior_cards",
        "chapter_gists",
        "source_blocks",
        "allowed_revised_values",
    }
    _exact_keys(payload, required_payload, "component semantic payload")
    return _clone(dict(payload))


def prior_claim_batch_response_schema_v1() -> dict[str, Any]:
    component_result = prior_claim_response_schema_v1()
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["batch_id", "component_results"],
        "properties": {
            "batch_id": {"type": "string", "minLength": 1},
            "component_results": {
                "type": "array",
                "items": component_result,
                "minItems": 2,
                "maxItems": DEFAULT_MAX_COMPONENTS,
            },
        },
    }


def render_prior_claim_batch_request_v1(
    *,
    component_requests: Sequence[Mapping[str, Any]],
    design_doc: Path,
    max_components: int = DEFAULT_MAX_COMPONENTS,
    max_source_blocks: int = DEFAULT_MAX_SOURCE_BLOCKS,
    max_involved_chapters: int = DEFAULT_MAX_INVOLVED_CHAPTERS,
) -> RenderedPriorClaimBatchRequestV1:
    if not 2 <= len(component_requests) <= max_components:
        raise BookEntityClaimContractError("batch component count is outside bounds")
    component_prompt = load_book_entity_claim_prompt_v1(design_doc)
    payloads = [
        _verify_component_request(row, component_prompt=component_prompt)
        for row in component_requests
    ]
    payloads.sort(key=lambda row: row["component_id"])
    component_ids = tuple(row["component_id"] for row in payloads)
    if len(set(component_ids)) != len(component_ids):
        raise BookEntityClaimContractError("batch repeats a component id")
    ticket_index_hashes = {row["ticket_index_hash"] for row in payloads}
    generation_hashes = {row["registry_generation_hash"] for row in payloads}
    allowed_values = {canonical_json(row["allowed_revised_values"]) for row in payloads}
    if len(ticket_index_hashes) != 1 or len(generation_hashes) != 1:
        raise BookEntityClaimContractError("batch components come from different state")
    if len(allowed_values) != 1:
        raise BookEntityClaimContractError("batch components use different closed values")

    chapter_sets: list[set[str]] = []
    ticket_ids: set[str] = set()
    card_by_id: dict[str, dict[str, Any]] = {}
    gist_by_chapter: dict[str, dict[str, Any]] = {}
    source_by_id: dict[str, dict[str, Any]] = {}
    components: list[dict[str, Any]] = []
    for payload in payloads:
        component_id = payload["component_id"]
        source_ids: list[str] = []
        chapters: set[str] = set()
        for block in payload["source_blocks"]:
            if not isinstance(block, Mapping):
                raise BookEntityClaimContractError("source block must be an object")
            block_id = _required_string(block.get("block_id"), "source block id")
            chapter_id = _required_string(block.get("chapter_id"), "source chapter id")
            source_ids.append(block_id)
            chapters.add(chapter_id)
            base = {
                key: _clone(value)
                for key, value in block.items()
                if key not in {"evidence_roles", "ticket_ids"}
            }
            observed = source_by_id.get(block_id)
            if observed is None:
                source_by_id[block_id] = {
                    **base,
                    "evidence_roles": set(block.get("evidence_roles") or []),
                    "ticket_ids": set(block.get("ticket_ids") or []),
                    "component_ids": {component_id},
                }
            else:
                observed_base = {
                    key: value
                    for key, value in observed.items()
                    if key not in {"evidence_roles", "ticket_ids", "component_ids"}
                }
                if canonical_json(observed_base) != canonical_json(base):
                    raise BookEntityClaimContractError("shared source block differs by component")
                observed["evidence_roles"].update(block.get("evidence_roles") or [])
                observed["ticket_ids"].update(block.get("ticket_ids") or [])
                observed["component_ids"].add(component_id)
        if not chapters:
            raise BookEntityClaimContractError("component has no source chapter")
        chapter_sets.append(chapters)

        component_ticket_ids: list[str] = []
        for ticket in payload["tickets"]:
            ticket_id = _required_string(ticket.get("ticket_id"), "ticket id")
            if ticket_id in ticket_ids:
                raise BookEntityClaimContractError("ticket appears in multiple components")
            ticket_ids.add(ticket_id)
            component_ticket_ids.append(ticket_id)
        prior_card_ids: list[str] = []
        for card in payload["prior_cards"]:
            card_id = _required_string(card.get("prior_card_id"), "prior card id")
            prior_card_ids.append(card_id)
            if card_id in card_by_id and canonical_json(card_by_id[card_id]) != canonical_json(card):
                raise BookEntityClaimContractError("shared prior card differs by component")
            card_by_id[card_id] = _clone(dict(card))
        for gist in payload["chapter_gists"]:
            chapter_id = _required_string(gist.get("chapter_id"), "gist chapter id")
            if chapter_id in gist_by_chapter and canonical_json(gist_by_chapter[chapter_id]) != canonical_json(gist):
                raise BookEntityClaimContractError("shared chapter gist differs")
            gist_by_chapter[chapter_id] = _clone(dict(gist))
        components.append(
            {
                "component_id": component_id,
                "ticket_ids": sorted(component_ticket_ids),
                "prior_card_ids": sorted(prior_card_ids),
                "chapter_ids": sorted(chapters),
                "source_block_ids": source_ids,
                "tickets": _clone(payload["tickets"]),
            }
        )

    shared_chapters = set.intersection(*chapter_sets)
    all_chapters = set.union(*chapter_sets)
    if not shared_chapters:
        raise BookEntityClaimContractError("batch components share no source chapter")
    if len(all_chapters) > max_involved_chapters:
        raise BookEntityClaimContractError("batch chapter count exceeds cap")
    if len(source_by_id) > max_source_blocks:
        raise BookEntityClaimContractError("batch source-block count exceeds cap")

    request_fingerprints = {
        row["component_id"]: next(
            request["request_fingerprint"]
            for request in component_requests
            if request["component_id"] == row["component_id"]
        )
        for row in payloads
    }
    batch_identity = {
        "ticket_index_hash": next(iter(ticket_index_hashes)),
        "component_requests": [
            {
                "component_id": component_id,
                "request_fingerprint": request_fingerprints[component_id],
            }
            for component_id in component_ids
        ],
        "shared_chapter_ids": sorted(shared_chapters),
    }
    batch_id = "bclaimbatch1_" + canonical_hash(batch_identity)[:20]
    source_blocks = [
        {
            **{
                key: value
                for key, value in row.items()
                if key not in {"evidence_roles", "ticket_ids", "component_ids"}
            },
            "evidence_roles": sorted(row["evidence_roles"]),
            "ticket_ids": sorted(row["ticket_ids"]),
            "component_ids": sorted(row["component_ids"]),
        }
        for row in sorted(source_by_id.values(), key=lambda value: value["book_order_index"])
    ]
    payload = {
        "contract_version": BATCH_VALIDATOR_VERSION,
        "ticket_index_hash": next(iter(ticket_index_hashes)),
        "registry_generation_hash": next(iter(generation_hashes)),
        "batch_id": batch_id,
        "batch_policy": {
            "semantic_components_independent": True,
            "cross_component_evidence_forbidden": True,
            "max_components": max_components,
            "max_source_blocks": max_source_blocks,
        },
        "shared_chapter_ids": sorted(shared_chapters),
        "components": components,
        "prior_cards": [card_by_id[key] for key in sorted(card_by_id)],
        "chapter_gists": [gist_by_chapter[key] for key in sorted(gist_by_chapter)],
        "source_blocks": source_blocks,
        "allowed_revised_values": _clone(payloads[0]["allowed_revised_values"]),
    }
    prompt = load_book_entity_claim_batch_prompt_v1(design_doc)
    schema = prior_claim_batch_response_schema_v1()
    fingerprint = canonical_hash(
        {
            "prompt_id": BATCH_PROMPT_ID,
            "prompt_sha256": BATCH_PROMPT_SHA256,
            "semantic_payload": payload,
            "response_schema": schema,
        }
    )
    return RenderedPriorClaimBatchRequestV1(
        batch_id=batch_id,
        component_ids=component_ids,
        request_fingerprint=fingerprint,
        messages=(
            {"role": "system", "content": prompt},
            {"role": "user", "content": canonical_json(payload)},
        ),
        response_schema=schema,
        semantic_payload=payload,
    )


def validate_prior_claim_batch_response_v1(
    response: Mapping[str, Any],
    *,
    index: Mapping[str, Any],
    request: RenderedPriorClaimBatchRequestV1,
) -> dict[str, Any]:
    if not isinstance(response, Mapping):
        raise BookEntityClaimContractError("batch response must be an object")
    normalized_response, response_normalization_notes = (
        normalize_code_owned_response_echoes_v1(
            response,
            expected={"batch_id": request.batch_id},
        )
    )
    _exact_keys(
        normalized_response,
        {"batch_id", "component_results"},
        "batch response",
    )
    rows = normalized_response.get("component_results")
    if not isinstance(rows, list):
        raise BookEntityClaimContractError("component_results must be a list")
    expected = set(request.component_ids)
    observed: set[str] = set()
    decisions: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise BookEntityClaimContractError("component result must be an object")
        component_id = _required_string(row.get("component_id"), "component result id")
        if component_id not in expected or component_id in observed:
            raise BookEntityClaimContractError("component results do not exact-cover batch")
        observed.add(component_id)
        decisions.append(
            validate_prior_claim_response_v1(
                row,
                index=index,
                request_fingerprint=request.request_fingerprint,
            )
        )
    if observed != expected:
        raise BookEntityClaimContractError("component results must exact-cover batch")
    decisions.sort(key=lambda row: row["component_id"])
    body = {
        "schema_version": BATCH_DECISION_SCHEMA_VERSION,
        "validator_version": BATCH_VALIDATOR_VERSION,
        "ticket_index_hash": request.semantic_payload["ticket_index_hash"],
        "registry_generation_hash": request.semantic_payload["registry_generation_hash"],
        "batch_id": request.batch_id,
        "request_fingerprint": request.request_fingerprint,
        "component_decisions": decisions,
    }
    body = attach_response_normalization_notes_v1(
        body, response_normalization_notes
    )
    return {**body, "batch_decision_hash": canonical_hash(body)}


__all__ = [
    "BATCH_DECISION_SCHEMA_VERSION",
    "BATCH_REQUEST_SCHEMA_VERSION",
    "BATCH_VALIDATOR_VERSION",
    "DEFAULT_MAX_COMPONENTS",
    "DEFAULT_MAX_INVOLVED_CHAPTERS",
    "DEFAULT_MAX_SOURCE_BLOCKS",
    "RenderedPriorClaimBatchRequestV1",
    "prior_claim_batch_response_schema_v1",
    "render_prior_claim_batch_request_v1",
    "validate_prior_claim_batch_response_v1",
]
