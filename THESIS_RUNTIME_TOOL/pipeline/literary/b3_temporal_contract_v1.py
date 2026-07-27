"""Local validation and append-only state application for Literary B3 V1."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Any, Mapping, Sequence

from jsonschema import Draft202012Validator

from pipeline.literary.b3_temporal_context_v1 import (
    B3_REQUEST_SCHEMA_VERSION_V1,
)
from pipeline.literary.b3_temporal_prompts_v1 import (
    B3_TEMPORAL_PROMPT_ID_V1,
    B3_TEMPORAL_SYSTEM_PROMPT_V1,
    bind_b3_temporal_response_schema_v1,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json
from pipeline.literary.response_normalization_v1 import (
    attach_response_normalization_notes_v1,
    normalize_code_owned_response_echoes_v1,
)


B3_TEMPORAL_ARTIFACT_SCHEMA_VERSION_V1 = "literary_b3_temporal_artifact_v1"


class B3TemporalContractError(RuntimeError):
    pass


def normalize_b3_temporal_response_v1(
    *, request: Mapping[str, Any], response: Mapping[str, Any] | str
) -> dict[str, Any]:
    return _normalize_b3_temporal_response_common(
        request=request,
        response=response,
        request_validator=_validated_request,
    )


def _normalize_b3_temporal_response_common(
    *,
    request: Mapping[str, Any],
    response: Mapping[str, Any] | str,
    request_validator: Any,
) -> dict[str, Any]:
    request_body, payload = request_validator(request)
    source_raw = _parsed_response(response)
    chapter_id = request_body["chapter_id"]
    batch_id = request_body["batch_id"]
    raw, response_normalization_notes = normalize_code_owned_response_echoes_v1(
        source_raw,
        expected={"chapter_id": chapter_id, "batch_id": batch_id},
    )
    context = _validated_context(payload)
    supplied_ids = list(context["component_order"])
    schema = request_body["response_schema"]
    inherited_field = _response_inherited_field(schema)
    raw_results = raw.get("component_results")
    if not isinstance(raw_results, list):
        raise B3TemporalContractError("B3 component_results must be a list")
    component_schema_quarantines = _component_schema_quarantine_reasons(
        response=raw,
        schema=schema,
        supplied_ids=supplied_ids,
    )
    by_component: dict[str, dict[str, Any]] = {}
    quarantined_component_results: list[dict[str, Any]] = []
    quarantined_actions: list[dict[str, Any]] = []
    for result_ordinal, raw_result in enumerate(raw_results):
        result = deepcopy(_mapping(raw_result, "B3 component result"))
        component_id = _required_string(result.get("component_id"), "component_id")
        if component_id in by_component:
            raise B3TemporalContractError("B3 response repeats a component")
        component = context["components"].get(component_id)
        if component is None:
            raise B3TemporalContractError("B3 response references a foreign component")
        schema_failure = component_schema_quarantines.get(result_ordinal)
        if schema_failure is not None:
            quarantine = _quarantined_component_result_row(
                request_fingerprint=request_body["request_fingerprint"],
                component=component,
                result=result,
                error=B3TemporalContractError(schema_failure),
            )
            quarantined_component_results.append(quarantine)
            by_component[component_id] = {
                "component_id": component_id,
                "disposition": "quarantined",
                "state_actions": [],
                "pending_route": "none",
                "pending_reason": None,
                inherited_field: _empty_inherited_value(inherited_field),
                "component_application_status": "quarantined",
                "quarantined_component_result_id": quarantine["quarantine_id"],
            }
            continue
        try:
            _validate_disposition(
                result,
                component=component,
                context=context,
                inherited_field=inherited_field,
            )
        except B3TemporalContractError as exc:
            if str(exc) not in {
                "parked identity annotation has no proposed state",
                "parked identity pending case uses another review route",
            }:
                raise
            quarantine = _quarantined_component_result_row(
                request_fingerprint=request_body["request_fingerprint"],
                component=component,
                result=result,
                error=exc,
            )
            quarantined_component_results.append(quarantine)
            by_component[component_id] = {
                "component_id": component_id,
                "disposition": "quarantined",
                "state_actions": [],
                "pending_route": "none",
                "pending_reason": None,
                inherited_field: _empty_inherited_value(inherited_field),
                "component_application_status": "quarantined",
                "quarantined_component_result_id": quarantine["quarantine_id"],
            }
            continue
        valid_actions: list[dict[str, Any]] = []
        component_quarantines: list[dict[str, Any]] = []
        for action_ordinal, action in enumerate(result["state_actions"], 1):
            try:
                valid_actions.append(
                    _validated_action(action, component=component, context=context)
                )
            except B3TemporalContractError as exc:
                component_quarantines.append(
                    _quarantined_action_row(
                        request_fingerprint=request_body["request_fingerprint"],
                        component=component,
                        action=action,
                        action_ordinal=action_ordinal,
                        error=exc,
                    )
                )
        result["state_actions"] = valid_actions
        if component_quarantines:
            result["quarantined_action_ids"] = [
                row["quarantine_id"] for row in component_quarantines
            ]
            result["action_application_status"] = (
                "partially_quarantined"
                if valid_actions
                else "all_actions_quarantined"
            )
            quarantined_actions.extend(component_quarantines)
        by_component[component_id] = result
    if set(by_component) != set(supplied_ids):
        raise B3TemporalContractError("B3 response does not exact-cover components")

    prior_states = _deduplicated_prior_states(context["components"].values())
    open_effective = {
        row["state_id"]: deepcopy(row)
        for row in prior_states
        if row.get("lifecycle_status") == "open"
        and row.get("authority_status") == "effective"
    }
    new_states: list[dict[str, Any]] = []
    transitions: list[dict[str, Any]] = []
    reinforcements: list[dict[str, Any]] = []
    historical: list[dict[str, Any]] = []
    non_effective: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    normalized_results: list[dict[str, Any]] = []
    closed_state_ids: set[str] = set()

    for component_id in supplied_ids:
        result = by_component[component_id]
        component = context["components"][component_id]
        normalized_results.append(deepcopy(result))
        if result["disposition"] == "quarantined":
            continue
        if result["disposition"] == "no_durable_change":
            continue
        if result["disposition"] == "pending_review":
            pending.append(
                _pending_row(
                    request_fingerprint=request_body["request_fingerprint"],
                    chapter_id=chapter_id,
                    batch_id=batch_id,
                    component_id=component_id,
                    route=result["pending_route"],
                    reason_codes=["model_requested_review"],
                    reason=result["pending_reason"],
                    action=None,
                    **_inherited_pending_kwargs(
                        result=result,
                        field=inherited_field,
                    ),
                )
            )
            continue
        for action in result["state_actions"]:
            authority_class, reason_codes, route = _classify_action_authority(
                action=action,
                component=component,
                context=context,
            )
            if authority_class == "pending":
                pending.append(
                    _pending_row(
                        request_fingerprint=request_body["request_fingerprint"],
                        chapter_id=chapter_id,
                        batch_id=batch_id,
                        component_id=component_id,
                        route=route,
                        reason_codes=reason_codes,
                        reason=action["reason"],
                        action=action,
                    )
                )
                continue
            observation = _observation_row(
                request_fingerprint=request_body["request_fingerprint"],
                chapter_id=chapter_id,
                batch_id=batch_id,
                component_id=component_id,
                action=action,
                authority_class=authority_class,
            )
            if authority_class == "historical":
                historical.append(observation)
                continue
            if authority_class == "non_effective":
                non_effective.append(observation)
                continue

            semantic_key = observation["semantic_key"]
            matching = [
                row
                for state_id, row in open_effective.items()
                if state_id not in closed_state_ids and row.get("semantic_key") == semantic_key
            ]
            operation = action["operation"]
            if operation == "open_state":
                if matching:
                    pending.append(
                        _lineage_pending(
                            request_body,
                            chapter_id,
                            batch_id,
                            component_id,
                            action,
                            "open_state_already_exists",
                        )
                    )
                    continue
                state = _new_state_row(observation, action=action, reveal_only=False)
                new_states.append(state)
                open_effective[state["state_id"]] = state
                continue
            if operation == "reveal_only":
                if len(matching) > 1:
                    pending.append(
                        _lineage_pending(
                            request_body,
                            chapter_id,
                            batch_id,
                            component_id,
                            action,
                            "multiple_open_predecessors",
                        )
                    )
                    continue
                if matching:
                    if _normalized_value(matching[0]["state_value"]) != _normalized_value(
                        action["state_value"]
                    ):
                        pending.append(
                            _lineage_pending(
                                request_body,
                                chapter_id,
                                batch_id,
                                component_id,
                                action,
                                "revealed_value_conflicts_with_open_state",
                            )
                        )
                        continue
                    reinforcements.append(_reinforcement_row(observation, matching[0]))
                    continue
                state = _new_state_row(observation, action=action, reveal_only=True)
                new_states.append(state)
                open_effective[state["state_id"]] = state
                continue
            if len(matching) != 1:
                pending.append(
                    _lineage_pending(
                        request_body,
                        chapter_id,
                        batch_id,
                        component_id,
                        action,
                        (
                            "missing_open_predecessor"
                            if not matching
                            else "multiple_open_predecessors"
                        ),
                    )
                )
                continue
            predecessor = matching[0]
            if operation == "reinforce_state":
                if _normalized_value(predecessor["state_value"]) != _normalized_value(
                    action["state_value"]
                ):
                    pending.append(
                        _lineage_pending(
                            request_body,
                            chapter_id,
                            batch_id,
                            component_id,
                            action,
                            "reinforcement_value_differs",
                        )
                    )
                    continue
                reinforcements.append(_reinforcement_row(observation, predecessor))
                continue
            closed_state_ids.add(predecessor["state_id"])
            if operation == "close_state":
                transitions.append(
                    _transition_row(observation, predecessor=predecessor, successor=None)
                )
                continue
            if operation == "change_state":
                successor = _new_state_row(observation, action=action, reveal_only=False)
                new_states.append(successor)
                open_effective[successor["state_id"]] = successor
                transitions.append(
                    _transition_row(
                        observation, predecessor=predecessor, successor=successor
                    )
                )
                continue
            raise B3TemporalContractError("unsupported B3 operation")

    effective_projection = sorted(
        [
            deepcopy(row)
            for state_id, row in open_effective.items()
            if state_id not in closed_state_ids
            and row.get("authority_status") == "effective"
        ],
        key=lambda row: (row["semantic_key"], row["state_id"]),
    )
    body = {
        "schema_version": B3_TEMPORAL_ARTIFACT_SCHEMA_VERSION_V1,
        "request_fingerprint": request_body["request_fingerprint"],
        "chapter_id": chapter_id,
        "batch_id": batch_id,
        "source_b2_artifact_hash": payload.get("source_b2_artifact_hash"),
        "source_prefix_bundle_hash": payload.get("source_prefix_bundle_hash"),
        "component_results": normalized_results,
        "new_state_rows": sorted(new_states, key=lambda row: row["state_id"]),
        "transition_rows": sorted(transitions, key=lambda row: row["transition_id"]),
        "reinforcement_rows": sorted(
            reinforcements, key=lambda row: row["reinforcement_id"]
        ),
        "historical_observations": sorted(
            historical, key=lambda row: row["observation_id"]
        ),
        "non_effective_observations": sorted(
            non_effective, key=lambda row: row["observation_id"]
        ),
        "pending_cases": sorted(pending, key=lambda row: row["pending_case_id"]),
        "effective_state_projection": effective_projection,
        "closed_prior_state_ids": sorted(closed_state_ids),
        "raw_response_sha256": canonical_hash(source_raw),
        "identity_mutation_performed": False,
        "production_publish_performed": False,
    }
    if quarantined_actions:
        body["quarantined_actions"] = sorted(
            quarantined_actions,
            key=lambda row: (row["component_id"], row["action_ordinal"]),
        )
    if quarantined_component_results:
        body["quarantined_component_results"] = sorted(
            quarantined_component_results,
            key=lambda row: row["component_id"],
        )
    body = attach_response_normalization_notes_v1(
        body, response_normalization_notes
    )
    return {**body, "artifact_hash": canonical_hash(body)}


def _validated_request(
    request: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    body = deepcopy(_mapping(request, "B3 request"))
    if body.get("schema_version") != B3_REQUEST_SCHEMA_VERSION_V1:
        raise B3TemporalContractError("foreign B3 request schema")
    fingerprint = _required_string(body.get("request_fingerprint"), "request_fingerprint")
    unsigned = dict(body)
    unsigned.pop("request_fingerprint", None)
    if canonical_hash(unsigned) != fingerprint:
        raise B3TemporalContractError("B3 request fingerprint mismatch")
    if body.get("prompt_id") != B3_TEMPORAL_PROMPT_ID_V1:
        raise B3TemporalContractError("B3 request prompt id mismatch")
    expected_prompt_hash = hashlib.sha256(
        B3_TEMPORAL_SYSTEM_PROMPT_V1.encode("utf-8")
    ).hexdigest()
    if body.get("prompt_sha256") != expected_prompt_hash:
        raise B3TemporalContractError("B3 request prompt bytes differ from contract")
    schema = _mapping(body.get("response_schema"), "B3 response schema")
    if canonical_hash(schema) != body.get("response_schema_hash"):
        raise B3TemporalContractError("B3 response schema hash mismatch")
    payload = _user_json_payload(body)
    component_ids = [
        _required_string(row.get("component_id"), "component_id")
        for row in payload.get("components") or []
        if isinstance(row, Mapping)
    ]
    if component_ids != list(body.get("component_ids") or []):
        raise B3TemporalContractError("B3 request component index differs from payload")
    expected_schema = bind_b3_temporal_response_schema_v1(
        chapter_id=_required_string(body.get("chapter_id"), "chapter_id"),
        batch_id=_required_string(body.get("batch_id"), "batch_id"),
        component_ids=component_ids,
        referent_refs=[
            str(row.get("referent_ref")) for row in payload.get("referent_packets") or []
        ],
        event_ids=[
            str(event.get("salient_event_id"))
            for component in payload.get("components") or []
            for event in component.get("salient_events") or []
        ],
        turn_ids=[
            str(turn.get("speaker_turn_id"))
            for component in payload.get("components") or []
            for turn in component.get("speaker_turns") or []
        ],
        block_ids=[str(row.get("block_id")) for row in payload.get("source_packets") or []],
        frame_segment_ids=[
            str(row.get("frame_segment_id")) for row in payload.get("frame_packets") or []
        ],
    )
    if canonical_json(schema) != canonical_json(expected_schema):
        raise B3TemporalContractError("B3 bound response schema differs from payload")
    return body, payload


def validate_b3_temporal_request_v1(
    request: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate a V1 request for versioned, provenance-preserving adapters."""

    body, payload = _validated_request(request)
    return deepcopy(body), deepcopy(payload)


