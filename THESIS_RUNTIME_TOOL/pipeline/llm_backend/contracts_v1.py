"""Closed, provider-neutral records for thesis LLM execution.

This module deliberately contains no provider SDK, credential loader, scheduler,
cache implementation, checkpoint writer, or pipeline semantic logic.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import ipaddress
import json
import math
import re
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit


API_SOURCE_SCHEMA_VERSION = "api_source_v1"
CAPABILITY_EVIDENCE_SCHEMA_VERSION = "capability_evidence_v1"
PIPELINE_PROFILE_SCHEMA_VERSION = "pipeline_profile_v1"
LLM_ATTEMPT_USAGE_SCHEMA_VERSION = "llm_attempt_usage_v1"
LLM_ERROR_SCHEMA_VERSION = "llm_error_v1"
CACHE_OBSERVATION_SCHEMA_VERSION = "cache_observation_v1"
REUSABLE_ARTIFACT_RECEIPT_SCHEMA_VERSION = "reusable_artifact_receipt_v1"

WORKSTREAMS = frozenset({"d2l", "literary", "input_normalization", "evaluation"})
SOURCE_CLASSES = frozenset({"remote_api", "local_endpoint", "local_in_process"})
ENDPOINT_CLASSES = frozenset({"remote", "loopback", "in_process"})
PROTOCOLS = frozenset(
    {
        "openai_chat_completions",
        "openai_responses",
        "google_genai_generate_content",
        "local_in_process",
    }
)
CAPABILITY_KINDS = frozenset(
    {"text_generation", "json_object", "native_structured_output", "reasoning"}
)
CAPABILITY_VERDICTS = frozenset({"qualified", "failed", "unknown"})
STRUCTURED_OUTPUT_MODES = frozenset(
    {"required", "prompt_validated", "preferred", "disabled"}
)
REASONING_EFFORTS = frozenset(
    {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
)
VERBOSITY_LEVELS = frozenset({"low", "medium", "high"})
FINISH_REASONS = frozenset(
    {"stop", "length", "content_filter", "tool_call", "safety", "error", "unknown"}
)
BACKOFF_POLICIES = frozenset({"none", "fixed", "exponential"})
RETRYABLE_TRANSPORT_CODES = frozenset(
    {"connection", "timeout", "rate_limit", "server_unavailable"}
)
SEMANTIC_RETRY_CATEGORIES = frozenset(
    {"parse", "canonical_schema", "pipeline_semantic"}
)
ERROR_CATEGORIES = frozenset(
    {
        "transport",
        "capability_policy",
        "parse",
        "canonical_schema",
        "pipeline_semantic",
    }
)
ERROR_RETRY_CLASSES = frozenset(
    {
        "connection",
        "timeout",
        "rate_limit",
        "server_unavailable",
        "authentication",
        "authorization",
        "invalid_request",
        "capability_policy",
        "parse",
        "canonical_schema",
        "pipeline_semantic",
    }
)
CACHE_KINDS = frozenset(
    {
        "provider_prompt_cache",
        "application_response_cache",
        "retrieval_context_cache",
        "checkpoint_stage_reuse",
        "none",
    }
)
CACHE_LOOKUP_STATUSES = frozenset({"hit", "miss", "not_checked", "bypassed"})
REUSABLE_ARTIFACT_KINDS = frozenset(
    {"application_response", "checkpoint_stage"}
)

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,191}$")
_NAMESPACE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,511}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SECRET_STRING_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{10,}\b"),
    re.compile(r"\bAIza[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{5,}"),
)
_FORBIDDEN_FIELD_NAMES = frozenset(
    {
        "api_key",
        "access_token",
        "refresh_token",
        "bearer_token",
        "credential_value",
        "credential_secret",
        "plaintext_credential",
        "environment_value",
        "password",
        "private_key",
        "gold",
        "gold_reference",
        "oracle",
        "human_reference",
        "answer_key",
        "expected_answer",
        "result_callback",
        "runtime_callback",
        "canonical_override",
        "publish_decision",
        "memory_mutation",
        "score_override",
        "eval_override",
    }
)
_EVALUATION_FORBIDDEN_ID_TOKENS = frozenset(
    {
        "gold",
        "oracle",
        "human_reference",
        "answer_key",
        "expected_answer",
        "result_callback",
        "score_override",
        "eval_override",
    }
)


class ContractValidationError(ValueError):
    """Raised when a neutral LLM record violates its closed contract."""


def canonical_json(value: Any) -> str:
    """Return deterministic UTF-8 JSON after rejecting non-JSON values."""

    _validate_json_value(value, path="$", scan_for_secrets=False)
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ContractValidationError("value is not canonical JSON") from exc


def canonical_sha256(value: Any) -> str:
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


def validate_api_source(value: Mapping[str, Any]) -> dict[str, Any]:
    row = _closed_object(
        value,
        {
            "schema_version",
            "source_id",
            "source_revision",
            "source_class",
            "adapter_id",
            "protocol",
            "route_id",
            "endpoint_class",
            "base_url",
            "credential_ref",
            "credential_commitment",
            "physical_quota_bucket_id",
            "enabled",
        },
        "API source",
    )
    _expect_literal(row["schema_version"], API_SOURCE_SCHEMA_VERSION, "API source schema")
    row["source_id"] = _identifier(row["source_id"], "source_id")
    row["source_revision"] = _identifier(row["source_revision"], "source_revision")
    row["source_class"] = _enum(row["source_class"], SOURCE_CLASSES, "source_class")
    row["adapter_id"] = _identifier(row["adapter_id"], "adapter_id")
    row["protocol"] = _enum(row["protocol"], PROTOCOLS, "protocol")
    row["route_id"] = _identifier(row["route_id"], "route_id")
    row["endpoint_class"] = _enum(
        row["endpoint_class"], ENDPOINT_CLASSES, "endpoint_class"
    )
    row["physical_quota_bucket_id"] = _identifier(
        row["physical_quota_bucket_id"], "physical_quota_bucket_id"
    )
    row["enabled"] = _boolean(row["enabled"], "enabled")

    source_class = row["source_class"]
    endpoint_class = row["endpoint_class"]
    base_url = row["base_url"]
    if source_class == "local_in_process":
        if endpoint_class != "in_process" or base_url is not None:
            raise ContractValidationError(
                "local_in_process source requires in_process endpoint and null base_url"
            )
        if row["protocol"] != "local_in_process":
            raise ContractValidationError("local_in_process source has foreign protocol")
    else:
        normalized_url, observed_endpoint_class = _normalized_base_url(base_url)
        if normalized_url != base_url:
            raise ContractValidationError("base_url is not normalized")
        if endpoint_class != observed_endpoint_class:
            raise ContractValidationError("endpoint_class differs from base_url")
        if source_class == "local_endpoint" and endpoint_class != "loopback":
            raise ContractValidationError("local_endpoint must use an explicit loopback URL")
        if source_class == "remote_api" and endpoint_class != "remote":
            raise ContractValidationError("remote_api may not use a loopback URL")
        if row["protocol"] == "local_in_process":
            raise ContractValidationError("network source may not use local_in_process protocol")

    credential_ref = row["credential_ref"]
    commitment = row["credential_commitment"]
    if credential_ref is None:
        if commitment is not None:
            raise ContractValidationError(
                "credential_commitment requires an opaque credential_ref"
            )
    else:
        row["credential_ref"] = _identifier(credential_ref, "credential_ref")
        row["credential_commitment"] = _sha256(commitment, "credential_commitment")
    if source_class != "local_in_process" and credential_ref is None:
        raise ContractValidationError("network source requires an opaque credential_ref")

    _reject_forbidden_recursive(row, "API source")
    return row


def validate_capability_evidence(value: Mapping[str, Any]) -> dict[str, Any]:
    row = _closed_object(
        value,
        {
            "schema_version",
            "capability_id",
            "capability_revision",
            "source_id",
            "source_revision",
            "adapter_id",
            "protocol",
            "route_id",
            "base_url",
            "requested_model_id",
            "observed_model_id",
            "capability_kind",
            "schema_dialect",
            "schema_sha256",
            "local_validator_id",
            "local_validator_sha256",
            "probe_id",
            "evidence_sha256",
            "observed_at_utc",
            "verdict",
        },
        "capability evidence",
    )
    _expect_literal(
        row["schema_version"],
        CAPABILITY_EVIDENCE_SCHEMA_VERSION,
        "capability schema",
    )
    for field in (
        "capability_id",
        "capability_revision",
        "source_id",
        "source_revision",
        "adapter_id",
        "route_id",
        "probe_id",
    ):
        row[field] = _identifier(row[field], field)
    row["protocol"] = _enum(row["protocol"], PROTOCOLS, "protocol")
    row["requested_model_id"] = _model_id(
        row["requested_model_id"], "requested_model_id"
    )
    if row["observed_model_id"] is not None:
        row["observed_model_id"] = _model_id(
            row["observed_model_id"], "observed_model_id"
        )
    row["capability_kind"] = _enum(
        row["capability_kind"], CAPABILITY_KINDS, "capability_kind"
    )
    row["verdict"] = _enum(row["verdict"], CAPABILITY_VERDICTS, "verdict")
    row["evidence_sha256"] = _sha256(row["evidence_sha256"], "evidence_sha256")
    row["observed_at_utc"] = _utc_timestamp(row["observed_at_utc"], "observed_at_utc")
    if row["base_url"] is not None:
        normalized, _ = _normalized_base_url(row["base_url"])
        if normalized != row["base_url"]:
            raise ContractValidationError("capability base_url is not normalized")

    for field in ("schema_dialect", "local_validator_id"):
        if row[field] is not None:
            row[field] = _identifier(row[field], field)
    for field in ("schema_sha256", "local_validator_sha256"):
        if row[field] is not None:
            row[field] = _sha256(row[field], field)

    if row["capability_kind"] in {"native_structured_output", "json_object"}:
        if not all(
            row[field] is not None
            for field in (
                "schema_dialect",
                "schema_sha256",
                "local_validator_id",
                "local_validator_sha256",
            )
        ):
            raise ContractValidationError(
                "structured response evidence needs schema and local validator bindings"
            )
    elif any(
        row[field] is not None
        for field in (
            "schema_dialect",
            "schema_sha256",
            "local_validator_id",
            "local_validator_sha256",
        )
    ):
        raise ContractValidationError(
            "non-structured capability may not claim schema or local validator bindings"
        )
    if row["verdict"] == "qualified" and row["observed_model_id"] is None:
        raise ContractValidationError("qualified capability lacks observed model identity")
    _reject_forbidden_recursive(row, "capability evidence")
    return row


def validate_pipeline_profile(value: Mapping[str, Any]) -> dict[str, Any]:
    profile = _closed_object(
        value,
        {
            "schema_version",
            "profile_id",
            "profile_revision",
            "workstream",
            "role_bindings",
        },
        "pipeline profile",
    )
    _expect_literal(
        profile["schema_version"], PIPELINE_PROFILE_SCHEMA_VERSION, "profile schema"
    )
    profile["profile_id"] = _identifier(profile["profile_id"], "profile_id")
    profile["profile_revision"] = _identifier(
        profile["profile_revision"], "profile_revision"
    )
    profile["workstream"] = _enum(profile["workstream"], WORKSTREAMS, "workstream")
    roles = profile["role_bindings"]
    if not isinstance(roles, list) or not roles:
        raise ContractValidationError("pipeline profile needs role_bindings")
    normalized_roles = [
        _validate_role_binding(role, profile["workstream"]) for role in roles
    ]
    ids = [role["role_id"] for role in normalized_roles]
    if len(ids) != len(set(ids)):
        raise ContractValidationError("pipeline profile repeats a role_id")
    profile["role_bindings"] = sorted(normalized_roles, key=lambda row: row["role_id"])
    namespaces: dict[str, str] = {}
    for role in profile["role_bindings"]:
        for kind, namespace in role["namespaces"].items():
            prior = namespaces.get(namespace)
            if prior is not None:
                raise ContractValidationError(
                    f"profile roles reuse namespace {namespace}: {prior} and "
                    f"{role['role_id']}.{kind}"
                )
            namespaces[namespace] = f"{role['role_id']}.{kind}"
    _reject_forbidden_recursive(profile, "pipeline profile")
    if profile["workstream"] == "evaluation":
        _reject_evaluation_authority_identifiers(profile)
    return profile


def validate_llm_attempt_usage(value: Mapping[str, Any]) -> dict[str, Any]:
    row = _closed_object(
        value,
        {
            "schema_version",
            "attempt_usage_id",
            "seal_sha256",
            "logical_request_id",
            "logical_request_sha256",
            "semantic_attempt_index",
            "transport_retry_ordinal",
            "physical_attempt_index",
            "request_id",
            "source_id",
            "source_revision",
            "physical_quota_bucket_id",
            "requested_model_id",
            "observed_model_id",
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
            "provider_usage_sha256",
            "error_id",
        },
        "attempt usage",
    )
    _expect_literal(
        row["schema_version"], LLM_ATTEMPT_USAGE_SCHEMA_VERSION, "usage schema"
    )
    for field in (
        "attempt_usage_id",
        "logical_request_id",
        "source_id",
        "source_revision",
        "physical_quota_bucket_id",
    ):
        row[field] = _identifier(row[field], field)
    row["seal_sha256"] = _sha256(row["seal_sha256"], "seal_sha256")
    row["logical_request_sha256"] = _sha256(
        row["logical_request_sha256"], "logical_request_sha256"
    )
    row["semantic_attempt_index"] = _integer(
        row["semantic_attempt_index"], "semantic_attempt_index", minimum=1
    )
    row["transport_retry_ordinal"] = _integer(
        row["transport_retry_ordinal"], "transport_retry_ordinal", minimum=0
    )
    row["physical_attempt_index"] = _integer(
        row["physical_attempt_index"], "physical_attempt_index", minimum=1
    )
    if row["request_id"] is not None:
        row["request_id"] = _nonempty_string(row["request_id"], "request_id")
    row["requested_model_id"] = _model_id(
        row["requested_model_id"], "requested_model_id"
    )
    if row["observed_model_id"] is not None:
        row["observed_model_id"] = _model_id(
            row["observed_model_id"], "observed_model_id"
        )
    started = _utc_datetime(row["started_at_utc"], "started_at_utc")
    finished = _utc_datetime(row["finished_at_utc"], "finished_at_utc")
    if finished < started:
        raise ContractValidationError("attempt finished before it started")
    row["latency_ms"] = _integer(row["latency_ms"], "latency_ms", minimum=0)
    expected_latency_ms = int((finished - started).total_seconds() * 1_000)
    if row["latency_ms"] != expected_latency_ms:
        raise ContractValidationError("latency_ms differs from attempt timestamps")
    row["outcome"] = _enum(
        row["outcome"],
        {"succeeded", "failed_before_request", "failed_after_request"},
        "outcome",
    )
    if row["finish_reason"] is not None:
        row["finish_reason"] = _enum(
            row["finish_reason"], FINISH_REASONS, "finish_reason"
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
    if row["provider_usage_sha256"] is not None:
        row["provider_usage_sha256"] = _sha256(
            row["provider_usage_sha256"], "provider_usage_sha256"
        )
    if row["error_id"] is not None:
        row["error_id"] = _identifier(row["error_id"], "error_id")
    row["cost_status"] = _enum(
        row["cost_status"], {"reported", "calculated", "unknown"}, "cost_status"
    )
    row["cost_provenance"] = _validate_cost_provenance(row["cost_provenance"])
    if row["cost_usd"] is not None:
        row["cost_usd"] = _number(row["cost_usd"], "cost_usd", minimum=0.0)
    if row["cost_status"] == "unknown":
        if row["cost_usd"] is not None:
            raise ContractValidationError("unknown cost must remain null")
        if row["cost_provenance"]["kind"] != "unavailable":
            raise ContractValidationError("unknown cost requires unavailable provenance")
    elif row["cost_usd"] is None:
        raise ContractValidationError("known cost status requires cost_usd")
    elif row["cost_status"] == "reported":
        if row["cost_provenance"]["kind"] != "provider_reported":
            raise ContractValidationError("reported cost requires provider provenance")
        if row["provider_usage_sha256"] is None:
            raise ContractValidationError("reported cost requires provider usage evidence")
    elif row["cost_provenance"]["kind"] != "pricing_manifest":
        raise ContractValidationError("calculated cost requires pricing-manifest provenance")

    prompt = row["prompt_tokens"]
    cached = row["cached_input_tokens"]
    completion = row["completion_tokens"]
    reasoning = row["reasoning_tokens"]
    total = row["total_tokens"]
    if cached is not None and (prompt is None or cached > prompt):
        raise ContractValidationError("cached input tokens must be a prompt subset")
    if reasoning is not None and (completion is None or reasoning > completion):
        raise ContractValidationError("reasoning tokens must be a completion subset")
    if prompt is not None and completion is not None:
        if total != prompt + completion:
            raise ContractValidationError(
                "total_tokens must equal prompt_tokens plus completion_tokens"
            )
    elif total is not None:
        raise ContractValidationError("partial token facts may not fabricate total_tokens")
    if row["outcome"] == "failed_before_request":
        if row["observed_model_id"] is not None or any(
            row[field] is not None
            for field in (
                "prompt_tokens",
                "cached_input_tokens",
                "completion_tokens",
                "reasoning_tokens",
                "total_tokens",
                "cost_usd",
                "provider_usage_sha256",
                "finish_reason",
            )
        ):
            raise ContractValidationError(
                "failed_before_request may not report provider usage or observed model"
            )
        if row["cost_status"] != "unknown":
            raise ContractValidationError("failed_before_request cost must be unknown")
    if row["outcome"] == "succeeded" and row["error_id"] is not None:
        raise ContractValidationError("successful attempt may not reference an error")
    if row["outcome"] == "succeeded" and row["finish_reason"] is None:
        raise ContractValidationError("successful attempt requires a finish_reason")
    if row["outcome"] != "succeeded" and row["error_id"] is None:
        raise ContractValidationError("failed attempt requires error_id")
    _reject_forbidden_recursive(row, "attempt usage")
    return row


def _validate_cost_provenance(value: Any) -> dict[str, Any]:
    provenance = _closed_object(
        value, {"kind", "reference_id", "reference_sha256"}, "cost provenance"
    )
    provenance["kind"] = _enum(
        provenance["kind"],
        {"provider_reported", "pricing_manifest", "unavailable"},
        "cost provenance kind",
    )
    if provenance["reference_id"] is not None:
        provenance["reference_id"] = _identifier(
            provenance["reference_id"], "cost provenance reference_id"
        )
    if provenance["reference_sha256"] is not None:
        provenance["reference_sha256"] = _sha256(
            provenance["reference_sha256"], "cost provenance reference_sha256"
        )
    if provenance["kind"] == "unavailable":
        if provenance["reference_id"] is not None or provenance[
            "reference_sha256"
        ] is not None:
            raise ContractValidationError(
                "unavailable cost provenance may not claim a reference"
            )
    elif provenance["reference_id"] is None or provenance["reference_sha256"] is None:
        raise ContractValidationError("known cost provenance requires an exact reference")
    return provenance


def validate_llm_error(value: Mapping[str, Any]) -> dict[str, Any]:
    row = _closed_object(
        value,
        {
            "schema_version",
            "error_id",
            "seal_sha256",
            "attempt_usage_id",
            "category",
            "code",
            "retry_class",
            "safe_message",
            "details_sha256",
            "source_health_effect",
            "retry_disposition",
            "occurred_at_utc",
        },
        "LLM error",
    )
    _expect_literal(row["schema_version"], LLM_ERROR_SCHEMA_VERSION, "error schema")
    row["error_id"] = _identifier(row["error_id"], "error_id")
    row["seal_sha256"] = _sha256(row["seal_sha256"], "seal_sha256")
    if row["attempt_usage_id"] is not None:
        row["attempt_usage_id"] = _identifier(
            row["attempt_usage_id"], "attempt_usage_id"
        )
    row["category"] = _enum(row["category"], ERROR_CATEGORIES, "category")
    row["code"] = _identifier(row["code"], "code")
    row["retry_class"] = _enum(
        row["retry_class"], ERROR_RETRY_CLASSES, "retry_class"
    )
    expected_http_retry_class = _http_retry_class(row["code"])
    if (
        expected_http_retry_class is not None
        and row["retry_class"] != expected_http_retry_class
    ):
        raise ContractValidationError("HTTP error code and retry_class differ")
    row["safe_message"] = _nonempty_string(row["safe_message"], "safe_message")
    row["details_sha256"] = _sha256(row["details_sha256"], "details_sha256")
    row["source_health_effect"] = _enum(
        row["source_health_effect"],
        {"none", "temporary_unavailable"},
        "source_health_effect",
    )
    row["retry_disposition"] = _enum(
        row["retry_disposition"],
        {"do_not_retry", "transport_retry_allowed", "semantic_retry_allowed"},
        "retry_disposition",
    )
    row["occurred_at_utc"] = _utc_timestamp(row["occurred_at_utc"], "occurred_at_utc")
    if row["category"] == "transport":
        if row["retry_class"] not in {
            "connection",
            "timeout",
            "rate_limit",
            "server_unavailable",
            "authentication",
            "authorization",
            "invalid_request",
        }:
            raise ContractValidationError("transport error has a non-transport retry_class")
        if row["retry_disposition"] == "semantic_retry_allowed":
            raise ContractValidationError("transport error cannot request semantic retry")
        if row["retry_class"] in {
            "authentication",
            "authorization",
            "invalid_request",
        } and row["retry_disposition"] != "do_not_retry":
            raise ContractValidationError(
                "authentication, authorization and invalid-request errors are not retryable"
            )
        if row["retry_class"] in {
            "authentication",
            "authorization",
            "invalid_request",
        } and row["source_health_effect"] != "none":
            raise ContractValidationError(
                "request/authentication errors do not mark source health unavailable"
            )
    else:
        if row["source_health_effect"] != "none":
            raise ContractValidationError(
                "non-transport error cannot mark the source unavailable"
            )
        if row["retry_disposition"] == "transport_retry_allowed":
            raise ContractValidationError(
                "non-transport error cannot request transport retry"
            )
        if row["category"] == "capability_policy":
            if row["retry_class"] != "capability_policy" or row[
                "retry_disposition"
            ] != "do_not_retry":
                raise ContractValidationError("capability-policy errors are not retryable")
        elif row["retry_class"] != row["category"]:
            raise ContractValidationError("semantic error retry_class differs from category")
    _reject_forbidden_recursive(row, "LLM error")
    return row


def validate_cache_observation(value: Mapping[str, Any]) -> dict[str, Any]:
    row = _closed_object(
        value,
        {
            "schema_version",
            "observation_id",
            "seal_sha256",
            "logical_request_id",
            "logical_request_sha256",
            "attempt_usage_id",
            "cache_kind",
            "cache_namespace",
            "cache_key_sha256",
            "lookup_status",
            "provider_call_avoided",
            "provider_cached_input_tokens",
            "reused_artifact_sha256",
            "producer_seal_sha256",
            "producer_input_bindings_sha256",
            "producer_artifact_receipt_sha256",
            "observed_at_utc",
        },
        "cache observation",
    )
    _expect_literal(
        row["schema_version"], CACHE_OBSERVATION_SCHEMA_VERSION, "cache schema"
    )
    row["observation_id"] = _identifier(row["observation_id"], "observation_id")
    row["seal_sha256"] = _sha256(row["seal_sha256"], "seal_sha256")
    row["logical_request_id"] = _identifier(
        row["logical_request_id"], "logical_request_id"
    )
    row["logical_request_sha256"] = _sha256(
        row["logical_request_sha256"], "logical_request_sha256"
    )
    if row["attempt_usage_id"] is not None:
        row["attempt_usage_id"] = _identifier(
            row["attempt_usage_id"], "attempt_usage_id"
        )
    row["cache_kind"] = _enum(row["cache_kind"], CACHE_KINDS, "cache_kind")
    row["cache_namespace"] = _namespace_identifier(
        row["cache_namespace"], "cache_namespace"
    )
    for field in (
        "cache_key_sha256",
        "reused_artifact_sha256",
        "producer_seal_sha256",
        "producer_input_bindings_sha256",
        "producer_artifact_receipt_sha256",
    ):
        if row[field] is not None:
            row[field] = _sha256(row[field], field)
    row["lookup_status"] = _enum(
        row["lookup_status"], CACHE_LOOKUP_STATUSES, "lookup_status"
    )
    row["provider_call_avoided"] = _boolean(
        row["provider_call_avoided"], "provider_call_avoided"
    )
    if row["provider_cached_input_tokens"] is not None:
        row["provider_cached_input_tokens"] = _integer(
            row["provider_cached_input_tokens"],
            "provider_cached_input_tokens",
            minimum=0,
        )
    row["observed_at_utc"] = _utc_timestamp(row["observed_at_utc"], "observed_at_utc")

    kind = row["cache_kind"]
    status = row["lookup_status"]
    if kind == "none":
        if status not in {"not_checked", "bypassed"} or any(
            row[field] is not None
            for field in (
                "cache_key_sha256",
                "provider_cached_input_tokens",
                "reused_artifact_sha256",
                "attempt_usage_id",
                "producer_seal_sha256",
                "producer_input_bindings_sha256",
                "producer_artifact_receipt_sha256",
            )
        ) or row["provider_call_avoided"]:
            raise ContractValidationError("cache kind none may not claim cache work")
    elif status in {"hit", "miss"} and row["cache_key_sha256"] is None:
        raise ContractValidationError("cache hit/miss requires cache_key_sha256")
    if status != "hit" and row["provider_call_avoided"]:
        raise ContractValidationError("only a cache hit may avoid a provider call")
    if status != "hit" and any(
        row[field] is not None
        for field in (
            "reused_artifact_sha256",
            "producer_seal_sha256",
            "producer_input_bindings_sha256",
            "producer_artifact_receipt_sha256",
        )
    ):
        raise ContractValidationError(
            "non-hit cache observation may not claim reused producer lineage"
        )
    if kind not in {"application_response_cache", "checkpoint_stage_reuse"} and any(
        row[field] is not None
        for field in (
            "producer_seal_sha256",
            "producer_input_bindings_sha256",
            "producer_artifact_receipt_sha256",
        )
    ):
        raise ContractValidationError(
            "producer lineage belongs only to reusable application artifacts"
        )
    if kind == "provider_prompt_cache":
        if status in {"hit", "miss"} and row["attempt_usage_id"] is None:
            raise ContractValidationError(
                "provider prompt-cache hit/miss requires attempt usage linkage"
            )
        if row["provider_call_avoided"]:
            raise ContractValidationError(
                "provider prompt cache still performs a provider call"
            )
        if status == "hit" and not row["provider_cached_input_tokens"]:
            raise ContractValidationError(
                "provider prompt-cache hit requires cached input token evidence"
            )
        if any(
            row[field] is not None
            for field in (
                "reused_artifact_sha256",
                "producer_seal_sha256",
                "producer_input_bindings_sha256",
                "producer_artifact_receipt_sha256",
            )
        ):
            raise ContractValidationError(
                "provider prompt cache may not claim application artifact lineage"
            )
    elif row["provider_cached_input_tokens"] is not None:
        raise ContractValidationError(
            "provider_cached_input_tokens belongs only to provider prompt cache"
        )
    if kind == "application_response_cache" and status == "hit":
        if (
            not row["provider_call_avoided"]
            or row["reused_artifact_sha256"] is None
            or row["producer_seal_sha256"] is None
            or row["producer_input_bindings_sha256"] is None
            or row["producer_artifact_receipt_sha256"] is None
            or row["attempt_usage_id"] is not None
        ):
            raise ContractValidationError(
                "application response-cache hit must bind producer lineage and avoid a new attempt"
            )
    if kind == "application_response_cache" and status == "miss":
        if row["attempt_usage_id"] is None:
            raise ContractValidationError(
                "application response-cache miss requires the resulting attempt linkage"
            )
    if kind == "retrieval_context_cache" and row["provider_call_avoided"]:
        raise ContractValidationError(
            "retrieval context cache does not avoid the provider call"
        )
    if kind == "retrieval_context_cache" and row["attempt_usage_id"] is not None:
        raise ContractValidationError(
            "retrieval context cache is not a physical provider attempt"
        )
    if kind == "checkpoint_stage_reuse" and status == "hit":
        if (
            not row["provider_call_avoided"]
            or row["reused_artifact_sha256"] is None
            or row["producer_seal_sha256"] is None
            or row["producer_input_bindings_sha256"] is None
            or row["producer_artifact_receipt_sha256"] is None
            or row["attempt_usage_id"] is not None
        ):
            raise ContractValidationError(
                "checkpoint reuse hit must bind producer lineage and avoid a new attempt"
            )
    if row["provider_call_avoided"] and row["reused_artifact_sha256"] is None:
        raise ContractValidationError(
            "avoiding a provider call requires a reused artifact binding"
        )
    _reject_forbidden_recursive(row, "cache observation")
    return row


def validate_reusable_artifact_receipt(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate a durable receipt that binds one artifact to its producer seal."""

    row = _closed_object(
        value,
        {
            "schema_version",
            "producer_seal_sha256",
            "producer_logical_request_id",
            "producer_logical_request_sha256",
            "artifact_kind",
            "artifact_sha256",
            "created_at_utc",
            "receipt_sha256",
        },
        "reusable artifact receipt",
    )
    _expect_literal(
        row["schema_version"],
        REUSABLE_ARTIFACT_RECEIPT_SCHEMA_VERSION,
        "reusable artifact receipt schema",
    )
    row["producer_seal_sha256"] = _sha256(
        row["producer_seal_sha256"], "producer_seal_sha256"
    )
    row["producer_logical_request_id"] = _identifier(
        row["producer_logical_request_id"], "producer_logical_request_id"
    )
    row["producer_logical_request_sha256"] = _sha256(
        row["producer_logical_request_sha256"],
        "producer_logical_request_sha256",
    )
    row["artifact_kind"] = _enum(
        row["artifact_kind"], REUSABLE_ARTIFACT_KINDS, "artifact_kind"
    )
    row["artifact_sha256"] = _sha256(row["artifact_sha256"], "artifact_sha256")
    row["created_at_utc"] = _utc_timestamp(
        row["created_at_utc"], "created_at_utc"
    )
    observed_hash = _sha256(row.pop("receipt_sha256"), "receipt_sha256")
    if canonical_sha256(row) != observed_hash:
        raise ContractValidationError("reusable artifact receipt hash mismatch")
    row["receipt_sha256"] = observed_hash
    _reject_forbidden_recursive(row, "reusable artifact receipt")
    return row


