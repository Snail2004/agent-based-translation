"""Offline gates for Literary request-local opaque-reference transport."""

from __future__ import annotations

from copy import deepcopy
import json

import pytest

from pipeline.literary.checkpoint import canonical_hash, canonical_json
from pipeline.literary.model_ref_v1 import (
    MODEL_REF_FIELDS_V1,
    MODEL_REF_MAP_SCHEMA_VERSION,
    ModelRefError,
    build_model_ref_map,
    model_ref_instruction_v1,
    project_model_request_v1,
    resolve_model_response_v1,
    validate_model_ref_map,
)


PERSISTENT_BY_FIELD = {
    "entity_id": "b0ent_aaaaaaaaaaaaaaaaaaaa",
    "scan_observation_id": "b1obs_bbbbbbbbbbbbbbbb",
    "term_observation_id": "b1term_cccccccccccccccc",
    "component_id": "b1lac_dddddddddddddddddddd",
    "frame_id": "b2frm2_eeeeeeeeeeeeeeeeeeee",
    "event_id": "b2evt3_ffffffffffffffffffff",
    "turn_id": "b2turn3_11111111111111111111",
    "state_id": "b3state1_22222222222222222222",
    "pending_case_id": "b3pending_333333333333333333",
    "review_id": "b2review_44444444444444444444",
    "batch_id": "b3batch_555555555555555555555",
}
LOCAL_BY_FIELD = {
    "entity_id": "E1",
    "scan_observation_id": "O1",
    "term_observation_id": "G1",
    "component_id": "C1",
    "frame_id": "F1",
    "event_id": "V1",
    "turn_id": "T1",
    "state_id": "S1",
    "pending_case_id": "Q1",
    "review_id": "R1",
    "batch_id": "B1",
}
MANIFEST_HASH = "a" * 64


def test_model_labels_are_confined_to_structured_reference_fields() -> None:
    instruction = model_ref_instruction_v1()
    assert "never from the output field's name" in instruction
    assert "every structured selection, citation, or pointer" in instruction
    assert "regardless of the output field's name" in instruction
    assert "free-form prose fields" in instruction
    assert "canonical surface or name" in instruction
    assert "never a transport label" in instruction
    assert "explicitly asks for an id" not in instruction
    assert "entities_mentioned" not in instruction
    assert "locations_mentioned" not in instruction


def _canonical_request() -> dict:
    payload = {
        "chapter_id": "wh_ch01",
        "block_id": "wh_ch01_b010",
        "window_id": "b2w1_wh_ch01_01",
        "manifest_hash": MANIFEST_HASH,
        **PERSISTENT_BY_FIELD,
    }
    properties = {
        "schema_version": {"const": "synthetic_response_v1"},
        "manifest_hash": {"type": "string", "minLength": 64, "maxLength": 64},
        "window_id": {"type": "string"},
        **{
            field: {"type": "string"}
            for field in PERSISTENT_BY_FIELD
        },
    }
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
    }
    body = {
        "messages": [
            {"role": "system", "content": "Keep every semantic field unchanged."},
            {"role": "user", "content": canonical_json(payload)},
        ],
        "response_schema": schema,
        "response_schema_hash": canonical_hash(schema),
        "request_fingerprint": "placeholder",
    }
    unsigned = deepcopy(body)
    unsigned.pop("request_fingerprint")
    body["request_fingerprint"] = canonical_hash(unsigned)
    return body