def _validated_context(payload: Mapping[str, Any]) -> dict[str, Any]:
    components: dict[str, dict[str, Any]] = {}
    component_order: list[str] = []
    event_owner: dict[str, str] = {}
    turn_owner: dict[str, str] = {}
    for raw in payload.get("components") or []:
        component = deepcopy(_mapping(raw, "B3 component"))
        component_id = _required_string(component.get("component_id"), "component_id")
        if component_id in components:
            raise B3TemporalContractError("B3 request repeats a component")
        component_order.append(component_id)
        component["referent_refs"] = set(component.get("referent_refs") or [])
        declared_frame_ids = component.get("frame_segment_ids")
        if declared_frame_ids is not None:
            if (
                not isinstance(declared_frame_ids, list)
                or not all(
                    isinstance(frame_id, str) and frame_id
                    for frame_id in declared_frame_ids
                )
                or declared_frame_ids != sorted(set(declared_frame_ids))
            ):
                raise B3TemporalContractError(
                    "B3 component frame index is malformed"
                )
            component["declared_frame_segment_ids"] = set(declared_frame_ids)
        component["event_ids"] = set()
        component["turn_ids"] = set()
        component["source_block_ids"] = set()
        for event in component.get("salient_events") or []:
            event_id = _required_string(event.get("salient_event_id"), "salient_event_id")
            if event_id in event_owner:
                raise B3TemporalContractError("B3 event belongs to multiple components")
            event_owner[event_id] = component_id
            component["event_ids"].add(event_id)
            component["source_block_ids"].update(event.get("source_block_ids") or [])
        for turn in component.get("speaker_turns") or []:
            turn_id = _required_string(turn.get("speaker_turn_id"), "speaker_turn_id")
            if turn_id in turn_owner:
                raise B3TemporalContractError("B3 turn belongs to multiple components")
            turn_owner[turn_id] = component_id
            component["turn_ids"].add(turn_id)
            component["source_block_ids"].add(turn.get("block_id"))
        components[component_id] = component

    referent_packets: dict[str, dict[str, Any]] = {}
    for raw in payload.get("referent_packets") or []:
        packet = deepcopy(_mapping(raw, "B3 referent packet"))
        ref = _required_string(packet.get("referent_ref"), "referent_ref")
        if ref in referent_packets:
            raise B3TemporalContractError("B3 request repeats a referent packet")
        card = _mapping(packet.get("candidate_card"), "B3 candidate card")
        if card.get("referent_ref") != ref:
            raise B3TemporalContractError("B3 referent packet/card mismatch")
        _validated_parked_identity_markers(card)
        owners = set(packet.get("component_ids") or [])
        if not owners or not owners <= set(components):
            raise B3TemporalContractError("B3 referent packet has foreign owner")
        for owner in owners:
            if ref not in components[owner]["referent_refs"]:
                raise B3TemporalContractError("B3 referent packet owner mismatch")
        packet["component_ids"] = owners
        referent_packets[ref] = packet
    for component_id, component in components.items():
        packet_refs = {
            ref
            for ref, packet in referent_packets.items()
            if component_id in packet["component_ids"]
        }
        if packet_refs != component["referent_refs"]:
            raise B3TemporalContractError("B3 component referents are not exact-covered")

    source_packets = _owned_packets(payload, "source_packets", "block_id", components)
    frame_packets = _owned_packets(
        payload, "frame_packets", "frame_segment_id", components
    )
    for component_id, component in components.items():
        owned_frame_ids = {
            frame_id
            for frame_id, packet in frame_packets.items()
            if component_id in packet["component_ids"]
        }
        declared_frame_ids = component.pop("declared_frame_segment_ids", None)
        if (
            declared_frame_ids is not None
            and declared_frame_ids != owned_frame_ids
        ):
            raise B3TemporalContractError(
                "B3 component frame index differs from chapter packets"
            )
        component["frame_segment_ids"] = owned_frame_ids
        packet_blocks = {
            block_id
            for block_id, packet in source_packets.items()
            if component_id in packet["component_ids"]
        }
        if packet_blocks != component["source_block_ids"]:
            raise B3TemporalContractError("B3 component source blocks are not exact-covered")
    return {
        "components": components,
        "component_order": component_order,
        "referent_packets": referent_packets,
        "source_packets": source_packets,
        "frame_packets": frame_packets,
        "event_owner": event_owner,
        "turn_owner": turn_owner,
    }