def _validate_role_binding(value: Any, workstream: str) -> dict[str, Any]:
    role = _closed_object(
        value,
        {
            "workstream",
            "role_id",
            "preset_id",
            "preset_revision",
            "primary",
            "fallback_plan",
            "generation",
            "transport_retry",
            "semantic_retry",
            "limits",
            "structured_output",
            "namespaces",
            "prompt",
            "response_schema",
            "validator",
            "semantic_extension",
        },
        "role binding",
    )
    role["workstream"] = _enum(role["workstream"], WORKSTREAMS, "role workstream")
    if role["workstream"] != workstream:
        raise ContractValidationError("role binding workstream differs from profile")
    role["role_id"] = _identifier(role["role_id"], "role_id")
    if not role["role_id"].startswith(f"{workstream}."):
        raise ContractValidationError("role_id is outside its workstream namespace")
    role["preset_id"] = _identifier(role["preset_id"], "preset_id")
    if not role["preset_id"].startswith(f"{role['role_id']}."):
        raise ContractValidationError("preset_id must be scoped to role_id")
    role["preset_revision"] = _identifier(
        role["preset_revision"], "preset_revision"
    )
    role["primary"] = _validate_target_binding(role["primary"], "primary")
    role["fallback_plan"] = _validate_fallback_plan(role["fallback_plan"])
    role["generation"] = _validate_generation(role["generation"])
    role["transport_retry"] = _validate_transport_retry(role["transport_retry"])
    role["semantic_retry"] = _validate_semantic_retry(role["semantic_retry"])
    role["limits"] = _validate_limits(role["limits"])
    role["structured_output"] = _validate_structured_output(
        role["structured_output"]
    )
    role["namespaces"] = _validate_namespaces(role["namespaces"], role["role_id"])
    role["prompt"] = _validate_artifact_ref(role["prompt"], "prompt")
    if role["response_schema"] is not None:
        role["response_schema"] = _validate_artifact_ref(
            role["response_schema"], "response_schema"
        )
    role["validator"] = _validate_artifact_ref(role["validator"], "validator")
    role["semantic_extension"] = _validate_semantic_extension(
        role["semantic_extension"]
    )
    mode = role["structured_output"]["mode"]
    if mode != "disabled" and role["response_schema"] is None:
        raise ContractValidationError(
            "structured output mode requires a response_schema binding"
        )
    generation = role["generation"]
    limits = role["limits"]
    if (
        limits["max_prompt_tokens"] is not None
        and generation["max_input_tokens"] is not None
        and generation["max_input_tokens"] > limits["max_prompt_tokens"]
    ):
        raise ContractValidationError("per-call input exceeds the aggregate prompt cap")
    if (
        limits["max_completion_tokens"] is not None
        and generation["max_output_tokens"] > limits["max_completion_tokens"]
    ):
        raise ContractValidationError(
            "per-call output exceeds the aggregate completion cap"
        )
    if (
        generation["max_input_tokens"] is not None
        and generation["max_input_tokens"] + generation["max_output_tokens"]
        > limits["max_total_tokens"]
    ):
        raise ContractValidationError("per-call token cap exceeds aggregate total cap")
    return role