def test_all_classified_reference_namespaces_round_trip_without_semantic_change() -> None:
    projected, ref_map = project_model_request_v1(_canonical_request())
    payload = json.loads(projected["messages"][1]["content"])

    assert payload["chapter_id"] == "wh_ch01"
    assert payload["block_id"] == "wh_ch01_b010"
    assert payload["window_id"] == "b2w1_wh_ch01_01"
    assert "manifest_hash" not in payload
    for field, local_ref in LOCAL_BY_FIELD.items():
        assert payload[field] == local_ref

    schema = projected["response_schema"]
    assert "manifest_hash" not in schema["properties"]
    assert "manifest_hash" not in schema["required"]
    for field in LOCAL_BY_FIELD:
        assert schema["properties"][field] == {"type": "string"}

    response = {
        "schema_version": "synthetic_response_v1",
        "window_id": "b2w1_wh_ch01_01",
        **LOCAL_BY_FIELD,
    }
    resolved = resolve_model_response_v1(projected, response)
    assert resolved == {
        "schema_version": "synthetic_response_v1",
        "window_id": "b2w1_wh_ch01_01",
        **PERSISTENT_BY_FIELD,
        "manifest_hash": MANIFEST_HASH,
    }
    assert validate_model_ref_map(ref_map) == ref_map


def test_b2_competing_card_ids_round_trip_through_issued_entity_labels() -> None:
    request = _canonical_request()
    first = "b0ent_competing_first"
    second = "b0ent_competing_second"
    payload = json.loads(request["messages"][1]["content"])
    payload["candidate_card_ids"] = [first, second]
    request["messages"][1]["content"] = canonical_json(payload)
    request["response_schema"]["properties"]["candidate_card_ids"] = {
        "type": "array",
        "items": {"type": "string"},
    }
    request["response_schema"]["properties"]["competing_card_ids"] = {
        "type": "array",
        "items": {"type": "string"},
    }
    request["response_schema"]["required"].extend(
        ["candidate_card_ids", "competing_card_ids"]
    )
    request["response_schema_hash"] = canonical_hash(request["response_schema"])
    unsigned = deepcopy(request)
    unsigned.pop("request_fingerprint")
    request["request_fingerprint"] = canonical_hash(unsigned)

    projected, _ref_map = project_model_request_v1(request)
    projected_payload = json.loads(projected["messages"][1]["content"])
    projected_candidates = projected_payload["candidate_card_ids"]
    assert len(projected_candidates) == 2
    assert all(value.startswith("E") for value in projected_candidates)

    resolved = resolve_model_response_v1(
        projected,
        {
            "schema_version": "synthetic_response_v1",
            "window_id": "b2w1_wh_ch01_01",
            **{
                field: projected_payload[field]
                for field in PERSISTENT_BY_FIELD
            },
            "candidate_card_ids": projected_candidates,
            "competing_card_ids": list(reversed(projected_candidates)),
        },
    )
    assert resolved["candidate_card_ids"] == [first, second]
    assert resolved["competing_card_ids"] == [second, first]


@pytest.mark.parametrize(
    ("field_name", "persistent_value", "is_list"),
    [
        ("narrowed_candidate_card_ids", "b0ent_narrowed", True),
        ("excluded_prior_card_ids", "b0ent_excluded", True),
        ("merge_target_prior_card_id", "b0ent_merge_target", False),
        ("supports_excluded_prior_card_ids", "b0ent_supports_excluded", True),
        ("candidate_ref", "bkcand_candidate", False),
        ("target_candidate_ref", "bkcand_target_candidate", False),
        ("target_prior_card_id", "b0ent_target_prior", False),
        ("target_candidate_id", "b0ent_target_candidate", False),
    ],
)
def test_live_entity_reference_fields_round_trip_through_transport(
    field_name: str,
    persistent_value: str,
    is_list: bool,
) -> None:
    request = _canonical_request()
    payload = json.loads(request["messages"][1]["content"])
    payload[field_name] = [persistent_value] if is_list else persistent_value
    request["messages"][1]["content"] = canonical_json(payload)
    request["response_schema"]["properties"][field_name] = (
        {"type": "array", "items": {"type": "string"}}
        if is_list
        else {"type": "string"}
    )
    request["response_schema"]["required"].append(field_name)
    request["response_schema_hash"] = canonical_hash(request["response_schema"])
    unsigned = deepcopy(request)
    unsigned.pop("request_fingerprint")
    request["request_fingerprint"] = canonical_hash(unsigned)

    projected, _ref_map = project_model_request_v1(request)
    projected_payload = json.loads(projected["messages"][1]["content"])
    local_value = projected_payload[field_name]
    local_values = local_value if is_list else [local_value]
    assert all(value.startswith("E") for value in local_values)

    response = {
        "schema_version": "synthetic_response_v1",
        "window_id": "b2w1_wh_ch01_01",
        **{
            field: projected_payload[field]
            for field in PERSISTENT_BY_FIELD
        },
        field_name: local_value,
    }
    resolved = resolve_model_response_v1(projected, response)
    assert resolved[field_name] == (
        [persistent_value] if is_list else persistent_value
    )


