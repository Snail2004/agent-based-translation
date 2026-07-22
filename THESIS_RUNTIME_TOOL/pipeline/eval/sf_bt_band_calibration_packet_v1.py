from __future__ import annotations

import hashlib
from typing import Any, Mapping

from pipeline.eval.contracts_v1 import (
    CanonicalPolicy,
    ContractValidationError,
    assert_no_forbidden_runtime_data,
    canonicalize,
    require_commit,
    require_enum,
    require_exact_keys,
    require_list,
    require_mapping,
    require_rfc3339,
    require_sha256,
    require_string,
    seal_payload,
    validate_producer,
    verify_payload_hash,
)
from pipeline.eval.sf_bt_band_calibration_v1 import (
    project_sf_bt_band_calibration_case,
    sf_bt_band_calibration_fixture_sha256,
    validate_sf_bt_band_calibration_fixture,
)


__all__ = [
    "SF_BT_BAND_CALIBRATION_PACKET_SCHEMA_ID",
    "SF_BT_BAND_CALIBRATION_PACKET_SCHEMA_VERSION",
    "build_sf_bt_band_calibration_packet_v1",
    "validate_sf_bt_band_calibration_packet_binding_v1",
    "validate_sf_bt_band_calibration_packet_v1",
]


SF_BT_BAND_CALIBRATION_PACKET_SCHEMA_ID = "SFBTBandCalibrationJudgePacketV1"
SF_BT_BAND_CALIBRATION_PACKET_SCHEMA_VERSION = "1.0.0"
_SELF_HASH_PATH = ("integrity", "packet_sha256")
_ORIENTATIONS = frozenset({"canonical", "reversed"})
_PRESENTATIONS = frozenset(
    {"calibration_reference_first", "calibration_candidate_first"}
)
_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(),
    semantic_sequence_paths=frozenset({("passages",)}),
)


def build_sf_bt_band_calibration_packet_v1(
    fixture_payload: Mapping[str, Any],
    *,
    case_id: str,
    orientation: str,
    created_at: str,
    producer_code_commit: str,
) -> dict[str, Any]:
    fixture = validate_sf_bt_band_calibration_fixture(fixture_payload)
    if fixture["review_status"] != "approved_independent_semantic_review":
        raise ContractValidationError(
            "fixture_review",
            "$.review_status",
            "live calibration packets require an independently approved fixture",
        )
    normalized_case_id = require_string(case_id, path="$.case_id")
    normalized_orientation = require_enum(
        orientation, _ORIENTATIONS, path="$.orientation"
    )
    timestamp = require_rfc3339(created_at, path="$.created_at")
    commit = require_commit(producer_code_commit, path="$.producer_code_commit")
    matches = [
        row for row in fixture["cases"] if row["case_id"] == normalized_case_id
    ]
    if len(matches) != 1:
        raise ContractValidationError(
            "case_reference", "$.case_id", "case ID does not resolve exactly once"
        )
    case = matches[0]
    projection = project_sf_bt_band_calibration_case(case)
    if normalized_orientation == "reversed":
        projection = {
            "presentation_id": (
                "calibration_candidate_first"
                if projection["presentation_id"] == "calibration_reference_first"
                else "calibration_reference_first"
            ),
            "passage_a": projection["passage_b"],
            "passage_b": projection["passage_a"],
        }
    fixture_hash = sf_bt_band_calibration_fixture_sha256(fixture)
    packet_id = "sfbt-band-packet-" + _digest(
        fixture_hash, normalized_case_id, normalized_orientation
    )[:24]
    packet = {
        "schema_id": SF_BT_BAND_CALIBRATION_PACKET_SCHEMA_ID,
        "schema_version": SF_BT_BAND_CALIBRATION_PACKET_SCHEMA_VERSION,
        "packet_id": packet_id,
        "created_at": timestamp,
        "producer": {
            "workstream": "evaluation",
            "component": "sf_bt_band_calibration_packet_v1",
            "component_version": "1.0.0",
            "code_commit": commit,
        },
        "binding": {
            "fixture_set_id": fixture["fixture_set_id"],
            "fixture_sha256": fixture_hash,
            "case_id": normalized_case_id,
            "ladder_id": case["ladder_id"],
            "orientation": normalized_orientation,
            "presentation_id": projection["presentation_id"],
        },
        "language": "en",
        "stage": "semantic_comparison",
        "passages": [
            {
                "slot_id": slot_id,
                "text": projection[slot_id],
                "text_sha256": hashlib.sha256(
                    projection[slot_id].encode("utf-8")
                ).hexdigest(),
            }
            for slot_id in ("passage_a", "passage_b")
        ],
        "integrity": {"packet_sha256": "0" * 64},
    }
    sealed = seal_payload(packet, policy=_POLICY, hash_path=_SELF_HASH_PATH)
    return validate_sf_bt_band_calibration_packet_v1(sealed)