def _validate_target_binding(value: Any, label: str) -> dict[str, Any]:
    target = _closed_object(
        value,
        {
            "source_id",
            "source_revision",
            "source_record_sha256",
            "requested_model_id",
            "capability_id",
            "capability_revision",
            "capability_record_sha256",
        },
        label,
    )
    for field in (
        "source_id",
        "source_revision",
        "capability_id",
        "capability_revision",
    ):
        target[field] = _identifier(target[field], f"{label}.{field}")
    target["requested_model_id"] = _model_id(
        target["requested_model_id"], f"{label}.requested_model_id"
    )
    target["source_record_sha256"] = _sha256(
        target["source_record_sha256"], f"{label}.source_record_sha256"
    )
    target["capability_record_sha256"] = _sha256(
        target["capability_record_sha256"], f"{label}.capability_record_sha256"
    )
    return target


def _validate_fallback_plan(value: Any) -> dict[str, Any]:
    plan = _closed_object(value, {"enabled", "steps"}, "fallback plan")
    plan["enabled"] = _boolean(plan["enabled"], "fallback enabled")
    if not isinstance(plan["steps"], list):
        raise ContractValidationError("fallback steps must be a list")
    plan["steps"] = [
        _validate_target_binding(step, f"fallback step {index}")
        for index, step in enumerate(plan["steps"], start=1)
    ]
    if plan["enabled"] != bool(plan["steps"]):
        raise ContractValidationError(
            "fallback enabled state must exactly match the presence of explicit steps"
        )
    fingerprints = [canonical_sha256(step) for step in plan["steps"]]
    if len(fingerprints) != len(set(fingerprints)):
        raise ContractValidationError("fallback plan repeats a target")
    return plan