def _owned_packets(
    payload: Mapping[str, Any],
    table: str,
    id_field: str,
    components: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for raw in payload.get(table) or []:
        packet = deepcopy(_mapping(raw, table))
        value = _required_string(packet.get(id_field), id_field)
        if value in result:
            raise B3TemporalContractError(f"B3 request repeats {id_field}")
        owners = set(packet.get("component_ids") or [])
        if not owners or not owners <= set(components):
            raise B3TemporalContractError(f"B3 {table} packet has foreign owner")
        packet["component_ids"] = owners
        result[value] = packet
    return result


def _validate_disposition(
    result: Mapping[str, Any],
    *,
    component: Mapping[str, Any],
    context: Mapping[str, Any],
    inherited_field: str = "inherited_parked_identity",
) -> None:
    disposition = result.get("disposition")
    actions = result.get("state_actions")
    route = result.get("pending_route")
    reason = result.get("pending_reason")
    inherited_values = _inherited_values(result, inherited_field)
    if disposition == "state_actions_proposed":
        if not isinstance(actions, list) or not actions or route != "none" or reason is not None:
            raise B3TemporalContractError("state_actions_proposed disposition is inconsistent")
    elif disposition == "no_durable_change":
        if actions != [] or route != "none" or reason is not None:
            raise B3TemporalContractError("no_durable_change disposition is inconsistent")
    elif disposition == "pending_review":
        if actions != [] or route == "none" or not isinstance(reason, str) or not reason.strip():
            raise B3TemporalContractError("pending_review disposition is inconsistent")
    else:
        raise B3TemporalContractError("unsupported B3 disposition")
    if inherited_values:
        expected = {
            (
                marker["hearing_component_id"],
                marker["resolution_condition"],
            )
            for marker in _component_parked_identities(
                component=component,
                context=context,
            )
        }
        observed = {
            (
                marker["hearing_component_id"],
                marker["resolution_condition"],
            )
            for marker in inherited_values
        }
        if not observed <= expected:
            raise B3TemporalContractError(
                "inherited identity block differs from supplied parked identity"
            )
        if disposition == "pending_review":
            if route != "inherited_identity_block":
                raise B3TemporalContractError(
                    "parked identity pending case uses another review route"
                )
        elif disposition != "state_actions_proposed" or route != "none":
            raise B3TemporalContractError(
                "parked identity annotation has no proposed state"
            )
    elif route == "inherited_identity_block":
        raise B3TemporalContractError(
            "inherited identity block lacks its parked identity"
        )


def _validated_action(
    value: Mapping[str, Any], *, component: Mapping[str, Any], context: Mapping[str, Any]
) -> dict[str, Any]:
    action = deepcopy(_mapping(value, "B3 state action"))
    refs = set(action.get("subject_referent_refs") or []).union(
        action.get("counterpart_referent_refs") or []
    )
    if not refs or not refs <= component["referent_refs"]:
        raise B3TemporalContractError("B3 action uses a foreign or empty referent set")
    event_ids = set(action.get("source_event_ids") or [])
    turn_ids = set(action.get("source_turn_ids") or [])
    if not event_ids and not turn_ids:
        raise B3TemporalContractError("B3 action must cite an event or turn")
    if not event_ids <= component["event_ids"] or not turn_ids <= component["turn_ids"]:
        raise B3TemporalContractError("B3 action cites foreign evidence")
    evidence_blocks: set[str] = set()
    for event in component.get("salient_events") or []:
        if event.get("salient_event_id") in event_ids:
            evidence_blocks.update(event.get("source_block_ids") or [])
    for turn in component.get("speaker_turns") or []:
        if turn.get("speaker_turn_id") in turn_ids:
            evidence_blocks.add(str(turn.get("block_id")))
    source_blocks = set(action.get("source_block_ids") or [])
    if not source_blocks or not source_blocks <= evidence_blocks:
        raise B3TemporalContractError("B3 action source blocks exceed cited evidence")
    expected_frames = {
        frame_id
        for frame_id, packet in context["frame_packets"].items()
        if component["component_id"] in set(packet.get("component_ids") or [])
        and source_blocks.intersection(
            set(_mapping(packet.get("frame"), "frame packet").get("relevant_block_ids") or [])
        )
    }
    if set(action.get("frame_segment_ids") or []) != expected_frames:
        raise B3TemporalContractError("B3 action frame refs differ from source blocks")
    action["subject_referent_refs"] = sorted(set(action["subject_referent_refs"]))
    action["counterpart_referent_refs"] = sorted(
        set(action["counterpart_referent_refs"])
    )
    action["source_event_ids"] = sorted(event_ids)
    action["source_turn_ids"] = sorted(turn_ids)
    action["source_block_ids"] = sorted(source_blocks)
    action["frame_segment_ids"] = sorted(expected_frames)
    return action


def _quarantined_action_row(
    *,
    request_fingerprint: str,
    component: Mapping[str, Any],
    action: Mapping[str, Any],
    action_ordinal: int,
    error: B3TemporalContractError,
) -> dict[str, Any]:
    raw_action = deepcopy(_mapping(action, "B3 state action"))
    supplied_refs = set(component["referent_refs"])
    observed_refs = {
        value
        for field in ("subject_referent_refs", "counterpart_referent_refs")
        for value in (raw_action.get(field) or [])
        if isinstance(value, str) and value
    }
    body = {
        "row_type": "b3_state_action",
        "state": "quarantined",
        "request_fingerprint": request_fingerprint,
        "component_id": component["component_id"],
        "action_ordinal": action_ordinal,
        "reason": str(error),
        "observed_referent_refs": sorted(observed_refs),
        "supplied_referent_refs": sorted(supplied_refs),
        "offending_referent_refs": sorted(observed_refs - supplied_refs),
        "raw_action": raw_action,
        "semantic_authority_granted": False,
    }
    return {
        **body,
        "quarantine_id": "b3qact1_" + canonical_hash(body)[:20],
    }


def _quarantined_component_result_row(
    *,
    request_fingerprint: str,
    component: Mapping[str, Any],
    result: Mapping[str, Any],
    error: B3TemporalContractError,
) -> dict[str, Any]:
    body = {
        "row_type": "b3_component_result",
        "state": "quarantined",
        "request_fingerprint": request_fingerprint,
        "component_id": component["component_id"],
        "reason": str(error),
        "raw_component_result": deepcopy(dict(result)),
        "semantic_authority_granted": False,
    }
    return {
        **body,
        "quarantine_id": "b3qcomp1_" + canonical_hash(body)[:20],
    }


def _component_schema_quarantine_reasons(
    *,
    response: Mapping[str, Any],
    schema: Mapping[str, Any],
    supplied_ids: Sequence[str],
) -> dict[int, str]:
    errors = sorted(
        Draft202012Validator(schema).iter_errors(response),
        key=lambda row: (list(row.absolute_path), row.message),
    )
    if not errors:
        return {}

    by_ordinal: dict[int, list[str]] = {}
    for error in errors:
        path = list(error.absolute_path)
        if (
            len(path) < 2
            or path[0] != "component_results"
            or not isinstance(path[1], int)
        ):
            raise B3TemporalContractError(
                f"B3 response schema failure: {error.message}"
            )
        by_ordinal.setdefault(path[1], []).append(error.message)

    raw_results = response.get("component_results")
    if not isinstance(raw_results, list):
        raise B3TemporalContractError("B3 component_results must be a list")
    if len(by_ordinal) == len(raw_results):
        raise B3TemporalContractError(
            "B3 response schema failure: all component results are invalid"
        )

    component_ids: list[str] = []
    supplied = set(supplied_ids)
    for raw_result in raw_results:
        if not isinstance(raw_result, Mapping):
            raise B3TemporalContractError(
                "B3 response schema failure: component identity is not recoverable"
            )
        component_id = raw_result.get("component_id")
        if not isinstance(component_id, str) or not component_id:
            raise B3TemporalContractError(
                "B3 response schema failure: component identity is not recoverable"
            )
        component_ids.append(component_id)
    if len(component_ids) != len(set(component_ids)):
        raise B3TemporalContractError("B3 response repeats a component")
    if set(component_ids) != supplied:
        raise B3TemporalContractError("B3 response does not exact-cover components")

    sanitized = deepcopy(dict(response))
    sanitized_results = sanitized["component_results"]
    for ordinal in by_ordinal:
        sanitized_results[ordinal] = _component_schema_quarantine_placeholder(
            component_id=component_ids[ordinal],
            schema=schema,
        )
    remaining = sorted(
        Draft202012Validator(schema).iter_errors(sanitized),
        key=lambda row: (list(row.absolute_path), row.message),
    )
    if remaining:
        raise B3TemporalContractError(
            f"B3 response schema failure: {remaining[0].message}"
        )
    return {
        ordinal: "B3 component result schema failure: "
        + "; ".join(dict.fromkeys(messages))
        for ordinal, messages in sorted(by_ordinal.items())
    }


def _component_schema_quarantine_placeholder(
    *,
    component_id: str,
    schema: Mapping[str, Any],
) -> dict[str, Any]:
    item_schema = (
        schema.get("properties", {})
        .get("component_results", {})
        .get("items", {})
    )
    required = set(item_schema.get("required") or [])
    placeholder: dict[str, Any] = {
        "component_id": component_id,
        "disposition": "no_durable_change",
        "state_actions": [],
        "pending_route": "none",
        "pending_reason": None,
    }
    if "inherited_parked_identities" in required:
        placeholder["inherited_parked_identities"] = []
    elif "inherited_parked_identity" in required:
        placeholder["inherited_parked_identity"] = None
    unsupported = required - set(placeholder)
    if unsupported:
        raise B3TemporalContractError(
            "B3 response schema has unsupported component requirements"
        )
    return placeholder


def _classify_action_authority(
    *, action: Mapping[str, Any], component: Mapping[str, Any], context: Mapping[str, Any]
) -> tuple[str, list[str], str]:
    reasons: list[str] = []
    route = "temporal_review"
    if action["state_domain"] in {"role", "name_usage", "life_status"}:
        reasons.append("stable_claim_domain_requires_review")
        route = "stable_claim_review"
    for ref in set(action["subject_referent_refs"]).union(
        action["counterpart_referent_refs"]
    ):
        card = context["referent_packets"][ref]["candidate_card"]
        if (
            card.get("identity_scope") != "chapter_confirmed_prefix"
        and not _validated_parked_identity_markers(card)
        ):
            reasons.append("referent_identity_not_confirmed")
            route = "identity_review"
    frame_modes = {
        context["frame_packets"][frame_id]["frame"].get("narrative_mode")
        for frame_id in action["frame_segment_ids"]
    }
    if "dream_or_vision" in frame_modes and action["temporal_position"] != "nonactual":
        reasons.append("dream_or_vision_cannot_mutate_current_state")
    authoritative = False
    non_effective_evidence = False
    event_ids = set(action["source_event_ids"])
    turn_ids = set(action["source_turn_ids"])
    for turn in component.get("speaker_turns") or []:
        if turn.get("speaker_turn_id") in turn_ids and turn.get(
            "evidence_authority"
        ) in {"provisional_grounded", "auditor_confirmed_chapter_local"}:
            authoritative = True
    for event in component.get("salient_events") or []:
        if event.get("salient_event_id") not in event_ids:
            continue
        if event.get("event_authority_status") == "provisional_occurred_observation":
            authoritative = True
        elif event.get("event_authority_status") == "non_authoritative_report_or_proposal":
            non_effective_evidence = True
    if reasons:
        return "pending", sorted(set(reasons)), route
    if action["event_status"] != "occurred" or action["temporal_position"] in {
        "prospective",
        "nonactual",
        "unknown",
    }:
        if authoritative or non_effective_evidence:
            return "non_effective", [], "none"
        return "pending", ["no_grounded_b2_evidence"], "temporal_review"
    if not authoritative:
        return "pending", ["no_authoritative_b2_evidence"], "temporal_review"
    if action["temporal_position"] == "prior_story_time":
        return "historical", [], "none"
    return "effective", [], "none"


def _observation_row(
    *,
    request_fingerprint: str,
    chapter_id: str,
    batch_id: str,
    component_id: str,
    action: Mapping[str, Any],
    authority_class: str,
) -> dict[str, Any]:
    semantic_key = _semantic_key(action)
    body = {
        "request_fingerprint": request_fingerprint,
        "chapter_id": chapter_id,
        "batch_id": batch_id,
        "component_id": component_id,
        "semantic_key": semantic_key,
        "operation": action["operation"],
        "state_domain": action["state_domain"],
        "subject_referent_refs": list(action["subject_referent_refs"]),
        "counterpart_referent_refs": list(action["counterpart_referent_refs"]),
        "state_value": action["state_value"],
        "event_status": action["event_status"],
        "temporal_position": action["temporal_position"],
        "source_event_ids": list(action["source_event_ids"]),
        "source_turn_ids": list(action["source_turn_ids"]),
        "source_block_ids": list(action["source_block_ids"]),
        "frame_segment_ids": list(action["frame_segment_ids"]),
        "reason": action["reason"],
        "authority_class": authority_class,
    }
    return {"observation_id": "b3obs1_" + canonical_hash(body)[:20], **body}


def _new_state_row(
    observation: Mapping[str, Any], *, action: Mapping[str, Any], reveal_only: bool
) -> dict[str, Any]:
    body = {
        "semantic_key": observation["semantic_key"],
        "state_domain": action["state_domain"],
        "subject_referent_refs": list(action["subject_referent_refs"]),
        "counterpart_referent_refs": list(action["counterpart_referent_refs"]),
        "state_value": action["state_value"],
        "lifecycle_status": "open",
        "authority_status": "effective",
        "observed_at_block_id": action["source_block_ids"][0],
        "valid_from_block_id": None if reveal_only else action["source_block_ids"][0],
        "valid_to_block_id": None,
        "opened_by_observation_id": observation["observation_id"],
        "source_event_ids": list(action["source_event_ids"]),
        "source_turn_ids": list(action["source_turn_ids"]),
        "source_block_ids": list(action["source_block_ids"]),
        "frame_segment_ids": list(action["frame_segment_ids"]),
    }
    return {"state_id": "b3state1_" + canonical_hash(body)[:20], **body}


def _transition_row(
    observation: Mapping[str, Any],
    *,
    predecessor: Mapping[str, Any],
    successor: Mapping[str, Any] | None,
) -> dict[str, Any]:
    body = {
        "semantic_key": observation["semantic_key"],
        "operation": observation["operation"],
        "predecessor_state_id": predecessor["state_id"],
        "successor_state_id": successor["state_id"] if successor else None,
        "effective_at_block_id": observation["source_block_ids"][0],
        "observation_id": observation["observation_id"],
        "source_block_ids": list(observation["source_block_ids"]),
    }
    return {"transition_id": "b3trans1_" + canonical_hash(body)[:20], **body}


def _reinforcement_row(
    observation: Mapping[str, Any], state: Mapping[str, Any]
) -> dict[str, Any]:
    body = {
        "state_id": state["state_id"],
        "semantic_key": state["semantic_key"],
        "observation_id": observation["observation_id"],
        "source_block_ids": list(observation["source_block_ids"]),
    }
    return {"reinforcement_id": "b3rein1_" + canonical_hash(body)[:20], **body}


def _pending_row(
    *,
    request_fingerprint: str,
    chapter_id: str,
    batch_id: str,
    component_id: str,
    route: str,
    reason_codes: Sequence[str],
    reason: Any,
    action: Mapping[str, Any] | None,
    inherited_parked_identity: Mapping[str, Any] | None = None,
    inherited_parked_identities: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    body = {
        "request_fingerprint": request_fingerprint,
        "chapter_id": chapter_id,
        "batch_id": batch_id,
        "component_id": component_id,
        "review_route": route,
        "reason_codes": sorted(set(reason_codes)),
        "reason": reason,
        "proposed_action": deepcopy(dict(action)) if action is not None else None,
        "authority_status": "pending_review",
    }
    if inherited_parked_identity is not None:
        body["inherited_parked_identity"] = deepcopy(
            dict(inherited_parked_identity)
        )
    if inherited_parked_identities is not None:
        body["inherited_parked_identities"] = deepcopy(
            [dict(value) for value in inherited_parked_identities]
        )
    return {"pending_case_id": "b3pend1_" + canonical_hash(body)[:20], **body}


def _validated_parked_identity_marker(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    marker = deepcopy(_mapping(value, "B3 parked identity marker"))
    if set(marker) != {
        "hearing_component_id",
        "resolution_condition",
        "co_parked_referent_refs",
        "parked_set_partially_supplied",
    }:
        raise B3TemporalContractError("B3 parked identity marker shape differs")
    _required_string(marker.get("hearing_component_id"), "hearing_component_id")
    _required_string(marker.get("resolution_condition"), "resolution_condition")
    refs = marker.get("co_parked_referent_refs")
    if (
        not isinstance(refs, list)
        or not all(isinstance(item, str) and item for item in refs)
        or refs != sorted(set(refs))
    ):
        raise B3TemporalContractError("B3 parked co-referent refs are malformed")
    if not isinstance(marker.get("parked_set_partially_supplied"), bool):
        raise B3TemporalContractError("B3 parked identity partial flag is malformed")
    return marker


def _validated_parked_identity_markers(
    card: Mapping[str, Any],
) -> list[dict[str, Any]]:
    singular = card.get("parked_identity")
    plural = card.get("parked_identities")
    if singular is not None and plural is not None:
        raise B3TemporalContractError(
            "B3 candidate card carries both parked identity shapes"
        )
    if plural is not None:
        if not isinstance(plural, list):
            raise B3TemporalContractError("B3 parked identities must be a list")
        result = [
            _validated_parked_identity_marker(value)
            for value in plural
        ]
        if any(value is None for value in result):
            raise B3TemporalContractError("B3 parked identities contain null")
        markers = [value for value in result if value is not None]
        keys = [
            (value["hearing_component_id"], value["resolution_condition"])
            for value in markers
        ]
        if keys != sorted(set(keys)):
            raise B3TemporalContractError(
                "B3 parked identities are not canonical or repeat a hearing"
            )
        return markers
    marker = _validated_parked_identity_marker(singular)
    return [] if marker is None else [marker]


def _response_inherited_field(schema: Mapping[str, Any]) -> str:
    properties = (
        schema.get("properties", {})
        .get("component_results", {})
        .get("items", {})
        .get("properties", {})
    )
    if "inherited_parked_identities" in properties:
        return "inherited_parked_identities"
    return "inherited_parked_identity"


def _empty_inherited_value(field: str) -> Any:
    return [] if field == "inherited_parked_identities" else None


def _inherited_values(
    result: Mapping[str, Any],
    field: str,
) -> list[dict[str, Any]]:
    value = result.get(field)
    if field == "inherited_parked_identities":
        if not isinstance(value, list):
            raise B3TemporalContractError(
                "inherited parked identities must be a list"
            )
        markers = [
            _mapping(item, "inherited parked identity")
            for item in value
        ]
        validated = [
            _validated_inherited_marker(item)
            for item in markers
        ]
        keys = [
            (item["hearing_component_id"], item["resolution_condition"])
            for item in validated
        ]
        if keys != sorted(set(keys)):
            raise B3TemporalContractError(
                "inherited parked identities are not canonical or repeat a hearing"
            )
        return validated
    if value is None:
        return []
    return [_validated_inherited_marker(_mapping(value, "inherited parked identity"))]


def _validated_inherited_marker(value: Mapping[str, Any]) -> dict[str, Any]:
    marker = deepcopy(dict(value))
    if set(marker) != {"hearing_component_id", "resolution_condition"}:
        raise B3TemporalContractError("inherited parked identity shape differs")
    _required_string(marker["hearing_component_id"], "hearing_component_id")
    _required_string(marker["resolution_condition"], "resolution_condition")
    return marker


def _inherited_pending_kwargs(
    *,
    result: Mapping[str, Any],
    field: str,
) -> dict[str, Any]:
    if field == "inherited_parked_identities":
        return {"inherited_parked_identities": deepcopy(list(result[field]))}
    return {"inherited_parked_identity": result.get(field)}


def _component_parked_identities(
    *, component: Mapping[str, Any], context: Mapping[str, Any]
) -> list[dict[str, Any]]:
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for ref in component["referent_refs"]:
        card = context["referent_packets"][ref]["candidate_card"]
        for marker in _validated_parked_identity_markers(card):
            key = (marker["hearing_component_id"], marker["resolution_condition"])
            result[key] = marker
    return [result[key] for key in sorted(result)]


def _lineage_pending(
    request: Mapping[str, Any],
    chapter_id: str,
    batch_id: str,
    component_id: str,
    action: Mapping[str, Any],
    reason_code: str,
) -> dict[str, Any]:
    return _pending_row(
        request_fingerprint=request["request_fingerprint"],
        chapter_id=chapter_id,
        batch_id=batch_id,
        component_id=component_id,
        route="temporal_review",
        reason_codes=[reason_code],
        reason=action["reason"],
        action=action,
    )


def _deduplicated_prior_states(components: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for component in components:
        for raw in component.get("prior_open_states") or []:
            row = deepcopy(_mapping(raw, "prior state"))
            state_id = _required_string(row.get("state_id"), "state_id")
            prior = result.setdefault(state_id, row)
            if canonical_json(prior) != canonical_json(row):
                raise B3TemporalContractError("prior state drifted across components")
    return [result[key] for key in sorted(result)]


def _semantic_key(action: Mapping[str, Any]) -> str:
    body = {
        "state_domain": action["state_domain"],
        "subject_referent_refs": sorted(set(action["subject_referent_refs"])),
        "counterpart_referent_refs": sorted(
            set(action["counterpart_referent_refs"])
        ),
    }
    return "b3skey1_" + canonical_hash(body)[:20]


def _normalized_value(value: Any) -> str:
    return " ".join(str(value).casefold().split())


def _parsed_response(value: Mapping[str, Any] | str) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise B3TemporalContractError("B3 response is invalid JSON") from exc
    return _mapping(value, "B3 response")


def _user_json_payload(request: Mapping[str, Any]) -> dict[str, Any]:
    messages = request.get("messages")
    if not isinstance(messages, list):
        raise B3TemporalContractError("B3 request has no messages")
    users = [row for row in messages if isinstance(row, Mapping) and row.get("role") == "user"]
    if len(users) != 1 or not isinstance(users[0].get("content"), str):
        raise B3TemporalContractError("B3 request must contain one JSON user message")
    try:
        return _mapping(json.loads(users[0]["content"]), "B3 user payload")
    except json.JSONDecodeError as exc:
        raise B3TemporalContractError("B3 user payload is invalid JSON") from exc


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise B3TemporalContractError(f"{label} must be an object")
    return dict(value)


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise B3TemporalContractError(f"{label} must be a non-empty string")
    return value


__all__ = [
    "B3_TEMPORAL_ARTIFACT_SCHEMA_VERSION_V1",
    "B3TemporalContractError",
    "normalize_b3_temporal_response_v1",
    "validate_b3_temporal_request_v1",
]
