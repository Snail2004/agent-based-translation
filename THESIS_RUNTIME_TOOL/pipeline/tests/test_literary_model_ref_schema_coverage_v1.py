"""Coverage gate for id-bearing fields in live Literary response schemas."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
import json
from typing import Any

from pipeline.literary.b0_chapter_summary_v1 import (
    b0_summary_response_schema_v1,
)
from pipeline.literary.b0_entity_conflict_auditor import (
    entity_conflict_response_schema,
)
from pipeline.literary.b0_entity_prior_challenge_experiment import (
    prior_challenge_response_schema,
)
from pipeline.literary.b1_cross_chapter_audit_bridge_v1 import (
    identity_hearing_response_schema_v1,
    stable_claim_hearing_response_schema_v1,
)
from pipeline.literary.b1_enrich_local_auditor_v1 import (
    b1_enrich_local_audit_response_schema_v1,
)
from pipeline.literary.b1_enrich_v1 import b1_enrich_response_schema_v1
from pipeline.literary.b1_scan_v1 import b1_scan_response_schema_v1
from pipeline.literary.b2_prompts_v3 import (
    b2_frame_response_schema_v2,
    b2_interaction_response_schema_v3,
)
from pipeline.literary.b2_recovery_prompts_v1 import (
    registry_recovery_response_schema_v1,
)
from pipeline.literary.b2_slim_speaker_recovery_v1 import (
    render_b2_slim_speaker_recovery_request_v1,
)
from pipeline.literary.b3_temporal_auditor_v1 import (
    b3_temporal_audit_response_schema_v1,
)
from pipeline.literary.b3_temporal_prompts_v4 import (
    b3_temporal_response_schema_v4,
)
from pipeline.literary.book_entity_claim_auditor_v1 import (
    prior_claim_response_schema_v1,
)
from pipeline.literary.book_entity_registry_v1 import (
    cross_chapter_response_schema_v1,
)
from pipeline.literary.incremental_identity_auditor_v1 import (
    incremental_identity_response_schema_v1,
)
from pipeline.literary.modelapi_b2_speaker_recovery_capability_probe_v1 import (
    synthetic_probe_index_v1,
)
from pipeline.literary import model_ref_v1 as model_ref_module
from pipeline.literary.model_ref_v1 import (
    MODEL_REF_FIELDS_V1,
    TRANSPORT_EXEMPT_FIELDS_V1,
)
from pipeline.literary.openai_b2_json_object_capability_probe_v1 import (
    synthetic_b2_probe_request_v1,
)
from pipeline.literary.openai_b3_json_object_capability_probe_v1 import (
    synthetic_b3_probe_request_v2,
)


# This list is deliberately explicit. Adding a live model role requires a
# conscious coverage decision here rather than discovery by filename pattern.
LIVE_RESPONSE_SCHEMA_BUILDERS: Mapping[str, Callable[[], dict[str, Any]]] = {
    "b0_summary": b0_summary_response_schema_v1,
    "b0_entity_conflict": entity_conflict_response_schema,
    "b1_scan": b1_scan_response_schema_v1,
    "b1_enrich": b1_enrich_response_schema_v1,
    "b1_local_auditor": b1_enrich_local_audit_response_schema_v1,
    "b1_identity_hearing": identity_hearing_response_schema_v1,
    "b1_stable_claim_hearing": stable_claim_hearing_response_schema_v1,
    "b2_frame": b2_frame_response_schema_v2,
    "b2_interaction": b2_interaction_response_schema_v3,
    "b2_speaker_recovery": registry_recovery_response_schema_v1,
    "b3_temporal": b3_temporal_response_schema_v4,
    "b3_temporal_auditor": b3_temporal_audit_response_schema_v1,
    "book_entity_cross_chapter": cross_chapter_response_schema_v1,
    "incremental_identity": incremental_identity_response_schema_v1,
    "prior_claim_auditor": prior_claim_response_schema_v1,
}

AUDITED_ID_FIELDS = (
    "narrowed_candidate_card_ids",
    "excluded_prior_card_ids",
    "merge_target_prior_card_id",
    "supports_excluded_prior_card_ids",
    "target_prior_card_id",
    "target_candidate_id",
    "glossary_card_id",
    "candidate_ref",
    "target_candidate_ref",
)

AUDITED_LIVE_REGISTERED_ID_FIELDS = tuple(
    field_name
    for field_name in AUDITED_ID_FIELDS
    if field_name != "glossary_card_id"
)

B0_FREE_TEXT_HANDOFF_FIELDS = {
    "entities_mentioned",
    "locations_mentioned",
}


def _iter_string_property_names(schema: Mapping[str, Any]):
    properties = schema.get("properties")
    if isinstance(properties, Mapping):
        for name, child in properties.items():
            if isinstance(child, Mapping):
                if _accepts_string_value(child):
                    yield str(name)
                yield from _iter_string_property_names(child)
    items = schema.get("items")
    if isinstance(items, Mapping):
        yield from _iter_string_property_names(items)
    for keyword in ("allOf", "anyOf", "oneOf"):
        branches = schema.get(keyword)
        if isinstance(branches, list):
            for branch in branches:
                if isinstance(branch, Mapping):
                    yield from _iter_string_property_names(branch)


def _accepts_string_value(schema: Mapping[str, Any]) -> bool:
    raw_type = schema.get("type")
    if raw_type == "string":
        return True
    if isinstance(raw_type, list) and "string" in raw_type:
        return True
    if raw_type == "array":
        items = schema.get("items")
        return isinstance(items, Mapping) and _accepts_string_value(items)
    for keyword in ("allOf", "anyOf", "oneOf"):
        branches = schema.get(keyword)
        if isinstance(branches, list) and any(
            isinstance(branch, Mapping) and _accepts_string_value(branch)
            for branch in branches
        ):
            return True
    return False


def _is_id_shaped_field(name: str) -> bool:
    return (
        name.endswith(
            (
                "card_id",
                "card_ids",
                "candidate_id",
                "candidate_ids",
                "_ref",
                "_refs",
                "entity_id",
                "entity_ids",
            )
        )
        or "referent_ref" in name
        or "prior_card_id" in name
    )


def _uncovered_live_id_fields(registered: set[str]) -> list[str]:
    uncovered: list[str] = []
    for role, builder in LIVE_RESPONSE_SCHEMA_BUILDERS.items():
        for field_name in sorted(set(_iter_string_property_names(builder()))):
            if (
                _is_id_shaped_field(field_name)
                and field_name not in registered
                and field_name not in TRANSPORT_EXEMPT_FIELDS_V1
            ):
                uncovered.append(f"{role}:{field_name}")
    return uncovered


def _iter_request_persistent_ref_fields(
    value: Any, *, field_name: str | None = None
):
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield from _iter_request_persistent_ref_fields(
                child, field_name=str(key)
            )
        return
    if isinstance(value, list):
        for child in value:
            yield from _iter_request_persistent_ref_fields(
                child, field_name=field_name
            )
        return
    if (
        isinstance(value, str)
        and field_name is not None
        and model_ref_module._OPAQUE_PERSISTENT_REF_RE.fullmatch(value)
    ):
        yield field_name


def _live_request_payloads() -> Mapping[str, Mapping[str, Any]]:
    recovery_index = synthetic_probe_index_v1()
    recovery_request = render_b2_slim_speaker_recovery_request_v1(recovery_index)
    assert recovery_request is not None

    interaction_request = synthetic_b2_probe_request_v1("interaction")
    interaction = json.loads(interaction_request["messages"][1]["content"])
    interaction["candidate_packets"]["candidate_cards"] = [
        {"candidate_card_id": "b0ent_request_entity"}
    ]
    interaction["frame_context"]["frame_segments"] = [
        {"frame_segment_id": "b2frm2_request_frame"}
    ]

    temporal_request = synthetic_b3_probe_request_v2()
    temporal = json.loads(temporal_request["messages"][1]["content"])
    temporal["components"][0] = {
        **temporal["components"][0],
        "component_id": "b3comp_request_component",
        "referent_refs": ["b0ent_request_entity"],
        "speaker_turns": [{"speaker_turn_id": "b2turn3_request_turn"}],
        "salient_events": [{"salient_event_id": "b2evt3_request_event"}],
        "prior_open_states": [{"state_id": "b3state1_request_state"}],
    }
    temporal["frame_packets"] = [
        {"frame_segment_id": "b2frm2_request_frame"}
    ]

    return {
        "b2_interaction": interaction,
        "b2_speaker_recovery": deepcopy(recovery_request.semantic_payload),
        "b3_temporal": temporal,
    }


def _uncovered_live_request_ref_fields(registered: set[str]) -> list[str]:
    uncovered = []
    for role, payload in _live_request_payloads().items():
        for field_name in sorted(set(_iter_request_persistent_ref_fields(payload))):
            if (
                field_name not in registered
                and field_name not in TRANSPORT_EXEMPT_FIELDS_V1
            ):
                uncovered.append(f"{role}:{field_name}")
    return uncovered


def test_every_live_id_field_is_registered_or_explicitly_exempted() -> None:
    registered = {
        field_name
        for namespace_fields in MODEL_REF_FIELDS_V1.values()
        for field_name in namespace_fields
    }
    assert _uncovered_live_id_fields(registered) == []


def test_every_live_request_ref_field_is_registered_or_explicitly_exempted() -> None:
    registered = {
        field_name
        for namespace_fields in MODEL_REF_FIELDS_V1.values()
        for field_name in namespace_fields
    }
    assert _uncovered_live_request_ref_fields(registered) == []


def test_request_coverage_gate_detects_removed_frame_registrations() -> None:
    registered = {
        field_name
        for namespace_fields in MODEL_REF_FIELDS_V1.values()
        for field_name in namespace_fields
    }
    for field_name in ("source_frame_segment_id", "source_window_id"):
        assert field_name in registered
        assert _uncovered_live_request_ref_fields(registered - {field_name}) == [
            f"b2_speaker_recovery:{field_name}"
        ]


def test_coverage_classifier_recognizes_every_audited_id_field() -> None:
    assert {
        field_name
        for field_name in AUDITED_ID_FIELDS
        if _is_id_shaped_field(field_name)
    } == set(AUDITED_ID_FIELDS)


def test_coverage_gate_is_sensitive_to_every_audited_live_registration() -> None:
    registered = {
        field_name
        for namespace_fields in MODEL_REF_FIELDS_V1.values()
        for field_name in namespace_fields
    }
    for removed in AUDITED_LIVE_REGISTERED_ID_FIELDS:
        assert removed in registered
        uncovered = _uncovered_live_id_fields(registered - {removed})
        assert any(row.endswith(f":{removed}") for row in uncovered), removed


def test_transport_exemptions_are_documented_and_not_live_shortcuts() -> None:
    assert all(
        isinstance(reason, str) and reason.strip()
        for reason in TRANSPORT_EXEMPT_FIELDS_V1.values()
    )
    live_fields = {
        field_name
        for builder in LIVE_RESPONSE_SCHEMA_BUILDERS.values()
        for field_name in _iter_string_property_names(builder())
    }
    assert set(TRANSPORT_EXEMPT_FIELDS_V1).isdisjoint(live_fields)
    assert "glossary_card_id" in set(
        _iter_string_property_names(prior_challenge_response_schema())
    )
    assert "glossary_card_id" in TRANSPORT_EXEMPT_FIELDS_V1


def test_b0_free_text_handoff_fields_are_not_registered_as_model_refs() -> None:
    registered = {
        field_name
        for namespace_fields in MODEL_REF_FIELDS_V1.values()
        for field_name in namespace_fields
    }
    handoff_properties = b0_summary_response_schema_v1()["properties"][
        "narrative_handoff"
    ]["properties"]

    assert B0_FREE_TEXT_HANDOFF_FIELDS.isdisjoint(registered)
    for field_name in B0_FREE_TEXT_HANDOFF_FIELDS:
        field_schema = handoff_properties[field_name]
        item_schema = field_schema["items"]
        assert field_schema["type"] == "array"
        assert item_schema["type"] == "string"
        assert item_schema["maxLength"] == 180
        assert "pattern" not in item_schema
        assert not _is_id_shaped_field(field_name)