def _validate_generation(value: Any) -> dict[str, Any]:
    generation = _closed_object(
        value,
        {
            "context_window_tokens",
            "max_input_tokens",
            "max_output_tokens",
            "temperature",
            "top_p",
            "seed",
            "reasoning_effort",
            "verbosity",
        },
        "generation settings",
    )
    for field in ("context_window_tokens", "max_input_tokens", "max_output_tokens"):
        if generation[field] is not None:
            generation[field] = _integer(generation[field], field, minimum=1)
    if generation["max_output_tokens"] is None:
        raise ContractValidationError("max_output_tokens must be explicit")
    if generation["temperature"] is not None:
        generation["temperature"] = _number(
            generation["temperature"], "temperature", minimum=0.0, maximum=2.0
        )
    if generation["top_p"] is not None:
        generation["top_p"] = _number(
            generation["top_p"], "top_p", minimum=0.0, maximum=1.0
        )
    if generation["seed"] is not None:
        generation["seed"] = _integer(generation["seed"], "seed")
    if generation["reasoning_effort"] is not None:
        generation["reasoning_effort"] = _enum(
            generation["reasoning_effort"], REASONING_EFFORTS, "reasoning_effort"
        )
    if generation["verbosity"] is not None:
        generation["verbosity"] = _enum(
            generation["verbosity"], VERBOSITY_LEVELS, "verbosity"
        )
    context = generation["context_window_tokens"]
    max_input = generation["max_input_tokens"]
    max_output = generation["max_output_tokens"]
    if context is not None and max_input is not None and max_input + max_output > context:
        raise ContractValidationError(
            "max_input_tokens plus max_output_tokens exceeds context window"
        )
    return generation


