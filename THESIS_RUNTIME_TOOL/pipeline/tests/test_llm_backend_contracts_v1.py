from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from pipeline.llm_backend.contracts_v1 import (
    ContractValidationError,
    canonical_json,
    canonical_sha256,
    validate_api_source,
    validate_cache_observation,
    validate_capability_evidence,
    validate_llm_attempt_usage,
    validate_llm_error,
    validate_pipeline_profile,
    validate_reusable_artifact_receipt,
)


FIXTURES = Path(__file__).parent / "fixtures" / "llm_backend_v1"
SHA_A = "a" * 64
SHA_B = "b" * 64


def _load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _profiles() -> list[dict]:
    return _load("profile_four_workstreams.json")["profiles"]


def test_all_four_workstream_profiles_share_shape_but_keep_values() -> None:
    raw = _profiles()
    before = deepcopy(raw)
    profiles = [validate_pipeline_profile(row) for row in raw]

    assert raw == before
    assert {row["workstream"] for row in profiles} == {
        "d2l",
        "literary",
        "input_normalization",
        "evaluation",
    }
    context_windows = {
        row["workstream"]: row["role_bindings"][0]["generation"][
            "context_window_tokens"
        ]
        for row in profiles
    }
    assert context_windows == {
        "d2l": 128000,
        "literary": 300000,
        "input_normalization": 128000,
        "evaluation": 64000,
    }
    assert len({canonical_sha256(row) for row in profiles}) == 4


def test_api_source_fixtures_are_closed_and_detached() -> None:
    for name, expected_class in (
        ("source_local.json", "local_endpoint"),
        ("source_remote.json", "remote_api"),
    ):
        raw = _load(name)
        before = deepcopy(raw)
        validated = validate_api_source(raw)
        assert raw == before
        assert validated is not raw
        assert validated["source_class"] == expected_class


@pytest.mark.parametrize(
    "mutation",
    [
        lambda row: row.update({"unknown": True}),
        lambda row: row.update({"api_key": "fixture-secret"}),
        lambda row: row.update({"base_url": "http://provider.invalid/v1"}),
        lambda row: row.update({"endpoint_class": "remote"}),
        lambda row: row.update({"credential_commitment": None}),
    ],
)
def test_api_source_rejects_unknown_secret_and_route_drift(mutation) -> None:
    row = _load("source_local.json")
    mutation(row)
    with pytest.raises(ContractValidationError):
        validate_api_source(row)


def test_remote_source_requires_normalized_https() -> None:
    row = _load("source_remote.json")
    row["base_url"] += "/"
    with pytest.raises(ContractValidationError, match="not normalized"):
        validate_api_source(row)


def test_capability_native_binds_schema_model_route_and_validator() -> None:
    raw = _load("capability_native.json")
    before = deepcopy(raw)
    result = validate_capability_evidence(raw)
    assert raw == before
    assert result["capability_kind"] == "native_structured_output"
    assert result["requested_model_id"] == "gpt-5.5"


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_sha256", None),
        ("local_validator_sha256", None),
        ("observed_model_id", None),
        ("evidence_sha256", "A" * 64),
    ],
)
def test_capability_native_fails_closed_on_missing_authority(field, value) -> None:
    row = _load("capability_native.json")
    row[field] = value
    with pytest.raises(ContractValidationError):
        validate_capability_evidence(row)


def test_profile_rejects_cross_workstream_namespace() -> None:
    profile = _profiles()[0]
    profile["role_bindings"][0]["namespaces"]["cache"] = "literary.shared.cache"
    with pytest.raises(ContractValidationError, match="exact role_id"):
        validate_pipeline_profile(profile)


def test_profile_requires_and_seals_pipeline_owned_verbosity() -> None:
    profile = _profiles()[0]
    assert validate_pipeline_profile(profile)["role_bindings"][0]["generation"][
        "verbosity"
    ] == "medium"
    del profile["role_bindings"][0]["generation"]["verbosity"]
    with pytest.raises(ContractValidationError, match="missing=.*verbosity"):
        validate_pipeline_profile(profile)


