"""Dedicated review boundary for pending Literary B3 temporal claims."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator

from pipeline.literary.b3_temporal_context_v1 import load_b2_temporal_input_v1
from pipeline.literary.b3_temporal_contract_v5 import validate_b3_temporal_request_v5
from pipeline.literary.b3_temporal_contract_v6 import validate_b3_temporal_request_v6
from pipeline.literary.b3_temporal_contract_v7 import validate_b3_temporal_request_v7
from pipeline.literary.b3_temporal_prefix_v1 import (
    load_b3_temporal_chapter_artifact_v1,
)
from pipeline.literary.b3_temporal_prompts_v5 import b3_temporal_response_schema_v5
from pipeline.literary.checkpoint import (
    canonical_hash,
    canonical_json,
    file_sha256,
    resolve_existing_canonical_path,
)
from pipeline.literary.response_normalization_v1 import (
    LiteraryResponseNormalizationError,
    attach_response_normalization_notes_v1,
    normalize_code_owned_response_echoes_v1,
    split_validated_response_normalization_notes_v1,
)
from pipeline.literary.model_ref_v1 import MODEL_REF_FIELDS_V1


ROLE_ID = "literary.audit.b3_stable_claim"
B3_TEMPORAL_AUDITOR_MODEL_REF_FIELDS_V1: Mapping[str, tuple[str, ...]] = {
    namespace: tuple(fields)
    + (("corroborating_state_ids",) if namespace == "state" else ())
    for namespace, fields in MODEL_REF_FIELDS_V1.items()
}
PROMPT_ID = "literary_b3_state_claim_auditor_v3"
PACKET_SCHEMA_VERSION = "literary_b3_temporal_review_packet_v1"
RESPONSE_SCHEMA_VERSION = "literary_b3_temporal_review_response_v1"
OVERLAY_SCHEMA_VERSION = "literary_b3_temporal_review_overlay_v1"
ROUTING_REPORT_SCHEMA_VERSION = "literary_b3_review_routing_report_v1"
STATE_REVIEW_ROUTES = frozenset({"stable_claim_review", "temporal_review"})
REVIEW_ROUTE_DESTINATIONS = {
    "stable_claim_review": {
        "destination_id": ROLE_ID,
        "implementation_status": "implemented",
        "lifecycle_state": "ready_for_state_auditor",
    },
    "temporal_review": {
        "destination_id": ROLE_ID,
        "implementation_status": "implemented",
        "lifecycle_state": "ready_for_state_auditor",
    },
    "identity_review": {
        "destination_id": "literary.audit.cross_chapter_identity",
        "implementation_status": "adapter_required",
        "lifecycle_state": "parked_pending_adapter",
    },
    "inherited_identity_block": {
        "destination_id": "literary.audit.cross_chapter_identity",
        "implementation_status": "inherited_holding_no_consumer",
        "lifecycle_state": "parked_inherited_identity",
    },
}
DISPOSITIONS = frozenset(
    {"confirm_state", "reject_claim", "keep_pending", "refer_identity"}
)
PENDING_REASON_CODES = frozenset(
    {"insufficient_evidence", "conflicting_evidence", "model_uncertain"}
)

SYSTEM_PROMPT = """You are the State-Claim Auditor for Literary B3.
Prompt version: literary_b3_state_claim_auditor_v3.

You receive exactly one pending durable-state case already raised by B3,
together with the exact component, candidate cards, evidence rows, source
blocks, and narrative frames that produced it. Review that case only. Do not
rescan the chapter, create identities, translate text, or invent missing facts.

The case keeps its original review route:
- stable_claim_review asks whether a role, name-usage, or life-status claim is
  sufficiently grounded and durable.
- temporal_review asks whether a proposed state transition is actually new,
  correctly timed, durable, duplicated by an existing state, or still unclear.
Do not reinterpret either route as an identity decision. Use refer_identity
when the state cannot be decided before identity is resolved.

For the case choose exactly one disposition:
- confirm_state: the supplied evidence is sufficient to retain a durable state
  for later chapters. Return one fully grounded resolved_action.
- reject_claim: the proposed interpretation is contradicted, transient, or not
  a useful durable state.
- keep_pending: the evidence remains insufficient or conflicting.
- refer_identity: deciding the state first requires resolving who or what a
  referenced entity is.

`pending_reason_code` is non-null only for keep_pending. Set it to null for
confirm_state, reject_claim, and refer_identity. `resolved_action` is non-null
only for confirm_state.

A character statement is evidence, but neither automatically true nor
automatically false. Judge attribution, narrative frame, corroboration,
contradiction, and whether the claim is durable. Use only supplied referent,
event, turn, block, and frame IDs. A confirmed action must describe an already
effective state: event_status=occurred, temporal_position=current_progression,
and operation=open_state or reveal_only. Output JSON only and return the
supplied pending_case_id exactly once.