def _validate_transport_retry(value: Any) -> dict[str, Any]:
    retry = _closed_object(
        value,
        {
            "max_retries",
            "backoff_policy",
            "initial_delay_ms",
            "max_delay_ms",
            "retryable_codes",
        },
        "transport retry",
    )
    retry["max_retries"] = _integer(retry["max_retries"], "max_retries", minimum=0)
    retry["backoff_policy"] = _enum(
        retry["backoff_policy"], BACKOFF_POLICIES, "backoff_policy"
    )
    retry["initial_delay_ms"] = _integer(
        retry["initial_delay_ms"], "initial_delay_ms", minimum=0
    )
    retry["max_delay_ms"] = _integer(
        retry["max_delay_ms"], "max_delay_ms", minimum=0
    )
    retry["retryable_codes"] = _sorted_unique_enum_list(
        retry["retryable_codes"], RETRYABLE_TRANSPORT_CODES, "retryable_codes"
    )
    if retry["max_retries"] == 0:
        if retry["backoff_policy"] != "none" or retry["initial_delay_ms"] or retry[
            "max_delay_ms"
        ] or retry["retryable_codes"]:
            raise ContractValidationError("zero transport retries require an empty policy")
    else:
        if (
            retry["backoff_policy"] == "none"
            or retry["initial_delay_ms"] <= 0
            or retry["max_delay_ms"] < retry["initial_delay_ms"]
            or not retry["retryable_codes"]
        ):
            raise ContractValidationError("transport retry policy is incomplete")
    return retry