def test_profile_rejects_profile_wide_namespace_reuse_for_nested_roles() -> None:
    profile = _profiles()[0]
    parent = profile["role_bindings"][0]
    parent["role_id"] = "d2l.translator"
    parent["preset_id"] = "d2l.translator.contract_fixture_v1"
    parent["namespaces"] = {
        kind: f"d2l.translator.s0.{kind}"
        for kind in ("output", "checkpoint", "cache")
    }
    child = deepcopy(parent)
    child["role_id"] = "d2l.translator.s0"
    child["preset_id"] = "d2l.translator.s0.contract_fixture_v1"
    profile["role_bindings"].append(child)
    with pytest.raises(ContractValidationError, match="reuse namespace"):
        validate_pipeline_profile(profile)


def test_profile_rejects_hidden_retry_policy() -> None:
    profile = _profiles()[1]
    retry = profile["role_bindings"][0]["transport_retry"]
    retry["max_retries"] = 2
    with pytest.raises(ContractValidationError, match="incomplete"):
        validate_pipeline_profile(profile)


def test_profile_rejects_implicit_fallback() -> None:
    profile = _profiles()[0]
    profile["role_bindings"][0]["fallback_plan"]["steps"] = [
        deepcopy(profile["role_bindings"][0]["primary"])
    ]
    with pytest.raises(ContractValidationError, match="enabled state"):
        validate_pipeline_profile(profile)


def test_evaluation_profile_rejects_runtime_gold_authority() -> None:
    profile = _profiles()[3]
    profile["role_bindings"][0]["semantic_extension"][
        "id"
    ] = "evaluation_gold_extension_v1"
    with pytest.raises(ContractValidationError, match="authority token gold"):
        validate_pipeline_profile(profile)


@pytest.mark.parametrize("forbidden", ["human_reference", "result_callback"])
def test_evaluation_profile_rejects_compound_runtime_authority(forbidden) -> None:
    profile = _profiles()[3]
    profile["role_bindings"][0]["semantic_extension"][
        "id"
    ] = f"evaluation_{forbidden}_extension_v1"
    with pytest.raises(ContractValidationError, match=forbidden):
        validate_pipeline_profile(profile)


def test_profile_rejects_per_call_caps_above_stage_caps() -> None:
    profile = _profiles()[0]
    profile["role_bindings"][0]["generation"]["max_output_tokens"] = 12001
    with pytest.raises(ContractValidationError, match="aggregate completion cap"):
        validate_pipeline_profile(profile)


def test_profile_role_order_is_canonical_without_mutation() -> None:
    profile = _profiles()[0]
    second = deepcopy(profile["role_bindings"][0])
    second["role_id"] = "d2l.auditor.term_policy"
    second["preset_id"] = "d2l.auditor.term_policy.contract_fixture_v1"
    for key in second["namespaces"]:
        second["namespaces"][key] = f"d2l.auditor.term_policy.{key}"
    profile["role_bindings"].insert(0, second)
    before = deepcopy(profile)

    result = validate_pipeline_profile(profile)

    assert profile == before
    assert [row["role_id"] for row in result["role_bindings"]] == [
        "d2l.auditor.term_policy",
        "d2l.builder.candidate_discovery",
    ]


def _usage() -> dict:
    return {
        "schema_version": "llm_attempt_usage_v1",
        "attempt_usage_id": "run1.attempt1.request1",
        "seal_sha256": SHA_A,
        "logical_request_id": "request1",
        "logical_request_sha256": SHA_B,
        "semantic_attempt_index": 1,
        "transport_retry_ordinal": 0,
        "physical_attempt_index": 1,
        "request_id": "provider-request-1",
        "source_id": "local_gpt_gateway_v1",
        "source_revision": "gateway_profile_v1",
        "physical_quota_bucket_id": "local-gpt-gateway-v1",
        "requested_model_id": "gpt-5.5",
        "observed_model_id": "gpt-5.5-2026-07-18",
        "started_at_utc": "2026-07-19T00:00:00Z",
        "finished_at_utc": "2026-07-19T00:00:03Z",
        "latency_ms": 3000,
        "outcome": "succeeded",
        "finish_reason": "stop",
        "prompt_tokens": 100,
        "cached_input_tokens": 40,
        "completion_tokens": 30,
        "reasoning_tokens": 20,
        "total_tokens": 130,
        "cost_usd": None,
        "cost_status": "unknown",
        "cost_provenance": {
            "kind": "unavailable",
            "reference_id": None,
            "reference_sha256": None,
        },
        "provider_usage_sha256": SHA_B,
        "error_id": None,
    }


