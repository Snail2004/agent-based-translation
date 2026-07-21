from __future__ import annotations

import math
from typing import Any, Mapping

from pipeline.eval.contracts_v1 import (
    CanonicalPolicy,
    ContractValidationError,
    canonical_sha256,
    canonicalize,
    require_enum,
    require_exact_keys,
    require_int,
    require_list,
    require_mapping,
    require_nullable_int,
    require_nullable_number,
    require_nullable_string,
    require_relative_path,
    require_rfc3339,
    require_sha256,
    require_string,
    require_unique,
    seal_payload,
    validate_method,
    validate_producer,
    verify_payload_hash,
)


__all__ = [
    "FULL_RUN_CANONICAL_POLICY",
    "SCHEMA_ID",
    "SCHEMA_VERSION",
    "seal_full_run_report",
    "validate_full_run_report",
]


SCHEMA_ID = "FullRunReportV1"
SCHEMA_VERSION = "1.0.0"
SELF_HASH_PATH = ("integrity", "report_sha256")


FULL_RUN_CANONICAL_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(
        {
            ("arms",),
            ("metrics",),
            ("metrics", "*", "arm_values"),
            ("metrics", "*", "source_artifact_ids"),
            ("claim", "reason_codes"),
            ("claim", "source_metric_ids"),
            ("usage", "source_artifact_ids"),
            ("integrity", "source_usage_artifact_ids"),
            ("stages", "*", "artifact_ids"),
            ("artifacts",),
        }
    ),
    semantic_sequence_paths=frozenset(
        {
            ("identity", "attempt_run_ids"),
            ("metrics", "*", "caveats"),
            ("usage", "by_stage"),
            ("usage", "notes"),
            ("stages",),
            ("caveats",),
        }
    ),
)


_USAGE_NUMERIC_FIELDS = (
    "request_count",
    "successful_request_count",
    "failed_request_count",
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "thought_tokens",
    "total_tokens",
)


def seal_full_run_report(payload: Mapping[str, Any]) -> dict[str, Any]:
    return seal_payload(payload, policy=FULL_RUN_CANONICAL_POLICY, hash_path=SELF_HASH_PATH)


