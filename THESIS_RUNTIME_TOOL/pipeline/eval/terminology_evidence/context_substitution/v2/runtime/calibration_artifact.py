from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from pipeline.eval.contracts_v1 import ContractValidationError


CALIBRATION_SCHEMA_ID = "CSTCalibrationArtifactV1"
CALIBRATION_SCHEMA_VERSION = "1.0.0"
CALIBRATION_POLICY_ID = "d2l_context_status_calibration_v1"


def build_calibration_artifact(
    *,
    dataset_manifest_sha256: str,
    gold_dataset_sha256: str,
    policy_version: str,
    supported_min_c: float,
    unsupported_below_c: float,
    supported_min_pass: int,
    supported_max_minor: int,
    unsupported_min_fail: int,
    second_judge_thresholds: tuple[float, ...],
    second_judge_tolerance: float,
    pairwise_close_margin: float,
    case_count: int,
    positive_case_count: int,
    negative_case_count: int,
    measured_auto_approval_precision: float,
    target_auto_approval_precision: float = 0.95,
    selection_rule: str = "maximize_coverage_subject_to_precision_floor_v1",
) -> dict[str, Any]:
    return seal_calibration_artifact(
        {
            "schema_id": CALIBRATION_SCHEMA_ID,
            "schema_version": CALIBRATION_SCHEMA_VERSION,
            "policy_id": CALIBRATION_POLICY_ID,
            "dataset_manifest_sha256": dataset_manifest_sha256,
            "gold_dataset_sha256": gold_dataset_sha256,
            "metric_target": {
                "auto_approval_precision": target_auto_approval_precision,
            },
            "calibration_results": {
                "case_count": case_count,
                "positive_case_count": positive_case_count,
                "negative_case_count": negative_case_count,
                "measured_auto_approval_precision": measured_auto_approval_precision,
                "selection_rule": selection_rule,
            },
            "selected_policy": {
                "policy_version": policy_version,
                "supported_min_c": supported_min_c,
                "unsupported_below_c": unsupported_below_c,
                "supported_min_pass": supported_min_pass,
                "supported_max_minor": supported_max_minor,
                "unsupported_min_fail": unsupported_min_fail,
                "second_judge_thresholds": list(second_judge_thresholds),
                "second_judge_tolerance": second_judge_tolerance,
                "pairwise_close_margin": pairwise_close_margin,
            },
            "integrity": {"artifact_sha256": "0" * 64},
        }
    )


def seal_calibration_artifact(value: Mapping[str, Any]) -> dict[str, Any]:
    row = json.loads(json.dumps(value))
    if not isinstance(row.get("integrity"), dict):
        row["integrity"] = {}
    row["integrity"]["artifact_sha256"] = "0" * 64
    row["integrity"]["artifact_sha256"] = _artifact_hash(row)
    return validate_calibration_artifact(row)


def load_calibration_artifact(
    source: Path,
    *,
    expected_physical_sha256: str | None = None,
) -> dict[str, Any]:
    source = Path(source)
    raw = source.read_bytes()
    physical = hashlib.sha256(raw).hexdigest()
    if expected_physical_sha256 is not None and physical != _sha256(
        expected_physical_sha256, "$.expected_physical_sha256"
    ):
        _fail("physical_hash", str(source), "calibration file hash mismatch")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail("calibration_json", str(source), str(exc))
    return validate_calibration_artifact(value)