def test_usage_reasoning_is_not_double_counted_and_unknown_cost_stays_null() -> None:
    result = validate_llm_attempt_usage(_usage())
    assert result["total_tokens"] == 130
    assert result["reasoning_tokens"] == 20
    assert result["cost_usd"] is None
    assert result["finish_reason"] == "stop"
    assert result["latency_ms"] == 3000


@pytest.mark.parametrize(
    "field,value",
    [
        ("total_tokens", 150),
        ("reasoning_tokens", 31),
        ("cached_input_tokens", 101),
        ("cost_usd", 0.0),
        ("prompt_tokens", float("nan")),
    ],
)
def test_usage_rejects_false_accounting(field, value) -> None:
    row = _usage()
    row[field] = value
    with pytest.raises(ContractValidationError):
        validate_llm_attempt_usage(row)


def test_failed_before_request_has_no_fake_zero_usage() -> None:
    row = _usage()
    row.update(
        {
            "outcome": "failed_before_request",
            "observed_model_id": None,
            "prompt_tokens": None,
            "cached_input_tokens": None,
            "completion_tokens": None,
            "reasoning_tokens": None,
            "total_tokens": None,
            "finish_reason": None,
            "provider_usage_sha256": None,
            "error_id": "gateway_dns_failure",
        }
    )
    result = validate_llm_attempt_usage(row)
    assert result["total_tokens"] is None


def test_usage_cost_provenance_matches_cost_status() -> None:
    row = _usage()
    row.update({"cost_status": "reported", "cost_usd": 0.25})
    with pytest.raises(ContractValidationError, match="provider provenance"):
        validate_llm_attempt_usage(row)
    row["cost_provenance"] = {
        "kind": "provider_reported",
        "reference_id": "provider_usage_record_v1",
        "reference_sha256": SHA_B,
    }
    assert validate_llm_attempt_usage(row)["cost_usd"] == 0.25


def _error(category: str = "pipeline_semantic") -> dict:
    return {
        "schema_version": "llm_error_v1",
        "error_id": "semantic_contract_failure",
        "seal_sha256": SHA_A,
        "attempt_usage_id": "run1.attempt1.request1",
        "category": category,
        "code": "validator_rejected",
        "retry_class": category,
        "safe_message": "The locally validated response was rejected.",
        "details_sha256": SHA_B,
        "source_health_effect": "none",
        "retry_disposition": "semantic_retry_allowed",
        "occurred_at_utc": "2026-07-19T00:00:03Z",
    }


def test_semantic_error_cannot_mark_source_dead() -> None:
    row = _error()
    row["source_health_effect"] = "temporary_unavailable"
    with pytest.raises(ContractValidationError, match="cannot mark"):
        validate_llm_error(row)


def test_error_rejects_secret_shaped_message() -> None:
    row = _error()
    row["safe_message"] = "Bearer " + ("x" * 26)
    with pytest.raises(ContractValidationError, match="secret-shaped"):
        validate_llm_error(row)


def test_authentication_error_cannot_be_marked_retryable() -> None:
    row = _error("transport")
    row.update(
        {
            "code": "http_401",
            "retry_class": "authentication",
            "source_health_effect": "none",
            "retry_disposition": "transport_retry_allowed",
        }
    )
    with pytest.raises(ContractValidationError, match="not retryable"):
        validate_llm_error(row)
    row.update(
        {
            "retry_class": "connection",
            "retry_disposition": "transport_retry_allowed",
        }
    )
    with pytest.raises(ContractValidationError, match="HTTP error code"):
        validate_llm_error(row)


