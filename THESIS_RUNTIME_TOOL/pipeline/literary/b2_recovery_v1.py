"""Append-only recovery and semantic review for normalized Literary B2 output."""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass
import hashlib
from typing import Any, Iterable, Mapping, Sequence

from jsonschema import Draft202012Validator

from pipeline.literary.b0_entity_inventory_experiment import REFERENT_KINDS
from pipeline.literary.b2_contract_v1 import B2ContractError, _validated_request
from pipeline.literary.b2_live_canary_v1 import CHAPTER_ARTIFACT_SCHEMA_VERSION
from pipeline.literary.b2_prompts_v2 import (
    B2_INTERACTION_PROMPT_ID_V2,
    B2_INTERACTION_PROMPT_ID_V2_1,
    B2_INTERACTION_SYSTEM_PROMPT_V2,
    B2_INTERACTION_SYSTEM_PROMPT_V2_1,
    bind_b2_interaction_response_schema_v2,
    b2_interaction_response_schema_v2,
)
from pipeline.literary.b2_recovery_prompts_v1 import (
    EVENT_REVIEW_PROMPT_ID_V1,
    EVENT_REVIEW_SYSTEM_PROMPT_V1,
    REGISTRY_RECOVERY_PROMPT_ID_V1,
    REGISTRY_RECOVERY_SYSTEM_PROMPT_V1,
    event_review_response_schema_v1,
    registry_recovery_response_schema_v1,
)
from pipeline.literary.b2_event_authority_prompts_v2 import (
    EVENT_REVIEW_PROMPT_ID_V2,
    EVENT_REVIEW_SYSTEM_PROMPT_V2,
    event_review_response_schema_v2,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json
from pipeline.literary.response_normalization_v1 import (
    LiteraryResponseNormalizationError,
    attach_response_normalization_notes_v1,
    normalize_code_owned_response_echoes_v1,
    split_validated_response_normalization_notes_v1,
)


RECOVERY_INDEX_SCHEMA_VERSION = "literary_b2_recovery_index_v1"
REGISTRY_DECISION_SCHEMA_VERSION = "literary_b2_registry_recovery_decision_v1"
REGISTRY_LEDGER_SCHEMA_VERSION = "literary_b2_registry_recovery_ledger_v1"
EVENT_DECISION_SCHEMA_VERSION = "literary_b2_event_review_decision_v1"
EVENT_LEDGER_SCHEMA_VERSION = "literary_b2_event_revision_ledger_v1"
EFFECTIVE_PROJECTION_SCHEMA_VERSION = "literary_b2_effective_projection_v1_1"
RECOVERY_VALIDATOR_VERSION = "literary_b2_recovery_validator_v1"
EVENT_DECISION_SCHEMA_VERSION_V2 = "literary_b2_event_review_decision_v2_2"
EVENT_LEDGER_SCHEMA_VERSION_V2 = "literary_b2_event_revision_ledger_v2_2"
EFFECTIVE_PROJECTION_SCHEMA_VERSION_V2 = "literary_b2_effective_projection_v2_3"
EVENT_AUTHORITY_VALIDATOR_VERSION_V2 = "literary_b2_event_authority_validator_v2"
EVENT_AUTHORITY_DECISION_VALIDATOR_VERSION_V2 = (
    "literary_b2_event_authority_decision_validator_v2_2"
)

MAX_TICKETS_PER_COMPONENT = 12
MAX_EVENTS_PER_COMPONENT = 12
MAX_COMPONENT_SOURCE_BLOCKS = 24
MAX_COMPONENT_CANDIDATE_CARDS = 32
MAX_AUTOMATIC_HEARINGS = 2

GAP_ENDPOINT_STATUSES = frozenset(
    {"unresolved", "ambiguous_candidates", "pending_contract_conflict"}
)
REGISTRY_ACTIONS = frozenset(
    {
        "attach_existing",
        "create_chapter_local",
        "keep_pending",
        "reject_non_registry",
    }
)
EVENT_ACTIONS = frozenset({"keep", "revise", "split", "pending", "reject"})
EVENT_DIRECTIONALITIES_V2 = frozenset(
    {"one_way", "reciprocal", "self_directed", "unknown"}
)
EVENT_ACTUALITIES_V2 = frozenset(
    {"occurred", "reported", "hypothetical_or_negated", "uncertain"}
)
EVENT_ENDPOINT_STATUSES_V2 = frozenset({"resolved", "partial", "pending"})
PAIRWISE_REFERENT_KINDS_V2 = frozenset(
    {"person", "animal", "nonhuman_character", "group_reference", "unknown"}
)


class B2RecoveryError(RuntimeError):
    """Base failure for the B2 recovery loop."""


class B2RecoveryContractError(B2RecoveryError):
    """Raised when recovery input or output violates the sealed contract."""


@dataclass(frozen=True)
class RenderedB2RecoveryRequestV1:
    request_kind: str
    component_id: str
    request_fingerprint: str
    messages: tuple[dict[str, str], dict[str, str]]
    response_schema: dict[str, Any]
    semantic_payload: dict[str, Any]


def _clone(value: Any) -> Any:
    return deepcopy(value)


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise B2RecoveryContractError(f"{label} must be a non-empty string")
    return value


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value.lower())
    )


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    observed = set(value)
    if observed != expected:
        missing = sorted(expected - observed)
        extra = sorted(observed - expected)
        raise B2RecoveryContractError(
            f"{label} keys differ; missing={missing}, extra={extra}"
        )