def test_value_qualified_scan_alias_uses_one_observation_label_across_fields() -> None:
    request = _canonical_request()
    observation_id = PERSISTENT_BY_FIELD["scan_observation_id"]
    entity_id = PERSISTENT_BY_FIELD["entity_id"]
    payload = json.loads(request["messages"][1]["content"])
    payload.update(
        {
            "task_ref": f"scan:{observation_id}",
            "target_ref": f"scan:{observation_id}",
            "entity_ref": entity_id,
        }
    )
    request["messages"][1]["content"] = canonical_json(payload)
    for field in ("task_ref", "target_ref", "entity_ref"):
        request["response_schema"]["properties"][field] = {"type": "string"}
        request["response_schema"]["required"].append(field)
    request["response_schema_hash"] = canonical_hash(request["response_schema"])
    unsigned = deepcopy(request)
    unsigned.pop("request_fingerprint")
    request["request_fingerprint"] = canonical_hash(unsigned)

    projected, ref_map = project_model_request_v1(request)
    projected_payload = json.loads(projected["messages"][1]["content"])
    assert projected_payload["scan_observation_id"] == "O1"
    assert projected_payload["task_ref"] == "O1"
    assert projected_payload["target_ref"] == "O1"
    assert projected_payload["entity_ref"] == "E1"
    observation_rows = [
        row
        for row in ref_map["entries"]
        if row["namespace"] == "scan_observation"
    ]
    assert observation_rows == [
        {
            "namespace": "scan_observation",
            "local_ref": "O1",
            "persistent_id": f"scan:{observation_id}",
        }
    ]

    response = {
        "schema_version": "synthetic_response_v1",
        "window_id": "b2w1_wh_ch01_01",
        "scan_observation_id": "O1",
        "task_ref": "O1",
        "target_ref": "O1",
        "entity_ref": "E1",
    }
    resolved = resolve_model_response_v1(projected, response)
    assert resolved["scan_observation_id"] == observation_id
    assert resolved["task_ref"] == f"scan:{observation_id}"
    assert resolved["target_ref"] == f"scan:{observation_id}"
    assert resolved["entity_ref"] == entity_id