For every decision, `cited_source_block_ids` means the source blocks from this
packet that you actually consulted while reaching the verdict. It may include
context or comparison blocks that are not direct anchors of the final action.
For a confirmed case, `resolved_action.source_block_ids` is the narrower set of
blocks that directly ground the effective state. Cite only supplied blocks;
never invent or cite a block outside this packet.
"""


class B3TemporalAuditorError(RuntimeError):
    pass


@dataclass(frozen=True)
class RenderedB3TemporalAuditRequestV1:
    request_fingerprint: str
    messages: tuple[dict[str, str], ...]
    response_schema: dict[str, Any]
    packet: dict[str, Any]


def b3_temporal_audit_response_schema_v1() -> dict[str, Any]:
    action_schema = deepcopy(
        b3_temporal_response_schema_v5()["properties"]["component_results"]
        ["items"]["properties"]["state_actions"]["items"]
    )
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "chapter_id", "case_decisions"],
        "properties": {
            "schema_version": {"type": "string", "const": RESPONSE_SCHEMA_VERSION},
            "chapter_id": {"type": "string", "minLength": 1},
            "case_decisions": {
                "type": "array",
                "minItems": 1,
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "pending_case_id",
                        "disposition",
                        "resolved_action",
                        "cited_source_block_ids",
                        "reason",
                        "pending_reason_code",
                    ],
                    "properties": {
                        "pending_case_id": {"type": "string", "minLength": 1},
                        "disposition": {
                            "type": "string",
                            "enum": sorted(DISPOSITIONS),
                        },
                        "resolved_action": {
                            "anyOf": [action_schema, {"type": "null"}]
                        },
                        "cited_source_block_ids": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 24,
                            "uniqueItems": True,
                            "items": {"type": "string", "minLength": 1},
                        },
                        "reason": {"type": "string", "minLength": 1, "maxLength": 800},
                        "pending_reason_code": {
                            "anyOf": [
                                {
                                    "type": "string",
                                    "enum": sorted(PENDING_REASON_CODES),
                                },
                                {"type": "null"},
                            ]
                        },
                    },
                    "allOf": [
                        {
                            "if": {
                                "properties": {
                                    "disposition": {"const": "keep_pending"}
                                },
                                "required": ["disposition"],
                            },
                            "then": {
                                "properties": {
                                    "pending_reason_code": {
                                        "type": "string",
                                        "enum": sorted(PENDING_REASON_CODES),
                                    }
                                }
                            },
                            "else": {
                                "properties": {
                                    "pending_reason_code": {"type": "null"}
                                }
                            },
                        }
                    ],
                },
            },
        },
    }


def classify_b3_review_selection_v1(
    *, pending_cases: list[Mapping[str, Any]], pending_case_id: str | None = None
) -> dict[str, Any]:
    if not isinstance(pending_cases, list):
        raise B3TemporalAuditorError("B3 pending cases must be a list")
    if pending_case_id is not None and (
        not isinstance(pending_case_id, str) or not pending_case_id.strip()
    ):
        raise B3TemporalAuditorError("B3 pending case selection must be a non-empty ID")

    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    by_route = {route: [] for route in sorted(REVIEW_ROUTE_DESTINATIONS)}
    for source in pending_cases:
        if not isinstance(source, Mapping):
            raise B3TemporalAuditorError("B3 pending case must be an object")
        row = deepcopy(dict(source))
        case_id = row.get("pending_case_id")
        route = row.get("review_route")
        if not isinstance(case_id, str) or not case_id:
            raise B3TemporalAuditorError("B3 pending case ID is absent")
        if case_id in seen_ids:
            raise B3TemporalAuditorError("B3 pending case IDs are not unique")
        if route not in REVIEW_ROUTE_DESTINATIONS:
            raise B3TemporalAuditorError(f"unknown B3 review route: {route}")
        if row.get("authority_status") != "pending_review":
            raise B3TemporalAuditorError("B3 review route contains non-pending authority")
        seen_ids.add(case_id)
        rows.append(row)
        by_route[str(route)].append(case_id)

    rows.sort(key=lambda row: row["pending_case_id"])
    for case_ids in by_route.values():
        case_ids.sort()
    state_rows = [row for row in rows if row["review_route"] in STATE_REVIEW_ROUTES]
    selected: dict[str, Any] | None = None
    if pending_case_id is not None:
        selected = next(
            (row for row in rows if row["pending_case_id"] == pending_case_id), None
        )
        if selected is None:
            status = "pending_case_not_found"
            reason = "The requested pending case is absent from the B3 artifact."
        elif selected["review_route"] not in STATE_REVIEW_ROUTES:
            status = "route_not_supported"
            destination = REVIEW_ROUTE_DESTINATIONS[selected["review_route"]]
            reason = (
                f"The B3 state auditor does not serve {selected['review_route']}; "
                f"the case is {destination['lifecycle_state']} for "
                f"{destination['destination_id']}."
            )
        else:
            status = "ready"
            reason = "The requested pending case is routed to the B3 state auditor."
    elif not state_rows:
        status = "no_matching_cases"
        reason = "No pending case is routed to the B3 state auditor."
    elif len(state_rows) > 1:
        status = "selection_required"
        reason = "More than one pending case is routed here; select a pending_case_id."
    else:
        selected = state_rows[0]
        status = "ready"
        reason = "Exactly one pending case is routed to the B3 state auditor."

    selected_route = None if selected is None else selected["review_route"]
    selected_destination = (
        None
        if selected_route is None
        else {
            "review_route": selected_route,
            **deepcopy(REVIEW_ROUTE_DESTINATIONS[selected_route]),
        }
    )
    body = {
        "schema_version": "literary_b3_review_selection_v1",
        "status": status,
        "reason": reason,
        "requested_pending_case_id": pending_case_id,
        "selected_pending_case_id": (
            None if selected is None else selected["pending_case_id"]
        ),
        "selected_review_route": selected_route,
        "selected_destination": selected_destination,
        "provider_call_allowed": status == "ready",
        "state_auditor_review_routes": sorted(STATE_REVIEW_ROUTES),
        "pending_case_ids_by_route": by_route,
        "route_destinations": [
            {"review_route": route, **deepcopy(REVIEW_ROUTE_DESTINATIONS[route])}
            for route in sorted(REVIEW_ROUTE_DESTINATIONS)
        ],
        "pending_case_count": len(rows),
        "state_auditor_case_count": len(state_rows),
        "unserved_case_count": len(rows) - len(state_rows),
        "authority_changed": False,
        "production_publish_performed": False,
    }
    return {**body, "selection_hash": canonical_hash(body)}


def build_b3_review_routing_report_v1(
    *, b3_root: Path, pending_case_id: str | None = None
) -> dict[str, Any]:
    root = Path(b3_root).resolve()
    artifact, artifact_path = load_b3_temporal_chapter_artifact_v1(root)
    selection = classify_b3_review_selection_v1(
        pending_cases=artifact.get("pending_cases") or [],
        pending_case_id=pending_case_id,
    )
    selection_body = deepcopy(selection)
    selection_body.pop("schema_version")
    selection_body.pop("selection_hash")
    body = {
        "schema_version": ROUTING_REPORT_SCHEMA_VERSION,
        "chapter_id": artifact["chapter_id"],
        "source_b3_root": str(root),
        "source_b3_tree_hash": _tree_hash(root),
        "source_b3_artifact_path": artifact_path.relative_to(root).as_posix(),
        "source_b3_artifact_hash": artifact["artifact_hash"],
        **selection_body,
    }
    return {**body, "routing_report_hash": canonical_hash(body)}


def load_b3_temporal_review_packet_v1(
    *, b3_root: Path, pending_case_id: str | None = None
) -> dict[str, Any]:
    root = Path(b3_root).resolve()
    artifact, artifact_path = load_b3_temporal_chapter_artifact_v1(root)
    routing = build_b3_review_routing_report_v1(
        b3_root=root, pending_case_id=pending_case_id
    )
    if routing["status"] != "ready":
        raise B3TemporalAuditorError(routing["reason"])
    selected_id = routing["selected_pending_case_id"]
    pending = next(
        (
            deepcopy(dict(row))
            for row in artifact.get("pending_cases") or []
            if row.get("pending_case_id") == selected_id
        ),
        None,
    )
    if pending is None:
        raise B3TemporalAuditorError("selected B3 pending case disappeared")
    request = _request_for_pending(root=root, pending=pending)
    _, payload = _validate_source_b3_request(request)
    component = next(
        (
            deepcopy(dict(row))
            for row in payload["components"]
            if row.get("component_id") == pending["component_id"]
        ),
        None,
    )
    if component is None:
        raise B3TemporalAuditorError("pending case component is absent from B3 request")
    component_id = component["component_id"]
    referent_packets = _owned_packets(payload, "referent_packets", component_id)
    frame_packets = _owned_packets(payload, "frame_packets", component_id)

    seal = _verified_hashed_object(root / "run_seal.json", "seal_hash")
    source_root = resolve_existing_canonical_path(seal["source_b2_run_root"])
    recovery_value = seal.get("source_b2_speaker_recovery_root")
    temporal_input = (
        load_b2_temporal_input_v1(source_root)
        if recovery_value is None
        else load_b2_temporal_input_v1(
            source_root,
            speaker_recovery_root=resolve_existing_canonical_path(recovery_value),
        )
    )
    if temporal_input["source_b2_artifact_hash"] != artifact["source_b2_artifact_hash"]:
        raise B3TemporalAuditorError("B3 review source lineage differs")
    evidence_block_ids = _component_block_ids(component)
    source_by_id = {
        str(row["block_id"]): deepcopy(dict(row))
        for row in temporal_input.get("source_blocks") or []
    }
    if not evidence_block_ids <= set(source_by_id):
        raise B3TemporalAuditorError("B3 review source blocks are incomplete")
    source_blocks = [source_by_id[block_id] for block_id in sorted(evidence_block_ids)]
    body = {
        "schema_version": PACKET_SCHEMA_VERSION,
        "chapter_id": artifact["chapter_id"],
        "source_b3_root": str(root),
        "source_b3_tree_hash": _tree_hash(root),
        "source_b3_artifact_path": artifact_path.relative_to(root).as_posix(),
        "source_b3_artifact_hash": artifact["artifact_hash"],
        "source_b3_request_fingerprint": request["request_fingerprint"],
        "pending_cases": [pending],
        "component": component,
        "referent_packets": referent_packets,
        "frame_packets": frame_packets,
        "source_blocks": source_blocks,
        "authority_policy": {
            "pending_input_authoritative": False,
            "auditor_may_confirm_state": True,
            "identity_mutation_allowed": False,
            "source_rewrite_allowed": False,
        },
        "gold_or_oracle_loaded": False,
        "production_publish_performed": False,
    }
    return {**body, "packet_hash": canonical_hash(body)}


def render_b3_temporal_audit_request_v1(
    packet: Mapping[str, Any],
) -> RenderedB3TemporalAuditRequestV1:
    verified = verify_b3_temporal_review_packet_v1(packet)
    messages = (
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": canonical_json(verified)},
    )
    schema = b3_temporal_audit_response_schema_v1()
    body = {
        "prompt_id": PROMPT_ID,
        "prompt_sha256": hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest(),
        "packet_hash": verified["packet_hash"],
        "messages": list(messages),
        "response_schema_hash": canonical_hash(schema),
    }
    return RenderedB3TemporalAuditRequestV1(
        request_fingerprint=canonical_hash(body),
        messages=messages,
        response_schema=schema,
        packet=verified,
    )


def validate_b3_temporal_audit_response_v1(
    *, packet: Mapping[str, Any], response: Mapping[str, Any] | str
) -> dict[str, Any]:
    verified = verify_b3_temporal_review_packet_v1(packet)
    source_raw = _parsed_object(response, "B3 temporal audit response")
    raw, response_normalization_notes = normalize_code_owned_response_echoes_v1(
        source_raw,
        expected={"chapter_id": verified["chapter_id"]},
    )
    _fill_conditionally_null_pending_reason_codes_v1(raw)
    errors = sorted(
        Draft202012Validator(b3_temporal_audit_response_schema_v1()).iter_errors(raw),
        key=lambda row: list(row.path),
    )
    if errors:
        raise B3TemporalAuditorError(
            f"B3 temporal audit schema failure: {errors[0].message}"
        )
    expected = {
        row["pending_case_id"]: row for row in verified["pending_cases"]
    }
    decisions: dict[str, dict[str, Any]] = {}
    for value in raw["case_decisions"]:
        row = deepcopy(dict(value))
        case_id = row["pending_case_id"]
        if case_id not in expected or case_id in decisions:
            raise B3TemporalAuditorError("B3 temporal audit case coverage differs")
        disposition = row["disposition"]
        action = row["resolved_action"]
        pending_reason = row["pending_reason_code"]
        cited = _unique_strings(row["cited_source_block_ids"], "cited blocks")
        available_blocks = {item["block_id"] for item in verified["source_blocks"]}
        if not set(cited) <= available_blocks:
            raise B3TemporalAuditorError("B3 temporal audit cites foreign source blocks")
        if disposition == "confirm_state":
            if not isinstance(action, Mapping) or pending_reason is not None:
                raise B3TemporalAuditorError("confirmed B3 claim lacks one resolved action")
            row["resolved_action"] = _validated_confirmed_action(
                packet=verified, action=action
            )
        else:
            if action is not None:
                raise B3TemporalAuditorError("non-confirming B3 audit carries an action")
            if disposition == "keep_pending":
                if pending_reason not in PENDING_REASON_CODES:
                    raise B3TemporalAuditorError("pending B3 audit lacks a reason code")
            elif pending_reason is not None:
                raise B3TemporalAuditorError(
                    "resolved or referred B3 audit carries pending reason"
                )
        row["cited_source_block_ids"] = cited
        decisions[case_id] = row
    if set(decisions) != set(expected):
        raise B3TemporalAuditorError("B3 temporal audit does not exact-cover cases")
    body = {
        "schema_version": RESPONSE_SCHEMA_VERSION,
        "chapter_id": verified["chapter_id"],
        "packet_hash": verified["packet_hash"],
        "case_decisions": [decisions[key] for key in sorted(decisions)],
    }
    body = attach_response_normalization_notes_v1(
        body, response_normalization_notes
    )
    return {**body, "decision_hash": canonical_hash(body)}


def _fill_conditionally_null_pending_reason_codes_v1(
    response: dict[str, Any],
) -> None:
    """Fill a structurally forced null without changing the model verdict."""

    decisions = response.get("case_decisions")
    if not isinstance(decisions, list):
        return
    for value in decisions:
        if not isinstance(value, dict) or "pending_reason_code" in value:
            continue
        if value.get("disposition") in DISPOSITIONS - {"keep_pending"}:
            value["pending_reason_code"] = None


def build_b3_temporal_review_overlay_v1(
    *, packet: Mapping[str, Any], decision: Mapping[str, Any]
) -> dict[str, Any]:
    verified = verify_b3_temporal_review_packet_v1(packet)
    normalized = _normalized_decision(packet=verified, decision=decision)
    confirmed_states: list[dict[str, Any]] = []
    confirmed_observations: list[dict[str, Any]] = []
    resolved_ids: list[str] = []
    retained_ids: list[str] = []
    identity_referrals: list[str] = []
    for row in normalized["case_decisions"]:
        case_id = row["pending_case_id"]
        if row["disposition"] == "confirm_state":
            observation, state = _confirmed_state_rows(
                packet=verified,
                decision_hash=normalized["decision_hash"],
                pending_case_id=case_id,
                action=row["resolved_action"],
            )
            confirmed_observations.append(observation)
            confirmed_states.append(state)
            resolved_ids.append(case_id)
        elif row["disposition"] == "reject_claim":
            resolved_ids.append(case_id)
        else:
            retained_ids.append(case_id)
            if row["disposition"] == "refer_identity":
                identity_referrals.append(case_id)
    body = {
        "schema_version": OVERLAY_SCHEMA_VERSION,
        "chapter_id": verified["chapter_id"],
        "source_b3_artifact_hash": verified["source_b3_artifact_hash"],
        "source_b3_tree_hash": verified["source_b3_tree_hash"],
        "packet_hash": verified["packet_hash"],
        "decision": normalized,
        "confirmed_observation_rows": confirmed_observations,
        "confirmed_state_rows": confirmed_states,
        "resolved_pending_case_ids": sorted(resolved_ids),
        "retained_pending_case_ids": sorted(retained_ids),
        "identity_referral_case_ids": sorted(identity_referrals),
        "authority_policy": {
            "confirmed_states": "auditor_confirmed_temporal_context",
            "retained_cases": "review_context_only",
            "identity_mutation_by_code": False,
        },
        "identity_mutation_performed": False,
        "production_publish_performed": False,
    }
    return {**body, "overlay_hash": canonical_hash(body)}


def _normalized_decision(
    *, packet: Mapping[str, Any], decision: Mapping[str, Any]
) -> dict[str, Any]:
    if "decision_hash" not in decision:
        return validate_b3_temporal_audit_response_v1(
            packet=packet, response=decision
        )
    row = deepcopy(dict(decision))
    observed = row.pop("decision_hash", None)
    if observed != canonical_hash(row):
        raise B3TemporalAuditorError("B3 temporal audit decision hash mismatch")
    if row.get("schema_version") != RESPONSE_SCHEMA_VERSION:
        raise B3TemporalAuditorError("foreign normalized B3 temporal audit decision")
    if row.get("packet_hash") != packet["packet_hash"]:
        raise B3TemporalAuditorError("B3 temporal audit decision packet differs")
    try:
        replay_row, _notes = split_validated_response_normalization_notes_v1(row)
    except LiteraryResponseNormalizationError as exc:
        raise B3TemporalAuditorError(str(exc)) from exc
    raw = {
        "schema_version": replay_row["schema_version"],
        "chapter_id": replay_row["chapter_id"],
        "case_decisions": replay_row["case_decisions"],
    }
    rebuilt = validate_b3_temporal_audit_response_v1(packet=packet, response=raw)
    rebuilt_body = deepcopy(dict(rebuilt))
    rebuilt_body.pop("decision_hash", None)
    if canonical_json(rebuilt_body) != canonical_json(replay_row):
        raise B3TemporalAuditorError("normalized B3 temporal audit is not reproducible")
    return deepcopy(dict(decision))


def verify_b3_temporal_review_packet_v1(packet: Mapping[str, Any]) -> dict[str, Any]:
    row = deepcopy(dict(packet))
    observed = row.pop("packet_hash", None)
    if observed != canonical_hash(row):
        raise B3TemporalAuditorError("B3 temporal review packet hash mismatch")
    if packet.get("schema_version") != PACKET_SCHEMA_VERSION:
        raise B3TemporalAuditorError("foreign B3 temporal review packet")
    if packet.get("authority_policy") != {
        "pending_input_authoritative": False,
        "auditor_may_confirm_state": True,
        "identity_mutation_allowed": False,
        "source_rewrite_allowed": False,
    }:
        raise B3TemporalAuditorError("B3 temporal review authority policy differs")
    pending = packet.get("pending_cases")
    if not isinstance(pending, list) or len(pending) != 1:
        raise B3TemporalAuditorError("B3 temporal review packet must contain one case")
    case = pending[0]
    if (
        not isinstance(case, Mapping)
        or case.get("review_route") not in STATE_REVIEW_ROUTES
        or case.get("authority_status") != "pending_review"
    ):
        raise B3TemporalAuditorError("B3 temporal review case has foreign authority")
    component = packet.get("component")
    if not isinstance(component, Mapping) or component.get("component_id") != case.get(
        "component_id"
    ):
        raise B3TemporalAuditorError("B3 temporal review component differs")
    if packet.get("identity_mutation_performed") is True or packet.get(
        "production_publish_performed"
    ) is not False:
        raise B3TemporalAuditorError("B3 temporal review packet claims forbidden effects")
    return deepcopy(dict(packet))


def verify_b3_temporal_review_overlay_v1(
    overlay: Mapping[str, Any], *, packet: Mapping[str, Any]
) -> dict[str, Any]:
    verified_packet = verify_b3_temporal_review_packet_v1(packet)
    row = deepcopy(dict(overlay))
    observed = row.pop("overlay_hash", None)
    if observed != canonical_hash(row):
        raise B3TemporalAuditorError("B3 temporal review overlay hash mismatch")
    if overlay.get("schema_version") != OVERLAY_SCHEMA_VERSION:
        raise B3TemporalAuditorError("foreign B3 temporal review overlay")
    if overlay.get("packet_hash") != verified_packet["packet_hash"]:
        raise B3TemporalAuditorError("B3 temporal review overlay packet differs")
    rebuilt = build_b3_temporal_review_overlay_v1(
        packet=verified_packet, decision=overlay["decision"]
    )
    if canonical_json(rebuilt) != canonical_json(overlay):
        raise B3TemporalAuditorError("B3 temporal review overlay is not reproducible")
    return deepcopy(dict(overlay))


def synthetic_b3_temporal_review_packet_v1(
    *, review_route: str = "stable_claim_review"
) -> dict[str, Any]:
    if review_route not in STATE_REVIEW_ROUTES:
        raise B3TemporalAuditorError("synthetic B3 packet requires a state review route")
    component_id = "probe_component"
    event_id = "probe_event"
    pending_body = {
        "request_fingerprint": "a" * 64,
        "chapter_id": "probe_chapter",
        "batch_id": "probe_batch",
        "component_id": component_id,
        "review_route": review_route,
        "reason_codes": ["model_requested_review"],
        "reason": "The durable state needs independent review.",
        "proposed_action": None,
        "authority_status": "pending_review",
    }
    pending = {
        "pending_case_id": "b3pend1_" + canonical_hash(pending_body)[:20],
        **pending_body,
    }
    body = {
        "schema_version": PACKET_SCHEMA_VERSION,
        "chapter_id": "probe_chapter",
        "source_b3_root": "synthetic",
        "source_b3_tree_hash": "b" * 64,
        "source_b3_artifact_path": "chapter_temporal_artifact.json",
        "source_b3_artifact_hash": "c" * 64,
        "source_b3_request_fingerprint": "a" * 64,
        "pending_cases": [pending],
        "component": {
            "component_id": component_id,
            "component_hash": "d" * 64,
            "component_kind": "referent_state",
            "domain_hints": ["ownership_or_residence"],
            "referent_refs": ["probe_owner", "probe_place"],
            "speaker_turns": [],
            "salient_events": [
                {
                    "salient_event_id": event_id,
                    "source_block_ids": ["probe_block"],
                    "event_status": "occurred",
                    "event_authority_status": "non_authoritative_report_or_proposal",
                    "summary": "A character states that the place belongs to them.",
                }
            ],
            "prior_open_states": [],
            "prior_pending_cases": [],
            "b2_review_requests": [],
        },
        "referent_packets": [
            {
                "referent_ref": "probe_owner",
                "component_ids": [component_id],
                "candidate_card": {"canonical_surface": "Mr. Rowan"},
            },
            {
                "referent_ref": "probe_place",
                "component_ids": [component_id],
                "candidate_card": {"canonical_surface": "North House"},
            },
        ],
        "frame_packets": [
            {
                "frame_segment_id": "probe_frame",
                "component_ids": [component_id],
                "frame": {
                    "frame_segment_id": "probe_frame",
                    "narrative_mode": "direct_current",
                    "relevant_block_ids": ["probe_block"],
                },
            }
        ],
        "source_blocks": [
            {
                "block_id": "probe_block",
                "text": "Mr. Rowan said, 'North House is my own.'",
            }
        ],
        "authority_policy": {
            "pending_input_authoritative": False,
            "auditor_may_confirm_state": True,
            "identity_mutation_allowed": False,
            "source_rewrite_allowed": False,
        },
        "gold_or_oracle_loaded": False,
        "production_publish_performed": False,
    }
    return {**body, "packet_hash": canonical_hash(body)}


def synthetic_keep_pending_response_v1(packet: Mapping[str, Any]) -> dict[str, Any]:
    verified = verify_b3_temporal_review_packet_v1(packet)
    return {
        "schema_version": RESPONSE_SCHEMA_VERSION,
        "chapter_id": verified["chapter_id"],
        "case_decisions": [
            {
                "pending_case_id": verified["pending_cases"][0]["pending_case_id"],
                "disposition": "keep_pending",
                "resolved_action": None,
                "cited_source_block_ids": [verified["source_blocks"][0]["block_id"]],
                "reason": "The synthetic probe preserves uncertainty.",
                "pending_reason_code": "insufficient_evidence",
            }
        ],
    }


def _validated_confirmed_action(
    *, packet: Mapping[str, Any], action: Mapping[str, Any]
) -> dict[str, Any]:
    row = deepcopy(dict(action))
    component = packet["component"]
    refs = set(row.get("subject_referent_refs") or []).union(
        row.get("counterpart_referent_refs") or []
    )
    if not refs or not refs <= set(component.get("referent_refs") or []):
        raise B3TemporalAuditorError("confirmed B3 action uses foreign referents")
    event_ids = set(row.get("source_event_ids") or [])
    turn_ids = set(row.get("source_turn_ids") or [])
    if not event_ids and not turn_ids:
        raise B3TemporalAuditorError("confirmed B3 action cites no event or turn")
    events = {
        item["salient_event_id"]: item for item in component.get("salient_events") or []
    }
    turns = {
        item["speaker_turn_id"]: item for item in component.get("speaker_turns") or []
    }
    if not event_ids <= set(events) or not turn_ids <= set(turns):
        raise B3TemporalAuditorError("confirmed B3 action cites foreign evidence")
    evidence_blocks: set[str] = set()
    for event_id in event_ids:
        evidence_blocks.update(events[event_id].get("source_block_ids") or [])
    for turn_id in turn_ids:
        evidence_blocks.add(str(turns[turn_id]["block_id"]))
    blocks = set(row.get("source_block_ids") or [])
    if not blocks or not blocks <= evidence_blocks:
        raise B3TemporalAuditorError("confirmed B3 action blocks exceed evidence")
    expected_frames = {
        item["frame_segment_id"]
        for item in packet["frame_packets"]
        if blocks.intersection(set(item["frame"].get("relevant_block_ids") or []))
    }
    if set(row.get("frame_segment_ids") or []) != expected_frames:
        raise B3TemporalAuditorError("confirmed B3 action frame refs differ")
    if row.get("operation") not in {"open_state", "reveal_only"}:
        raise B3TemporalAuditorError("B3 stable auditor may only open or reveal a state")
    if row.get("event_status") != "occurred" or row.get(
        "temporal_position"
    ) != "current_progression":
        raise B3TemporalAuditorError("B3 stable auditor cannot confirm non-current state")
    row["subject_referent_refs"] = sorted(set(row["subject_referent_refs"]))
    row["counterpart_referent_refs"] = sorted(
        set(row["counterpart_referent_refs"])
    )
    row["source_event_ids"] = sorted(event_ids)
    row["source_turn_ids"] = sorted(turn_ids)
    row["source_block_ids"] = sorted(blocks)
    row["frame_segment_ids"] = sorted(expected_frames)
    return row


def _confirmed_state_rows(
    *,
    packet: Mapping[str, Any],
    decision_hash: str,
    pending_case_id: str,
    action: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    semantic_body = {
        "state_domain": action["state_domain"],
        "subject_referent_refs": action["subject_referent_refs"],
        "counterpart_referent_refs": action["counterpart_referent_refs"],
    }
    semantic_key = "b3skey1_" + canonical_hash(semantic_body)[:20]
    observation_body = {
        "pending_case_id": pending_case_id,
        "audit_decision_hash": decision_hash,
        "chapter_id": packet["chapter_id"],
        "component_id": packet["component"]["component_id"],
        "semantic_key": semantic_key,
        "operation": action["operation"],
        "state_domain": action["state_domain"],
        "subject_referent_refs": action["subject_referent_refs"],
        "counterpart_referent_refs": action["counterpart_referent_refs"],
        "state_value": action["state_value"],
        "event_status": action["event_status"],
        "temporal_position": action["temporal_position"],
        "source_event_ids": action["source_event_ids"],
        "source_turn_ids": action["source_turn_ids"],
        "source_block_ids": action["source_block_ids"],
        "frame_segment_ids": action["frame_segment_ids"],
        "reason": action["reason"],
        "authority_class": "auditor_confirmed",
    }
    observation = {
        "observation_id": "b3auditobs1_" + canonical_hash(observation_body)[:20],
        **observation_body,
    }
    state_body = {
        "semantic_key": semantic_key,
        "state_domain": action["state_domain"],
        "subject_referent_refs": action["subject_referent_refs"],
        "counterpart_referent_refs": action["counterpart_referent_refs"],
        "state_value": action["state_value"],
        "lifecycle_status": "open",
        "authority_status": "effective",
        "observed_at_block_id": action["source_block_ids"][0],
        "valid_from_block_id": (
            None if action["operation"] == "reveal_only" else action["source_block_ids"][0]
        ),
        "valid_to_block_id": None,
        "opened_by_observation_id": observation["observation_id"],
        "source_event_ids": action["source_event_ids"],
        "source_turn_ids": action["source_turn_ids"],
        "source_block_ids": action["source_block_ids"],
        "frame_segment_ids": action["frame_segment_ids"],
        "audit_decision_hash": decision_hash,
        "source_pending_case_id": pending_case_id,
    }
    state = {"state_id": "b3state1_" + canonical_hash(state_body)[:20], **state_body}
    return observation, state


def _request_for_pending(*, root: Path, pending: Mapping[str, Any]) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    for path in root.glob("batches/*/attempts/*/request.json"):
        value = _read_object(path, "B3 batch request")
        if value.get("request_fingerprint") == pending.get("request_fingerprint"):
            matches.append(value)
    if len(matches) != 1:
        raise B3TemporalAuditorError("B3 pending case request is missing or ambiguous")
    return matches[0]


def _validate_source_b3_request(
    request: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    schema_version = request.get("schema_version")
    if schema_version == "literary_b3_temporal_request_v7":
        return validate_b3_temporal_request_v7(request)
    if schema_version == "literary_b3_temporal_request_v6":
        return validate_b3_temporal_request_v6(request)
    if schema_version == "literary_b3_temporal_request_v5":
        return validate_b3_temporal_request_v5(request)
    raise B3TemporalAuditorError("unsupported B3 source request schema")


def _owned_packets(
    payload: Mapping[str, Any], table: str, component_id: str
) -> list[dict[str, Any]]:
    return [
        deepcopy(dict(row))
        for row in payload.get(table) or []
        if component_id in (row.get("component_ids") or [])
    ]


def _component_block_ids(component: Mapping[str, Any]) -> set[str]:
    result: set[str] = set()
    for event in component.get("salient_events") or []:
        result.update(event.get("source_block_ids") or [])
    for turn in component.get("speaker_turns") or []:
        result.add(str(turn.get("block_id")))
    if not result:
        raise B3TemporalAuditorError("B3 review component has no source blocks")
    return result


def _unique_strings(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not value or not all(
        isinstance(item, str) and item for item in value
    ):
        raise B3TemporalAuditorError(f"{label} must be a non-empty string list")
    if len(value) != len(set(value)):
        raise B3TemporalAuditorError(f"{label} contains duplicates")
    return sorted(value)


def _verified_hashed_object(path: Path, field: str) -> dict[str, Any]:
    row = _read_object(path, path.name)
    observed = row.get(field)
    unsigned = dict(row)
    unsigned.pop(field, None)
    if observed != canonical_hash(unsigned):
        raise B3TemporalAuditorError(f"{path.name} hash mismatch")
    return row


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise B3TemporalAuditorError(f"cannot read {label}") from exc
    if not isinstance(value, dict):
        raise B3TemporalAuditorError(f"{label} must be an object")
    return value


def _parsed_object(value: Mapping[str, Any] | str, label: str) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise B3TemporalAuditorError(f"{label} is invalid JSON") from exc
    if not isinstance(value, Mapping):
        raise B3TemporalAuditorError(f"{label} must be an object")
    return deepcopy(dict(value))


def _tree_hash(root: Path) -> str:
    rows = []
    for path in sorted(Path(root).rglob("*")):
        if path.is_file():
            rows.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "sha256": file_sha256(path),
                }
            )
    return canonical_hash(rows)


__all__ = [
    "DISPOSITIONS",
    "OVERLAY_SCHEMA_VERSION",
    "PACKET_SCHEMA_VERSION",
    "PROMPT_ID",
    "REVIEW_ROUTE_DESTINATIONS",
    "RESPONSE_SCHEMA_VERSION",
    "ROLE_ID",
    "ROUTING_REPORT_SCHEMA_VERSION",
    "STATE_REVIEW_ROUTES",
    "SYSTEM_PROMPT",
    "B3TemporalAuditorError",
    "RenderedB3TemporalAuditRequestV1",
    "b3_temporal_audit_response_schema_v1",
    "build_b3_review_routing_report_v1",
    "build_b3_temporal_review_overlay_v1",
    "classify_b3_review_selection_v1",
    "load_b3_temporal_review_packet_v1",
    "render_b3_temporal_audit_request_v1",
    "synthetic_b3_temporal_review_packet_v1",
    "synthetic_keep_pending_response_v1",
    "validate_b3_temporal_audit_response_v1",
    "verify_b3_temporal_review_overlay_v1",
    "verify_b3_temporal_review_packet_v1",
]