def validate_calibration_artifact(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _fail("type", "$", "expected an object")
    row = dict(value)
    required = {
        "schema_id",
        "schema_version",
        "policy_id",
        "dataset_manifest_sha256",
        "gold_dataset_sha256",
        "metric_target",
        "calibration_results",
        "selected_policy",
        "integrity",
    }
    if set(row) != required:
        _fail("shape", "$", "unexpected calibration artifact fields")
    exact = {
        "schema_id": CALIBRATION_SCHEMA_ID,
        "schema_version": CALIBRATION_SCHEMA_VERSION,
        "policy_id": CALIBRATION_POLICY_ID,
    }
    for key, expected in exact.items():
        if row[key] != expected:
            _fail("identity", f"$.{key}", f"expected {expected}")
    row["dataset_manifest_sha256"] = _sha256(
        row["dataset_manifest_sha256"], "$.dataset_manifest_sha256"
    )
    row["gold_dataset_sha256"] = _sha256(
        row["gold_dataset_sha256"], "$.gold_dataset_sha256"
    )
    target = _exact_mapping(
        row["metric_target"],
        {"auto_approval_precision"},
        "$.metric_target",
    )
    target_precision = _unit(
        target["auto_approval_precision"],
        "$.metric_target.auto_approval_precision",
    )
    results = _exact_mapping(
        row["calibration_results"],
        {
            "case_count",
            "positive_case_count",
            "negative_case_count",
            "measured_auto_approval_precision",
            "selection_rule",
        },
        "$.calibration_results",
    )
    case_count = _positive_int(results["case_count"], "$.calibration_results.case_count")
    positives = _positive_int(
        results["positive_case_count"],
        "$.calibration_results.positive_case_count",
        allow_zero=True,
    )
    negatives = _positive_int(
        results["negative_case_count"],
        "$.calibration_results.negative_case_count",
        allow_zero=True,
    )
    if positives + negatives != case_count:
        _fail("case_count", "$.calibration_results", "class counts must equal case_count")
    measured = _unit(
        results["measured_auto_approval_precision"],
        "$.calibration_results.measured_auto_approval_precision",
    )
    if measured < target_precision:
        _fail("precision_floor", "$.calibration_results", "selected policy misses precision target")
    rule = results["selection_rule"]
    if rule != "maximize_coverage_subject_to_precision_floor_v1":
        _fail("selection_rule", "$.calibration_results.selection_rule", "unregistered rule")
    policy = _validate_selected_policy(row["selected_policy"])
    integrity = _exact_mapping(
        row["integrity"], {"artifact_sha256"}, "$.integrity"
    )
    claimed = _sha256(integrity["artifact_sha256"], "$.integrity.artifact_sha256")
    normalized = {
        **row,
        "metric_target": {"auto_approval_precision": target_precision},
        "calibration_results": {
            "case_count": case_count,
            "positive_case_count": positives,
            "negative_case_count": negatives,
            "measured_auto_approval_precision": measured,
            "selection_rule": rule,
        },
        "selected_policy": policy,
        "integrity": {"artifact_sha256": claimed},
    }
    if claimed != _artifact_hash(normalized):
        _fail("self_hash", "$.integrity.artifact_sha256", "calibration self-hash mismatch")
    return normalized


def _validate_selected_policy(value: Any) -> dict[str, Any]:
    path = "$.selected_policy"
    row = _exact_mapping(
        value,
        {
            "policy_version",
            "supported_min_c",
            "unsupported_below_c",
            "supported_min_pass",
            "supported_max_minor",
            "unsupported_min_fail",
            "second_judge_thresholds",
            "second_judge_tolerance",
            "pairwise_close_margin",
        },
        path,
    )
    supported = _unit(row["supported_min_c"], f"{path}.supported_min_c")
    unsupported = _unit(row["unsupported_below_c"], f"{path}.unsupported_below_c")
    if unsupported >= supported:
        _fail("threshold_order", path, "unsupported threshold must be lower")
    raw_thresholds = row["second_judge_thresholds"]
    if not isinstance(raw_thresholds, list):
        _fail("type", f"{path}.second_judge_thresholds", "expected a list")
    thresholds = [_unit(item, f"{path}.second_judge_thresholds") for item in raw_thresholds]
    if not thresholds or thresholds != sorted(set(thresholds)):
        _fail("threshold_order", f"{path}.second_judge_thresholds", "must be sorted and unique")
    policy_version = row["policy_version"]
    if not isinstance(policy_version, str) or not policy_version.strip():
        _fail("policy_version", f"{path}.policy_version", "expected a nonempty string")
    return {
        "policy_version": policy_version,
        "supported_min_c": supported,
        "unsupported_below_c": unsupported,
        "supported_min_pass": _positive_int(row["supported_min_pass"], f"{path}.supported_min_pass"),
        "supported_max_minor": _positive_int(row["supported_max_minor"], f"{path}.supported_max_minor", allow_zero=True),
        "unsupported_min_fail": _positive_int(row["unsupported_min_fail"], f"{path}.unsupported_min_fail"),
        "second_judge_thresholds": thresholds,
        "second_judge_tolerance": _unit(row["second_judge_tolerance"], f"{path}.second_judge_tolerance"),
        "pairwise_close_margin": _unit(row["pairwise_close_margin"], f"{path}.pairwise_close_margin"),
    }


def _artifact_hash(value: Mapping[str, Any]) -> str:
    row = json.loads(json.dumps(value))
    row["integrity"]["artifact_sha256"] = "0" * 64
    payload = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _exact_mapping(value: Any, keys: set[str], path: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        _fail("shape", path, "unexpected object fields")
    return dict(value)


def _positive_int(value: Any, path: str, *, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        _fail("integer", path, f"expected integer >= {minimum}")
    return value


def _unit(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail("number", path, "expected a number")
    result = float(value)
    if result < 0 or result > 1:
        _fail("range", path, "expected value in [0,1]")
    return result


def _sha256(value: Any, path: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        _fail("sha256", path, "expected 64 hexadecimal characters")
    try:
        int(value, 16)
    except ValueError:
        _fail("sha256", path, "expected hexadecimal characters")
    normalized = value.lower()
    if normalized == "0" * 64:
        _fail("sha256", path, "zero hash is not an authority")
    return normalized


def _fail(code: str, path: str, message: str) -> None:
    raise ContractValidationError(code, path, message)