def test_within_chapter_merge_refs_round_trip_as_scan_labels() -> None:
    request = _canonical_request()
    first = "b1obs_merge_first"
    second = "b1obs_merge_second"
    representative = "b1obs_merge_representative"
    payload = json.loads(request["messages"][1]["content"])
    payload["within_chapter_identity_merge"] = {
        "member_source_refs": [f"scan:{first}", f"scan:{second}"],
        "representative_source_ref": f"scan:{representative}",
    }
    request["messages"][1]["content"] = canonical_json(payload)
    request["response_schema"]["properties"]["within_chapter_identity_merge"] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["member_source_refs", "representative_source_ref"],
        "properties": {
            "member_source_refs": {
                "type": "array",
                "items": {"type": "string"},
            },
            "representative_source_ref": {"type": "string"},
        },
    }
    request["response_schema"]["required"].append(
        "within_chapter_identity_merge"
    )
    request["response_schema_hash"] = canonical_hash(request["response_schema"])
    unsigned = deepcopy(request)
    unsigned.pop("request_fingerprint")
    request["request_fingerprint"] = canonical_hash(unsigned)

    projected, ref_map = project_model_request_v1(request)
    projected_payload = json.loads(projected["messages"][1]["content"])
    merge_payload = projected_payload["within_chapter_identity_merge"]
    local_by_persistent = {
        row["persistent_id"]: row["local_ref"]
        for row in ref_map["entries"]
        if row["namespace"] == "scan_observation"
    }
    assert merge_payload["member_source_refs"] == [
        local_by_persistent[f"scan:{first}"],
        local_by_persistent[f"scan:{second}"],
    ]
    assert merge_payload["representative_source_ref"] == local_by_persistent[
        f"scan:{representative}"
    ]
    assert {
        f"scan:{first}",
        f"scan:{representative}",
        f"scan:{second}",
    }.issubset(local_by_persistent)

    response = {
        "schema_version": "synthetic_response_v1",
        "window_id": "b2w1_wh_ch01_01",
        "within_chapter_identity_merge": {
            "member_source_refs": [
                local_by_persistent[f"scan:{first}"],
                local_by_persistent[f"scan:{second}"],
            ],
            "representative_source_ref": local_by_persistent[
                f"scan:{representative}"
            ],
        },
    }
    resolved = resolve_model_response_v1(projected, response)
    assert resolved["within_chapter_identity_merge"] == {
        "member_source_refs": [f"scan:{first}", f"scan:{second}"],
        "representative_source_ref": f"scan:{representative}",
    }


def test_unregistered_reference_shaped_merge_field_fails_before_transport() -> None:
    request = _canonical_request()
    payload = json.loads(request["messages"][1]["content"])
    payload["within_chapter_identity_merge"] = {
        "future_source_ref": "scan:b1obs_future_reference"
    }
    request["messages"][1]["content"] = canonical_json(payload)
    with pytest.raises(ModelRefError, match="unmapped persistent reference"):
        project_model_request_v1(request)


def test_bare_and_qualified_scan_values_collapse_in_direct_map() -> None:
    observation_id = PERSISTENT_BY_FIELD["scan_observation_id"]
    ref_map = build_model_ref_map(
        {
            "scan_observation": [observation_id, f"scan:{observation_id}"],
        }
    )
    assert [
        row for row in ref_map["entries"] if row["namespace"] == "scan_observation"
    ] == [
        {
            "namespace": "scan_observation",
            "local_ref": "O1",
            "persistent_id": f"scan:{observation_id}",
        }
    ]


def test_model_never_receives_persistent_ids_or_code_owned_hashes() -> None:
    projected, _ref_map = project_model_request_v1(_canonical_request())
    model_bytes = canonical_json(
        {
            "messages": projected["messages"],
            "response_schema": projected["response_schema"],
        }
    )
    for persistent_id in PERSISTENT_BY_FIELD.values():
        assert persistent_id not in model_bytes
    assert MANIFEST_HASH not in model_bytes
    assert "manifest_hash" not in model_bytes
    assert "b2w1_wh_ch01_01" in model_bytes
    assert "request_fingerprint" not in model_bytes
    assert "response_schema_hash" not in model_bytes


def test_foreign_label_persistent_id_and_model_echoed_hash_fail_closed() -> None:
    projected, _ref_map = project_model_request_v1(_canonical_request())
    valid = {
        "schema_version": "synthetic_response_v1",
        "window_id": "b2w1_wh_ch01_01",
        **LOCAL_BY_FIELD,
    }

    foreign = deepcopy(valid)
    foreign["entity_id"] = "E99"
    with pytest.raises(ModelRefError):
        resolve_model_response_v1(projected, foreign)

    wrong_namespace = deepcopy(valid)
    wrong_namespace["entity_id"] = "O1"
    with pytest.raises(ModelRefError, match="foreign entity"):
        resolve_model_response_v1(projected, wrong_namespace)

    persistent = deepcopy(valid)
    persistent["entity_id"] = PERSISTENT_BY_FIELD["entity_id"]
    with pytest.raises(ModelRefError):
        resolve_model_response_v1(projected, persistent)

    echoed = {**valid, "manifest_hash": MANIFEST_HASH}
    with pytest.raises(ModelRefError):
        resolve_model_response_v1(projected, echoed)