def _validate_semantic_retry(value: Any) -> dict[str, Any]:
    retry = _closed_object(
        value, {"max_retries", "retryable_categories"}, "semantic retry"
    )
    retry["max_retries"] = _integer(
        retry["max_retries"], "semantic max_retries", minimum=0
    )
    retry["retryable_categories"] = _sorted_unique_enum_list(
        retry["retryable_categories"],
        SEMANTIC_RETRY_CATEGORIES,
        "semantic retryable_categories",
    )
    if retry["max_retries"] == 0 and retry["retryable_categories"]:
        raise ContractValidationError(
            "zero semantic retries require empty retryable_categories"
        )
    if retry["max_retries"] > 0 and not retry["retryable_categories"]:
        raise ContractValidationError("semantic retry policy lacks categories")
    return retry


def _validate_limits(value: Any) -> dict[str, Any]:
    limits = _closed_object(
        value,
        {
            "max_calls",
            "max_prompt_tokens",
            "max_completion_tokens",
            "max_total_tokens",
            "max_cost_usd",
            "request_timeout_ms",
        },
        "limits",
    )
    limits["max_calls"] = _integer(limits["max_calls"], "max_calls", minimum=1)
    for field in ("max_prompt_tokens", "max_completion_tokens"):
        if limits[field] is not None:
            limits[field] = _integer(limits[field], field, minimum=1)
    limits["max_total_tokens"] = _integer(
        limits["max_total_tokens"], "max_total_tokens", minimum=1
    )
    if (
        limits["max_prompt_tokens"] is not None
        and limits["max_completion_tokens"] is not None
        and limits["max_prompt_tokens"] + limits["max_completion_tokens"]
        > limits["max_total_tokens"]
    ):
        raise ContractValidationError(
            "prompt and completion caps exceed max_total_tokens"
        )
    if limits["max_cost_usd"] is not None:
        limits["max_cost_usd"] = _number(
            limits["max_cost_usd"], "max_cost_usd", minimum=0.0
        )
    limits["request_timeout_ms"] = _integer(
        limits["request_timeout_ms"],
        "request_timeout_ms",
        minimum=1_000,
        maximum=3_600_000,
    )
    return limits