def _cache(kind: str, status: str) -> dict:
    return {
        "schema_version": "cache_observation_v1",
        "observation_id": "run1.cache1",
        "seal_sha256": SHA_A,
        "logical_request_id": "request1",
        "logical_request_sha256": SHA_B,
        "attempt_usage_id": None,
        "cache_kind": kind,
        "cache_namespace": "d2l.builder.candidate_discovery.cache",
        "cache_key_sha256": SHA_B,
        "lookup_status": status,
        "provider_call_avoided": False,
        "provider_cached_input_tokens": None,
        "reused_artifact_sha256": None,
        "producer_seal_sha256": None,
        "producer_input_bindings_sha256": None,
        "producer_artifact_receipt_sha256": None,
        "observed_at_utc": "2026-07-19T00:00:03Z",
    }


def test_provider_prompt_cache_never_claims_avoided_call() -> None:
    row = _cache("provider_prompt_cache", "hit")
    row["attempt_usage_id"] = "run1.attempt1.request1"
    row["provider_cached_input_tokens"] = 80
    assert validate_cache_observation(row)["provider_call_avoided"] is False
    row["provider_call_avoided"] = True
    with pytest.raises(ContractValidationError, match="still performs"):
        validate_cache_observation(row)


def test_application_response_cache_hit_requires_reused_artifact() -> None:
    row = _cache("application_response_cache", "hit")
    row["provider_call_avoided"] = True
    with pytest.raises(ContractValidationError, match="producer lineage"):
        validate_cache_observation(row)
    row["reused_artifact_sha256"] = SHA_A
    row["producer_seal_sha256"] = SHA_A
    row["producer_input_bindings_sha256"] = SHA_B
    row["producer_artifact_receipt_sha256"] = SHA_A
    assert validate_cache_observation(row)["provider_call_avoided"] is True


def test_retrieval_cache_cannot_claim_provider_call_avoidance() -> None:
    row = _cache("retrieval_context_cache", "hit")
    row["provider_call_avoided"] = True
    row["reused_artifact_sha256"] = SHA_A
    with pytest.raises(ContractValidationError, match="does not avoid"):
        validate_cache_observation(row)


def test_checkpoint_reuse_requires_exact_reused_artifact() -> None:
    row = _cache("checkpoint_stage_reuse", "hit")
    with pytest.raises(ContractValidationError, match="producer lineage"):
        validate_cache_observation(row)
    row["provider_call_avoided"] = True
    row["reused_artifact_sha256"] = SHA_A
    row["producer_seal_sha256"] = SHA_A
    row["producer_input_bindings_sha256"] = SHA_B
    row["producer_artifact_receipt_sha256"] = SHA_A
    assert validate_cache_observation(row)["provider_call_avoided"] is True


def test_reusable_artifact_receipt_is_content_addressed() -> None:
    body = {
        "schema_version": "reusable_artifact_receipt_v1",
        "producer_seal_sha256": SHA_A,
        "producer_logical_request_id": "request1",
        "producer_logical_request_sha256": SHA_B,
        "artifact_kind": "application_response",
        "artifact_sha256": SHA_A,
        "created_at_utc": "2026-07-19T00:00:03Z",
    }
    receipt = {**body, "receipt_sha256": canonical_sha256(body)}
    assert validate_reusable_artifact_receipt(receipt) == receipt
    receipt["artifact_sha256"] = SHA_B
    with pytest.raises(ContractValidationError, match="receipt hash"):
        validate_reusable_artifact_receipt(receipt)


def test_canonical_json_is_byte_stable_and_rejects_nonfinite() -> None:
    left = {"b": [2, 1], "a": {"x": "y"}}
    right = {"a": {"x": "y"}, "b": [2, 1]}
    assert canonical_json(left).encode("utf-8") == canonical_json(right).encode("utf-8")
    assert canonical_sha256(left) == canonical_sha256(right)
    with pytest.raises(ContractValidationError):
        canonical_json({"x": float("inf")})