def test_map_request_and_code_echo_tampering_fail_closed() -> None:
    projected, _ref_map = project_model_request_v1(_canonical_request())
    response = {
        "schema_version": "synthetic_response_v1",
        "window_id": "b2w1_wh_ch01_01",
        **LOCAL_BY_FIELD,
    }

    tampered = deepcopy(projected)
    tampered["model_ref_map"]["entries"][0]["local_ref"] = "B9"
    unsigned_map = deepcopy(tampered["model_ref_map"])
    unsigned_map.pop("map_hash")
    tampered["model_ref_map"]["map_hash"] = canonical_hash(unsigned_map)
    tampered["model_ref_map_hash"] = tampered["model_ref_map"]["map_hash"]
    unsigned_request = deepcopy(tampered)
    unsigned_request.pop("request_fingerprint")
    tampered["request_fingerprint"] = canonical_hash(unsigned_request)
    with pytest.raises(ModelRefError, match="deterministic"):
        resolve_model_response_v1(tampered, response)

    tampered = deepcopy(projected)
    tampered["model_code_owned_echoes"]["fields"]["manifest_hash"] = "b" * 64
    with pytest.raises(ModelRefError):
        resolve_model_response_v1(tampered, response)


def test_old_two_label_scan_alias_map_is_rejected_even_when_rehashed() -> None:
    request = _canonical_request()
    observation_id = PERSISTENT_BY_FIELD["scan_observation_id"]
    projected, _ref_map = project_model_request_v1(request)
    stale_map = deepcopy(projected["model_ref_map"])
    stale_map["schema_version"] = MODEL_REF_MAP_SCHEMA_VERSION
    stale_map["entries"] = [
        {
            "namespace": "entity",
            "local_ref": "E1",
            "persistent_id": f"scan:{observation_id}",
        },
        {
            "namespace": "scan_observation",
            "local_ref": "O1",
            "persistent_id": observation_id,
        },
    ]
    unsigned_map = deepcopy(stale_map)
    unsigned_map.pop("map_hash")
    stale_map["map_hash"] = canonical_hash(unsigned_map)

    with pytest.raises(ModelRefError, match="not canonical"):
        validate_model_ref_map(stale_map)


def test_field_registry_does_not_classify_source_addresses_or_hashes() -> None:
    classified_fields = {
        field
        for fields in MODEL_REF_FIELDS_V1.values()
        for field in fields
    }
    assert "chapter_id" not in classified_fields
    assert "block_id" not in classified_fields
    assert "manifest_hash" not in classified_fields
    assert "request_fingerprint" not in classified_fields
    assert "response_schema_hash" not in classified_fields


def test_unmapped_known_persistent_id_family_fails_before_transport() -> None:
    request = _canonical_request()
    payload = json.loads(request["messages"][1]["content"])
    payload["forgotten_transport_field"] = "b2review_deadbeefdeadbeefdead"
    request["messages"][1]["content"] = canonical_json(payload)
    unsigned = deepcopy(request)
    unsigned.pop("request_fingerprint")
    request["request_fingerprint"] = canonical_hash(unsigned)

    with pytest.raises(ModelRefError, match="unmapped persistent reference"):
        project_model_request_v1(request)


def test_request_specific_schema_reference_constraints_are_removed_from_wire() -> None:
    request = _canonical_request()
    request["response_schema"]["properties"]["entity_id"] = {
        "type": "string",
        "enum": [PERSISTENT_BY_FIELD["entity_id"]],
    }
    request["response_schema_hash"] = canonical_hash(request["response_schema"])
    unsigned = deepcopy(request)
    unsigned.pop("request_fingerprint")
    request["request_fingerprint"] = canonical_hash(unsigned)

    projected, _ref_map = project_model_request_v1(request)

    assert projected["response_schema"]["properties"]["entity_id"] == {
        "type": "string",
    }
    assert "enum" not in projected["response_schema"]["properties"]["frame_id"]