def validate_full_run_report(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a persisted report and return a canonical, detached copy.

    The caller's object graph is never mutated. Transport consumers that must
    relay persisted array order unchanged may ignore the returned canonical
    copy after successful validation and relay their original parsed payload.
    """

    root = require_mapping(payload, path="$")
    require_exact_keys(
        root,
        required={
            "schema_id",
            "schema_version",
            "report_id",
            "generated_at",
            "producer",
            "report_method",
            "identity",
            "integrity",
            "report_state",
            "arms",
            "metrics",
            "claim",
            "usage",
            "stages",
            "artifacts",
            "caveats",
        },
        path="$",
    )
    normalized: dict[str, Any] = {
        "schema_id": require_enum(root["schema_id"], {SCHEMA_ID}, path="$.schema_id"),
        "schema_version": require_enum(
            root["schema_version"], {SCHEMA_VERSION}, path="$.schema_version"
        ),
        "report_id": require_string(root["report_id"], path="$.report_id"),
        "generated_at": require_rfc3339(root["generated_at"], path="$.generated_at"),
        "producer": validate_producer(
            root["producer"], path="$.producer", workstream="evaluation"
        ),
        "report_method": _validate_report_method(root["report_method"]),
        "identity": _validate_identity(root["identity"]),
        "integrity": _validate_integrity(root["integrity"]),
        "report_state": require_enum(
            root["report_state"], {"complete", "partial", "failed"}, path="$.report_state"
        ),
        "arms": _validate_arms(root["arms"]),
        "metrics": _validate_metrics(root["metrics"]),
        "claim": _validate_claim(root["claim"]),
        "usage": _validate_usage(root["usage"]),
        "stages": _validate_stages(root["stages"]),
        "artifacts": _validate_artifacts(root["artifacts"]),
        "caveats": _validate_string_sequence(root["caveats"], path="$.caveats"),
    }
    _validate_references_and_semantics(normalized)
    expected_artifact_set = canonical_sha256(
        {"artifacts": normalized["artifacts"]}, policy=FULL_RUN_CANONICAL_POLICY
    )
    if normalized["integrity"]["artifact_set_sha256"] != expected_artifact_set:
        raise ContractValidationError(
            "artifact_set_hash",
            "$.integrity.artifact_set_sha256",
            "artifact set hash does not match artifacts",
        )
    if not verify_payload_hash(
        normalized, policy=FULL_RUN_CANONICAL_POLICY, hash_path=SELF_HASH_PATH
    ):
        raise ContractValidationError(
            "report_hash",
            "$.integrity.report_sha256",
            "report self-hash does not match canonical content",
        )
    canonical = canonicalize(normalized, policy=FULL_RUN_CANONICAL_POLICY)
    if not isinstance(canonical, dict):
        raise AssertionError("canonical full run report must remain an object")
    return canonical


def _validate_report_method(value: Any) -> dict[str, Any]:
    path = "$.report_method"
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={"method_id", "method_version", "policy_profile_id"},
        path=path,
    )
    return {
        "method_id": require_string(row["method_id"], path=f"{path}.method_id"),
        "method_version": require_string(
            row["method_version"], path=f"{path}.method_version"
        ),
        "policy_profile_id": require_nullable_string(
            row["policy_profile_id"], path=f"{path}.policy_profile_id"
        ),
    }


def _validate_identity(value: Any) -> dict[str, Any]:
    path = "$.identity"
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "project_id",
            "logical_run_id",
            "attempt_run_ids",
            "document_id",
            "profile_id",
            "input_manifest_sha256",
        },
        path=path,
    )
    attempts = _validate_string_sequence(
        row["attempt_run_ids"], path=f"{path}.attempt_run_ids"
    )
    if not attempts:
        raise ContractValidationError(
            "empty_array", f"{path}.attempt_run_ids", "at least one attempt is required"
        )
    require_unique(attempts, path=f"{path}.attempt_run_ids")
    return {
        "project_id": require_string(row["project_id"], path=f"{path}.project_id"),
        "logical_run_id": require_string(
            row["logical_run_id"], path=f"{path}.logical_run_id"
        ),
        "attempt_run_ids": attempts,
        "document_id": require_nullable_string(
            row["document_id"], path=f"{path}.document_id"
        ),
        "profile_id": require_string(row["profile_id"], path=f"{path}.profile_id"),
        "input_manifest_sha256": require_sha256(
            row["input_manifest_sha256"], path=f"{path}.input_manifest_sha256"
        ),
    }


def _validate_integrity(value: Any) -> dict[str, Any]:
    path = "$.integrity"
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "evaluation_config_sha256",
            "artifact_set_sha256",
            "source_usage_artifact_ids",
            "report_sha256",
        },
        path=path,
    )
    usage_artifacts = _validate_string_set(
        row["source_usage_artifact_ids"], path=f"{path}.source_usage_artifact_ids"
    )
    return {
        "evaluation_config_sha256": require_sha256(
            row["evaluation_config_sha256"], path=f"{path}.evaluation_config_sha256"
        ),
        "artifact_set_sha256": require_sha256(
            row["artifact_set_sha256"], path=f"{path}.artifact_set_sha256"
        ),
        "source_usage_artifact_ids": usage_artifacts,
        "report_sha256": require_sha256(
            row["report_sha256"], path=f"{path}.report_sha256"
        ),
    }


def _validate_arms(value: Any) -> list[dict[str, Any]]:
    path = "$.arms"
    rows = require_list(value, path=path)
    if not rows:
        raise ContractValidationError("empty_array", path, "at least one arm is required")
    result: list[dict[str, Any]] = []
    for index, raw_row in enumerate(rows):
        row_path = f"{path}[{index}]"
        row = require_mapping(raw_row, path=row_path)
        require_exact_keys(
            row,
            required={
                "arm_id",
                "role",
                "kind",
                "label",
                "translation_artifact_id",
                "translation_sha256",
            },
            path=row_path,
        )
        result.append(
            {
                "arm_id": require_string(row["arm_id"], path=f"{row_path}.arm_id"),
                "role": require_enum(
                    row["role"],
                    {"baseline", "candidate", "reference", "external_baseline"},
                    path=f"{row_path}.role",
                ),
                "kind": require_enum(
                    row["kind"],
                    {"system", "human_reference", "machine_baseline"},
                    path=f"{row_path}.kind",
                ),
                "label": require_string(row["label"], path=f"{row_path}.label"),
                "translation_artifact_id": require_string(
                    row["translation_artifact_id"],
                    path=f"{row_path}.translation_artifact_id",
                ),
                "translation_sha256": require_sha256(
                    row["translation_sha256"], path=f"{row_path}.translation_sha256"
                ),
            }
        )
    require_unique([row["arm_id"] for row in result], path=path)
    require_unique(
        [row["translation_artifact_id"] for row in result],
        path=f"{path}.translation_artifact_id",
    )
    roles = [row["role"] for row in result]
    if roles.count("baseline") > 1 or roles.count("candidate") > 1:
        raise ContractValidationError(
            "arm_roles", path, "baseline and candidate roles may appear at most once"
        )
    return result


def _validate_metrics(value: Any) -> list[dict[str, Any]]:
    path = "$.metrics"
    rows = require_list(value, path=path)
    result: list[dict[str, Any]] = []
    for index, raw_row in enumerate(rows):
        row_path = f"{path}[{index}]"
        row = require_mapping(raw_row, path=row_path)
        require_exact_keys(
            row,
            required={
                "metric_id",
                "display_name",
                "profile_scope",
                "status",
                "unit",
                "direction",
                "method",
                "arm_values",
                "comparison",
                "source_artifact_ids",
                "caveats",
            },
            path=row_path,
        )
        unit = require_enum(
            row["unit"],
            {"ratio", "score_0_100", "pairwise_counts", "categorical"},
            path=f"{row_path}.unit",
        )
        result.append(
            {
                "metric_id": require_string(
                    row["metric_id"], path=f"{row_path}.metric_id"
                ),
                "display_name": require_string(
                    row["display_name"], path=f"{row_path}.display_name"
                ),
                "profile_scope": require_enum(
                    row["profile_scope"], {"common", "d2l"}, path=f"{row_path}.profile_scope"
                ),
                "status": require_enum(
                    row["status"],
                    {"available", "not_run", "not_applicable", "failed", "missing_artifact"},
                    path=f"{row_path}.status",
                ),
                "unit": unit,
                "direction": require_enum(
                    row["direction"],
                    {"higher_is_better", "lower_is_better", "descriptive"},
                    path=f"{row_path}.direction",
                ),
                "method": validate_method(row["method"], path=f"{row_path}.method"),
                "arm_values": _validate_arm_values(
                    row["arm_values"], path=f"{row_path}.arm_values", unit=unit
                ),
                "comparison": _validate_comparison(
                    row["comparison"], path=f"{row_path}.comparison"
                ),
                "source_artifact_ids": _validate_string_set(
                    row["source_artifact_ids"], path=f"{row_path}.source_artifact_ids"
                ),
                "caveats": _validate_string_sequence(
                    row["caveats"], path=f"{row_path}.caveats"
                ),
            }
        )
    require_unique([row["metric_id"] for row in result], path=path)
    return result


def _validate_arm_values(value: Any, *, path: str, unit: str) -> list[dict[str, Any]]:
    rows = require_list(value, path=path)
    result: list[dict[str, Any]] = []
    for index, raw_row in enumerate(rows):
        row_path = f"{path}[{index}]"
        row = require_mapping(raw_row, path=row_path)
        require_exact_keys(
            row,
            required={
                "arm_id",
                "value",
                "numerator",
                "denominator",
                "interval_low",
                "interval_high",
                "interval_level",
            },
            path=row_path,
        )
        metric_value = _validate_metric_value(row["value"], path=f"{row_path}.value", unit=unit)
        numerator = require_nullable_number(
            row["numerator"], path=f"{row_path}.numerator", minimum=0
        )
        denominator = require_nullable_number(
            row["denominator"], path=f"{row_path}.denominator", minimum=0
        )
        low = require_nullable_number(row["interval_low"], path=f"{row_path}.interval_low")
        high = require_nullable_number(row["interval_high"], path=f"{row_path}.interval_high")
        level = require_nullable_number(
            row["interval_level"], path=f"{row_path}.interval_level", minimum=0
        )
        interval_values = (low, high, level)
        if any(item is None for item in interval_values) and any(
            item is not None for item in interval_values
        ):
            raise ContractValidationError(
                "interval", row_path, "interval fields must be all null or all present"
            )
        if low is not None and high is not None and low > high:
            raise ContractValidationError(
                "interval", row_path, "interval_low must not exceed interval_high"
            )
        if level is not None and level > 1:
            raise ContractValidationError(
                "interval", f"{row_path}.interval_level", "interval level must be <= 1"
            )
        result.append(
            {
                "arm_id": require_string(row["arm_id"], path=f"{row_path}.arm_id"),
                "value": metric_value,
                "numerator": numerator,
                "denominator": denominator,
                "interval_low": low,
                "interval_high": high,
                "interval_level": level,
            }
        )
    require_unique([row["arm_id"] for row in result], path=path)
    return result


def _validate_metric_value(value: Any, *, path: str, unit: str) -> Any:
    if value is None:
        return None
    if unit == "categorical":
        return require_string(value, path=path)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractValidationError("type", path, "numeric metric requires a number")
    if isinstance(value, float) and not math.isfinite(value):
        raise ContractValidationError("non_finite", path, "metric value must be finite")
    if unit == "ratio" and not 0 <= value <= 1:
        raise ContractValidationError("range", path, "ratio must be in [0, 1]")
    if unit == "score_0_100" and not 0 <= value <= 100:
        raise ContractValidationError("range", path, "score must be in [0, 100]")
    if unit == "pairwise_counts" and value < 0:
        raise ContractValidationError("range", path, "pairwise count must be non-negative")
    return value


def _validate_comparison(value: Any, *, path: str) -> dict[str, Any]:
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "status",
            "baseline_arm_id",
            "candidate_arm_id",
            "delta",
            "wins",
            "ties",
            "losses",
        },
        path=path,
    )
    status = require_enum(
        row["status"], {"available", "not_applicable", "insufficient"}, path=f"{path}.status"
    )
    result = {
        "status": status,
        "baseline_arm_id": require_nullable_string(
            row["baseline_arm_id"], path=f"{path}.baseline_arm_id"
        ),
        "candidate_arm_id": require_nullable_string(
            row["candidate_arm_id"], path=f"{path}.candidate_arm_id"
        ),
        "delta": require_nullable_number(row["delta"], path=f"{path}.delta"),
        "wins": require_nullable_int(row["wins"], path=f"{path}.wins", minimum=0),
        "ties": require_nullable_int(row["ties"], path=f"{path}.ties", minimum=0),
        "losses": require_nullable_int(row["losses"], path=f"{path}.losses", minimum=0),
    }
    comparison_values = (
        result["baseline_arm_id"],
        result["candidate_arm_id"],
        result["delta"],
    )
    if status == "available" and any(item is None for item in comparison_values):
        raise ContractValidationError(
            "comparison", path, "available comparison needs arm IDs and delta"
        )
    if status == "not_applicable" and any(
        result[field] is not None
        for field in (
            "baseline_arm_id",
            "candidate_arm_id",
            "delta",
            "wins",
            "ties",
            "losses",
        )
    ):
        raise ContractValidationError(
            "comparison", path, "not-applicable comparison fields must be null"
        )
    return result


def _validate_claim(value: Any) -> dict[str, Any]:
    path = "$.claim"
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "status",
            "verdict",
            "method_id",
            "method_version",
            "reason_codes",
            "source_metric_ids",
        },
        path=path,
    )
    status = require_enum(
            row["status"],
            {"available", "not_run", "not_applicable", "failed", "insufficient"},
            path=f"{path}.status",
        )
    verdict = require_enum(
            row["verdict"],
            {"BETTER", "NOT_BETTER", "INCONCLUSIVE", "NOT_APPLICABLE"},
            path=f"{path}.verdict",
        )
    if status == "not_applicable" and verdict != "NOT_APPLICABLE":
        raise ContractValidationError(
            "claim_status", path, "not-applicable claim must use NOT_APPLICABLE verdict"
        )
    if status in {"not_run", "failed", "insufficient"} and verdict != "INCONCLUSIVE":
        raise ContractValidationError(
            "claim_status", path, "unresolved claim status must use INCONCLUSIVE verdict"
        )
    if status == "available" and verdict == "NOT_APPLICABLE":
        raise ContractValidationError(
            "claim_status", path, "available claim cannot use NOT_APPLICABLE verdict"
        )
    return {
        "status": status,
        "verdict": verdict,
        "method_id": require_string(row["method_id"], path=f"{path}.method_id"),
        "method_version": require_string(
            row["method_version"], path=f"{path}.method_version"
        ),
        "reason_codes": _validate_string_set(
            row["reason_codes"], path=f"{path}.reason_codes"
        ),
        "source_metric_ids": _validate_string_set(
            row["source_metric_ids"], path=f"{path}.source_metric_ids"
        ),
    }


def _validate_usage(value: Any) -> dict[str, Any]:
    path = "$.usage"
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "status",
            "accounting_basis",
            "totals",
            "unknown_attempt_count",
            "by_stage",
            "notes",
            "source_artifact_ids",
        },
        path=path,
    )
    status = require_enum(
        row["status"],
        {"available", "partial", "unavailable", "not_applicable"},
        path=f"{path}.status",
    )
    totals = _validate_usage_totals(row["totals"], path=f"{path}.totals")
    by_stage = _validate_usage_stages(row["by_stage"], path=f"{path}.by_stage")
    if status in {"unavailable", "not_applicable"} and any(
        totals[field] is not None for field in (*_USAGE_NUMERIC_FIELDS, "cost_usd", "currency")
    ):
        raise ContractValidationError(
            "usage_unknown", f"{path}.totals", "unavailable usage facts must remain null"
        )
    accounting_basis = require_enum(
        row["accounting_basis"],
        {"provider_reported", "proxy_reported", "local_metered", "mixed", "unavailable"},
        path=f"{path}.accounting_basis",
    )
    if status in {"unavailable", "not_applicable"} and accounting_basis != "unavailable":
        raise ContractValidationError(
            "usage_basis", f"{path}.accounting_basis", "unavailable usage needs unavailable basis"
        )
    if status in {"available", "partial"} and accounting_basis == "unavailable":
        raise ContractValidationError(
            "usage_basis",
            f"{path}.accounting_basis",
            "available or partial usage needs a reported accounting basis",
        )
    unknown_attempt_count = require_int(
        row["unknown_attempt_count"], path=f"{path}.unknown_attempt_count", minimum=0
    )
    if status == "available" and unknown_attempt_count:
        raise ContractValidationError(
            "usage_status",
            f"{path}.unknown_attempt_count",
            "fully available usage cannot have unknown attempts",
        )
    return {
        "status": status,
        "accounting_basis": accounting_basis,
        "totals": totals,
        "unknown_attempt_count": unknown_attempt_count,
        "by_stage": by_stage,
        "notes": _validate_string_sequence(row["notes"], path=f"{path}.notes"),
        "source_artifact_ids": _validate_string_set(
            row["source_artifact_ids"], path=f"{path}.source_artifact_ids"
        ),
    }


def _validate_usage_totals(value: Any, *, path: str) -> dict[str, Any]:
    row = require_mapping(value, path=path)
    required = {*_USAGE_NUMERIC_FIELDS, "cost_usd", "currency"}
    require_exact_keys(row, required=required, path=path)
    result = {
        field: require_nullable_int(row[field], path=f"{path}.{field}", minimum=0)
        for field in _USAGE_NUMERIC_FIELDS
    }
    result["cost_usd"] = require_nullable_number(
        row["cost_usd"], path=f"{path}.cost_usd", minimum=0
    )
    result["currency"] = require_nullable_string(row["currency"], path=f"{path}.currency")
    if (result["cost_usd"] is None) != (result["currency"] is None):
        raise ContractValidationError(
            "usage_currency", path, "cost_usd and currency must be null or present together"
        )
    return result


def _validate_usage_stages(value: Any, *, path: str) -> list[dict[str, Any]]:
    rows = require_list(value, path=path)
    result: list[dict[str, Any]] = []
    for index, raw_row in enumerate(rows):
        row_path = f"{path}[{index}]"
        row = require_mapping(raw_row, path=row_path)
        required = {
            "stage_id",
            "provider",
            "model_id",
            "quota_bucket_id",
            "credential_family",
            "accounting_basis",
            "status",
            *_USAGE_NUMERIC_FIELDS,
            "cost_usd",
            "currency",
        }
        require_exact_keys(row, required=required, path=row_path)
        totals = _validate_usage_totals(
            {field: row[field] for field in (*_USAGE_NUMERIC_FIELDS, "cost_usd", "currency")},
            path=row_path,
        )
        status = require_enum(
            row["status"],
            {"available", "partial", "unavailable", "not_applicable"},
            path=f"{row_path}.status",
        )
        if status in {"unavailable", "not_applicable"} and any(
            totals[field] is not None
            for field in (*_USAGE_NUMERIC_FIELDS, "cost_usd", "currency")
        ):
            raise ContractValidationError(
                "usage_unknown", row_path, "unavailable stage usage must remain null"
            )
        accounting_basis = require_enum(
            row["accounting_basis"],
            {"provider_reported", "proxy_reported", "local_metered", "mixed", "unavailable"},
            path=f"{row_path}.accounting_basis",
        )
        if status in {"unavailable", "not_applicable"} and accounting_basis != "unavailable":
            raise ContractValidationError(
                "usage_basis",
                f"{row_path}.accounting_basis",
                "unavailable usage needs unavailable basis",
            )
        if status in {"available", "partial"} and accounting_basis == "unavailable":
            raise ContractValidationError(
                "usage_basis",
                f"{row_path}.accounting_basis",
                "available or partial stage usage needs a reported accounting basis",
            )
        result.append(
            {
                "stage_id": require_string(row["stage_id"], path=f"{row_path}.stage_id"),
                "provider": require_nullable_string(
                    row["provider"], path=f"{row_path}.provider"
                ),
                "model_id": require_nullable_string(
                    row["model_id"], path=f"{row_path}.model_id"
                ),
                "quota_bucket_id": require_nullable_string(
                    row["quota_bucket_id"], path=f"{row_path}.quota_bucket_id"
                ),
                "credential_family": require_nullable_string(
                    row["credential_family"], path=f"{row_path}.credential_family"
                ),
                "accounting_basis": accounting_basis,
                "status": status,
                **totals,
            }
        )
    require_unique([row["stage_id"] for row in result], path=path)
    return result


def _validate_stages(value: Any) -> list[dict[str, Any]]:
    path = "$.stages"
    rows = require_list(value, path=path)
    result: list[dict[str, Any]] = []
    for index, raw_row in enumerate(rows):
        row_path = f"{path}[{index}]"
        row = require_mapping(raw_row, path=row_path)
        require_exact_keys(
            row,
            required={
                "stage_id",
                "status",
                "started_at",
                "ended_at",
                "duration_ms",
                "attempt_run_id",
                "artifact_ids",
                "error_code",
            },
            path=row_path,
        )
        started = _validate_nullable_timestamp(row["started_at"], path=f"{row_path}.started_at")
        ended = _validate_nullable_timestamp(row["ended_at"], path=f"{row_path}.ended_at")
        duration = require_nullable_int(
            row["duration_ms"], path=f"{row_path}.duration_ms", minimum=0
        )
        if (started is None) != (ended is None):
            raise ContractValidationError(
                "stage_time", row_path, "started_at and ended_at must be null or present together"
            )
        result.append(
            {
                "stage_id": require_string(row["stage_id"], path=f"{row_path}.stage_id"),
                "status": require_enum(
                    row["status"],
                    {"complete", "partial", "failed", "not_run", "not_applicable"},
                    path=f"{row_path}.status",
                ),
                "started_at": started,
                "ended_at": ended,
                "duration_ms": duration,
                "attempt_run_id": require_nullable_string(
                    row["attempt_run_id"], path=f"{row_path}.attempt_run_id"
                ),
                "artifact_ids": _validate_string_set(
                    row["artifact_ids"], path=f"{row_path}.artifact_ids"
                ),
                "error_code": require_nullable_string(
                    row["error_code"], path=f"{row_path}.error_code"
                ),
            }
        )
    require_unique([row["stage_id"] for row in result], path=path)
    return result


def _validate_artifacts(value: Any) -> list[dict[str, Any]]:
    path = "$.artifacts"
    rows = require_list(value, path=path)
    result: list[dict[str, Any]] = []
    for index, raw_row in enumerate(rows):
        row_path = f"{path}[{index}]"
        row = require_mapping(raw_row, path=row_path)
        require_exact_keys(
            row,
            required={
                "artifact_id",
                "kind",
                "requirement",
                "status",
                "relative_path",
                "sha256",
                "producer_method_id",
                "error_code",
            },
            path=row_path,
        )
        status = require_enum(
            row["status"],
            {"present", "missing", "failed", "not_applicable"},
            path=f"{row_path}.status",
        )
        relative_path = None
        if row["relative_path"] is not None:
            relative_path = require_relative_path(
                row["relative_path"], path=f"{row_path}.relative_path"
            )
        digest = None if row["sha256"] is None else require_sha256(
            row["sha256"], path=f"{row_path}.sha256"
        )
        error_code = require_nullable_string(
            row["error_code"], path=f"{row_path}.error_code"
        )
        if status == "present" and (relative_path is None or digest is None):
            raise ContractValidationError(
                "artifact_presence", row_path, "present artifacts need path and hash"
            )
        if status != "present" and digest is not None:
            raise ContractValidationError(
                "artifact_presence", f"{row_path}.sha256", "absent artifacts cannot have a hash"
            )
        if status == "missing" and relative_path is not None:
            raise ContractValidationError(
                "artifact_presence",
                f"{row_path}.relative_path",
                "missing artifact path must be null",
            )
        if status == "failed" and error_code is None:
            raise ContractValidationError(
                "artifact_error", f"{row_path}.error_code", "failed artifact needs an error code"
            )
        result.append(
            {
                "artifact_id": require_string(
                    row["artifact_id"], path=f"{row_path}.artifact_id"
                ),
                "kind": require_enum(
                    row["kind"],
                    {
                        "evaluation_input",
                        "translation",
                        "human_reference",
                        "machine_baseline",
                        "metric_report",
                        "judge_report",
                        "usage_ledger",
                        "audit",
                        "other",
                    },
                    path=f"{row_path}.kind",
                ),
                "requirement": require_enum(
                    row["requirement"], {"required", "optional"}, path=f"{row_path}.requirement"
                ),
                "status": status,
                "relative_path": relative_path,
                "sha256": digest,
                "producer_method_id": require_nullable_string(
                    row["producer_method_id"], path=f"{row_path}.producer_method_id"
                ),
                "error_code": error_code,
            }
        )
    require_unique([row["artifact_id"] for row in result], path=path)
    present_paths = [row["relative_path"] for row in result if row["relative_path"] is not None]
    require_unique(present_paths, path=path)
    return result


def _validate_references_and_semantics(payload: Mapping[str, Any]) -> None:
    arms = {row["arm_id"]: row for row in payload["arms"]}
    metrics = {row["metric_id"]: row for row in payload["metrics"]}
    artifacts = {row["artifact_id"]: row for row in payload["artifacts"]}
    attempts = set(payload["identity"]["attempt_run_ids"])

    for index, arm in enumerate(payload["arms"]):
        artifact = _require_artifact(
            artifacts, arm["translation_artifact_id"], f"$.arms[{index}].translation_artifact_id"
        )
        expected_kind = {
            "system": "translation",
            "human_reference": "human_reference",
            "machine_baseline": "machine_baseline",
        }[arm["kind"]]
        if artifact["kind"] != expected_kind or artifact["status"] != "present":
            raise ContractValidationError(
                "arm_artifact",
                f"$.arms[{index}].translation_artifact_id",
                "arm must reference a present artifact of the matching kind",
            )
        if artifact["sha256"] != arm["translation_sha256"]:
            raise ContractValidationError(
                "translation_hash",
                f"$.arms[{index}].translation_sha256",
                "arm hash does not match artifact hash",
            )

    for metric_index, metric in enumerate(payload["metrics"]):
        metric_path = f"$.metrics[{metric_index}]"
        if metric["status"] != "available" and any(
            row["value"] is not None
            or row["numerator"] is not None
            or row["denominator"] is not None
            or row["interval_low"] is not None
            or row["interval_high"] is not None
            or row["interval_level"] is not None
            for row in metric["arm_values"]
        ):
            raise ContractValidationError(
                "metric_status",
                f"{metric_path}.arm_values",
                "unavailable metrics cannot carry measured values",
            )
        for value in metric["arm_values"]:
            if value["arm_id"] not in arms:
                raise ContractValidationError(
                    "arm_reference", f"{metric_path}.arm_values", "unknown arm"
                )
        comparison = metric["comparison"]
        for field, expected_role in (
            ("baseline_arm_id", "baseline"),
            ("candidate_arm_id", "candidate"),
        ):
            referenced_arm_id = comparison[field]
            if referenced_arm_id is None:
                continue
            referenced_arm = arms.get(referenced_arm_id)
            if referenced_arm is None:
                raise ContractValidationError(
                    "arm_reference",
                    f"{metric_path}.comparison.{field}",
                    "comparison references an unknown arm",
                )
            if referenced_arm["role"] != expected_role:
                raise ContractValidationError(
                    "comparison_role",
                    f"{metric_path}.comparison.{field}",
                    f"comparison {field} must reference the {expected_role} arm",
                )
        for artifact_id in metric["source_artifact_ids"]:
            _require_artifact(artifacts, artifact_id, f"{metric_path}.source_artifact_ids")

    for metric_id in payload["claim"]["source_metric_ids"]:
        if metric_id not in metrics:
            raise ContractValidationError(
                "metric_reference",
                "$.claim.source_metric_ids",
                "claim references an unknown metric",
            )
    if payload["claim"]["verdict"] in {"BETTER", "NOT_BETTER"}:
        if not payload["claim"]["source_metric_ids"]:
            raise ContractValidationError(
                "claim_evidence",
                "$.claim.source_metric_ids",
                "a comparative verdict needs at least one source metric",
            )
        unavailable_sources = [
            metric_id
            for metric_id in payload["claim"]["source_metric_ids"]
            if metrics[metric_id]["status"] != "available"
        ]
        if unavailable_sources:
            raise ContractValidationError(
                "claim_evidence",
                "$.claim.source_metric_ids",
                "comparative verdict cites unavailable metrics: "
                + ", ".join(unavailable_sources),
            )
    for artifact_id in payload["usage"]["source_artifact_ids"]:
        _require_artifact(artifacts, artifact_id, "$.usage.source_artifact_ids")
    for artifact_id in payload["integrity"]["source_usage_artifact_ids"]:
        artifact = _require_artifact(
            artifacts, artifact_id, "$.integrity.source_usage_artifact_ids"
        )
        if artifact["kind"] != "usage_ledger":
            raise ContractValidationError(
                "artifact_kind",
                "$.integrity.source_usage_artifact_ids",
                "usage provenance must reference usage ledgers",
            )
    if set(payload["integrity"]["source_usage_artifact_ids"]) != set(
        payload["usage"]["source_artifact_ids"]
    ):
        raise ContractValidationError(
            "usage_provenance",
            "$.integrity.source_usage_artifact_ids",
            "integrity and usage must name the same persisted usage sources",
        )
    stage_ids = {row["stage_id"] for row in payload["stages"]}
    for usage_index, usage_stage in enumerate(payload["usage"]["by_stage"]):
        if usage_stage["stage_id"] not in stage_ids:
            raise ContractValidationError(
                "stage_reference",
                f"$.usage.by_stage[{usage_index}].stage_id",
                "usage row references an unknown stage",
            )
    for stage_index, stage in enumerate(payload["stages"]):
        if stage["attempt_run_id"] is not None and stage["attempt_run_id"] not in attempts:
            raise ContractValidationError(
                "attempt_reference",
                f"$.stages[{stage_index}].attempt_run_id",
                "stage references an unknown attempt",
            )
        for artifact_id in stage["artifact_ids"]:
            _require_artifact(artifacts, artifact_id, f"$.stages[{stage_index}].artifact_ids")

    if len(arms) == 1:
        for metric_index, metric in enumerate(payload["metrics"]):
            if metric["comparison"]["status"] != "not_applicable":
                raise ContractValidationError(
                    "one_arm_comparison",
                    f"$.metrics[{metric_index}].comparison.status",
                    "one-arm reports cannot publish a comparison",
                )
        if payload["claim"]["verdict"] not in {"NOT_APPLICABLE", "INCONCLUSIVE"}:
            raise ContractValidationError(
                "one_arm_claim", "$.claim.verdict", "one-arm report cannot claim better/not-better"
            )

    if payload["report_state"] == "complete":
        broken_required = [
            row["artifact_id"]
            for row in payload["artifacts"]
            if row["requirement"] == "required" and row["status"] != "present"
        ]
        if broken_required:
            raise ContractValidationError(
                "report_state",
                "$.report_state",
                "complete report has missing or failed required artifacts",
            )


def _validate_string_set(value: Any, *, path: str) -> list[str]:
    rows = [
        require_string(item, path=f"{path}[{index}]")
        for index, item in enumerate(require_list(value, path=path))
    ]
    require_unique(rows, path=path)
    return rows


def _validate_string_sequence(value: Any, *, path: str) -> list[str]:
    return [
        require_string(item, path=f"{path}[{index}]")
        for index, item in enumerate(require_list(value, path=path))
    ]


def _validate_nullable_timestamp(value: Any, *, path: str) -> str | None:
    if value is None:
        return None
    return require_rfc3339(value, path=path)


def _require_artifact(
    artifacts: Mapping[str, Mapping[str, Any]], artifact_id: str, path: str
) -> Mapping[str, Any]:
    artifact = artifacts.get(artifact_id)
    if artifact is None:
        raise ContractValidationError(
            "artifact_reference", path, f"unknown artifact: {artifact_id}"
        )
    return artifact
