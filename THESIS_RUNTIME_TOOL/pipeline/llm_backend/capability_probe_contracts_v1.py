"""Closed records for one-shot Shared LLM capability qualification.

Capability probes are deliberately separate from normal run seals. An unknown
source may execute one bounded probe, but the resulting provider payload has no
pipeline authority. Only a validated probe receipt may back a qualified
CapabilityEvidenceV1 record consumed by the normal resolver.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import re
from typing import Any, Mapping

from .contracts_v1 import (
    ContractValidationError,
    canonical_json,
    canonical_sha256,
    validate_api_source,
    validate_capability_evidence,
)


CAPABILITY_PROBE_SEAL_SCHEMA_VERSION = "capability_probe_seal_v1"
CAPABILITY_PROBE_RECEIPT_SCHEMA_VERSION = "capability_probe_receipt_v1"

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,191}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_WORKSTREAMS = frozenset({"d2l", "literary", "input_normalization", "evaluation"})
_CAPABILITY_KINDS = frozenset({"json_object", "native_structured_output"})
_SCHEMA_DIALECTS = frozenset(
    {"json_schema_2020_12", "openai_strict_json_schema_subset_v1"}
)
_OUTCOMES = frozenset({"qualified", "failed"})
_FAILURE_CATEGORIES = frozenset(
    {
        "transport",
        "response_contract",
        "model_identity",
        "usage_limits",
    }
)
_FINISH_REASONS = frozenset(
    {"stop", "length", "content_filter", "tool_call", "safety", "error", "unknown"}
)


def validate_capability_probe_seal(value: Mapping[str, Any]) -> dict[str, Any]:
    row = _closed_object(
        value,
        {
            "schema_version",
            "probe_run_id",
            "consumer_workstream",
            "role_id",
            "probe_profile_id",
            "probe_profile_revision",
            "implementation_binding",
            "authority",
            "source_binding",
            "capability_intent",
            "response_schema",
            "request_body_sha256",
            "limits",
            "issued_at_utc",
            "seal_sha256",
        },
        "capability probe seal",
    )
    _expect(row["schema_version"], CAPABILITY_PROBE_SEAL_SCHEMA_VERSION, "schema")
    row["probe_run_id"] = _identifier(row["probe_run_id"], "probe_run_id")
    row["consumer_workstream"] = _enum(
        row["consumer_workstream"], _WORKSTREAMS, "consumer_workstream"
    )
    row["role_id"] = _identifier(row["role_id"], "role_id")
    if not row["role_id"].startswith(f"{row['consumer_workstream']}."):
        raise ContractValidationError("probe role_id belongs to another workstream")
    row["probe_profile_id"] = _identifier(
        row["probe_profile_id"], "probe_profile_id"
    )
    row["probe_profile_revision"] = _identifier(
        row["probe_profile_revision"], "probe_profile_revision"
    )
    row["implementation_binding"] = _validate_implementation_binding(
        row["implementation_binding"]
    )
    _expect(row["authority"], "capability_only", "probe authority")

    source_binding = _closed_object(
        row["source_binding"], {"record_sha256", "record"}, "source_binding"
    )
    source = validate_api_source(source_binding["record"])
    source_sha256 = _sha256(source_binding["record_sha256"], "source record hash")
    if canonical_sha256(source) != source_sha256:
        raise ContractValidationError("probe source record hash mismatch")
    if not source["enabled"]:
        raise ContractValidationError("disabled API source cannot be probed")
    row["source_binding"] = {"record_sha256": source_sha256, "record": source}

    intent = _validate_capability_intent(row["capability_intent"])
    schema = deepcopy(row["response_schema"])
    if not isinstance(schema, Mapping):
        raise ContractValidationError("response_schema must be an object")
    canonical_json(schema)
    if canonical_sha256(schema) != intent["schema_sha256"]:
        raise ContractValidationError("response schema hash differs from capability intent")
    row["capability_intent"] = intent
    row["response_schema"] = dict(schema)
    row["request_body_sha256"] = _sha256(
        row["request_body_sha256"], "request_body_sha256"
    )
    row["limits"] = _validate_limits(row["limits"])
    row["issued_at_utc"] = _utc_timestamp(row["issued_at_utc"], "issued_at_utc")
    row["seal_sha256"] = _sha256(row["seal_sha256"], "seal_sha256")
    expected = canonical_sha256(
        {key: item for key, item in row.items() if key != "seal_sha256"}
    )
    if row["seal_sha256"] != expected:
        raise ContractValidationError("capability probe seal hash mismatch")
    return row


def validate_capability_probe_receipt(value: Mapping[str, Any]) -> dict[str, Any]:
    row = _closed_object(
        value,
        {
            "schema_version",
            "receipt_sha256",
            "probe_seal_sha256",
            "probe_run_id",
            "authority",
            "source_record_sha256",
            "source_id",
            "source_revision",
            "physical_quota_bucket_id",
            "capability_id",
            "capability_revision",
            "request_body_sha256",
            "requested_model_id",
            "observed_model_id",
            "provider_called",
            "physical_attempt_index",
            "request_id",
            "started_at_utc",
            "finished_at_utc",
            "latency_ms",
            "outcome",
            "finish_reason",
            "prompt_tokens",
            "cached_input_tokens",
            "completion_tokens",
            "reasoning_tokens",
            "total_tokens",
            "cost_usd",
            "cost_status",
            "cost_provenance",
            "raw_response_sha256",
            "response_artifact_sha256",
            "parsed_content_sha256",
            "response_contract_validated",
            "failure",
        },
        "capability probe receipt",
    )
    _expect(
        row["schema_version"],
        CAPABILITY_PROBE_RECEIPT_SCHEMA_VERSION,
        "receipt schema",
    )
    for field in (
        "receipt_sha256",
        "probe_seal_sha256",
        "source_record_sha256",
        "request_body_sha256",
    ):
        row[field] = _sha256(row[field], field)
    for field in (
        "probe_run_id",
        "source_id",
        "source_revision",
        "physical_quota_bucket_id",
        "capability_id",
        "capability_revision",
    ):
        row[field] = _identifier(row[field], field)
    _expect(row["authority"], "capability_only", "receipt authority")
    row["requested_model_id"] = _model_id(
        row["requested_model_id"], "requested_model_id"
    )
    if row["observed_model_id"] is not None:
        row["observed_model_id"] = _model_id(
            row["observed_model_id"], "observed_model_id"
        )
    row["provider_called"] = _boolean(row["provider_called"], "provider_called")
    if row["provider_called"] is not True:
        raise ContractValidationError("probe receipt must represent one provider call")
    row["physical_attempt_index"] = _integer(
        row["physical_attempt_index"], "physical_attempt_index", minimum=1, maximum=1
    )
    if row["request_id"] is not None:
        row["request_id"] = _trimmed(row["request_id"], "request_id", maximum=512)
    row["started_at_utc"] = _utc_timestamp(row["started_at_utc"], "started_at_utc")
    row["finished_at_utc"] = _utc_timestamp(
        row["finished_at_utc"], "finished_at_utc"
    )
    started = _utc_datetime(row["started_at_utc"], "started_at_utc")
    finished = _utc_datetime(row["finished_at_utc"], "finished_at_utc")
    if finished < started:
        raise ContractValidationError("probe receipt clock moved backwards")
    row["latency_ms"] = _integer(row["latency_ms"], "latency_ms", minimum=0)
    expected_latency = int((finished - started).total_seconds() * 1000)
    if row["latency_ms"] != expected_latency:
        raise ContractValidationError("probe latency differs from receipt timestamps")
    row["outcome"] = _enum(row["outcome"], _OUTCOMES, "outcome")
    row["finish_reason"] = _enum(
        row["finish_reason"], _FINISH_REASONS, "finish_reason"
    )

    for field in (
        "prompt_tokens",
        "cached_input_tokens",
        "completion_tokens",
        "reasoning_tokens",
        "total_tokens",
    ):
        if row[field] is not None:
            row[field] = _integer(row[field], field, minimum=0)
    if (
        row["cached_input_tokens"] is not None
        and row["prompt_tokens"] is not None
        and row["cached_input_tokens"] > row["prompt_tokens"]
    ):
        raise ContractValidationError("cached_input_tokens exceeds prompt_tokens")
    if (
        row["reasoning_tokens"] is not None
        and row["completion_tokens"] is not None
        and row["reasoning_tokens"] > row["completion_tokens"]
    ):
        raise ContractValidationError("reasoning_tokens exceeds completion_tokens")
    if all(row[field] is not None for field in ("prompt_tokens", "completion_tokens", "total_tokens")):
        if row["total_tokens"] != row["prompt_tokens"] + row["completion_tokens"]:
            raise ContractValidationError("probe total_tokens is inconsistent")

    _validate_cost(row)
    for field in (
        "raw_response_sha256",
        "response_artifact_sha256",
        "parsed_content_sha256",
    ):
        if row[field] is not None:
            row[field] = _sha256(row[field], field)
    if (row["raw_response_sha256"] is None) != (
        row["response_artifact_sha256"] is None
    ):
        raise ContractValidationError("raw response and artifact hashes must co-exist")
    if (
        row["raw_response_sha256"] is not None
        and row["raw_response_sha256"] != row["response_artifact_sha256"]
    ):
        raise ContractValidationError("probe response artifact is not content addressed")
    row["response_contract_validated"] = _boolean(
        row["response_contract_validated"], "response_contract_validated"
    )
    if row["failure"] is not None:
        row["failure"] = _validate_failure(row["failure"])

    if row["outcome"] == "qualified":
        if row["failure"] is not None:
            raise ContractValidationError("qualified probe may not contain failure evidence")
        if not row["response_contract_validated"]:
            raise ContractValidationError("qualified probe lacks response contract validation")
        if row["observed_model_id"] is None or row["parsed_content_sha256"] is None:
            raise ContractValidationError("qualified probe lacks response identity")
        if row["finish_reason"] != "stop":
            raise ContractValidationError("qualified probe did not finish normally")
        if any(
            row[field] is None
            for field in ("prompt_tokens", "completion_tokens", "total_tokens")
        ):
            raise ContractValidationError("qualified probe lacks certifiable token usage")
    else:
        if row["failure"] is None:
            raise ContractValidationError("failed probe lacks failure evidence")
        if row["response_contract_validated"]:
            raise ContractValidationError("failed probe cannot validate its response contract")

    expected = canonical_sha256(
        {key: item for key, item in row.items() if key != "receipt_sha256"}
    )
    if row["receipt_sha256"] != expected:
        raise ContractValidationError("capability probe receipt hash mismatch")
    return row


def validate_capability_probe_bundle(
    *,
    seal: Mapping[str, Any],
    receipt: Mapping[str, Any],
    capability_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    normalized_seal = validate_capability_probe_seal(seal)
    normalized_receipt = validate_capability_probe_receipt(receipt)
    evidence = validate_capability_evidence(capability_evidence)
    source = normalized_seal["source_binding"]["record"]
    intent = normalized_seal["capability_intent"]

    exact_receipt = {
        "probe_seal_sha256": normalized_seal["seal_sha256"],
        "probe_run_id": normalized_seal["probe_run_id"],
        "source_record_sha256": normalized_seal["source_binding"]["record_sha256"],
        "source_id": source["source_id"],
        "source_revision": source["source_revision"],
        "physical_quota_bucket_id": source["physical_quota_bucket_id"],
        "capability_id": intent["capability_id"],
        "capability_revision": intent["capability_revision"],
        "request_body_sha256": normalized_seal["request_body_sha256"],
        "requested_model_id": intent["requested_model_id"],
    }
    for field, expected in exact_receipt.items():
        if normalized_receipt[field] != expected:
            raise ContractValidationError(f"probe receipt {field} differs from seal")
    if normalized_receipt["outcome"] == "qualified":
        if normalized_receipt["observed_model_id"] not in intent[
            "accepted_observed_model_ids"
        ]:
            raise ContractValidationError("qualified probe observed an unaccepted model")
        _validate_receipt_limits(normalized_receipt, normalized_seal["limits"])

    expected_evidence = {
        "capability_id": intent["capability_id"],
        "capability_revision": intent["capability_revision"],
        "source_id": source["source_id"],
        "source_revision": source["source_revision"],
        "adapter_id": source["adapter_id"],
        "protocol": source["protocol"],
        "route_id": source["route_id"],
        "base_url": source["base_url"],
        "requested_model_id": intent["requested_model_id"],
        "observed_model_id": normalized_receipt["observed_model_id"],
        "capability_kind": intent["capability_kind"],
        "schema_dialect": intent["schema_dialect"],
        "schema_sha256": intent["schema_sha256"],
        "local_validator_id": intent["local_validator_id"],
        "local_validator_sha256": intent["local_validator_sha256"],
        "probe_id": normalized_seal["probe_run_id"],
        "evidence_sha256": normalized_receipt["receipt_sha256"],
        "observed_at_utc": normalized_receipt["finished_at_utc"],
        "verdict": normalized_receipt["outcome"],
    }
    for field, expected in expected_evidence.items():
        if evidence[field] != expected:
            raise ContractValidationError(f"capability evidence {field} differs from probe")
    return {
        "seal": normalized_seal,
        "receipt": normalized_receipt,
        "capability_evidence": evidence,
    }


def _validate_capability_intent(value: Any) -> dict[str, Any]:
    row = _closed_object(
        value,
        {
            "capability_id",
            "capability_revision",
            "requested_model_id",
            "accepted_observed_model_ids",
            "capability_kind",
            "schema_name",
            "schema_dialect",
            "schema_sha256",
            "local_validator_id",
            "local_validator_sha256",
        },
        "capability_intent",
    )
    for field in (
        "capability_id",
        "capability_revision",
        "schema_name",
        "schema_dialect",
        "local_validator_id",
    ):
        row[field] = _identifier(row[field], field)
    row["schema_dialect"] = _enum(
        row["schema_dialect"], _SCHEMA_DIALECTS, "schema_dialect"
    )
    row["requested_model_id"] = _model_id(
        row["requested_model_id"], "requested_model_id"
    )
    observed = row["accepted_observed_model_ids"]
    if not isinstance(observed, list) or not observed:
        raise ContractValidationError("accepted_observed_model_ids must be nonempty")
    normalized_observed = [
        _model_id(item, "accepted_observed_model_ids item") for item in observed
    ]
    if normalized_observed != sorted(set(normalized_observed)):
        raise ContractValidationError("accepted_observed_model_ids must be sorted unique")
    if row["requested_model_id"] not in normalized_observed:
        raise ContractValidationError("requested model must be an accepted observed model")
    row["accepted_observed_model_ids"] = normalized_observed
    row["capability_kind"] = _enum(
        row["capability_kind"], _CAPABILITY_KINDS, "capability_kind"
    )
    row["schema_sha256"] = _sha256(row["schema_sha256"], "schema_sha256")
    row["local_validator_sha256"] = _sha256(
        row["local_validator_sha256"], "local_validator_sha256"
    )
    return row


def _validate_implementation_binding(value: Any) -> dict[str, Any]:
    row = _closed_object(
        value,
        {
            "shared_core_revision",
            "consumer_revision",
            "consumer_implementation_sha256",
        },
        "implementation_binding",
    )
    for field in ("shared_core_revision", "consumer_revision"):
        revision = row[field]
        if (
            not isinstance(revision, str)
            or len(revision) not in {40, 64}
            or any(character not in "0123456789abcdef" for character in revision)
        ):
            raise ContractValidationError(
                f"{field} must be an exact lowercase Git object ID"
            )
    row["consumer_implementation_sha256"] = _sha256(
        row["consumer_implementation_sha256"],
        "consumer_implementation_sha256",
    )
    return row


def _validate_limits(value: Any) -> dict[str, Any]:
    row = _closed_object(
        value,
        {
            "max_calls",
            "max_prompt_utf8_bytes",
            "max_response_utf8_bytes",
            "max_prompt_tokens",
            "max_completion_tokens",
            "max_total_tokens",
            "request_timeout_ms",
        },
        "probe limits",
    )
    row["max_calls"] = _integer(row["max_calls"], "max_calls", minimum=1, maximum=1)
    maxima = {
        "max_prompt_utf8_bytes": 65_536,
        "max_response_utf8_bytes": 65_536,
        "max_prompt_tokens": 16_000,
        "max_completion_tokens": 4_000,
        "max_total_tokens": 20_000,
        "request_timeout_ms": 120_000,
    }
    for field, maximum in maxima.items():
        row[field] = _integer(row[field], field, minimum=1, maximum=maximum)
    if row["max_total_tokens"] < (
        row["max_prompt_tokens"] + row["max_completion_tokens"]
    ):
        raise ContractValidationError(
            "max_total_tokens cannot undercut prompt plus completion caps"
        )
    return row


def _validate_receipt_limits(receipt: Mapping[str, Any], limits: Mapping[str, Any]) -> None:
    comparisons = (
        ("prompt_tokens", "max_prompt_tokens"),
        ("completion_tokens", "max_completion_tokens"),
        ("total_tokens", "max_total_tokens"),
    )
    for usage_field, limit_field in comparisons:
        if receipt[usage_field] is None or receipt[usage_field] > limits[limit_field]:
            raise ContractValidationError(f"qualified probe exceeds {limit_field}")


def _validate_failure(value: Any) -> dict[str, Any]:
    row = _closed_object(
        value,
        {"category", "code", "safe_message", "details_sha256"},
        "probe failure",
    )
    row["category"] = _enum(row["category"], _FAILURE_CATEGORIES, "failure category")
    row["code"] = _identifier(row["code"], "failure code")
    row["safe_message"] = _trimmed(
        row["safe_message"], "failure safe_message", maximum=512
    )
    row["details_sha256"] = _sha256(row["details_sha256"], "failure details_sha256")
    return row


def _validate_cost(row: dict[str, Any]) -> None:
    row["cost_status"] = _enum(
        row["cost_status"], frozenset({"reported", "calculated", "unknown"}), "cost_status"
    )
    provenance = _closed_object(
        row["cost_provenance"],
        {"kind", "reference_id", "reference_sha256"},
        "cost_provenance",
    )
    provenance["kind"] = _enum(
        provenance["kind"],
        frozenset({"provider_reported", "pricing_manifest", "unavailable"}),
        "cost provenance kind",
    )
    if row["cost_status"] == "unknown":
        if row["cost_usd"] is not None or provenance != {
            "kind": "unavailable",
            "reference_id": None,
            "reference_sha256": None,
        }:
            raise ContractValidationError("unknown probe cost requires unavailable provenance")
    else:
        if not isinstance(row["cost_usd"], (int, float)) or isinstance(row["cost_usd"], bool):
            raise ContractValidationError("known probe cost must be numeric")
        if row["cost_usd"] < 0:
            raise ContractValidationError("known probe cost cannot be negative")
        provenance["reference_id"] = _identifier(
            provenance["reference_id"], "cost provenance reference_id"
        )
        provenance["reference_sha256"] = _sha256(
            provenance["reference_sha256"], "cost provenance reference_sha256"
        )
        expected_kind = "provider_reported" if row["cost_status"] == "reported" else "pricing_manifest"
        if provenance["kind"] != expected_kind:
            raise ContractValidationError("probe cost status and provenance differ")
    row["cost_provenance"] = provenance


def _closed_object(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractValidationError(f"{label} must be an object")
    row = deepcopy(dict(value))
    missing = keys - set(row)
    extra = set(row) - keys
    if missing or extra:
        raise ContractValidationError(
            f"{label} fields differ; missing={sorted(missing)}, extra={sorted(extra)}"
        )
    return row


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ContractValidationError(f"{label} must be a normalized identifier")
    return value


def _model_id(value: Any, label: str) -> str:
    value = _trimmed(value, label, maximum=256)
    if "latest" in value.casefold() or any(
        ord(character) < 0x21 or ord(character) > 0x7E for character in value
    ):
        raise ContractValidationError(f"{label} must be an exact pinned model ID")
    return value


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ContractValidationError(f"{label} must be lowercase SHA-256")
    return value


def _trimmed(value: Any, label: str, *, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 0x20 for character in value)
    ):
        raise ContractValidationError(f"{label} must be a safe nonempty string")
    return value


def _integer(value: Any, label: str, *, minimum: int, maximum: int | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ContractValidationError(f"{label} must be an integer >= {minimum}")
    if maximum is not None and value > maximum:
        raise ContractValidationError(f"{label} must be <= {maximum}")
    return value


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ContractValidationError(f"{label} must be boolean")
    return value


def _enum(value: Any, options: frozenset[str], label: str) -> str:
    if value not in options:
        raise ContractValidationError(f"{label} has unsupported value")
    return value


def _expect(value: Any, expected: str, label: str) -> None:
    if value != expected:
        raise ContractValidationError(f"{label} must equal {expected}")


def _utc_timestamp(value: Any, label: str) -> str:
    _utc_datetime(value, label)
    return value


def _utc_datetime(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ContractValidationError(f"{label} must be a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ContractValidationError(f"{label} must be a UTC timestamp") from exc
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise ContractValidationError(f"{label} must be UTC")
    return parsed