def _validate_structured_output(value: Any) -> dict[str, Any]:
    output = _closed_object(value, {"mode", "schema_dialect"}, "structured output")
    output["mode"] = _enum(
        output["mode"], STRUCTURED_OUTPUT_MODES, "structured output mode"
    )
    if output["schema_dialect"] is not None:
        output["schema_dialect"] = _identifier(
            output["schema_dialect"], "schema_dialect"
        )
    if output["mode"] in {"required", "prompt_validated"} and output[
        "schema_dialect"
    ] is None:
        raise ContractValidationError(
            "validated structured output needs an exact schema dialect"
        )
    if output["mode"] == "disabled" and output["schema_dialect"] is not None:
        raise ContractValidationError(
            "disabled structured output may not claim a schema dialect"
        )
    return output


def _validate_namespaces(value: Any, role_id: str) -> dict[str, Any]:
    namespaces = _closed_object(
        value, {"output", "checkpoint", "cache"}, "namespaces"
    )
    prefix = f"{role_id}."
    for field in ("output", "checkpoint", "cache"):
        namespaces[field] = _namespace_identifier(
            namespaces[field], f"{field} namespace"
        )
        if not namespaces[field].startswith(prefix):
            raise ContractValidationError(
                f"{field} namespace must be scoped to the exact role_id"
            )
    if len(set(namespaces.values())) != 3:
        raise ContractValidationError("output, checkpoint and cache namespaces differ")
    return namespaces


def _http_retry_class(code: str) -> str | None:
    match = re.fullmatch(r"http_([1-5][0-9]{2})", code)
    if match is None:
        return None
    status = int(match.group(1))
    if status == 401:
        return "authentication"
    if status in {402, 403}:
        return "authorization"
    if status == 408:
        return "timeout"
    if status == 429:
        return "rate_limit"
    if 500 <= status <= 599:
        return "server_unavailable"
    return "invalid_request"


def _validate_artifact_ref(value: Any, label: str) -> dict[str, Any]:
    ref = _closed_object(value, {"id", "revision", "sha256"}, label)
    ref["id"] = _identifier(ref["id"], f"{label}.id")
    ref["revision"] = _identifier(ref["revision"], f"{label}.revision")
    ref["sha256"] = _sha256(ref["sha256"], f"{label}.sha256")
    return ref


def _validate_semantic_extension(value: Any) -> dict[str, Any]:
    ref = _closed_object(
        value, {"id", "schema_version", "sha256"}, "semantic extension"
    )
    ref["id"] = _identifier(ref["id"], "semantic_extension.id")
    ref["schema_version"] = _identifier(
        ref["schema_version"], "semantic_extension.schema_version"
    )
    ref["sha256"] = _sha256(ref["sha256"], "semantic_extension.sha256")
    return ref