def _verified_hash(
    value: Mapping[str, Any], *, hash_field: str, label: str
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise B2RecoveryContractError(f"{label} must be an object")
    body = _clone(dict(value))
    observed = _required_string(body.pop(hash_field, None), f"{label} {hash_field}")
    if canonical_hash(body) != observed:
        raise B2RecoveryContractError(f"{label} hash mismatch")
    return {**body, hash_field: observed}


def _decision_replay_projection_v1(decision: Mapping[str, Any]) -> dict[str, Any]:
    try:
        core, _notes = split_validated_response_normalization_notes_v1(decision)
    except LiteraryResponseNormalizationError as exc:
        raise B2RecoveryContractError(str(exc)) from exc
    body = _clone(core)
    body.pop("decision_hash", None)
    return {**body, "decision_hash": canonical_hash(body)}


def _validate_json_schema(
    value: Mapping[str, Any], schema: Mapping[str, Any], label: str
) -> None:
    errors = sorted(
        Draft202012Validator(schema).iter_errors(value),
        key=lambda error: (
            tuple(str(part) for part in error.absolute_path),
            error.message,
        ),
    )
    if errors:
        first = errors[0]
        path = ".".join(str(part) for part in first.absolute_path) or "$"
        raise B2RecoveryContractError(
            f"{label} violates response schema at {path}: {first.message}"
        )


def _mint_id(prefix: str, value: Mapping[str, Any]) -> str:
    return f"{prefix}_{canonical_hash(value)[:20]}"


def _source_spans(text: str, anchor: str) -> list[dict[str, int]]:
    result: list[dict[str, int]] = []
    start = 0
    while True:
        index = text.find(anchor, start)
        if index < 0:
            return result
        result.append({"char_start": index, "char_end": index + len(anchor)})
        start = index + max(1, len(anchor))


def _verified_chapter_artifact(value: Mapping[str, Any]) -> dict[str, Any]:
    artifact = _verified_hash(value, hash_field="artifact_hash", label="B2 artifact")
    if artifact.get("schema_version") != CHAPTER_ARTIFACT_SCHEMA_VERSION:
        raise B2RecoveryContractError("foreign B2 chapter artifact schema")
    if artifact.get("identity_or_claim_mutation_performed") is not False:
        raise B2RecoveryContractError("source B2 artifact mutated identity or claims")
    return artifact


def _request_context(
    requests: Sequence[Mapping[str, Any]], *, chapter_id: str
) -> dict[str, Any]:
    if not requests:
        raise B2RecoveryContractError("at least one B2 interaction request is required")
    blocks: dict[str, dict[str, Any]] = {}
    block_order: dict[str, int] = {}
    cards: dict[str, dict[str, Any]] = {}
    cards_by_block: dict[str, set[str]] = defaultdict(set)
    request_fingerprints: list[str] = []
    window_ids: list[str] = []
    prompt_variants = {
        B2_INTERACTION_PROMPT_ID_V2: B2_INTERACTION_SYSTEM_PROMPT_V2,
        B2_INTERACTION_PROMPT_ID_V2_1: B2_INTERACTION_SYSTEM_PROMPT_V2_1,
    }

    for request in requests:
        prompt_id = request.get("prompt_id")
        prompt_text = prompt_variants.get(str(prompt_id))
        if prompt_text is None:
            raise B2RecoveryContractError(
                "interaction request uses an unsupported prompt version"
            )
        supplied_schema = request.get("response_schema")
        if not isinstance(supplied_schema, Mapping):
            raise B2RecoveryContractError(
                "interaction request omits its response schema"
            )
        try:
            request_body, payload = _validated_request(
                request,
                request_kind="window_interaction",
                prompt_id=str(prompt_id),
                prompt_text=prompt_text,
                response_schema=dict(supplied_schema),
            )
        except B2ContractError as exc:
            raise B2RecoveryContractError(str(exc)) from exc
        if payload.get("chapter_id") != chapter_id:
            raise B2RecoveryContractError("interaction request belongs to another chapter")
        window_id = _required_string(payload.get("window_id"), "window_id")
        request_fingerprints.append(str(request_body["request_fingerprint"]))
        window_ids.append(window_id)
        active_blocks = payload.get("active_blocks")
        if not isinstance(active_blocks, list):
            raise B2RecoveryContractError("active_blocks must be a list")
        tail_blocks = payload.get("preceding_tail")
        if not isinstance(tail_blocks, list):
            raise B2RecoveryContractError("preceding_tail must be a list")
        active_block_ids: list[str] = []
        tail_block_ids: list[str] = []
        for raw_block in active_blocks:
            if not isinstance(raw_block, Mapping):
                raise B2RecoveryContractError(
                    "active source block must be an object"
                )
            active_block_ids.append(
                _required_string(raw_block.get("block_id"), "active block id")
            )
        for raw_block in tail_blocks:
            if not isinstance(raw_block, Mapping):
                raise B2RecoveryContractError(
                    "tail source block must be an object"
                )
            tail_block_ids.append(
                _required_string(raw_block.get("block_id"), "tail block id")
            )
        packet = payload.get("candidate_packets")
        if not isinstance(packet, Mapping):
            raise B2RecoveryContractError("candidate packet is missing")
        packet_cards = packet.get("candidate_cards")
        if not isinstance(packet_cards, list):
            raise B2RecoveryContractError("candidate cards must be a list")
        packet_card_ids: set[str] = set()
        for raw_card in packet_cards:
            if not isinstance(raw_card, Mapping):
                raise B2RecoveryContractError("candidate card must be an object")
            card = _clone(dict(raw_card))
            card_id = _required_string(
                card.get("candidate_card_id"), "candidate_card_id"
            )
            existing = cards.get(card_id)
            if existing is not None and canonical_json(existing) != canonical_json(card):
                raise B2RecoveryContractError(
                    "same candidate id has conflicting card payloads"
                )
            cards[card_id] = card
            packet_card_ids.add(card_id)
        if prompt_id == B2_INTERACTION_PROMPT_ID_V2:
            expected_schema = b2_interaction_response_schema_v2()
        else:
            expected_schema = bind_b2_interaction_response_schema_v2(
                chapter_id=chapter_id,
                window_id=window_id,
                active_block_ids=active_block_ids,
                support_block_ids=[*active_block_ids, *tail_block_ids],
                candidate_card_ids=sorted(packet_card_ids),
            )
        if canonical_json(request_body["response_schema"]) != canonical_json(
            expected_schema
        ):
            raise B2RecoveryContractError(
                "interaction response-schema bindings differ from context"
            )
        for raw_block in active_blocks:
            if not isinstance(raw_block, Mapping):
                raise B2RecoveryContractError("active source block must be an object")
            block = _clone(dict(raw_block))
            block_id = _required_string(block.get("block_id"), "source block id")
            text = _required_string(block.get("text"), "source block text")
            normalized = {
                "block_id": block_id,
                "block_type": str(block.get("block_type") or "unknown"),
                "text": text,
            }
            existing = blocks.get(block_id)
            if existing is not None and canonical_json(existing) != canonical_json(
                normalized
            ):
                raise B2RecoveryContractError(
                    "same block id has conflicting source text"
                )
            if block_id not in blocks:
                block_order[block_id] = len(block_order)
                blocks[block_id] = normalized
            cards_by_block[block_id].update(packet_card_ids)

    return {
        "blocks": blocks,
        "block_order": block_order,
        "cards": cards,
        "cards_by_block": cards_by_block,
        "request_fingerprints": request_fingerprints,
        "window_ids": window_ids,
    }


def _endpoint_ticket(
    *,
    chapter_id: str,
    row_kind: str,
    row_id: str,
    endpoint_role: str,
    endpoint: Mapping[str, Any],
    block_id: str,
    source_anchor: str,
    source_text: str,
    review_issue_kind: str | None = None,
) -> dict[str, Any] | None:
    status = str(endpoint.get("resolution_status") or "")
    contextual_single_candidate = (
        review_issue_kind == "contextual_speaker_attribution"
        and status == "resolved_candidate"
    )
    if status not in GAP_ENDPOINT_STATUSES and not contextual_single_candidate:
        return None
    candidate_ids = [
        _required_string(value, "endpoint candidate id")
        for value in endpoint.get("candidate_card_ids") or []
    ]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise B2RecoveryContractError("endpoint repeats a candidate id")
    issue_kind = review_issue_kind or (
        "missing_registry_candidate"
        if not candidate_ids and status == "unresolved"
        else (
            "ambiguous_registry_candidate"
            if status == "ambiguous_candidates"
            else "endpoint_contract_conflict"
        )
    )
    evidence = {
        "source_row_kind": row_kind,
        "source_row_id": row_id,
        "endpoint_role": endpoint_role,
        "endpoint": _clone(dict(endpoint)),
        "block_id": block_id,
        "source_anchor": source_anchor,
        "source_text_sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
    }
    body = {
        "chapter_id": chapter_id,
        "source_row_kind": row_kind,
        "source_row_id": row_id,
        "endpoint_role": endpoint_role,
        "observed_surface": endpoint.get("surface"),
        "reference_form": str(endpoint.get("reference_form") or "unknown"),
        "resolution_status": status,
        "candidate_card_ids": sorted(candidate_ids),
        "issue_kind": issue_kind,
        "source_anchor": source_anchor,
        "source_block_ids": [block_id],
        "evidence_hash": canonical_hash(evidence),
        "lifecycle_state": "open",
        "hearing_count": 0,
        "authority_effect": "none",
    }
    return {"ticket_id": _mint_id("b2gap1", body), **body}


def _chunked(rows: Sequence[dict[str, Any]], cap: int) -> Iterable[list[dict[str, Any]]]:
    for start in range(0, len(rows), cap):
        yield list(rows[start : start + cap])


def _source_closure(
    direct_block_ids: Sequence[str],
    *,
    blocks: Mapping[str, Mapping[str, Any]],
    block_order: Mapping[str, int],
    neighbor_radius: int = 1,
) -> list[dict[str, Any]]:
    ordered_ids = [
        block_id
        for block_id, _index in sorted(block_order.items(), key=lambda item: item[1])
    ]
    selected: set[str] = set(direct_block_ids)
    if len(selected) > MAX_COMPONENT_SOURCE_BLOCKS:
        raise B2RecoveryContractError(
            "direct component evidence exceeds the source-block cap"
        )
    for block_id in direct_block_ids:
        index = block_order[block_id]
        for offset in range(-neighbor_radius, neighbor_radius + 1):
            neighbor_index = index + offset
            if (
                0 <= neighbor_index < len(ordered_ids)
                and len(selected) < MAX_COMPONENT_SOURCE_BLOCKS
            ):
                selected.add(ordered_ids[neighbor_index])
    return [
        _clone(dict(blocks[block_id]))
        for block_id in ordered_ids
        if block_id in selected
    ]


def _component_cards(
    direct_block_ids: Sequence[str],
    *,
    explicit_candidate_ids: Sequence[str],
    cards_by_block: Mapping[str, set[str]],
    cards: Mapping[str, Mapping[str, Any]],
) -> tuple[list[str], bool]:
    selected = set(explicit_candidate_ids)
    for block_id in direct_block_ids:
        selected.update(cards_by_block.get(block_id, set()))
    foreign = selected - set(cards)
    if foreign:
        raise B2RecoveryContractError("component references foreign candidate cards")
    ordered = sorted(selected)
    return ordered, len(ordered) > MAX_COMPONENT_CANDIDATE_CARDS


def _registry_components(
    tickets: Sequence[dict[str, Any]], *, context: Mapping[str, Any]
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for ordinal, group in enumerate(_chunked(tickets, MAX_TICKETS_PER_COMPONENT), 1):
        direct_blocks = sorted(
            {
                block_id
                for ticket in group
                for block_id in ticket["source_block_ids"]
            },
            key=context["block_order"].__getitem__,
        )
        candidate_ids, card_overflow = _component_cards(
            direct_blocks,
            explicit_candidate_ids=[
                candidate_id
                for ticket in group
                for candidate_id in ticket["candidate_card_ids"]
            ],
            cards_by_block=context["cards_by_block"],
            cards=context["cards"],
        )
        source_blocks = _source_closure(
            direct_blocks,
            blocks=context["blocks"],
            block_order=context["block_order"],
        )
        body = {
            "component_kind": "registry_gap",
            "chapter_id": group[0]["chapter_id"],
            "ordinal": ordinal,
            "ticket_ids": [ticket["ticket_id"] for ticket in group],
            "source_block_ids": [row["block_id"] for row in source_blocks],
            "candidate_card_ids": candidate_ids,
            "overflow": card_overflow,
            "overflow_reasons": (
                ["candidate_card_cap_exceeded"] if card_overflow else []
            ),
            "authority_effect": "none",
        }
        result.append(
            {"component_id": _mint_id("b2gapcomp1", body), **body}
        )
    return result


def _event_cases(
    artifact: Mapping[str, Any], *, blocks: Mapping[str, Mapping[str, Any]]
) -> list[dict[str, Any]]:
    events = artifact.get("interaction_events")
    if not isinstance(events, list):
        raise B2RecoveryContractError("B2 interaction_events must be a list")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in events:
        if not isinstance(raw, Mapping):
            raise B2RecoveryContractError("B2 event must be an object")
        event = _clone(dict(raw))
        event_id = _required_string(
            event.get("interaction_event_id"), "interaction_event_id"
        )
        if event_id in seen:
            raise B2RecoveryContractError("B2 artifact repeats an event id")
        seen.add(event_id)
        block_id = _required_string(event.get("block_id"), "event block_id")
        if block_id not in blocks:
            raise B2RecoveryContractError("B2 event cites a missing source block")
        body = {
            "chapter_id": artifact["chapter_id"],
            "interaction_event_id": event_id,
            "source_block_ids": [block_id],
            "candidate_card_ids": sorted(
                {
                    candidate_id
                    for role in ("actor", "target")
                    for candidate_id in (event.get(role) or {}).get(
                        "candidate_card_ids", []
                    )
                }
            ),
            "event_snapshot": event,
            "event_snapshot_hash": canonical_hash(event),
            "review_scope": "directed_event_semantics",
            "authority_effect": "none",
        }
        result.append({"case_id": _mint_id("b2evtcase1", body), **body})
    return result


def _event_components(
    cases: Sequence[dict[str, Any]], *, context: Mapping[str, Any]
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for ordinal, group in enumerate(_chunked(cases, MAX_EVENTS_PER_COMPONENT), 1):
        direct_blocks = sorted(
            {
                block_id
                for case in group
                for block_id in case["source_block_ids"]
            },
            key=context["block_order"].__getitem__,
        )
        candidate_ids, card_overflow = _component_cards(
            direct_blocks,
            explicit_candidate_ids=[
                candidate_id
                for case in group
                for candidate_id in case["candidate_card_ids"]
            ],
            cards_by_block=context["cards_by_block"],
            cards=context["cards"],
        )
        source_blocks = _source_closure(
            direct_blocks,
            blocks=context["blocks"],
            block_order=context["block_order"],
            neighbor_radius=0,
        )
        body = {
            "component_kind": "event_semantic_review",
            "chapter_id": group[0]["chapter_id"],
            "ordinal": ordinal,
            "case_ids": [case["case_id"] for case in group],
            "source_block_ids": [row["block_id"] for row in source_blocks],
            "candidate_card_ids": candidate_ids,
            "overflow": card_overflow,
            "overflow_reasons": (
                ["candidate_card_cap_exceeded"] if card_overflow else []
            ),
            "authority_effect": "none",
        }
        result.append(
            {"component_id": _mint_id("b2evtcomp1", body), **body}
        )
    return result


def build_b2_recovery_index_v1(
    *,
    chapter_artifact: Mapping[str, Any],
    interaction_requests: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    artifact = _verified_chapter_artifact(chapter_artifact)
    chapter_id = _required_string(artifact.get("chapter_id"), "chapter_id")
    context = _request_context(interaction_requests, chapter_id=chapter_id)
    expected_windows = [
        _required_string(row.get("window_id"), "artifact window_id")
        for row in artifact.get("interaction_artifacts") or []
    ]
    if sorted(expected_windows) != sorted(context["window_ids"]):
        raise B2RecoveryContractError(
            "interaction requests do not exact-cover artifact windows"
        )

    tickets: list[dict[str, Any]] = []
    row_specs = (
        ("speaker_turn", artifact.get("speaker_turns") or [], ("speaker", "addressee")),
        (
            "interaction_event",
            artifact.get("interaction_events") or [],
            ("actor", "target"),
        ),
    )
    for row_kind, rows, roles in row_specs:
        for raw_row in rows:
            if not isinstance(raw_row, Mapping):
                raise B2RecoveryContractError(f"{row_kind} must be an object")
            row = dict(raw_row)
            id_field = f"{row_kind}_id"
            row_id = _required_string(row.get(id_field), id_field)
            block_id = _required_string(row.get("block_id"), f"{row_kind} block")
            if block_id not in context["blocks"]:
                raise B2RecoveryContractError(f"{row_kind} cites a missing block")
            anchor_field = (
                "utterance_anchor"
                if row_kind == "speaker_turn"
                else "event_anchor"
            )
            anchor = _required_string(row.get(anchor_field), f"{row_kind} anchor")
            for role in roles:
                endpoint = row.get(role)
                if not isinstance(endpoint, Mapping):
                    raise B2RecoveryContractError(
                        f"{row_kind}.{role} must be an object"
                    )
                ticket = _endpoint_ticket(
                    chapter_id=chapter_id,
                    row_kind=row_kind,
                    row_id=row_id,
                    endpoint_role=role,
                    endpoint=endpoint,
                    block_id=block_id,
                    source_anchor=anchor,
                    source_text=context["blocks"][block_id]["text"],
                    review_issue_kind=(
                        "contextual_speaker_attribution"
                        if row_kind == "speaker_turn"
                        and role == "speaker"
                        and row.get("speaker_authority_status")
                        in {"provisional_contextual", "pending_review"}
                        and endpoint.get("resolution_status")
                        == "resolved_candidate"
                        else None
                    ),
                )
                if ticket is not None:
                    tickets.append(ticket)
    tickets.sort(
        key=lambda row: (
            context["block_order"][row["source_block_ids"][0]],
            row["source_row_kind"],
            row["source_row_id"],
            row["endpoint_role"],
        )
    )
    cases = _event_cases(artifact, blocks=context["blocks"])
    cases.sort(
        key=lambda row: (
            context["block_order"][row["source_block_ids"][0]],
            row["interaction_event_id"],
        )
    )
    registry_components = _registry_components(tickets, context=context)
    event_components = _event_components(cases, context=context)
    body = {
        "schema_version": RECOVERY_INDEX_SCHEMA_VERSION,
        "validator_version": RECOVERY_VALIDATOR_VERSION,
        "chapter_id": chapter_id,
        "source_b2_artifact_hash": artifact["artifact_hash"],
        "source_request_fingerprints": sorted(context["request_fingerprints"]),
        "source_blocks": [
            _clone(context["blocks"][block_id])
            for block_id, _index in sorted(
                context["block_order"].items(), key=lambda item: item[1]
            )
        ],
        "candidate_cards": [
            _clone(context["cards"][card_id]) for card_id in sorted(context["cards"])
        ],
        "registry_gap_tickets": tickets,
        "event_review_cases": cases,
        "registry_components": registry_components,
        "event_components": event_components,
        "counts": {
            "registry_gap_tickets": len(tickets),
            "event_review_cases": len(cases),
            "registry_components": len(registry_components),
            "event_components": len(event_components),
            "overflow_components": sum(
                bool(row["overflow"])
                for row in [*registry_components, *event_components]
            ),
        },
        "semantic_halt_required": False,
        "book_global_identity_mutation_performed": False,
        "translation_performed": False,
        "production_publish_performed": False,
    }
    return {**body, "recovery_index_hash": canonical_hash(body)}


def verify_b2_recovery_index_v1(index: Mapping[str, Any]) -> dict[str, Any]:
    verified = _verified_hash(
        index, hash_field="recovery_index_hash", label="B2 recovery index"
    )
    if verified.get("schema_version") != RECOVERY_INDEX_SCHEMA_VERSION:
        raise B2RecoveryContractError("foreign B2 recovery index schema")
    if verified.get("validator_version") != RECOVERY_VALIDATOR_VERSION:
        raise B2RecoveryContractError("foreign B2 recovery validator")
    ticket_ids = [
        _required_string(row.get("ticket_id"), "ticket_id")
        for row in verified.get("registry_gap_tickets") or []
    ]
    case_ids = [
        _required_string(row.get("case_id"), "case_id")
        for row in verified.get("event_review_cases") or []
    ]
    if len(ticket_ids) != len(set(ticket_ids)) or len(case_ids) != len(set(case_ids)):
        raise B2RecoveryContractError("recovery index repeats a ticket or case id")
    component_tickets = [
        ticket_id
        for component in verified.get("registry_components") or []
        for ticket_id in component.get("ticket_ids") or []
    ]
    component_cases = [
        case_id
        for component in verified.get("event_components") or []
        for case_id in component.get("case_ids") or []
    ]
    if sorted(component_tickets) != sorted(ticket_ids):
        raise B2RecoveryContractError(
            "registry components do not exact-cover gap tickets"
        )
    if sorted(component_cases) != sorted(case_ids):
        raise B2RecoveryContractError(
            "event components do not exact-cover event cases"
        )
    if any(row.get("authority_effect") != "none" for row in (
        list(verified.get("registry_gap_tickets") or [])
        + list(verified.get("event_review_cases") or [])
    )):
        raise B2RecoveryContractError("unreviewed recovery row gained authority")
    if verified.get("semantic_halt_required") is not False:
        raise B2RecoveryContractError("recovery index requests a semantic halt")
    return _clone(verified)


def _component(
    index: Mapping[str, Any], *, component_id: str, collection: str
) -> dict[str, Any]:
    component = next(
        (
            row
            for row in index.get(collection) or []
            if row.get("component_id") == component_id
        ),
        None,
    )
    if component is None:
        raise B2RecoveryContractError("unknown recovery component")
    if component.get("overflow"):
        raise B2RecoveryContractError("overflow recovery component cannot be rendered")
    return _clone(dict(component))


def _catalog(index: Mapping[str, Any], collection: str, key: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for raw in index.get(collection) or []:
        if not isinstance(raw, Mapping):
            raise B2RecoveryContractError(f"{collection} row must be an object")
        row = _clone(dict(raw))
        row_id = _required_string(row.get(key), key)
        if row_id in result:
            raise B2RecoveryContractError(f"{collection} repeats {key}")
        result[row_id] = row
    return result


def _request(
    *,
    request_kind: str,
    prompt_id: str,
    prompt: str,
    component_id: str,
    payload: Mapping[str, Any],
    response_schema: Mapping[str, Any],
) -> RenderedB2RecoveryRequestV1:
    semantic_payload = _clone(dict(payload))
    fingerprint = canonical_hash(
        {
            "request_kind": request_kind,
            "prompt_id": prompt_id,
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "semantic_payload": semantic_payload,
            "response_schema": response_schema,
        }
    )
    return RenderedB2RecoveryRequestV1(
        request_kind=request_kind,
        component_id=component_id,
        request_fingerprint=fingerprint,
        messages=(
            {"role": "system", "content": prompt},
            {"role": "user", "content": canonical_json(semantic_payload)},
        ),
        response_schema=_clone(dict(response_schema)),
        semantic_payload=semantic_payload,
    )


def render_registry_recovery_request_v1(
    *, index: Mapping[str, Any], component_id: str
) -> RenderedB2RecoveryRequestV1:
    verified = verify_b2_recovery_index_v1(index)
    component = _component(
        verified, component_id=component_id, collection="registry_components"
    )
    tickets = _catalog(verified, "registry_gap_tickets", "ticket_id")
    blocks = _catalog(verified, "source_blocks", "block_id")
    cards = _catalog(verified, "candidate_cards", "candidate_card_id")
    schema = registry_recovery_response_schema_v1()
    schema["properties"]["chapter_id"]["enum"] = [verified["chapter_id"]]
    schema["properties"]["component_id"]["enum"] = [component_id]
    schema["properties"]["ticket_actions"]["minItems"] = len(
        component["ticket_ids"]
    )
    schema["properties"]["ticket_actions"]["maxItems"] = len(
        component["ticket_ids"]
    )
    schema["properties"]["ticket_actions"]["items"]["properties"]["ticket_id"][
        "enum"
    ] = list(component["ticket_ids"])
    payload = {
        "contract_version": RECOVERY_VALIDATOR_VERSION,
        "recovery_index_hash": verified["recovery_index_hash"],
        "chapter_id": verified["chapter_id"],
        "component_id": component_id,
        "tickets": [tickets[ticket_id] for ticket_id in component["ticket_ids"]],
        "candidate_cards": [
            cards[card_id] for card_id in component["candidate_card_ids"]
        ],
        "source_blocks": [
            blocks[block_id] for block_id in component["source_block_ids"]
        ],
        "authority_policy": {
            "new_card_scope": "chapter_local_recovery",
            "global_alias_authority": False,
            "book_global_identity_mutation": False,
        },
    }
    return _request(
        request_kind="registry_gap_recovery",
        prompt_id=REGISTRY_RECOVERY_PROMPT_ID_V1,
        prompt=REGISTRY_RECOVERY_SYSTEM_PROMPT_V1,
        component_id=component_id,
        payload=payload,
        response_schema=schema,
    )


def validate_registry_recovery_response_v1(
    response: Mapping[str, Any],
    *,
    index: Mapping[str, Any],
    component_id: str,
    request_fingerprint: str,
) -> dict[str, Any]:
    verified = verify_b2_recovery_index_v1(index)
    component = _component(
        verified, component_id=component_id, collection="registry_components"
    )
    schema = registry_recovery_response_schema_v1()
    normalized_response, response_normalization_notes = (
        normalize_code_owned_response_echoes_v1(
            response,
            expected={"chapter_id": verified["chapter_id"]},
        )
    )
    _validate_json_schema(
        normalized_response, schema, "registry recovery response"
    )
    if normalized_response.get("component_id") != component_id:
        raise B2RecoveryContractError("registry response component differs from request")
    ticket_by_id = _catalog(verified, "registry_gap_tickets", "ticket_id")
    card_by_id = _catalog(verified, "candidate_cards", "candidate_card_id")
    block_by_id = _catalog(verified, "source_blocks", "block_id")
    allowed_tickets = set(component["ticket_ids"])
    allowed_cards = set(component["candidate_card_ids"])
    allowed_blocks = set(component["source_block_ids"])
    actions: list[dict[str, Any]] = []
    seen: set[str] = set()
    group_profiles: dict[str, str] = {}
    for raw in normalized_response.get("ticket_actions") or []:
        action = _clone(dict(raw))
        ticket_id = _required_string(action.get("ticket_id"), "ticket_id")
        if ticket_id not in allowed_tickets or ticket_id in seen:
            raise B2RecoveryContractError(
                "registry actions do not exact-cover the component"
            )
        seen.add(ticket_id)
        ticket = ticket_by_id[ticket_id]
        action_name = _required_string(action.get("action"), "registry action")
        if action_name not in REGISTRY_ACTIONS:
            raise B2RecoveryContractError("foreign registry action")
        source_block_ids = [
            _required_string(value, "registry evidence block")
            for value in action.get("source_block_ids") or []
        ]
        if (
            len(source_block_ids) != len(set(source_block_ids))
            or not set(source_block_ids) <= allowed_blocks
            or not set(ticket["source_block_ids"]) <= set(source_block_ids)
        ):
            raise B2RecoveryContractError(
                "registry action cites incomplete or foreign evidence"
            )
        target = action.get("target_candidate_card_id")
        group_key = action.get("provisional_group_key")
        surface = action.get("canonical_surface")
        referent_kind = action.get("referent_kind")
        identity_summary = action.get("identity_summary")
        pending_reason = action.get("pending_reason")
        narrowed_present = "narrowed_candidate_card_ids" in action
        narrowed = action.get("narrowed_candidate_card_ids")
        if action_name == "keep_pending":
            if not narrowed_present or not isinstance(narrowed, list):
                raise B2RecoveryContractError(
                    "pending action lacks narrowed candidate card ids"
                )
            normalized_narrowed = [
                _required_string(value, "narrowed candidate card id")
                for value in narrowed
            ]
            if (
                len(normalized_narrowed) != len(set(normalized_narrowed))
                or not set(normalized_narrowed).issubset(
                    set(ticket.get("candidate_card_ids") or [])
                )
            ):
                raise B2RecoveryContractError(
                    "pending action narrowed candidates differ from its ticket"
                )
            action["narrowed_candidate_card_ids"] = normalized_narrowed
        elif narrowed_present:
            if not isinstance(narrowed, list) or narrowed:
                raise B2RecoveryContractError(
                    "non-pending action carries narrowed candidate card ids"
                )
            action["narrowed_candidate_card_ids"] = []
        if action_name == "keep_pending" and pending_reason is None:
            # The model already authored the explanation; copy it into the
            # lifecycle-specific field when the transport leaves that field null.
            pending_reason = _required_string(
                action.get("resolution_note"), "pending resolution_note"
            )
            action["pending_reason"] = pending_reason
        if action_name == "attach_existing":
            if target not in allowed_cards or target not in card_by_id:
                raise B2RecoveryContractError(
                    "attach_existing cites a foreign candidate card"
                )
            if any(
                value is not None
                for value in (group_key, surface, referent_kind, identity_summary, pending_reason)
            ):
                raise B2RecoveryContractError(
                    "attach_existing carries forbidden new-card fields"
                )
        elif action_name == "create_chapter_local":
            if target is not None or pending_reason is not None:
                raise B2RecoveryContractError(
                    "create_chapter_local carries a target or pending reason"
                )
            group_key = _required_string(group_key, "provisional_group_key")
            surface = _required_string(surface, "canonical_surface")
            identity_summary = _required_string(
                identity_summary, "identity_summary"
            )
            if referent_kind not in REFERENT_KINDS:
                raise B2RecoveryContractError("foreign recovery referent kind")
            if not any(
                _source_spans(block_by_id[block_id]["text"], surface)
                for block_id in source_block_ids
            ):
                raise B2RecoveryContractError(
                    "recovery canonical surface is not exact source text"
                )
            profile_hash = canonical_hash(
                {
                    "referent_kind": referent_kind,
                    "identity_summary": identity_summary,
                }
            )
            if (
                group_key in group_profiles
                and group_profiles[group_key] != profile_hash
            ):
                raise B2RecoveryContractError(
                    "one provisional group has conflicting entity profiles"
                )
            group_profiles[group_key] = profile_hash
        elif action_name == "keep_pending":
            if pending_reason is None:
                raise B2RecoveryContractError("pending action lacks a reason")
            if any(
                value is not None
                for value in (target, group_key, surface, referent_kind, identity_summary)
            ):
                raise B2RecoveryContractError(
                    "pending action carries authority-bearing fields"
                )
        else:
            if any(
                value is not None
                for value in (
                    target,
                    group_key,
                    surface,
                    referent_kind,
                    identity_summary,
                    pending_reason,
                )
            ):
                raise B2RecoveryContractError(
                    "reject action carries authority-bearing fields"
                )
        action["source_block_ids"] = sorted(
            source_block_ids,
            key=lambda block_id: component["source_block_ids"].index(block_id),
        )
        actions.append(action)
    if seen != allowed_tickets:
        raise B2RecoveryContractError(
            "registry actions do not exact-cover the component"
        )
    body = {
        "schema_version": REGISTRY_DECISION_SCHEMA_VERSION,
        "validator_version": RECOVERY_VALIDATOR_VERSION,
        "recovery_index_hash": verified["recovery_index_hash"],
        "chapter_id": verified["chapter_id"],
        "component_id": component_id,
        "request_fingerprint": _required_string(
            request_fingerprint, "request_fingerprint"
        ),
        "ticket_actions": sorted(actions, key=lambda row: row["ticket_id"]),
    }
    body = attach_response_normalization_notes_v1(
        body, response_normalization_notes
    )
    return {**body, "decision_hash": canonical_hash(body)}


def verify_registry_recovery_decision_v1(
    decision: Mapping[str, Any], *, index: Mapping[str, Any]
) -> dict[str, Any]:
    verified = _verified_hash(
        decision, hash_field="decision_hash", label="registry recovery decision"
    )
    if verified.get("schema_version") != REGISTRY_DECISION_SCHEMA_VERSION:
        raise B2RecoveryContractError("foreign registry recovery decision schema")
    normalized = validate_registry_recovery_response_v1(
        {
            "schema_version": "literary_b2_registry_recovery_response_v1",
            "chapter_id": verified.get("chapter_id"),
            "component_id": verified.get("component_id"),
            "ticket_actions": _clone(verified.get("ticket_actions")),
        },
        index=index,
        component_id=_required_string(verified.get("component_id"), "component_id"),
        request_fingerprint=_required_string(
            verified.get("request_fingerprint"), "request_fingerprint"
        ),
    )
    if canonical_json(normalized) != canonical_json(
        _decision_replay_projection_v1(verified)
    ):
        raise B2RecoveryContractError("registry recovery decision normalization drift")
    return _clone(verified)


def validate_registry_recovery_component_quarantines_v1(
    *,
    index: Mapping[str, Any],
    quarantines: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    verified = verify_b2_recovery_index_v1(index)
    components = {
        row["component_id"]: row
        for row in verified["registry_components"]
        if not row["overflow"]
    }
    allowed_keys = {
        "component_id",
        "ticket_ids",
        "state",
        "reason_code",
        "reason",
        "raw_result_hash",
        "ticket_action_hashes",
    }
    normalized: list[dict[str, Any]] = []
    seen_components: set[str] = set()
    for raw in quarantines:
        if not isinstance(raw, Mapping) or set(raw) != allowed_keys:
            raise B2RecoveryContractError(
                "registry recovery component quarantine is malformed"
            )
        row = _clone(dict(raw))
        component_id = _required_string(
            row.get("component_id"), "quarantine component_id"
        )
        component = components.get(component_id)
        if component is None or component_id in seen_components:
            raise B2RecoveryContractError(
                "registry recovery component quarantine is foreign or repeated"
            )
        seen_components.add(component_id)
        ticket_ids = row.get("ticket_ids")
        if (
            not isinstance(ticket_ids, list)
            or not all(isinstance(value, str) and value for value in ticket_ids)
            or len(ticket_ids) != len(set(ticket_ids))
            or set(ticket_ids) != set(component["ticket_ids"])
        ):
            raise B2RecoveryContractError(
                "registry recovery quarantine tickets differ from the component"
            )
        if (
            row.get("state") != "unreviewed"
            or row.get("reason_code")
            != "component_semantic_contract_rejected"
            or not isinstance(row.get("reason"), str)
            or not row["reason"].strip()
            or not _is_sha256(row.get("raw_result_hash"))
        ):
            raise B2RecoveryContractError(
                "registry recovery component quarantine status is malformed"
            )
        action_rows = row.get("ticket_action_hashes")
        if not isinstance(action_rows, list):
            raise B2RecoveryContractError(
                "registry recovery quarantine action hashes are malformed"
            )
        action_by_ticket: dict[str, list[str]] = {}
        for action_row in action_rows:
            if (
                not isinstance(action_row, Mapping)
                or set(action_row) != {"ticket_id", "action_hashes"}
            ):
                raise B2RecoveryContractError(
                    "registry recovery quarantine action row is malformed"
                )
            ticket_id = _required_string(
                action_row.get("ticket_id"), "quarantine ticket_id"
            )
            action_hashes = action_row.get("action_hashes")
            if (
                ticket_id in action_by_ticket
                or not isinstance(action_hashes, list)
                or not action_hashes
                or not all(_is_sha256(value) for value in action_hashes)
                or len(action_hashes) != len(set(action_hashes))
            ):
                raise B2RecoveryContractError(
                    "registry recovery quarantine action hashes differ or repeat"
                )
            action_by_ticket[ticket_id] = sorted(action_hashes)
        if set(action_by_ticket) != set(ticket_ids):
            raise B2RecoveryContractError(
                "registry recovery quarantine actions do not exact-cover tickets"
            )
        row["ticket_ids"] = sorted(ticket_ids)
        row["ticket_action_hashes"] = [
            {
                "ticket_id": ticket_id,
                "action_hashes": action_by_ticket[ticket_id],
            }
            for ticket_id in sorted(action_by_ticket)
        ]
        normalized.append(row)
    return sorted(normalized, key=lambda row: row["component_id"])


def build_registry_recovery_ledger_v1(
    *,
    index: Mapping[str, Any],
    decisions: Sequence[Mapping[str, Any]],
    quarantined_components: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    verified = verify_b2_recovery_index_v1(index)
    components = {
        row["component_id"]: row for row in verified["registry_components"]
    }
    expected = {
        component_id
        for component_id, row in components.items()
        if not row["overflow"]
    }
    validated_quarantines = validate_registry_recovery_component_quarantines_v1(
        index=verified,
        quarantines=quarantined_components,
    )
    quarantined_component_ids = {
        row["component_id"] for row in validated_quarantines
    }
    decision_by_component: dict[str, dict[str, Any]] = {}
    for raw in decisions:
        decision = verify_registry_recovery_decision_v1(raw, index=verified)
        component_id = decision["component_id"]
        if component_id in decision_by_component:
            raise B2RecoveryContractError(
                "registry recovery repeats a component decision"
            )
        decision_by_component[component_id] = decision
    if (
        set(decision_by_component).intersection(quarantined_component_ids)
        or set(decision_by_component).union(quarantined_component_ids) != expected
    ):
        raise B2RecoveryContractError(
            "registry decisions and quarantines do not exact-cover renderable components"
        )
    tickets = _catalog(verified, "registry_gap_tickets", "ticket_id")
    block_order = {
        row["block_id"]: index for index, row in enumerate(verified["source_blocks"])
    }
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for decision in decision_by_component.values():
        for action in decision["ticket_actions"]:
            if action["action"] == "create_chapter_local":
                grouped[
                    (decision["component_id"], action["provisional_group_key"])
                ].append(action)
    local_cards: list[dict[str, Any]] = []
    card_by_group: dict[tuple[str, str], str] = {}
    for group_key, actions in sorted(grouped.items()):
        ordered_actions = sorted(
            actions,
            key=lambda action: (
                min(block_order[block_id] for block_id in action["source_block_ids"]),
                action["ticket_id"],
            ),
        )
        first = ordered_actions[0]
        surface_blocks: dict[str, set[str]] = defaultdict(set)
        for action in ordered_actions:
            surface_blocks[action["canonical_surface"]].update(
                action["source_block_ids"]
            )
        profile = {
            "chapter_id": verified["chapter_id"],
            "canonical_surface": first["canonical_surface"],
            "referent_kind": first["referent_kind"],
            "identity_summary": first["identity_summary"],
            "source_block_ids": sorted(
                {
                    block_id
                    for action in actions
                    for block_id in action["source_block_ids"]
                },
                key=block_order.__getitem__,
            ),
            "ticket_ids": sorted(action["ticket_id"] for action in actions),
        }
        card_id = _mint_id("b2localcard1", profile)
        card = {
            "candidate_card_id": card_id,
            "canonical_surface": profile["canonical_surface"],
            "stable_surfaces": [],
            "local_surface_evidence": [
                {
                    "surface": surface,
                    "source_block_ids": sorted(
                        block_ids, key=block_order.__getitem__
                    ),
                }
                for surface, block_ids in sorted(
                    surface_blocks.items(),
                    key=lambda item: (
                        min(block_order[block_id] for block_id in item[1]),
                        item[0],
                    ),
                )
            ],
            "authority_scope": "chapter_local_recovery",
            "effective_claims_as_of": {
                "identity_summary": profile["identity_summary"],
                "referent_kind": profile["referent_kind"],
                "referential_gender": None,
            },
            "first_supported_block_id": profile["source_block_ids"][0],
            "provenance_refs": [
                {"chapter_id": verified["chapter_id"], "block_id": block_id}
                for block_id in profile["source_block_ids"]
            ],
            "uncertainty_flags": [
                "chapter_local_only",
                "no_global_alias_authority",
            ],
            "source_ticket_ids": profile["ticket_ids"],
        }
        local_cards.append(card)
        card_by_group[group_key] = card_id

    resolutions: list[dict[str, Any]] = []
    for component_id in sorted(decision_by_component):
        decision = decision_by_component[component_id]
        for action in decision["ticket_actions"]:
            ticket = tickets[action["ticket_id"]]
            action_name = action["action"]
            bound_card_id: str | None = None
            lifecycle_state = "resolved"
            authority_effect = "candidate_only"
            next_review_trigger: str | None = None
            if action_name == "attach_existing":
                bound_card_id = action["target_candidate_card_id"]
            elif action_name == "create_chapter_local":
                bound_card_id = card_by_group[
                    (component_id, action["provisional_group_key"])
                ]
            elif action_name == "keep_pending":
                lifecycle_state = "pending"
                authority_effect = "none"
                next_review_trigger = "new_b2_or_registry_evidence"
            else:
                lifecycle_state = "rejected"
                authority_effect = "none"
            resolutions.append(
                {
                    "ticket_id": ticket["ticket_id"],
                    "source_row_kind": ticket["source_row_kind"],
                    "source_row_id": ticket["source_row_id"],
                    "endpoint_role": ticket["endpoint_role"],
                    "action": action_name,
                    "bound_candidate_card_id": bound_card_id,
                    "source_block_ids": action["source_block_ids"],
                    "lifecycle_state": lifecycle_state,
                    "hearing_count": ticket["hearing_count"] + 1,
                    "evidence_hash": ticket["evidence_hash"],
                    "pending_reason": action["pending_reason"],
                    "next_review_trigger": next_review_trigger,
                    "authority_effect": authority_effect,
                    "resolution_note": action["resolution_note"],
                    "decision_hash": decision["decision_hash"],
                }
            )
    for component_id, component in components.items():
        if not component["overflow"]:
            continue
        for ticket_id in component["ticket_ids"]:
            ticket = tickets[ticket_id]
            resolutions.append(
                {
                    "ticket_id": ticket_id,
                    "source_row_kind": ticket["source_row_kind"],
                    "source_row_id": ticket["source_row_id"],
                    "endpoint_role": ticket["endpoint_role"],
                    "action": "keep_pending",
                    "bound_candidate_card_id": None,
                    "source_block_ids": ticket["source_block_ids"],
                    "lifecycle_state": "pending",
                    "hearing_count": ticket["hearing_count"],
                    "evidence_hash": ticket["evidence_hash"],
                    "pending_reason": "component_overflow",
                    "next_review_trigger": "bounded_component_repack",
                    "authority_effect": "none",
                    "resolution_note": "Component exceeded the sealed context cap.",
                    "decision_hash": None,
                }
            )
    body = {
        "schema_version": REGISTRY_LEDGER_SCHEMA_VERSION,
        "validator_version": RECOVERY_VALIDATOR_VERSION,
        "recovery_index_hash": verified["recovery_index_hash"],
        "source_b2_artifact_hash": verified["source_b2_artifact_hash"],
        "chapter_id": verified["chapter_id"],
        "decisions": [
            decision_by_component[component_id]
            for component_id in sorted(decision_by_component)
        ],
        "decision_hashes": sorted(
            decision["decision_hash"] for decision in decision_by_component.values()
        ),
        "local_candidate_cards": sorted(
            local_cards, key=lambda row: row["candidate_card_id"]
        ),
        "ticket_resolutions": sorted(
            resolutions, key=lambda row: row["ticket_id"]
        ),
        "book_global_identity_mutation_performed": False,
        "global_alias_authority_granted": False,
        "production_publish_performed": False,
    }
    if validated_quarantines:
        body["quarantined_components"] = validated_quarantines
    return {**body, "registry_recovery_ledger_hash": canonical_hash(body)}


def verify_registry_recovery_ledger_v1(
    ledger: Mapping[str, Any], *, index: Mapping[str, Any]
) -> dict[str, Any]:
    verified = _verified_hash(
        ledger,
        hash_field="registry_recovery_ledger_hash",
        label="registry recovery ledger",
    )
    source = verify_b2_recovery_index_v1(index)
    if verified.get("schema_version") != REGISTRY_LEDGER_SCHEMA_VERSION:
        raise B2RecoveryContractError("foreign registry recovery ledger schema")
    if verified.get("recovery_index_hash") != source["recovery_index_hash"]:
        raise B2RecoveryContractError("registry recovery ledger cites another index")
    if verified.get("book_global_identity_mutation_performed") is not False:
        raise B2RecoveryContractError("registry recovery mutated book identity")
    if verified.get("global_alias_authority_granted") is not False:
        raise B2RecoveryContractError("registry recovery granted global alias authority")
    expected = {
        row["ticket_id"] for row in source["registry_gap_tickets"]
    }
    observed = {
        _required_string(row.get("ticket_id"), "ledger ticket_id")
        for row in verified.get("ticket_resolutions") or []
    }
    validated_quarantines = validate_registry_recovery_component_quarantines_v1(
        index=source,
        quarantines=verified.get("quarantined_components") or [],
    )
    quarantined_ticket_ids = {
        ticket_id
        for row in validated_quarantines
        for ticket_id in row["ticket_ids"]
    }
    if (
        observed.intersection(quarantined_ticket_ids)
        or expected != observed.union(quarantined_ticket_ids)
    ):
        raise B2RecoveryContractError(
            "registry recovery ledger decisions and quarantines do not exact-cover tickets"
        )
    rebuilt = build_registry_recovery_ledger_v1(
        index=source,
        decisions=verified.get("decisions") or [],
        quarantined_components=validated_quarantines,
    )
    if canonical_json(rebuilt) != canonical_json(verified):
        raise B2RecoveryContractError(
            "registry recovery ledger differs from validated decisions"
        )
    return _clone(verified)


def _registry_binding_map(
    ledger: Mapping[str, Any],
    *,
    index: Mapping[str, Any],
) -> dict[tuple[str, str], dict[str, Any]]:
    tickets = _catalog(index, "registry_gap_tickets", "ticket_id")
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for row in ledger.get("ticket_resolutions") or []:
        card_id = row.get("bound_candidate_card_id")
        if not card_id:
            continue
        ticket = tickets.get(str(row.get("ticket_id")))
        if ticket is None:
            raise B2RecoveryContractError(
                "registry binding cites a missing recovery ticket"
            )
        key = (str(row["source_row_id"]), str(row["endpoint_role"]))
        if key in result and result[key]["candidate_card_id"] != card_id:
            raise B2RecoveryContractError(
                "one B2 endpoint receives conflicting recovery bindings"
            )
        result[key] = {
            "ticket_id": row["ticket_id"],
            "candidate_card_id": card_id,
            "action": row["action"],
            "issue_kind": ticket["issue_kind"],
            "source_block_ids": _clone(row.get("source_block_ids") or []),
            "decision_hash": row.get("decision_hash"),
            "resolution_note": row.get("resolution_note"),
        }
    return result


def _registry_pending_map(
    ledger: Mapping[str, Any], *, index: Mapping[str, Any]
) -> dict[tuple[str, str], dict[str, Any]]:
    tickets = _catalog(index, "registry_gap_tickets", "ticket_id")
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for row in ledger.get("ticket_resolutions") or []:
        if row.get("action") != "keep_pending":
            continue
        ticket = tickets.get(str(row.get("ticket_id")))
        if ticket is None or ticket.get("resolution_status") != "resolved_candidate":
            continue
        key = (str(row["source_row_id"]), str(row["endpoint_role"]))
        if key in result:
            raise B2RecoveryContractError(
                "one B2 endpoint receives repeated pending recovery decisions"
            )
        result[key] = {
            "ticket_id": row["ticket_id"],
            "action": row["action"],
            "pending_reason": row.get("pending_reason"),
        }
    return result


def _overlay_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    row_kind: str,
    endpoint_roles: Sequence[str],
    binding_map: Mapping[tuple[str, str], Mapping[str, Any]],
    pending_map: Mapping[tuple[str, str], Mapping[str, Any]],
) -> list[dict[str, Any]]:
    id_field = f"{row_kind}_id"
    result: list[dict[str, Any]] = []
    for raw in rows:
        row = _clone(dict(raw))
        row_id = _required_string(row.get(id_field), id_field)
        original_row_status = row.get("row_status")
        original_speaker_authority = row.get("speaker_authority_status")
        bindings: list[dict[str, Any]] = []
        pending_rows: list[dict[str, Any]] = []
        for role in endpoint_roles:
            binding = binding_map.get((row_id, role))
            pending = pending_map.get((row_id, role))
            if binding is None and pending is None:
                continue
            original = _clone(row[role])
            if binding is not None:
                row[role] = {
                    **_clone(row[role]),
                    "resolution_status": "resolved_candidate",
                    "candidate_card_ids": [binding["candidate_card_id"]],
                    "resolution_basis": "registry_recovery",
                }
                bindings.append(
                    {
                        "endpoint_role": role,
                        "ticket_id": binding["ticket_id"],
                        "candidate_card_id": binding["candidate_card_id"],
                        "original_endpoint": original,
                    }
                )
                if (
                    row_kind == "speaker_turn"
                    and role == "speaker"
                    and binding["issue_kind"]
                    == "contextual_speaker_attribution"
                ):
                    row["speaker_authority_status"] = (
                        "auditor_confirmed_contextual"
                    )
                    if row.get("row_status") == (
                        "review_required_speaker_attribution"
                    ):
                        row["row_status"] = "accepted_observation"
                    row["speaker_recovery_authority"] = {
                        "authority_scope": (
                            "chapter_local_speaker_attribution"
                        ),
                        "ticket_id": binding["ticket_id"],
                        "candidate_card_id": binding["candidate_card_id"],
                        "source_block_ids": _clone(
                            binding["source_block_ids"]
                        ),
                        "decision_hash": binding["decision_hash"],
                        "resolution_note": binding["resolution_note"],
                        "original_speaker_authority_status": (
                            original_speaker_authority
                        ),
                        "original_row_status": original_row_status,
                        "book_global_identity_authority_granted": False,
                    }
                continue
            row[role] = {
                **_clone(row[role]),
                "resolution_status": "pending_contract_conflict",
                "candidate_card_ids": [],
                "resolution_basis": "registry_recovery",
            }
            pending_rows.append(
                {
                    "endpoint_role": role,
                    "ticket_id": pending["ticket_id"],
                    "pending_reason": pending.get("pending_reason"),
                    "original_endpoint": original,
                }
            )
        if bindings:
            row["registry_recovery_bindings"] = bindings
        if pending_rows:
            row["registry_recovery_pending"] = pending_rows
            if row_kind == "speaker_turn" and any(
                pending["endpoint_role"] == "speaker" for pending in pending_rows
            ):
                row["speaker_authority_status"] = "pending_review"
                row["row_status"] = "review_required_speaker_attribution"
        result.append(row)
    return result


def overlay_b2_rows_with_registry_recovery_v1(
    *,
    chapter_artifact: Mapping[str, Any],
    index: Mapping[str, Any],
    registry_ledger: Mapping[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    artifact = _verified_chapter_artifact(chapter_artifact)
    verified_index = verify_b2_recovery_index_v1(index)
    ledger = verify_registry_recovery_ledger_v1(
        registry_ledger, index=verified_index
    )
    if artifact["artifact_hash"] != verified_index["source_b2_artifact_hash"]:
        raise B2RecoveryContractError("recovery index cites another B2 artifact")
    binding_map = _registry_binding_map(ledger, index=verified_index)
    pending_map = _registry_pending_map(ledger, index=verified_index)
    return {
        "speaker_turns": _overlay_rows(
            artifact["speaker_turns"],
            row_kind="speaker_turn",
            endpoint_roles=("speaker", "addressee"),
            binding_map=binding_map,
            pending_map=pending_map,
        ),
        "interaction_events": _overlay_rows(
            artifact["interaction_events"],
            row_kind="interaction_event",
            endpoint_roles=("actor", "target"),
            binding_map=binding_map,
            pending_map=pending_map,
        ),
    }


def _event_review_context(
    *,
    index: Mapping[str, Any],
    component: Mapping[str, Any],
    registry_ledger: Mapping[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    cards = _catalog(index, "candidate_cards", "candidate_card_id")
    selected_cards = [
        cards[card_id] for card_id in component["candidate_card_ids"]
    ]
    local_cards: list[dict[str, Any]] = []
    if registry_ledger is not None:
        ledger = verify_registry_recovery_ledger_v1(
            registry_ledger, index=index
        )
        component_blocks = set(component["source_block_ids"])
        for card in ledger["local_candidate_cards"]:
            support = {
                row["block_id"] for row in card.get("provenance_refs") or []
            }
            if support.intersection(component_blocks):
                local_cards.append(_clone(card))
    combined = [*selected_cards, *local_cards]
    if len(combined) > MAX_COMPONENT_CANDIDATE_CARDS:
        raise B2RecoveryContractError(
            "event review cards exceed the sealed component cap"
        )
    return combined, local_cards


def render_event_review_request_v1(
    *,
    index: Mapping[str, Any],
    component_id: str,
    chapter_artifact: Mapping[str, Any],
    registry_ledger: Mapping[str, Any] | None = None,
) -> RenderedB2RecoveryRequestV1:
    verified = verify_b2_recovery_index_v1(index)
    component = _component(
        verified, component_id=component_id, collection="event_components"
    )
    artifact = _verified_chapter_artifact(chapter_artifact)
    if artifact["artifact_hash"] != verified["source_b2_artifact_hash"]:
        raise B2RecoveryContractError("event review cites another B2 artifact")
    cases = _catalog(verified, "event_review_cases", "case_id")
    blocks = _catalog(verified, "source_blocks", "block_id")
    cards, _local = _event_review_context(
        index=verified,
        component=component,
        registry_ledger=registry_ledger,
    )
    if registry_ledger is None:
        events_by_id = {
            row["interaction_event_id"]: _clone(row)
            for row in artifact["interaction_events"]
        }
    else:
        overlay = overlay_b2_rows_with_registry_recovery_v1(
            chapter_artifact=artifact,
            index=verified,
            registry_ledger=registry_ledger,
        )
        events_by_id = {
            row["interaction_event_id"]: row
            for row in overlay["interaction_events"]
        }
    case_payloads = []
    for case_id in component["case_ids"]:
        case = _clone(cases[case_id])
        case["event_for_review"] = events_by_id[case["interaction_event_id"]]
        case_payloads.append(case)
    schema = event_review_response_schema_v1()
    schema["properties"]["chapter_id"]["enum"] = [verified["chapter_id"]]
    schema["properties"]["component_id"]["enum"] = [component_id]
    schema["properties"]["event_actions"]["minItems"] = len(component["case_ids"])
    schema["properties"]["event_actions"]["maxItems"] = len(component["case_ids"])
    schema["properties"]["event_actions"]["items"]["properties"]["case_id"][
        "enum"
    ] = list(component["case_ids"])
    payload = {
        "contract_version": RECOVERY_VALIDATOR_VERSION,
        "recovery_index_hash": verified["recovery_index_hash"],
        "registry_recovery_ledger_hash": (
            registry_ledger.get("registry_recovery_ledger_hash")
            if registry_ledger is not None
            else None
        ),
        "chapter_id": verified["chapter_id"],
        "component_id": component_id,
        "event_cases": case_payloads,
        "candidate_cards": cards,
        "source_blocks": [
            blocks[block_id] for block_id in component["source_block_ids"]
        ],
        "authority_policy": {
            "new_entity_allowed": False,
            "replacement_events_append_only": True,
            "pending_has_effective_authority": False,
        },
    }
    return _request(
        request_kind="event_semantic_review",
        prompt_id=EVENT_REVIEW_PROMPT_ID_V1,
        prompt=EVENT_REVIEW_SYSTEM_PROMPT_V1,
        component_id=component_id,
        payload=payload,
        response_schema=schema,
    )


def _normalize_replacement_endpoint(
    endpoint: Mapping[str, Any], *, allowed_cards: set[str], label: str
) -> dict[str, Any]:
    result = _clone(dict(endpoint))
    candidate_ids = [
        _required_string(value, f"{label} candidate id")
        for value in result.get("candidate_card_ids") or []
    ]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise B2RecoveryContractError(f"{label} repeats a candidate id")
    if not set(candidate_ids) <= allowed_cards:
        raise B2RecoveryContractError(f"{label} cites a foreign candidate id")
    status = str(result.get("resolution_status"))
    consistent = (
        (status == "resolved_candidate" and len(candidate_ids) == 1)
        or (
            status in {"resolved_joint_candidates", "ambiguous_candidates"}
            and len(candidate_ids) >= 2
        )
        or (status == "unresolved" and not candidate_ids)
    )
    if not consistent:
        raise B2RecoveryContractError(
            f"{label} status and candidate cardinality disagree"
        )
    result["candidate_card_ids"] = sorted(candidate_ids)
    return result


def _normalize_or_downgrade_replacement_endpoint_v2(
    endpoint: Mapping[str, Any],
    *,
    allowed_cards: set[str],
    label: str,
    replacement_ordinal: int,
    endpoint_role: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    result = _clone(dict(endpoint))
    candidate_ids = [
        _required_string(value, f"{label} candidate id")
        for value in result.get("candidate_card_ids") or []
    ]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise B2RecoveryContractError(f"{label} repeats a candidate id")
    if not set(candidate_ids) <= allowed_cards:
        raise B2RecoveryContractError(f"{label} cites a foreign candidate id")
    status = str(result.get("resolution_status"))
    consistent = (
        (status == "resolved_candidate" and len(candidate_ids) == 1)
        or (
            status in {"resolved_joint_candidates", "ambiguous_candidates"}
            and len(candidate_ids) >= 2
        )
        or (status == "unresolved" and not candidate_ids)
    )
    if consistent:
        result["candidate_card_ids"] = sorted(candidate_ids)
        return result, None

    downgrade = {
        "replacement_ordinal": replacement_ordinal,
        "endpoint_role": endpoint_role,
        "reason_code": "status_candidate_cardinality_mismatch",
        "original_resolution_status": status,
        "original_candidate_card_ids": sorted(candidate_ids),
        "normalized_resolution_status": "unresolved",
        "normalized_candidate_card_ids": [],
    }
    result["resolution_status"] = "unresolved"
    result["candidate_card_ids"] = []
    result["resolution_basis"] = "unknown"
    return result, downgrade


def _validate_contract_downgrades_v2(
    action: Mapping[str, Any],
    *,
    downgrades: Sequence[Mapping[str, Any]],
    allowed_cards: set[str],
) -> list[dict[str, Any]]:
    required_keys = {
        "replacement_ordinal",
        "endpoint_role",
        "reason_code",
        "original_resolution_status",
        "original_candidate_card_ids",
        "normalized_resolution_status",
        "normalized_candidate_card_ids",
    }
    replacements = action.get("replacement_events") or []
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[int, str]] = set()
    for raw in downgrades:
        row = _clone(dict(raw))
        if set(row) != required_keys:
            raise B2RecoveryContractError("event V2 contract downgrade keys drifted")
        ordinal = row.get("replacement_ordinal")
        role = str(row.get("endpoint_role"))
        key = (ordinal, role)
        if (
            not isinstance(ordinal, int)
            or isinstance(ordinal, bool)
            or not 0 <= ordinal < len(replacements)
            or role not in {"actor", "target"}
            or key in seen
        ):
            raise B2RecoveryContractError("event V2 contract downgrade target drifted")
        seen.add(key)
        if row.get("reason_code") != "status_candidate_cardinality_mismatch":
            raise B2RecoveryContractError("foreign event V2 contract downgrade reason")
        original_status = str(row.get("original_resolution_status"))
        if original_status not in {
            "resolved_candidate",
            "resolved_joint_candidates",
            "ambiguous_candidates",
            "unresolved",
        }:
            raise B2RecoveryContractError("foreign original endpoint status in downgrade")
        original_ids = [
            _required_string(value, "contract downgrade candidate id")
            for value in row.get("original_candidate_card_ids") or []
        ]
        if (
            len(original_ids) != len(set(original_ids))
            or not set(original_ids) <= allowed_cards
        ):
            raise B2RecoveryContractError("invalid candidate ids in contract downgrade")
        originally_consistent = (
            (original_status == "resolved_candidate" and len(original_ids) == 1)
            or (
                original_status
                in {"resolved_joint_candidates", "ambiguous_candidates"}
                and len(original_ids) >= 2
            )
            or (original_status == "unresolved" and not original_ids)
        )
        endpoint = replacements[ordinal][role]
        if (
            originally_consistent
            or row.get("normalized_resolution_status") != "unresolved"
            or row.get("normalized_candidate_card_ids") != []
            or endpoint.get("resolution_status") != "unresolved"
            or endpoint.get("candidate_card_ids") != []
            or endpoint.get("resolution_basis") != "unknown"
        ):
            raise B2RecoveryContractError("event V2 contract downgrade is not monotonic")
        row["original_candidate_card_ids"] = sorted(original_ids)
        normalized.append(row)
    return sorted(
        normalized,
        key=lambda row: (row["replacement_ordinal"], row["endpoint_role"]),
    )


def _event_semantic_view(event: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: _clone(event.get(key))
        for key in (
            "block_id",
            "event_anchor",
            "actor",
            "target",
            "interaction_kind",
            "action_summary",
            "observed_valence",
        )
    }


def validate_event_review_response_v1(
    response: Mapping[str, Any],
    *,
    index: Mapping[str, Any],
    component_id: str,
    chapter_artifact: Mapping[str, Any],
    registry_ledger: Mapping[str, Any] | None,
    request_fingerprint: str,
) -> dict[str, Any]:
    verified = verify_b2_recovery_index_v1(index)
    component = _component(
        verified, component_id=component_id, collection="event_components"
    )
    normalized_response, response_normalization_notes = (
        normalize_code_owned_response_echoes_v1(
            response,
            expected={"chapter_id": verified["chapter_id"]},
        )
    )
    _validate_json_schema(
        normalized_response,
        event_review_response_schema_v1(),
        "event review response",
    )
    if normalized_response.get("component_id") != component_id:
        raise B2RecoveryContractError("event response component differs from request")
    rendered = render_event_review_request_v1(
        index=verified,
        component_id=component_id,
        chapter_artifact=chapter_artifact,
        registry_ledger=registry_ledger,
    )
    event_by_case = {
        row["case_id"]: row["event_for_review"]
        for row in rendered.semantic_payload["event_cases"]
    }
    blocks = {
        row["block_id"]: row["text"]
        for row in rendered.semantic_payload["source_blocks"]
    }
    allowed_cards = {
        row["candidate_card_id"]
        for row in rendered.semantic_payload["candidate_cards"]
    }
    allowed_cases = set(component["case_ids"])
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in normalized_response.get("event_actions") or []:
        action = _clone(dict(raw))
        case_id = _required_string(action.get("case_id"), "case_id")
        if case_id not in allowed_cases or case_id in seen:
            raise B2RecoveryContractError(
                "event actions do not exact-cover the component"
            )
        seen.add(case_id)
        original = event_by_case[case_id]
        action_name = _required_string(action.get("action"), "event action")
        if action_name not in EVENT_ACTIONS:
            raise B2RecoveryContractError("foreign event action")
        source_block_ids = [
            _required_string(value, "event evidence block")
            for value in action.get("source_block_ids") or []
        ]
        if (
            len(source_block_ids) != len(set(source_block_ids))
            or not set(source_block_ids) <= set(blocks)
            or str(original["block_id"]) not in source_block_ids
        ):
            raise B2RecoveryContractError(
                "event action cites incomplete or foreign evidence"
            )
        replacements = action.get("replacement_events") or []
        expected_count = {
            "keep": 0,
            "revise": 1,
            "split": None,
            "pending": 0,
            "reject": 0,
        }[action_name]
        if expected_count is not None and len(replacements) != expected_count:
            raise B2RecoveryContractError(
                "event action has the wrong replacement cardinality"
            )
        if action_name == "split" and not 2 <= len(replacements) <= 3:
            raise B2RecoveryContractError("split must return two or three events")
        pending_reason = action.get("pending_reason")
        if action_name == "pending" and pending_reason is None:
            pending_reason = _required_string(
                action.get("resolution_note"), "pending event resolution_note"
            )
            action["pending_reason"] = pending_reason
        if action_name == "pending":
            if pending_reason is None:
                raise B2RecoveryContractError("pending event lacks a reason")
        elif pending_reason is not None:
            raise B2RecoveryContractError(
                "non-pending event action carries a pending reason"
            )
        normalized_replacements: list[dict[str, Any]] = []
        for raw_event in replacements:
            event = _clone(dict(raw_event))
            block_id = _required_string(event.get("block_id"), "replacement block")
            if block_id != original["block_id"] or block_id not in blocks:
                raise B2RecoveryContractError(
                    "replacement event must remain in the original source block"
                )
            anchor = _required_string(event.get("event_anchor"), "event anchor")
            spans = _source_spans(blocks[block_id], anchor)
            if not spans:
                raise B2RecoveryContractError(
                    "replacement event anchor is not exact source text"
                )
            event["actor"] = _normalize_replacement_endpoint(
                event["actor"], allowed_cards=allowed_cards, label="replacement actor"
            )
            event["target"] = _normalize_replacement_endpoint(
                event["target"], allowed_cards=allowed_cards, label="replacement target"
            )
            event["source_spans"] = spans
            event["grounding_status"] = "grounded"
            event["row_status"] = "audited_observation"
            normalized_replacements.append(event)
        hashes = [canonical_hash(row) for row in normalized_replacements]
        if len(hashes) != len(set(hashes)):
            raise B2RecoveryContractError("event action repeats a replacement event")
        if (
            action_name == "revise"
            and _event_semantic_view(normalized_replacements[0])
            == _event_semantic_view(original)
        ):
            raise B2RecoveryContractError("revise does not change event semantics")
        action["source_block_ids"] = source_block_ids
        action["replacement_events"] = normalized_replacements
        action["review_input_event_hash"] = canonical_hash(original)
        normalized.append(action)
    if seen != allowed_cases:
        raise B2RecoveryContractError(
            "event actions do not exact-cover the component"
        )
    body = {
        "schema_version": EVENT_DECISION_SCHEMA_VERSION,
        "validator_version": RECOVERY_VALIDATOR_VERSION,
        "recovery_index_hash": verified["recovery_index_hash"],
        "registry_recovery_ledger_hash": (
            registry_ledger.get("registry_recovery_ledger_hash")
            if registry_ledger is not None
            else None
        ),
        "chapter_id": verified["chapter_id"],
        "component_id": component_id,
        "request_fingerprint": _required_string(
            request_fingerprint, "request_fingerprint"
        ),
        "event_actions": sorted(normalized, key=lambda row: row["case_id"]),
    }
    body = attach_response_normalization_notes_v1(
        body, response_normalization_notes
    )
    return {**body, "decision_hash": canonical_hash(body)}


def verify_event_review_decision_v1(
    decision: Mapping[str, Any],
    *,
    index: Mapping[str, Any],
    chapter_artifact: Mapping[str, Any],
    registry_ledger: Mapping[str, Any] | None,
) -> dict[str, Any]:
    verified = _verified_hash(
        decision, hash_field="decision_hash", label="event review decision"
    )
    if verified.get("schema_version") != EVENT_DECISION_SCHEMA_VERSION:
        raise B2RecoveryContractError("foreign event review decision schema")
    response_actions = []
    for row in verified.get("event_actions") or []:
        clean = _clone(dict(row))
        clean.pop("review_input_event_hash", None)
        for replacement in clean.get("replacement_events") or []:
            replacement.pop("source_spans", None)
            replacement.pop("grounding_status", None)
            replacement.pop("row_status", None)
        response_actions.append(clean)
    normalized = validate_event_review_response_v1(
        {
            "schema_version": "literary_b2_event_review_response_v1",
            "chapter_id": verified.get("chapter_id"),
            "component_id": verified.get("component_id"),
            "event_actions": response_actions,
        },
        index=index,
        component_id=_required_string(verified.get("component_id"), "component_id"),
        chapter_artifact=chapter_artifact,
        registry_ledger=registry_ledger,
        request_fingerprint=_required_string(
            verified.get("request_fingerprint"), "request_fingerprint"
        ),
    )
    if canonical_json(normalized) != canonical_json(
        _decision_replay_projection_v1(verified)
    ):
        raise B2RecoveryContractError("event review decision normalization drift")
    return _clone(verified)


def build_event_revision_ledger_v1(
    *,
    index: Mapping[str, Any],
    chapter_artifact: Mapping[str, Any],
    registry_ledger: Mapping[str, Any] | None,
    decisions: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    verified = verify_b2_recovery_index_v1(index)
    artifact = _verified_chapter_artifact(chapter_artifact)
    components = {
        row["component_id"]: row for row in verified["event_components"]
    }
    expected = {
        component_id for component_id, row in components.items() if not row["overflow"]
    }
    decision_by_component: dict[str, dict[str, Any]] = {}
    for raw in decisions:
        decision = verify_event_review_decision_v1(
            raw,
            index=verified,
            chapter_artifact=artifact,
            registry_ledger=registry_ledger,
        )
        if decision["component_id"] in decision_by_component:
            raise B2RecoveryContractError("event review repeats a component decision")
        decision_by_component[decision["component_id"]] = decision
    if set(decision_by_component) != expected:
        raise B2RecoveryContractError(
            "event decisions do not exact-cover renderable components"
        )
    if registry_ledger is None:
        review_events = {
            row["interaction_event_id"]: _clone(row)
            for row in artifact["interaction_events"]
        }
    else:
        review_events = {
            row["interaction_event_id"]: row
            for row in overlay_b2_rows_with_registry_recovery_v1(
                chapter_artifact=artifact,
                index=verified,
                registry_ledger=registry_ledger,
            )["interaction_events"]
        }
    cases = _catalog(verified, "event_review_cases", "case_id")
    revisions: list[dict[str, Any]] = []
    for component_id in sorted(decision_by_component):
        decision = decision_by_component[component_id]
        for action in decision["event_actions"]:
            case = cases[action["case_id"]]
            original_id = case["interaction_event_id"]
            review_input = review_events[original_id]
            effective_events: list[dict[str, Any]] = []
            if action["action"] == "keep":
                event = _clone(review_input)
                if canonical_hash(event) != case["event_snapshot_hash"]:
                    event["supersedes_event_id"] = original_id
                    event["interaction_event_id"] = _mint_id(
                        "b2eventr1", _event_semantic_view(event)
                    )
                effective_events = [event]
            elif action["action"] in {"revise", "split"}:
                for replacement in action["replacement_events"]:
                    event = _clone(replacement)
                    event["supersedes_event_id"] = original_id
                    event["interaction_event_id"] = _mint_id(
                        "b2eventr1",
                        {
                            "supersedes_event_id": original_id,
                            "event": _event_semantic_view(event),
                        },
                    )
                    effective_events.append(event)
            revisions.append(
                {
                    "case_id": action["case_id"],
                    "original_event_id": original_id,
                    "original_event": case["event_snapshot"],
                    "review_input_event": review_input,
                    "action": action["action"],
                    "effective_events": effective_events,
                    "source_block_ids": action["source_block_ids"],
                    "lifecycle_state": (
                        "pending"
                        if action["action"] == "pending"
                        else (
                            "rejected"
                            if action["action"] == "reject"
                            else "resolved"
                        )
                    ),
                    "hearing_count": 1,
                    "pending_reason": action["pending_reason"],
                    "next_review_trigger": (
                        "new_b2_or_registry_evidence"
                        if action["action"] == "pending"
                        else None
                    ),
                    "authority_effect": (
                        "effective_event_observation"
                        if action["action"] in {"keep", "revise", "split"}
                        else "none"
                    ),
                    "resolution_note": action["resolution_note"],
                    "decision_hash": decision["decision_hash"],
                }
            )
    for component_id, component in components.items():
        if not component["overflow"]:
            continue
        for case_id in component["case_ids"]:
            case = cases[case_id]
            revisions.append(
                {
                    "case_id": case_id,
                    "original_event_id": case["interaction_event_id"],
                    "original_event": case["event_snapshot"],
                    "review_input_event": review_events[
                        case["interaction_event_id"]
                    ],
                    "action": "pending",
                    "effective_events": [],
                    "source_block_ids": case["source_block_ids"],
                    "lifecycle_state": "pending",
                    "hearing_count": 0,
                    "pending_reason": "component_overflow",
                    "next_review_trigger": "bounded_component_repack",
                    "authority_effect": "none",
                    "resolution_note": "Component exceeded the sealed context cap.",
                    "decision_hash": None,
                }
            )
    body = {
        "schema_version": EVENT_LEDGER_SCHEMA_VERSION,
        "validator_version": RECOVERY_VALIDATOR_VERSION,
        "recovery_index_hash": verified["recovery_index_hash"],
        "registry_recovery_ledger_hash": (
            registry_ledger.get("registry_recovery_ledger_hash")
            if registry_ledger is not None
            else None
        ),
        "source_b2_artifact_hash": artifact["artifact_hash"],
        "chapter_id": verified["chapter_id"],
        "decisions": [
            decision_by_component[component_id]
            for component_id in sorted(decision_by_component)
        ],
        "decision_hashes": sorted(
            decision["decision_hash"] for decision in decision_by_component.values()
        ),
        "event_revisions": sorted(revisions, key=lambda row: row["case_id"]),
        "original_history_retained": True,
        "book_global_identity_mutation_performed": False,
        "production_publish_performed": False,
    }
    return {**body, "event_revision_ledger_hash": canonical_hash(body)}


def verify_event_revision_ledger_v1(
    ledger: Mapping[str, Any],
    *,
    index: Mapping[str, Any],
    chapter_artifact: Mapping[str, Any],
    registry_ledger: Mapping[str, Any] | None,
) -> dict[str, Any]:
    verified = _verified_hash(
        ledger, hash_field="event_revision_ledger_hash", label="event revision ledger"
    )
    source = verify_b2_recovery_index_v1(index)
    if verified.get("schema_version") != EVENT_LEDGER_SCHEMA_VERSION:
        raise B2RecoveryContractError("foreign event revision ledger schema")
    if verified.get("recovery_index_hash") != source["recovery_index_hash"]:
        raise B2RecoveryContractError("event revision ledger cites another index")
    expected = {row["case_id"] for row in source["event_review_cases"]}
    observed = {
        _required_string(row.get("case_id"), "event revision case_id")
        for row in verified.get("event_revisions") or []
    }
    if expected != observed:
        raise B2RecoveryContractError(
            "event revision ledger does not exact-cover event cases"
        )
    if verified.get("original_history_retained") is not True:
        raise B2RecoveryContractError("event revision ledger discarded history")
    rebuilt = build_event_revision_ledger_v1(
        index=source,
        chapter_artifact=chapter_artifact,
        registry_ledger=registry_ledger,
        decisions=verified.get("decisions") or [],
    )
    if canonical_json(rebuilt) != canonical_json(verified):
        raise B2RecoveryContractError(
            "event revision ledger differs from validated decisions"
        )
    return _clone(verified)


def build_effective_b2_projection_v1(
    *,
    chapter_artifact: Mapping[str, Any],
    index: Mapping[str, Any],
    registry_ledger: Mapping[str, Any],
    event_ledger: Mapping[str, Any],
) -> dict[str, Any]:
    artifact = _verified_chapter_artifact(chapter_artifact)
    verified_index = verify_b2_recovery_index_v1(index)
    registry = verify_registry_recovery_ledger_v1(
        registry_ledger, index=verified_index
    )
    events = verify_event_revision_ledger_v1(
        event_ledger,
        index=verified_index,
        chapter_artifact=artifact,
        registry_ledger=registry,
    )
    if not (
        artifact["artifact_hash"]
        == verified_index["source_b2_artifact_hash"]
        == events["source_b2_artifact_hash"]
    ):
        raise B2RecoveryContractError("projection inputs cite different B2 artifacts")
    overlaid = overlay_b2_rows_with_registry_recovery_v1(
        chapter_artifact=artifact,
        index=verified_index,
        registry_ledger=registry,
    )
    effective_events = [
        _clone(event)
        for revision in events["event_revisions"]
        for event in revision["effective_events"]
    ]
    pending_registry = [
        _clone(row)
        for row in registry["ticket_resolutions"]
        if row["lifecycle_state"] == "pending"
    ]
    pending_events = [
        _clone(row)
        for row in events["event_revisions"]
        if row["lifecycle_state"] == "pending"
    ]
    rejected_registry = [
        row["ticket_id"]
        for row in registry["ticket_resolutions"]
        if row["lifecycle_state"] == "rejected"
    ]
    retired_events = [
        row["original_event_id"]
        for row in events["event_revisions"]
        if row["lifecycle_state"] == "rejected"
    ]
    block_order = {
        row["block_id"]: index
        for index, row in enumerate(verified_index["source_blocks"])
    }
    body = {
        "schema_version": EFFECTIVE_PROJECTION_SCHEMA_VERSION,
        "validator_version": RECOVERY_VALIDATOR_VERSION,
        "chapter_id": verified_index["chapter_id"],
        "source_b2_artifact_hash": artifact["artifact_hash"],
        "recovery_index_hash": verified_index["recovery_index_hash"],
        "registry_recovery_ledger_hash": registry[
            "registry_recovery_ledger_hash"
        ],
        "event_revision_ledger_hash": events["event_revision_ledger_hash"],
        "recovered_candidate_cards": registry["local_candidate_cards"],
        "speaker_turns": overlaid["speaker_turns"],
        "interaction_events": sorted(
            effective_events,
            key=lambda row: (
                block_order[row["block_id"]],
                row["event_anchor"],
                row["interaction_event_id"],
            ),
        ),
        "pending_registry_tickets": pending_registry,
        "pending_event_cases": pending_events,
        "rejected_registry_ticket_ids": sorted(rejected_registry),
        "retired_event_ids": sorted(retired_events),
        "original_b2_history": {
            "speaker_turns": artifact["speaker_turns"],
            "interaction_events": artifact["interaction_events"],
            "review_requests": artifact["review_requests"],
        },
        "book_global_identity_mutation_performed": False,
        "global_alias_authority_granted": False,
        "relation_phase_inference_performed": False,
        "translation_performed": False,
        "production_publish_performed": False,
    }
    return {**body, "effective_projection_hash": canonical_hash(body)}


def _endpoint_candidate_ids_v2(endpoint: Mapping[str, Any]) -> set[str]:
    return {
        _required_string(value, "event endpoint candidate id")
        for value in endpoint.get("candidate_card_ids") or []
    }


def _endpoint_is_resolved_v2(endpoint: Mapping[str, Any]) -> bool:
    return str(endpoint.get("resolution_status")) in {
        "resolved_candidate",
        "resolved_joint_candidates",
    }


def _event_mechanical_review_flags_v2(
    event: Mapping[str, Any], *, cards: Mapping[str, Mapping[str, Any]]
) -> list[str]:
    actor = dict(event.get("actor") or {})
    target = dict(event.get("target") or {})
    actor_ids = _endpoint_candidate_ids_v2(actor)
    target_ids = _endpoint_candidate_ids_v2(target)
    flags: set[str] = set()
    if actor_ids.intersection(target_ids):
        flags.add("actor_target_candidate_overlap")
    if len(actor_ids) > 1:
        flags.add("joint_actor")
    if len(target_ids) > 1:
        flags.add("joint_target")
    if not _endpoint_is_resolved_v2(actor):
        flags.add("actor_endpoint_unsettled")
    if not _endpoint_is_resolved_v2(target):
        flags.add("target_endpoint_unsettled")
    for role, candidate_ids in (("actor", actor_ids), ("target", target_ids)):
        kinds = {
            str(
                (cards[candidate_id].get("effective_claims_as_of") or {}).get(
                    "referent_kind"
                )
            )
            for candidate_id in candidate_ids
            if candidate_id in cards
        }
        if any(kind not in PAIRWISE_REFERENT_KINDS_V2 for kind in kinds):
            flags.add(f"{role}_has_non_pairwise_referent_kind")
    return sorted(flags)


def render_event_review_request_v2(
    *,
    index: Mapping[str, Any],
    component_id: str,
    chapter_artifact: Mapping[str, Any],
    registry_ledger: Mapping[str, Any] | None = None,
) -> RenderedB2RecoveryRequestV1:
    base = render_event_review_request_v1(
        index=index,
        component_id=component_id,
        chapter_artifact=chapter_artifact,
        registry_ledger=registry_ledger,
    )
    payload = _clone(base.semantic_payload)
    cards = {
        row["candidate_card_id"]: row for row in payload.get("candidate_cards") or []
    }
    for case in payload.get("event_cases") or []:
        case["mechanical_review_flags"] = _event_mechanical_review_flags_v2(
            case["event_for_review"], cards=cards
        )
    payload["contract_version"] = EVENT_AUTHORITY_VALIDATOR_VERSION_V2
    payload["authority_policy"] = {
        "new_entity_allowed": False,
        "replacement_events_append_only": True,
        "pending_has_effective_authority": False,
        "nonactual_has_event_authority": False,
        "self_directed_has_pairwise_relation_authority": False,
        "endpoint_uncertain_has_pairwise_relation_authority": False,
    }
    verified = verify_b2_recovery_index_v1(index)
    component = _component(
        verified, component_id=component_id, collection="event_components"
    )
    schema = event_review_response_schema_v2()
    schema["properties"]["chapter_id"]["enum"] = [verified["chapter_id"]]
    schema["properties"]["component_id"]["enum"] = [component_id]
    schema["properties"]["event_actions"]["minItems"] = len(component["case_ids"])
    schema["properties"]["event_actions"]["maxItems"] = len(component["case_ids"])
    schema["properties"]["event_actions"]["items"]["properties"]["case_id"][
        "enum"
    ] = list(component["case_ids"])
    return _request(
        request_kind="event_semantic_review",
        prompt_id=EVENT_REVIEW_PROMPT_ID_V2,
        prompt=EVENT_REVIEW_SYSTEM_PROMPT_V2,
        component_id=component_id,
        payload=payload,
        response_schema=schema,
    )


def _validate_event_assessment_v2(
    assessment: Mapping[str, Any],
    *,
    event: Mapping[str, Any],
    label: str,
) -> tuple[dict[str, str], dict[str, Any] | None]:
    result = _clone(dict(assessment))
    _exact_keys(
        result,
        {"directionality", "actuality", "endpoint_status"},
        label,
    )
    directionality = _required_string(
        result.get("directionality"), f"{label} directionality"
    )
    actuality = _required_string(result.get("actuality"), f"{label} actuality")
    endpoint_status = _required_string(
        result.get("endpoint_status"), f"{label} endpoint_status"
    )
    if directionality not in EVENT_DIRECTIONALITIES_V2:
        raise B2RecoveryContractError(f"{label} has a foreign directionality")
    if actuality not in EVENT_ACTUALITIES_V2:
        raise B2RecoveryContractError(f"{label} has a foreign actuality")
    if endpoint_status not in EVENT_ENDPOINT_STATUSES_V2:
        raise B2RecoveryContractError(f"{label} has a foreign endpoint status")

    actor = dict(event.get("actor") or {})
    target = dict(event.get("target") or {})
    actor_resolved = _endpoint_is_resolved_v2(actor)
    target_resolved = _endpoint_is_resolved_v2(target)
    actor_ids = _endpoint_candidate_ids_v2(actor)
    target_ids = _endpoint_candidate_ids_v2(target)
    overlap = actor_ids.intersection(target_ids)
    resolved_count = int(actor_resolved) + int(target_resolved)

    mechanical_max = (
        "resolved"
        if resolved_count == 2
        else ("partial" if resolved_count == 1 else "pending")
    )
    authority_rank = {"pending": 0, "partial": 1, "resolved": 2}
    assessment_downgrade: dict[str, Any] | None = None
    if authority_rank[endpoint_status] > authority_rank[mechanical_max]:
        assessment_downgrade = {
            "reason_code": "endpoint_status_exceeds_mechanical_maximum",
            "original_endpoint_status": endpoint_status,
            "normalized_endpoint_status": mechanical_max,
            "mechanically_resolved_endpoint_count": resolved_count,
        }
        endpoint_status = mechanical_max
    if endpoint_status == "resolved" and overlap:
        valid_self = (
            directionality == "self_directed"
            and actor_ids == target_ids
            and len(actor_ids) == 1
        )
        if not valid_self:
            raise B2RecoveryContractError(
                f"{label} grants authority to an overlapping endpoint"
            )
    if (
        endpoint_status == "resolved"
        and directionality == "self_directed"
        and not (actor_ids == target_ids and len(actor_ids) == 1)
    ):
        raise B2RecoveryContractError(
            f"{label} self-directed event lacks one shared referent"
        )
    return (
        {
            "directionality": directionality,
            "actuality": actuality,
            "endpoint_status": endpoint_status,
        },
        assessment_downgrade,
    )


def _validate_assessment_contract_downgrades_v2(
    action: Mapping[str, Any],
    *,
    downgrades: Sequence[Mapping[str, Any]],
    effective_events: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    required_keys = {
        "assessment_ordinal",
        "reason_code",
        "original_endpoint_status",
        "normalized_endpoint_status",
        "mechanically_resolved_endpoint_count",
    }
    assessments = action.get("effective_event_assessments") or []
    if len(assessments) != len(effective_events):
        raise B2RecoveryContractError(
            "assessment downgrade events drifted from assessments"
        )
    normalized: list[dict[str, Any]] = []
    seen: set[int] = set()
    authority_rank = {"pending": 0, "partial": 1, "resolved": 2}
    for raw in downgrades:
        row = _clone(dict(raw))
        if set(row) != required_keys:
            raise B2RecoveryContractError(
                "event V2 assessment downgrade keys drifted"
            )
        ordinal = row.get("assessment_ordinal")
        if (
            not isinstance(ordinal, int)
            or isinstance(ordinal, bool)
            or not 0 <= ordinal < len(assessments)
            or ordinal in seen
        ):
            raise B2RecoveryContractError(
                "event V2 assessment downgrade target drifted"
            )
        seen.add(ordinal)
        if (
            row.get("reason_code")
            != "endpoint_status_exceeds_mechanical_maximum"
        ):
            raise B2RecoveryContractError(
                "foreign event V2 assessment downgrade reason"
            )
        original = str(row.get("original_endpoint_status"))
        normalized_status = str(row.get("normalized_endpoint_status"))
        if (
            original not in EVENT_ENDPOINT_STATUSES_V2
            or normalized_status not in EVENT_ENDPOINT_STATUSES_V2
        ):
            raise B2RecoveryContractError(
                "foreign endpoint status in assessment downgrade"
            )
        event = effective_events[ordinal]
        resolved_count = int(
            _endpoint_is_resolved_v2(dict(event.get("actor") or {}))
        ) + int(_endpoint_is_resolved_v2(dict(event.get("target") or {})))
        mechanical_max = (
            "resolved"
            if resolved_count == 2
            else ("partial" if resolved_count == 1 else "pending")
        )
        if (
            row.get("mechanically_resolved_endpoint_count") != resolved_count
            or normalized_status != mechanical_max
            or authority_rank[original] <= authority_rank[normalized_status]
            or assessments[ordinal].get("endpoint_status") != normalized_status
        ):
            raise B2RecoveryContractError(
                "event V2 assessment downgrade is not monotonic"
            )
        normalized.append(row)
    return sorted(normalized, key=lambda row: row["assessment_ordinal"])


def validate_event_review_response_v2(
    response: Mapping[str, Any],
    *,
    index: Mapping[str, Any],
    component_id: str,
    chapter_artifact: Mapping[str, Any],
    registry_ledger: Mapping[str, Any] | None,
    request_fingerprint: str,
) -> dict[str, Any]:
    verified = verify_b2_recovery_index_v1(index)
    component = _component(
        verified, component_id=component_id, collection="event_components"
    )
    normalized_response, response_normalization_notes = (
        normalize_code_owned_response_echoes_v1(
            response,
            expected={"chapter_id": verified["chapter_id"]},
        )
    )
    _validate_json_schema(
        normalized_response,
        event_review_response_schema_v2(),
        "event review V2 response",
    )
    rendered = render_event_review_request_v2(
        index=verified,
        component_id=component_id,
        chapter_artifact=chapter_artifact,
        registry_ledger=registry_ledger,
    )
    if request_fingerprint != rendered.request_fingerprint:
        raise B2RecoveryContractError("event review V2 request fingerprint differs")
    if normalized_response.get("component_id") != component_id:
        raise B2RecoveryContractError("event response component differs from request")

    allowed_cards = {
        row["candidate_card_id"]
        for row in rendered.semantic_payload.get("candidate_cards") or []
    }
    raw_actions: dict[str, dict[str, Any]] = {}
    contract_downgrades: dict[str, list[dict[str, Any]]] = {}
    sanitized_actions: list[dict[str, Any]] = []
    for raw_row in normalized_response.get("event_actions") or []:
        row = _clone(dict(raw_row))
        case_id = _required_string(row.get("case_id"), "event V2 case_id")
        if case_id in raw_actions:
            raise B2RecoveryContractError("event V2 response repeats a case")
        row_downgrades: list[dict[str, Any]] = []
        for replacement_ordinal, replacement in enumerate(
            row.get("replacement_events") or []
        ):
            for endpoint_role in ("actor", "target"):
                endpoint, downgrade = _normalize_or_downgrade_replacement_endpoint_v2(
                    replacement[endpoint_role],
                    allowed_cards=allowed_cards,
                    label=f"replacement {endpoint_role}",
                    replacement_ordinal=replacement_ordinal,
                    endpoint_role=endpoint_role,
                )
                replacement[endpoint_role] = endpoint
                if downgrade is not None:
                    row_downgrades.append(downgrade)
        raw_actions[case_id] = row
        contract_downgrades[case_id] = row_downgrades
        sanitized_actions.append(row)
    base_response = {
        "schema_version": "literary_b2_event_review_response_v1",
        "chapter_id": normalized_response.get("chapter_id"),
        "component_id": normalized_response.get("component_id"),
        "event_actions": [
            {
                key: _clone(value)
                for key, value in row.items()
                if key != "effective_event_assessments"
            }
            for row in sanitized_actions
        ],
    }
    base_decision = validate_event_review_response_v1(
        base_response,
        index=verified,
        component_id=component_id,
        chapter_artifact=chapter_artifact,
        registry_ledger=registry_ledger,
        request_fingerprint=request_fingerprint,
    )
    cases = {
        row["case_id"]: row
        for row in rendered.semantic_payload.get("event_cases") or []
    }
    actions: list[dict[str, Any]] = []
    for base_action in base_decision["event_actions"]:
        case_id = base_action["case_id"]
        raw_action = raw_actions[case_id]
        assessments = raw_action.get("effective_event_assessments") or []
        if base_action["action"] == "keep":
            effective_events = [cases[case_id]["event_for_review"]]
        elif base_action["action"] in {"revise", "split"}:
            effective_events = base_action["replacement_events"]
        else:
            effective_events = []
        if len(assessments) != len(effective_events):
            raise B2RecoveryContractError(
                "event V2 assessments do not exact-cover effective events"
            )
        normalized_assessments: list[dict[str, str]] = []
        assessment_downgrades: list[dict[str, Any]] = []
        for index_value, (assessment, event) in enumerate(
            zip(assessments, effective_events, strict=True)
        ):
            normalized_assessment, downgrade = _validate_event_assessment_v2(
                assessment,
                event=event,
                label=f"event action {case_id} assessment {index_value}",
            )
            normalized_assessments.append(normalized_assessment)
            if downgrade is not None:
                assessment_downgrades.append(
                    {"assessment_ordinal": index_value, **downgrade}
                )
        normalized_action = {
            **base_action,
            "effective_event_assessments": normalized_assessments,
        }
        actions.append(
            {
                **normalized_action,
                "contract_downgrades": _validate_contract_downgrades_v2(
                    base_action,
                    downgrades=contract_downgrades[case_id],
                    allowed_cards=allowed_cards,
                ),
                "assessment_contract_downgrades": (
                    _validate_assessment_contract_downgrades_v2(
                        normalized_action,
                        downgrades=assessment_downgrades,
                        effective_events=effective_events,
                    )
                ),
            }
        )
    body = {
        "schema_version": EVENT_DECISION_SCHEMA_VERSION_V2,
        "validator_version": EVENT_AUTHORITY_DECISION_VALIDATOR_VERSION_V2,
        "recovery_index_hash": verified["recovery_index_hash"],
        "registry_recovery_ledger_hash": (
            registry_ledger.get("registry_recovery_ledger_hash")
            if registry_ledger is not None
            else None
        ),
        "chapter_id": verified["chapter_id"],
        "component_id": component_id,
        "request_fingerprint": request_fingerprint,
        "event_actions": sorted(actions, key=lambda row: row["case_id"]),
    }
    body = attach_response_normalization_notes_v1(
        body, response_normalization_notes
    )
    return {**body, "decision_hash": canonical_hash(body)}


def verify_event_review_decision_v2(
    decision: Mapping[str, Any],
    *,
    index: Mapping[str, Any],
    chapter_artifact: Mapping[str, Any],
    registry_ledger: Mapping[str, Any] | None,
) -> dict[str, Any]:
    verified = _verified_hash(
        decision, hash_field="decision_hash", label="event review V2 decision"
    )
    if verified.get("schema_version") != EVENT_DECISION_SCHEMA_VERSION_V2:
        raise B2RecoveryContractError("foreign event review V2 decision schema")
    if (
        verified.get("validator_version")
        != EVENT_AUTHORITY_DECISION_VALIDATOR_VERSION_V2
    ):
        raise B2RecoveryContractError("foreign event authority V2 validator")
    response_actions: list[dict[str, Any]] = []
    stored_downgrades: dict[str, list[dict[str, Any]]] = {}
    stored_assessment_downgrades: dict[str, list[dict[str, Any]]] = {}
    for row in verified.get("event_actions") or []:
        clean = _clone(dict(row))
        case_id = _required_string(clean.get("case_id"), "event V2 case_id")
        raw_downgrades = clean.pop("contract_downgrades", None)
        if not isinstance(raw_downgrades, list):
            raise B2RecoveryContractError("event V2 decision lacks downgrade ledger")
        stored_downgrades[case_id] = raw_downgrades
        raw_assessment_downgrades = clean.pop(
            "assessment_contract_downgrades", None
        )
        if not isinstance(raw_assessment_downgrades, list):
            raise B2RecoveryContractError(
                "event V2 decision lacks assessment downgrade ledger"
            )
        stored_assessment_downgrades[case_id] = raw_assessment_downgrades
        clean.pop("review_input_event_hash", None)
        for replacement in clean.get("replacement_events") or []:
            replacement.pop("source_spans", None)
            replacement.pop("grounding_status", None)
            replacement.pop("row_status", None)
        response_actions.append(clean)
    normalized = validate_event_review_response_v2(
        {
            "schema_version": "literary_b2_event_review_response_v2",
            "chapter_id": verified.get("chapter_id"),
            "component_id": verified.get("component_id"),
            "event_actions": response_actions,
        },
        index=index,
        component_id=_required_string(verified.get("component_id"), "component_id"),
        chapter_artifact=chapter_artifact,
        registry_ledger=registry_ledger,
        request_fingerprint=_required_string(
            verified.get("request_fingerprint"), "request_fingerprint"
        ),
    )
    rendered = render_event_review_request_v2(
        index=index,
        component_id=_required_string(verified.get("component_id"), "component_id"),
        chapter_artifact=chapter_artifact,
        registry_ledger=registry_ledger,
    )
    allowed_cards = {
        row["candidate_card_id"]
        for row in rendered.semantic_payload.get("candidate_cards") or []
    }
    cases = {
        row["case_id"]: row
        for row in rendered.semantic_payload.get("event_cases") or []
    }
    for action in normalized["event_actions"]:
        case_id = action["case_id"]
        action["contract_downgrades"] = _validate_contract_downgrades_v2(
            action,
            downgrades=stored_downgrades.get(case_id) or [],
            allowed_cards=allowed_cards,
        )
        if action["action"] == "keep":
            effective_events = [cases[case_id]["event_for_review"]]
        elif action["action"] in {"revise", "split"}:
            effective_events = action["replacement_events"]
        else:
            effective_events = []
        action["assessment_contract_downgrades"] = (
            _validate_assessment_contract_downgrades_v2(
                action,
                downgrades=(
                    stored_assessment_downgrades.get(case_id) or []
                ),
                effective_events=effective_events,
            )
        )
    normalized_body = {
        key: value for key, value in normalized.items() if key != "decision_hash"
    }
    normalized = {
        **normalized_body,
        "decision_hash": canonical_hash(normalized_body),
    }
    if canonical_json(normalized) != canonical_json(
        _decision_replay_projection_v1(verified)
    ):
        raise B2RecoveryContractError("event review V2 decision normalization drift")
    return _clone(verified)


def _relation_projection_status_v2(
    event: Mapping[str, Any],
    *,
    cards: Mapping[str, Mapping[str, Any]],
) -> str:
    actuality = str(event["actuality"])
    endpoint_status = str(event["endpoint_status"])
    directionality = str(event["directionality"])
    if actuality != "occurred":
        return f"held_{actuality}"
    if endpoint_status != "resolved":
        return f"held_endpoint_{endpoint_status}"
    if directionality == "self_directed":
        return "non_pairwise_self_directed"
    if directionality == "unknown":
        return "held_direction_unknown"
    actor_ids = _endpoint_candidate_ids_v2(event["actor"])
    target_ids = _endpoint_candidate_ids_v2(event["target"])
    if actor_ids.intersection(target_ids):
        return "held_endpoint_overlap"
    kinds = {
        str(
            (cards[candidate_id].get("effective_claims_as_of") or {}).get(
                "referent_kind"
            )
        )
        for candidate_id in actor_ids.union(target_ids)
        if candidate_id in cards
    }
    if any(kind not in PAIRWISE_REFERENT_KINDS_V2 for kind in kinds):
        return "non_pairwise_referent_kind"
    return (
        "eligible_pairwise_reciprocal"
        if directionality == "reciprocal"
        else "eligible_pairwise_directed"
    )


def build_event_revision_ledger_v2(
    *,
    index: Mapping[str, Any],
    chapter_artifact: Mapping[str, Any],
    registry_ledger: Mapping[str, Any] | None,
    decisions: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    verified_index = verify_b2_recovery_index_v1(index)
    artifact = _verified_chapter_artifact(chapter_artifact)
    components = {
        row["component_id"]: row for row in verified_index["event_components"]
    }
    expected_components = {
        component_id
        for component_id, component in components.items()
        if not component["overflow"]
    }
    verified_decisions = [
        verify_event_review_decision_v2(
            decision,
            index=verified_index,
            chapter_artifact=chapter_artifact,
            registry_ledger=registry_ledger,
        )
        for decision in decisions
    ]
    decision_by_component: dict[str, dict[str, Any]] = {}
    for decision in verified_decisions:
        component_id = decision["component_id"]
        if component_id in decision_by_component:
            raise B2RecoveryContractError(
                "event V2 review repeats a component decision"
            )
        decision_by_component[component_id] = decision
    if set(decision_by_component) != expected_components:
        raise B2RecoveryContractError(
            "event V2 decisions do not exact-cover renderable components"
        )

    if registry_ledger is None:
        review_events = {
            row["interaction_event_id"]: _clone(row)
            for row in artifact["interaction_events"]
        }
    else:
        review_events = {
            row["interaction_event_id"]: row
            for row in overlay_b2_rows_with_registry_recovery_v1(
                chapter_artifact=artifact,
                index=verified_index,
                registry_ledger=registry_ledger,
            )["interaction_events"]
        }
    cases = _catalog(verified_index, "event_review_cases", "case_id")
    cards = _catalog(verified_index, "candidate_cards", "candidate_card_id")
    if registry_ledger is not None:
        registry = verify_registry_recovery_ledger_v1(
            registry_ledger, index=verified_index
        )
        cards.update(
            {
                row["candidate_card_id"]: row
                for row in registry["local_candidate_cards"]
            }
        )
    revisions: list[dict[str, Any]] = []
    for component_id in sorted(decision_by_component):
        decision = decision_by_component[component_id]
        for action in decision["event_actions"]:
            case = cases[action["case_id"]]
            original_id = case["interaction_event_id"]
            review_input = review_events[original_id]
            effective_events: list[dict[str, Any]] = []
            if action["action"] == "keep":
                event = _clone(review_input)
                if canonical_hash(event) != case["event_snapshot_hash"]:
                    event["supersedes_event_id"] = original_id
                    event["interaction_event_id"] = _mint_id(
                        "b2eventr2", _event_semantic_view(event)
                    )
                effective_events = [event]
            elif action["action"] in {"revise", "split"}:
                for replacement in action["replacement_events"]:
                    event = _clone(replacement)
                    event["supersedes_event_id"] = original_id
                    event["interaction_event_id"] = _mint_id(
                        "b2eventr2",
                        {
                            "supersedes_event_id": original_id,
                            "event": _event_semantic_view(event),
                        },
                    )
                    effective_events.append(event)
            assessments = action["effective_event_assessments"]
            if len(assessments) != len(effective_events):
                raise B2RecoveryContractError(
                    "event authority assessments drifted from effective events"
                )
            classified_events: list[dict[str, Any]] = []
            for event, assessment in zip(
                effective_events, assessments, strict=True
            ):
                classified = {**_clone(event), **_clone(assessment)}
                classified["event_authority_status"] = (
                    "effective_observation"
                    if assessment["actuality"] == "occurred"
                    else "held_nonactual"
                )
                classified["relation_edge_projection_status"] = (
                    _relation_projection_status_v2(classified, cards=cards)
                )
                classified_events.append(classified)
            revisions.append(
                {
                    "case_id": action["case_id"],
                    "original_event_id": original_id,
                    "original_event": case["event_snapshot"],
                    "review_input_event": review_input,
                    "action": action["action"],
                    "effective_event_assessments": _clone(assessments),
                    "contract_downgrades": _clone(
                        action["contract_downgrades"]
                    ),
                    "assessment_contract_downgrades": _clone(
                        action["assessment_contract_downgrades"]
                    ),
                    "effective_events": classified_events,
                    "source_block_ids": action["source_block_ids"],
                    "lifecycle_state": (
                        "pending"
                        if action["action"] == "pending"
                        else (
                            "rejected"
                            if action["action"] == "reject"
                            else "resolved"
                        )
                    ),
                    "hearing_count": 1,
                    "pending_reason": action["pending_reason"],
                    "next_review_trigger": (
                        "new_b2_or_registry_evidence"
                        if action["action"] == "pending"
                        else None
                    ),
                    "authority_effect": (
                        "effective_event_observation"
                        if any(
                            row["event_authority_status"]
                            == "effective_observation"
                            for row in classified_events
                        )
                        else "none"
                    ),
                    "resolution_note": action["resolution_note"],
                    "decision_hash": decision["decision_hash"],
                }
            )
    for component_id, component in components.items():
        if not component["overflow"]:
            continue
        for case_id in component["case_ids"]:
            case = cases[case_id]
            revisions.append(
                {
                    "case_id": case_id,
                    "original_event_id": case["interaction_event_id"],
                    "original_event": case["event_snapshot"],
                    "review_input_event": review_events[
                        case["interaction_event_id"]
                    ],
                    "action": "pending",
                    "effective_event_assessments": [],
                    "contract_downgrades": [],
                    "assessment_contract_downgrades": [],
                    "effective_events": [],
                    "source_block_ids": case["source_block_ids"],
                    "lifecycle_state": "pending",
                    "hearing_count": 0,
                    "pending_reason": "component_overflow",
                    "next_review_trigger": "bounded_component_repack",
                    "authority_effect": "none",
                    "resolution_note": "Component exceeded the sealed context cap.",
                    "decision_hash": None,
                }
            )
    body = {
        "schema_version": EVENT_LEDGER_SCHEMA_VERSION_V2,
        "validator_version": EVENT_AUTHORITY_DECISION_VALIDATOR_VERSION_V2,
        "recovery_index_hash": verified_index["recovery_index_hash"],
        "registry_recovery_ledger_hash": (
            registry_ledger.get("registry_recovery_ledger_hash")
            if registry_ledger is not None
            else None
        ),
        "source_b2_artifact_hash": artifact["artifact_hash"],
        "chapter_id": verified_index["chapter_id"],
        "decisions": [
            decision_by_component[component_id]
            for component_id in sorted(decision_by_component)
        ],
        "decision_hashes": sorted(
            decision["decision_hash"] for decision in decision_by_component.values()
        ),
        "event_revisions": sorted(revisions, key=lambda row: row["case_id"]),
        "original_history_retained": True,
        "book_global_identity_mutation_performed": False,
        "production_publish_performed": False,
    }
    return {**body, "event_revision_ledger_hash": canonical_hash(body)}


def verify_event_revision_ledger_v2(
    ledger: Mapping[str, Any],
    *,
    index: Mapping[str, Any],
    chapter_artifact: Mapping[str, Any],
    registry_ledger: Mapping[str, Any] | None,
) -> dict[str, Any]:
    verified = _verified_hash(
        ledger, hash_field="event_revision_ledger_hash", label="event V2 ledger"
    )
    if verified.get("schema_version") != EVENT_LEDGER_SCHEMA_VERSION_V2:
        raise B2RecoveryContractError("foreign event V2 ledger schema")
    if (
        verified.get("validator_version")
        != EVENT_AUTHORITY_DECISION_VALIDATOR_VERSION_V2
    ):
        raise B2RecoveryContractError("foreign event V2 ledger validator")
    rebuilt = build_event_revision_ledger_v2(
        index=index,
        chapter_artifact=chapter_artifact,
        registry_ledger=registry_ledger,
        decisions=verified.get("decisions") or [],
    )
    if canonical_json(rebuilt) != canonical_json(verified):
        raise B2RecoveryContractError("event V2 ledger normalization drift")
    return _clone(verified)


def build_effective_b2_projection_v2(
    *,
    chapter_artifact: Mapping[str, Any],
    index: Mapping[str, Any],
    registry_ledger: Mapping[str, Any],
    event_ledger: Mapping[str, Any],
) -> dict[str, Any]:
    artifact = _verified_chapter_artifact(chapter_artifact)
    verified_index = verify_b2_recovery_index_v1(index)
    registry = verify_registry_recovery_ledger_v1(
        registry_ledger, index=verified_index
    )
    events = verify_event_revision_ledger_v2(
        event_ledger,
        index=verified_index,
        chapter_artifact=artifact,
        registry_ledger=registry,
    )
    if not (
        artifact["artifact_hash"]
        == verified_index["source_b2_artifact_hash"]
        == events["source_b2_artifact_hash"]
    ):
        raise B2RecoveryContractError("projection V2 inputs cite different artifacts")
    overlaid = overlay_b2_rows_with_registry_recovery_v1(
        chapter_artifact=artifact,
        index=verified_index,
        registry_ledger=registry,
    )
    classified_events = [
        _clone(event)
        for revision in events["event_revisions"]
        for event in revision["effective_events"]
    ]
    effective_events = [
        row
        for row in classified_events
        if row["event_authority_status"] == "effective_observation"
    ]
    held_mentions = [
        row
        for row in classified_events
        if row["event_authority_status"] != "effective_observation"
    ]
    pending_registry = [
        _clone(row)
        for row in registry["ticket_resolutions"]
        if row["lifecycle_state"] == "pending"
    ]
    pending_events = [
        _clone(row)
        for row in events["event_revisions"]
        if row["lifecycle_state"] == "pending"
    ]
    rejected_registry = [
        row["ticket_id"]
        for row in registry["ticket_resolutions"]
        if row["lifecycle_state"] == "rejected"
    ]
    retired_events = [
        row["original_event_id"]
        for row in events["event_revisions"]
        if row["lifecycle_state"] == "rejected"
    ]
    block_order = {
        row["block_id"]: index_value
        for index_value, row in enumerate(verified_index["source_blocks"])
    }

    def event_sort_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
        return (
            block_order[row["block_id"]],
            row["event_anchor"],
            row["interaction_event_id"],
        )

    relation_projection = [
        {
            "interaction_event_id": row["interaction_event_id"],
            "block_id": row["block_id"],
            "directionality": row["directionality"],
            "actuality": row["actuality"],
            "endpoint_status": row["endpoint_status"],
            "actor_candidate_card_ids": sorted(
                _endpoint_candidate_ids_v2(row["actor"])
            ),
            "target_candidate_card_ids": sorted(
                _endpoint_candidate_ids_v2(row["target"])
            ),
            "projection_status": row["relation_edge_projection_status"],
            "authority_effect": (
                "relation_edge_candidate"
                if row["relation_edge_projection_status"].startswith(
                    "eligible_pairwise_"
                )
                else "none"
            ),
        }
        for row in sorted(classified_events, key=event_sort_key)
    ]
    body = {
        "schema_version": EFFECTIVE_PROJECTION_SCHEMA_VERSION_V2,
        "validator_version": EVENT_AUTHORITY_DECISION_VALIDATOR_VERSION_V2,
        "chapter_id": verified_index["chapter_id"],
        "source_b2_artifact_hash": artifact["artifact_hash"],
        "recovery_index_hash": verified_index["recovery_index_hash"],
        "registry_recovery_ledger_hash": registry[
            "registry_recovery_ledger_hash"
        ],
        "event_revision_ledger_hash": events["event_revision_ledger_hash"],
        "recovered_candidate_cards": registry["local_candidate_cards"],
        "speaker_turns": overlaid["speaker_turns"],
        "interaction_events": sorted(effective_events, key=event_sort_key),
        "held_event_mentions": sorted(held_mentions, key=event_sort_key),
        "relation_event_projection": relation_projection,
        "pending_registry_tickets": pending_registry,
        "pending_event_cases": pending_events,
        "rejected_registry_ticket_ids": sorted(rejected_registry),
        "retired_event_ids": sorted(retired_events),
        "original_b2_history": {
            "speaker_turns": artifact["speaker_turns"],
            "interaction_events": artifact["interaction_events"],
            "review_requests": artifact["review_requests"],
        },
        "book_global_identity_mutation_performed": False,
        "global_alias_authority_granted": False,
        "relation_phase_inference_performed": False,
        "translation_performed": False,
        "production_publish_performed": False,
    }
    return {**body, "effective_projection_hash": canonical_hash(body)}


def classify_recovery_reopen_v1(
    *,
    previous_resolution: Mapping[str, Any],
    new_evidence_hash: str,
) -> dict[str, Any]:
    state = str(previous_resolution.get("lifecycle_state") or "")
    hearing_count = int(previous_resolution.get("hearing_count") or 0)
    old_hash = _required_string(
        previous_resolution.get("evidence_hash"), "previous evidence_hash"
    )
    changed = old_hash != _required_string(new_evidence_hash, "new evidence_hash")
    eligible = (
        state == "pending"
        and hearing_count < MAX_AUTOMATIC_HEARINGS
        and changed
    )
    return {
        "eligible": eligible,
        "evidence_changed": changed,
        "hearing_count": hearing_count,
        "max_automatic_hearings": MAX_AUTOMATIC_HEARINGS,
        "next_state": (
            "reopened"
            if eligible
            else (
                "book_end_review"
                if state == "pending"
                and hearing_count >= MAX_AUTOMATIC_HEARINGS
                else state
            )
        ),
    }


__all__ = [
    "B2RecoveryContractError",
    "B2RecoveryError",
    "EVENT_DECISION_SCHEMA_VERSION",
    "EVENT_DECISION_SCHEMA_VERSION_V2",
    "EVENT_LEDGER_SCHEMA_VERSION",
    "EVENT_LEDGER_SCHEMA_VERSION_V2",
    "EFFECTIVE_PROJECTION_SCHEMA_VERSION",
    "EFFECTIVE_PROJECTION_SCHEMA_VERSION_V2",
    "MAX_AUTOMATIC_HEARINGS",
    "RECOVERY_INDEX_SCHEMA_VERSION",
    "REGISTRY_DECISION_SCHEMA_VERSION",
    "REGISTRY_LEDGER_SCHEMA_VERSION",
    "RenderedB2RecoveryRequestV1",
    "build_b2_recovery_index_v1",
    "build_effective_b2_projection_v1",
    "build_effective_b2_projection_v2",
    "build_event_revision_ledger_v1",
    "build_event_revision_ledger_v2",
    "build_registry_recovery_ledger_v1",
    "classify_recovery_reopen_v1",
    "overlay_b2_rows_with_registry_recovery_v1",
    "render_event_review_request_v1",
    "render_event_review_request_v2",
    "render_registry_recovery_request_v1",
    "validate_event_review_response_v1",
    "validate_event_review_response_v2",
    "validate_registry_recovery_component_quarantines_v1",
    "validate_registry_recovery_response_v1",
    "verify_b2_recovery_index_v1",
    "verify_event_revision_ledger_v1",
    "verify_event_revision_ledger_v2",
    "verify_event_review_decision_v2",
    "verify_registry_recovery_ledger_v1",
]