def validate_sf_bt_band_calibration_packet_v1(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    assert_no_forbidden_runtime_data(payload)
    root = require_mapping(payload, path="$")
    require_exact_keys(
        root,
        required={
            "schema_id",
            "schema_version",
            "packet_id",
            "created_at",
            "producer",
            "binding",
            "language",
            "stage",
            "passages",
            "integrity",
        },
        path="$",
    )
    normalized = {
        "schema_id": require_enum(
            root["schema_id"],
            {SF_BT_BAND_CALIBRATION_PACKET_SCHEMA_ID},
            path="$.schema_id",
        ),
        "schema_version": require_enum(
            root["schema_version"],
            {SF_BT_BAND_CALIBRATION_PACKET_SCHEMA_VERSION},
            path="$.schema_version",
        ),
        "packet_id": require_string(root["packet_id"], path="$.packet_id"),
        "created_at": require_rfc3339(root["created_at"], path="$.created_at"),
        "producer": validate_producer(
            root["producer"], path="$.producer", workstream="evaluation"
        ),
        "binding": _validate_binding(root["binding"]),
        "language": require_enum(root["language"], {"en"}, path="$.language"),
        "stage": require_enum(
            root["stage"], {"semantic_comparison"}, path="$.stage"
        ),
        "passages": _validate_passages(root["passages"]),
        "integrity": _validate_integrity(root["integrity"]),
    }
    if not verify_payload_hash(
        normalized, policy=_POLICY, hash_path=_SELF_HASH_PATH
    ):
        raise ContractValidationError(
            "packet_hash",
            "$.integrity.packet_sha256",
            "calibration packet self-hash does not match canonical content",
        )
    canonical = canonicalize(normalized, policy=_POLICY)
    if not isinstance(canonical, dict):
        raise AssertionError("canonical calibration packet must remain an object")
    return canonical


def validate_sf_bt_band_calibration_packet_binding_v1(
    payload: Mapping[str, Any], fixture_payload: Mapping[str, Any]
) -> dict[str, Any]:
    packet = validate_sf_bt_band_calibration_packet_v1(payload)
    expected = build_sf_bt_band_calibration_packet_v1(
        fixture_payload,
        case_id=packet["binding"]["case_id"],
        orientation=packet["binding"]["orientation"],
        created_at=packet["created_at"],
        producer_code_commit=packet["producer"]["code_commit"],
    )
    if packet != expected:
        raise ContractValidationError(
            "packet_binding",
            "$",
            "calibration packet differs from its bound approved fixture row",
        )
    return packet


def _validate_binding(value: Any) -> dict[str, str]:
    path = "$.binding"
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "fixture_set_id",
            "fixture_sha256",
            "case_id",
            "ladder_id",
            "orientation",
            "presentation_id",
        },
        path=path,
    )
    return {
        "fixture_set_id": require_string(
            row["fixture_set_id"], path=f"{path}.fixture_set_id"
        ),
        "fixture_sha256": require_sha256(
            row["fixture_sha256"], path=f"{path}.fixture_sha256"
        ),
        "case_id": require_string(row["case_id"], path=f"{path}.case_id"),
        "ladder_id": require_string(row["ladder_id"], path=f"{path}.ladder_id"),
        "orientation": require_enum(
            row["orientation"], _ORIENTATIONS, path=f"{path}.orientation"
        ),
        "presentation_id": require_enum(
            row["presentation_id"],
            _PRESENTATIONS,
            path=f"{path}.presentation_id",
        ),
    }


def _validate_passages(value: Any) -> list[dict[str, str]]:
    path = "$.passages"
    rows = require_list(value, path=path)
    if len(rows) != 2:
        raise ContractValidationError(
            "passage_count", path, "calibration packet requires exactly two passages"
        )
    result = []
    for index, value in enumerate(rows):
        row_path = f"{path}[{index}]"
        row = require_mapping(value, path=row_path)
        require_exact_keys(
            row, required={"slot_id", "text", "text_sha256"}, path=row_path
        )
        text = require_string(row["text"], path=f"{row_path}.text")
        text_sha256 = require_sha256(
            row["text_sha256"], path=f"{row_path}.text_sha256"
        )
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != text_sha256:
            raise ContractValidationError(
                "passage_hash",
                f"{row_path}.text_sha256",
                "passage hash differs from exact UTF-8 text",
            )
        result.append(
            {
                "slot_id": require_enum(
                    row["slot_id"],
                    {"passage_a", "passage_b"},
                    path=f"{row_path}.slot_id",
                ),
                "text": text,
                "text_sha256": text_sha256,
            }
        )
    if [row["slot_id"] for row in result] != ["passage_a", "passage_b"]:
        raise ContractValidationError(
            "passage_order", path, "passages must be ordered passage_a then passage_b"
        )
    return result


def _validate_integrity(value: Any) -> dict[str, str]:
    path = "$.integrity"
    row = require_mapping(value, path=path)
    require_exact_keys(row, required={"packet_sha256"}, path=path)
    return {
        "packet_sha256": require_sha256(
            row["packet_sha256"], path=f"{path}.packet_sha256"
        )
    }


def _digest(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