def test_b0_free_text_mentions_bypass_entity_transport() -> None:
    request = _canonical_request()
    payload = json.loads(request["messages"][1]["content"])
    payload["entities_mentioned"] = ["Mara", "North House"]
    payload["locations_mentioned"] = ["North House"]
    payload["narrative_note"] = "North House remains the named location."
    request["messages"][1]["content"] = canonical_json(payload)
    for field in ("entities_mentioned", "locations_mentioned"):
        request["response_schema"]["properties"][field] = {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 180},
        }
        request["response_schema"]["required"].append(field)
    request["response_schema"]["properties"]["narrative_note"] = {
        "type": "string"
    }
    request["response_schema"]["required"].append("narrative_note")
    request["response_schema_hash"] = canonical_hash(request["response_schema"])
    unsigned = deepcopy(request)
    unsigned.pop("request_fingerprint")
    request["request_fingerprint"] = canonical_hash(unsigned)

    projected, _ref_map = project_model_request_v1(request)
    model_payload = json.loads(projected["messages"][1]["content"])
    assert model_payload["entities_mentioned"] == ["Mara", "North House"]
    assert model_payload["locations_mentioned"] == ["North House"]
    assert model_payload["narrative_note"] == "North House remains the named location."

    response = {
        "schema_version": "synthetic_response_v1",
        "window_id": "b2w1_wh_ch01_01",
        **LOCAL_BY_FIELD,
        "entities_mentioned": ["Mara", "North House"],
        "locations_mentioned": ["Thrushcross Grange"],
        "narrative_note": "North House remains the named location.",
    }
    resolved = resolve_model_response_v1(projected, response)
    assert resolved["entities_mentioned"] == ["Mara", "North House"]
    assert resolved["locations_mentioned"] == ["Thrushcross Grange"]
    assert resolved["narrative_note"] == "North House remains the named location."


def test_transport_does_not_enforce_non_reference_semantic_constraints() -> None:
    request = _canonical_request()
    request["response_schema"]["properties"]["evidence"] = {
        "type": "array",
        "items": {"type": "string"},
        "maxItems": 1,
    }
    request["response_schema"]["required"].append("evidence")
    request["response_schema_hash"] = canonical_hash(request["response_schema"])
    unsigned = deepcopy(request)
    unsigned.pop("request_fingerprint")
    request["request_fingerprint"] = canonical_hash(unsigned)

    projected, _ref_map = project_model_request_v1(request)
    response = {
        "schema_version": "synthetic_response_v1",
        "window_id": "b2w1_wh_ch01_01",
        **LOCAL_BY_FIELD,
        "evidence": ["first", "second"],
    }

    resolved = resolve_model_response_v1(projected, response)
    assert resolved["evidence"] == ["first", "second"]


def test_nullable_reference_stays_null_without_entering_the_map() -> None:
    request = _canonical_request()
    payload = json.loads(request["messages"][1]["content"])
    payload["continued_prior_card_id"] = None
    request["messages"][1]["content"] = canonical_json(payload)
    request["response_schema"]["properties"]["continued_prior_card_id"] = {
        "type": ["string", "null"],
    }
    request["response_schema"]["required"].append("continued_prior_card_id")
    request["response_schema_hash"] = canonical_hash(request["response_schema"])
    unsigned = deepcopy(request)
    unsigned.pop("request_fingerprint")
    request["request_fingerprint"] = canonical_hash(unsigned)

    projected, ref_map = project_model_request_v1(request)
    model_payload = json.loads(projected["messages"][1]["content"])
    assert model_payload["continued_prior_card_id"] is None
    assert all(
        row["persistent_id"] is not None for row in ref_map["entries"]
    )

    response = {
        "schema_version": "synthetic_response_v1",
        "window_id": "b2w1_wh_ch01_01",
        **LOCAL_BY_FIELD,
        "continued_prior_card_id": None,
    }
    resolved = resolve_model_response_v1(projected, response)
    assert resolved["continued_prior_card_id"] is None