def _closed_object(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractValidationError(f"{label} must be an object")
    observed = set(value)
    if observed != keys:
        raise ContractValidationError(
            f"{label} keys differ; missing={sorted(keys - observed)}, "
            f"extra={sorted(observed - keys)}"
        )
    row = deepcopy(dict(value))
    _validate_json_value(row, path=label, scan_for_secrets=True)
    return row


def _validate_json_value(value: Any, *, path: str, scan_for_secrets: bool) -> None:
    if value is None or isinstance(value, (str, bool)):
        if scan_for_secrets and isinstance(value, str):
            _reject_secret_string(value, path)
        return
    if isinstance(value, int) and not isinstance(value, bool):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ContractValidationError(f"{path} contains a nonfinite number")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_value(
                item, path=f"{path}[{index}]", scan_for_secrets=scan_for_secrets
            )
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ContractValidationError(f"{path} contains a non-string key")
            _validate_json_value(
                item, path=f"{path}.{key}", scan_for_secrets=scan_for_secrets
            )
        return
    raise ContractValidationError(f"{path} contains a non-JSON value")


def _reject_secret_string(value: str, path: str) -> None:
    if any(pattern.search(value) for pattern in _SECRET_STRING_PATTERNS):
        raise ContractValidationError(f"{path} contains secret-shaped text")


def _reject_forbidden_recursive(value: Any, label: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]+", "_", key.casefold()).strip("_")
            if normalized in _FORBIDDEN_FIELD_NAMES:
                raise ContractValidationError(f"{label} contains forbidden field {key}")
            _reject_forbidden_recursive(item, label)
    elif isinstance(value, list):
        for item in value:
            _reject_forbidden_recursive(item, label)


def _reject_evaluation_authority_identifiers(profile: Mapping[str, Any]) -> None:
    fields: list[tuple[str, str]] = [("profile_id", str(profile["profile_id"]))]
    for role in profile["role_bindings"]:
        fields.extend(
            [
                ("role_id", role["role_id"]),
                ("preset_id", role["preset_id"]),
                ("prompt.id", role["prompt"]["id"]),
                ("validator.id", role["validator"]["id"]),
                ("semantic_extension.id", role["semantic_extension"]["id"]),
            ]
        )
        if role["response_schema"] is not None:
            fields.append(("response_schema.id", role["response_schema"]["id"]))
        for target_label, target in [
            ("primary", role["primary"]),
            *[
                (f"fallback.{index}", target)
                for index, target in enumerate(
                    role["fallback_plan"]["steps"], start=1
                )
            ],
        ]:
            fields.extend(
                (
                    (f"{target_label}.source_id", target["source_id"]),
                    (
                        f"{target_label}.source_revision",
                        target["source_revision"],
                    ),
                    (
                        f"{target_label}.requested_model_id",
                        target["requested_model_id"],
                    ),
                    (f"{target_label}.capability_id", target["capability_id"]),
                    (
                        f"{target_label}.capability_revision",
                        target["capability_revision"],
                    ),
                )
            )
    for label, value in fields:
        _reject_evaluation_authority_identifier(value, f"evaluation {label}")


def _reject_evaluation_authority_identifier(value: str, label: str) -> None:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")
    for forbidden in sorted(_EVALUATION_FORBIDDEN_ID_TOKENS):
        pattern = rf"(?:^|_){re.escape(forbidden)}(?:_|$)"
        if re.search(pattern, normalized):
            raise ContractValidationError(
                f"{label} contains runtime authority token {forbidden}"
            )


def _normalized_base_url(value: Any) -> tuple[str, str]:
    if not isinstance(value, str) or not value or any(character.isspace() for character in value):
        raise ContractValidationError("base_url must be a nonempty URL without whitespace")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except (ValueError, UnicodeError) as exc:
        raise ContractValidationError("base_url is malformed") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or host is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "%" in host
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ContractValidationError("base_url has forbidden URL components")
    try:
        loopback = host.casefold() == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host.casefold() == "localhost"
    if not loopback and parsed.scheme != "https":
        raise ContractValidationError("remote base_url must use https")
    normalized_host = host.casefold()
    if ":" in normalized_host and not normalized_host.startswith("["):
        normalized_host = f"[{normalized_host}]"
    netloc = normalized_host if port is None else f"{normalized_host}:{port}"
    path = parsed.path.rstrip("/")
    normalized = urlunsplit((parsed.scheme.casefold(), netloc, path, "", ""))
    return normalized, "loopback" if loopback else "remote"


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ContractValidationError(f"{label} must be a normalized identifier")
    return value


def _namespace_identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _NAMESPACE_RE.fullmatch(value):
        raise ContractValidationError(f"{label} must be a normalized namespace")
    return value


def _model_id(value: Any, label: str) -> str:
    value = _nonempty_string(value, label)
    if value != value.strip() or len(value) > 256 or "latest" in value.casefold():
        raise ContractValidationError(f"{label} must be an exact pinned model ID")
    if any(ord(character) < 0x21 or ord(character) > 0x7E for character in value):
        raise ContractValidationError(f"{label} contains unsupported characters")
    return value


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ContractValidationError(f"{label} must be lowercase SHA-256")
    return value


def _nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ContractValidationError(f"{label} must be a nonempty trimmed string")
    return value


def _integer(
    value: Any,
    label: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ContractValidationError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise ContractValidationError(f"{label} is below its minimum")
    if maximum is not None and value > maximum:
        raise ContractValidationError(f"{label} exceeds its maximum")
    return value


def _number(
    value: Any,
    label: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractValidationError(f"{label} must be numeric")
    if not math.isfinite(float(value)):
        raise ContractValidationError(f"{label} must be finite")
    if minimum is not None and value < minimum:
        raise ContractValidationError(f"{label} is below its minimum")
    if maximum is not None and value > maximum:
        raise ContractValidationError(f"{label} exceeds its maximum")
    return value


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ContractValidationError(f"{label} must be boolean")
    return value


def _enum(value: Any, options: Sequence[str] | set[str] | frozenset[str], label: str) -> str:
    if not isinstance(value, str) or value not in options:
        raise ContractValidationError(f"{label} has unsupported value")
    return value


def _sorted_unique_enum_list(value: Any, options: frozenset[str], label: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ContractValidationError(f"{label} must be a string list")
    if any(item not in options for item in value):
        raise ContractValidationError(f"{label} contains an unsupported value")
    if len(value) != len(set(value)):
        raise ContractValidationError(f"{label} repeats a value")
    return sorted(value)


def _expect_literal(value: Any, expected: str, label: str) -> None:
    if value != expected:
        raise ContractValidationError(f"{label} differs from {expected}")


def _utc_timestamp(value: Any, label: str) -> str:
    _utc_datetime(value, label)
    return value


def _utc_datetime(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ContractValidationError(f"{label} must be an ISO-8601 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ContractValidationError(f"{label} is not a valid timestamp") from exc
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise ContractValidationError(f"{label} must be UTC")
    return parsed
