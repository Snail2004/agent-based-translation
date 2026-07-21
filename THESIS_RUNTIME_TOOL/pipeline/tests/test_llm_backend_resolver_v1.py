from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from pipeline.llm_backend import (
    ContractValidationError,
    canonical_json,
    canonical_sha256,
    create_reusable_artifact_receipt,
    derive_cache_key_sha256,
    derive_llm_attempt_identity,
    resolve_llm_run_seal,
    validate_llm_run_records,
    validate_resolved_llm_run_seal,
)


FIXTURES = Path(__file__).parent / "fixtures" / "llm_backend_v1"
SHA_A = "a" * 64
SHA_B = "b" * 64


def _load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _profile(index: int = 0) -> dict:
    return _load("profile_four_workstreams.json")["profiles"][index]


def _resolve(
    profile: dict | None = None,
    *,
    inputs: list[dict] | None = None,
    stage_id: str = "phase2a_fixture",
    run_id: str = "run_fixture_001",
    attempt_run_id: str = "attempt_fixture_001",
):
    selected = profile or _profile()
    role = selected["role_bindings"][0]
    return resolve_llm_run_seal(
        profile=selected,
        api_sources=[_load("source_local.json")],
        capability_evidence=[_load("capability_native.json")],
        role_id=role["role_id"],
        run_id=run_id,
        attempt_run_id=attempt_run_id,
        stage_id=stage_id,
        input_bindings=inputs
        or [
            {"name": "source_package", "sha256": "1" * 64},
            {"name": "rendered_prompt", "sha256": "2" * 64},
        ],
    )


def _usage(
    seal: dict,
    *,
    outcome: str = "succeeded",
    error_id: str | None = None,
    logical_request_id: str = "request_1",
    semantic_attempt_index: int = 1,
    transport_retry_ordinal: int = 0,
    physical_attempt_index: int = 1,
) -> dict:
    source = seal["primary"]["source"]
    capability = seal["primary"]["capability"]
    failed_before_request = outcome == "failed_before_request"
    lineage = derive_llm_attempt_identity(
        seal=seal,
        logical_request_id=logical_request_id,
        semantic_attempt_index=semantic_attempt_index,
        transport_retry_ordinal=transport_retry_ordinal,
    )
    return {
        "schema_version": "llm_attempt_usage_v1",
        **lineage,
        "seal_sha256": seal["seal_sha256"],
        "physical_attempt_index": physical_attempt_index,
        "request_id": None
        if failed_before_request
        else f"provider-request-{physical_attempt_index}",
        "source_id": source["source_id"],
        "source_revision": source["source_revision"],
        "physical_quota_bucket_id": source["physical_quota_bucket_id"],
        "requested_model_id": seal["primary"]["target"]["requested_model_id"],
        "observed_model_id": None
        if failed_before_request
        else capability["observed_model_id"],
        "started_at_utc": "2026-07-19T00:00:00Z",
        "finished_at_utc": "2026-07-19T00:00:03Z",
        "latency_ms": 3000,
        "outcome": outcome,
        "finish_reason": None if failed_before_request else (
            "stop" if outcome == "succeeded" else "error"
        ),
        "prompt_tokens": None if failed_before_request else 100,
        "cached_input_tokens": None if failed_before_request else 0,
        "completion_tokens": None if failed_before_request else 30,
        "reasoning_tokens": None if failed_before_request else 10,
        "total_tokens": None if failed_before_request else 130,
        "cost_usd": None if failed_before_request else 0.25,
        "cost_status": "unknown" if failed_before_request else "calculated",
        "cost_provenance": {
            "kind": "unavailable" if failed_before_request else "pricing_manifest",
            "reference_id": None if failed_before_request else "fixture_pricing_v1",
            "reference_sha256": None if failed_before_request else SHA_A,
        },
        "provider_usage_sha256": None if failed_before_request else SHA_B,
        "error_id": error_id,
    }


def _error(seal: dict, usage: dict) -> dict:
    return {
        "schema_version": "llm_error_v1",
        "error_id": usage["error_id"],
        "seal_sha256": seal["seal_sha256"],
        "attempt_usage_id": usage["attempt_usage_id"],
        "category": "pipeline_semantic",
        "code": "validator_rejected",
        "retry_class": "pipeline_semantic",
        "safe_message": "The locally validated response was rejected.",
        "details_sha256": SHA_A,
        "source_health_effect": "none",
        "retry_disposition": "semantic_retry_allowed",
        "occurred_at_utc": "2026-07-19T00:00:03Z",
    }


def _cache(seal: dict, *, logical_request_id: str = "request_1") -> dict:
    logical = derive_llm_attempt_identity(
        seal=seal,
        logical_request_id=logical_request_id,
        semantic_attempt_index=1,
        transport_retry_ordinal=0,
    )
    receipt = _receipt(seal, logical_request_id=logical_request_id)
    return {
        "schema_version": "cache_observation_v1",
        "observation_id": "run_fixture_001.cache_1",
        "seal_sha256": seal["seal_sha256"],
        "logical_request_id": logical_request_id,
        "logical_request_sha256": logical["logical_request_sha256"],
        "attempt_usage_id": None,
        "cache_kind": "application_response_cache",
        "cache_namespace": seal["cache_namespace"],
        "cache_key_sha256": derive_cache_key_sha256(
            seal=seal,
            logical_request_id=logical_request_id,
            cache_kind="application_response_cache",
        ),
        "lookup_status": "hit",
        "provider_call_avoided": True,
        "provider_cached_input_tokens": None,
        "reused_artifact_sha256": SHA_B,
        "producer_seal_sha256": seal["seal_sha256"],
        "producer_input_bindings_sha256": seal["input_bindings_sha256"],
        "producer_artifact_receipt_sha256": receipt["receipt_sha256"],
        "observed_at_utc": "2026-07-19T00:00:03Z",
    }


def _receipt(
    seal: dict,
    *,
    logical_request_id: str = "request_1",
    artifact_kind: str = "application_response",
) -> dict:
    return create_reusable_artifact_receipt(
        producer_seal=seal,
        logical_request_id=logical_request_id,
        artifact_kind=artifact_kind,
        artifact_sha256=SHA_B,
        created_at_utc="2026-07-19T00:00:03Z",
    )


def test_resolver_is_deterministic_detached_and_credential_free() -> None:
    profile = _profile()
    source = _load("source_local.json")
    capability = _load("capability_native.json")
    before = deepcopy((profile, source, capability))
    role = profile["role_bindings"][0]
    kwargs = {
        "profile": profile,
        "api_sources": [source],
        "capability_evidence": [capability],
        "role_id": role["role_id"],
        "run_id": "run_fixture_001",
        "attempt_run_id": "attempt_fixture_001",
        "stage_id": "phase2a_fixture",
        "input_bindings": [{"name": "source_package", "sha256": "1" * 64}],
    }

    first = resolve_llm_run_seal(**kwargs)
    second = resolve_llm_run_seal(**kwargs)

    assert (profile, source, capability) == before
    assert canonical_json(first).encode("utf-8") == canonical_json(second).encode(
        "utf-8"
    )
    assert first["seal_sha256"] == second["seal_sha256"]
    rendered = canonical_json(first).casefold()
    assert "credential_ref" in rendered
    assert "credential_commitment" in rendered
    assert "bearer" not in rendered
    assert "api_key" not in rendered


@pytest.mark.parametrize("index", [0, 1, 2, 3])
def test_resolver_accepts_each_workstream_profile_without_sharing_values(index) -> None:
    seal = _resolve(_profile(index))
    assert seal["workstream"] == _profile(index)["workstream"]
    assert seal["role_id"] == _profile(index)["role_bindings"][0]["role_id"]
    assert seal["role_binding"]["record"]["generation"] == _profile(index)[
        "role_bindings"
    ][0]["generation"]


def test_ordered_input_bindings_are_material_to_seal() -> None:
    first = _resolve(
        inputs=[
            {"name": "source_package", "sha256": "1" * 64},
            {"name": "rendered_prompt", "sha256": "2" * 64},
        ]
    )
    second = _resolve(
        inputs=[
            {"name": "rendered_prompt", "sha256": "2" * 64},
            {"name": "source_package", "sha256": "1" * 64},
        ]
    )
    assert first["seal_sha256"] != second["seal_sha256"]
    assert first["output_root_id"] != second["output_root_id"]
    assert first["checkpoint_namespace"] != second["checkpoint_namespace"]
    assert first["cache_namespace"] != second["cache_namespace"]


def test_resolver_rejects_duplicate_input_names() -> None:
    with pytest.raises(ContractValidationError, match="repeats a name"):
        _resolve(
            inputs=[
                {"name": "source_package", "sha256": "1" * 64},
                {"name": "source_package", "sha256": "2" * 64},
            ]
        )


def test_resolver_rejects_source_revision_drift() -> None:
    profile = _profile()
    source = _load("source_local.json")
    source["source_revision"] = "gateway_profile_v2"
    role = profile["role_bindings"][0]
    with pytest.raises(ContractValidationError, match="source revision mismatch"):
        resolve_llm_run_seal(
            profile=profile,
            api_sources=[source],
            capability_evidence=[_load("capability_native.json")],
            role_id=role["role_id"],
            run_id="run1",
            attempt_run_id="attempt1",
            stage_id="stage1",
            input_bindings=[{"name": "source", "sha256": "1" * 64}],
        )


@pytest.mark.parametrize(
    "field,value,match",
    [
        ("route_id", "responses_create", "route_id differs"),
        ("requested_model_id", "gpt-5.4", "requested model mismatch"),
        ("schema_sha256", "9" * 64, "schema mismatch"),
        ("local_validator_sha256", "8" * 64, "validator binding mismatch"),
    ],
)
def test_resolver_rejects_capability_drift(field, value, match) -> None:
    profile = _profile()
    capability = _load("capability_native.json")
    capability[field] = value
    role = profile["role_bindings"][0]
    with pytest.raises(ContractValidationError, match=match):
        resolve_llm_run_seal(
            profile=profile,
            api_sources=[_load("source_local.json")],
            capability_evidence=[capability],
            role_id=role["role_id"],
            run_id="run1",
            attempt_run_id="attempt1",
            stage_id="stage1",
            input_bindings=[{"name": "source", "sha256": "1" * 64}],
        )


def test_required_structured_output_rejects_json_only_capability() -> None:
    profile = _profile()
    capability = _load("capability_native.json")
    capability.update(
        {
            "capability_kind": "json_object",
        }
    )
    role = profile["role_bindings"][0]
    with pytest.raises(ContractValidationError, match="native structured-output"):
        resolve_llm_run_seal(
            profile=profile,
            api_sources=[_load("source_local.json")],
            capability_evidence=[capability],
            role_id=role["role_id"],
            run_id="run1",
            attempt_run_id="attempt1",
            stage_id="stage1",
            input_bindings=[{"name": "source", "sha256": "1" * 64}],
        )


def test_prompt_validated_mode_accepts_json_object_with_exact_local_validator() -> None:
    profile = _profile()
    profile["role_bindings"][0]["structured_output"]["mode"] = "prompt_validated"
    capability = _load("capability_native.json")
    capability["capability_kind"] = "json_object"
    role = profile["role_bindings"][0]
    role["primary"]["capability_record_sha256"] = canonical_sha256(capability)
    seal = resolve_llm_run_seal(
        profile=profile,
        api_sources=[_load("source_local.json")],
        capability_evidence=[capability],
        role_id=role["role_id"],
        run_id="run1",
        attempt_run_id="attempt1",
        stage_id="stage1",
        input_bindings=[{"name": "source", "sha256": "1" * 64}],
    )
    assert seal["primary"]["capability"]["capability_kind"] == "json_object"


def test_prompt_validated_mode_rejects_native_structured_output() -> None:
    profile = _profile()
    profile["role_bindings"][0]["structured_output"]["mode"] = "prompt_validated"
    with pytest.raises(ContractValidationError, match="prompt-validated JSON"):
        _resolve(profile)


def test_source_catalog_rejects_false_independent_quota_alias() -> None:
    profile = _profile()
    source = _load("source_local.json")
    alias = deepcopy(source)
    alias.update(
        {
            "source_id": "local_gateway_alias_v1",
            "source_revision": "alias_v1",
            "adapter_id": "renamed_adapter_v2",
            "protocol": "openai_responses",
            "physical_quota_bucket_id": "fake-independent-bucket",
        }
    )
    role = profile["role_bindings"][0]
    with pytest.raises(ContractValidationError, match="physical source identity"):
        resolve_llm_run_seal(
            profile=profile,
            api_sources=[source, alias],
            capability_evidence=[_load("capability_native.json")],
            role_id=role["role_id"],
            run_id="run1",
            attempt_run_id="attempt1",
            stage_id="stage1",
            input_bindings=[{"name": "source", "sha256": "1" * 64}],
        )


@pytest.mark.parametrize("name", ["human_reference", "result_callback"])
def test_evaluation_seal_rejects_authority_input_binding(name) -> None:
    with pytest.raises(ContractValidationError, match=name):
        _resolve(
            _profile(3), inputs=[{"name": name, "sha256": "1" * 64}]
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_id", "evaluation_human_reference_source_v1"),
        ("source_revision", "evaluation_human_reference_revision_v1"),
        ("capability_id", "evaluation_result_callback_capability_v1"),
        ("capability_revision", "evaluation_result_callback_revision_v1"),
        ("requested_model_id", "human_reference/model-v1"),
    ],
)
def test_evaluation_profile_rejects_authority_target_ids(field, value) -> None:
    profile = _profile(3)
    profile["role_bindings"][0]["primary"][field] = value
    with pytest.raises(ContractValidationError, match="human_reference|result_callback"):
        _resolve(profile)


@pytest.mark.parametrize(
    "record,field,value",
    [
        ("source", "route_id", "human_reference_route_v1"),
        ("source", "adapter_id", "result_callback_adapter_v1"),
        ("capability", "probe_id", "gold_reference_probe_v1"),
    ],
)
def test_evaluation_seal_rejects_authority_in_resolved_catalog_records(
    record, field, value
) -> None:
    profile = _profile(3)
    role = profile["role_bindings"][0]
    source = _load("source_local.json")
    capability = _load("capability_native.json")
    if record == "source":
        source[field] = value
        if field in capability:
            capability[field] = value
    else:
        capability[field] = value
    role["primary"]["source_record_sha256"] = canonical_sha256(source)
    role["primary"]["capability_record_sha256"] = canonical_sha256(capability)
    with pytest.raises(
        ContractValidationError, match="human_reference|result_callback|gold"
    ):
        resolve_llm_run_seal(
            profile=profile,
            api_sources=[source],
            capability_evidence=[capability],
            role_id=role["role_id"],
            run_id="run_fixture_001",
            attempt_run_id="attempt_fixture_001",
            stage_id="phase2a_fixture",
            input_bindings=[{"name": "source", "sha256": "1" * 64}],
        )


def test_resolver_does_not_infer_fallback() -> None:
    seal = _resolve()
    assert seal["fallback_plan"] == {"enabled": False, "steps": []}


def test_explicit_fallback_is_fully_resolved() -> None:
    profile = _profile()
    role = profile["role_bindings"][0]
    remote = _load("source_remote.json")
    fallback_capability = _load("capability_native.json")
    fallback_capability.update(
        {
            "capability_id": "remote_fixture_native_so_v1",
            "capability_revision": "probe_remote_v1",
            "source_id": remote["source_id"],
            "source_revision": remote["source_revision"],
            "adapter_id": remote["adapter_id"],
            "protocol": remote["protocol"],
            "route_id": remote["route_id"],
            "base_url": remote["base_url"],
            "probe_id": "remote_fixture_probe_v1",
        }
    )
    target = {
        "source_id": remote["source_id"],
        "source_revision": remote["source_revision"],
        "source_record_sha256": canonical_sha256(remote),
        "requested_model_id": "gpt-5.5",
        "capability_id": fallback_capability["capability_id"],
        "capability_revision": fallback_capability["capability_revision"],
        "capability_record_sha256": canonical_sha256(fallback_capability),
    }
    role["fallback_plan"] = {"enabled": True, "steps": [target]}

    seal = resolve_llm_run_seal(
        profile=profile,
        api_sources=[_load("source_local.json"), remote],
        capability_evidence=[_load("capability_native.json"), fallback_capability],
        role_id=role["role_id"],
        run_id="run1",
        attempt_run_id="attempt1",
        stage_id="stage1",
        input_bindings=[{"name": "source", "sha256": "1" * 64}],
    )
    assert seal["fallback_plan"]["enabled"] is True
    assert seal["fallback_plan"]["steps"][0]["source"]["source_id"] == remote[
        "source_id"
    ]


def test_namespace_material_changes_with_profile_revision_and_generation() -> None:
    profile = _profile()
    first = _resolve(profile)
    profile["profile_revision"] = "v2"
    profile["role_bindings"][0]["generation"]["temperature"] = 0.3
    second = _resolve(profile)
    assert first["namespace_material_sha256"] != second[
        "namespace_material_sha256"
    ]
    assert first["output_root_id"] != second["output_root_id"]
    assert first["checkpoint_namespace"] != second["checkpoint_namespace"]
    assert first["cache_namespace"] != second["cache_namespace"]


def test_input_change_invalidates_all_writable_namespaces_and_cache_key() -> None:
    first = _resolve(inputs=[{"name": "source", "sha256": "1" * 64}])
    second = _resolve(inputs=[{"name": "source", "sha256": "2" * 64}])
    assert first["input_bindings_sha256"] != second["input_bindings_sha256"]
    assert first["output_root_id"] != second["output_root_id"]
    assert first["checkpoint_namespace"] != second["checkpoint_namespace"]
    assert first["cache_namespace"] != second["cache_namespace"]
    assert derive_cache_key_sha256(
        seal=first,
        logical_request_id="request_1",
        cache_kind="application_response_cache",
    ) != derive_cache_key_sha256(
        seal=second,
        logical_request_id="request_1",
        cache_kind="application_response_cache",
    )


def test_stage_change_invalidates_all_writable_namespaces() -> None:
    first = _resolve(stage_id="stage_a")
    second = _resolve(stage_id="stage_b")

    assert first["output_root_id"] != second["output_root_id"]
    assert first["checkpoint_namespace"] != second["checkpoint_namespace"]
    assert first["cache_namespace"] != second["cache_namespace"]


def test_run_record_collection_accepts_exact_seal_relative_rows() -> None:
    seal = _resolve()
    usage = _usage(seal)
    cache = _cache(seal)
    result = validate_llm_run_records(
        seal=seal,
        usage_rows=[usage],
        cache_observations=[cache],
        reusable_artifact_receipts=[_receipt(seal)],
    )
    assert result["usage_rows"][0]["attempt_usage_id"] == usage["attempt_usage_id"]
    assert result["cache_observations"][0]["cache_namespace"] == seal[
        "cache_namespace"
    ]


def test_provider_prompt_cache_is_bound_to_exact_physical_attempt() -> None:
    seal = _resolve()
    usage = _usage(seal)
    cache = _cache(seal)
    cache.update(
        {
            "cache_kind": "provider_prompt_cache",
            "cache_key_sha256": derive_cache_key_sha256(
                seal=seal,
                logical_request_id="request_1",
                cache_kind="provider_prompt_cache",
            ),
            "attempt_usage_id": usage["attempt_usage_id"],
            "provider_call_avoided": False,
            "provider_cached_input_tokens": 50,
            "reused_artifact_sha256": None,
            "producer_seal_sha256": None,
            "producer_input_bindings_sha256": None,
            "producer_artifact_receipt_sha256": None,
        }
    )
    usage["cached_input_tokens"] = 50
    assert validate_llm_run_records(
        seal=seal, usage_rows=[usage], cache_observations=[cache]
    )["cache_observations"][0]["attempt_usage_id"] == usage["attempt_usage_id"]
    cache["attempt_usage_id"] = "f" * 64
    with pytest.raises(ContractValidationError, match="foreign attempt usage"):
        validate_llm_run_records(
            seal=seal, usage_rows=[usage], cache_observations=[cache]
        )


def test_provider_prompt_cache_miss_uses_attempt_usage_without_fake_hit_tokens() -> None:
    seal = _resolve()
    usage = _usage(seal)
    cache = _cache(seal)
    cache.update(
        {
            "cache_kind": "provider_prompt_cache",
            "cache_key_sha256": derive_cache_key_sha256(
                seal=seal,
                logical_request_id="request_1",
                cache_kind="provider_prompt_cache",
            ),
            "lookup_status": "miss",
            "provider_call_avoided": False,
            "provider_cached_input_tokens": None,
            "reused_artifact_sha256": None,
            "producer_seal_sha256": None,
            "producer_input_bindings_sha256": None,
            "producer_artifact_receipt_sha256": None,
            "attempt_usage_id": usage["attempt_usage_id"],
        }
    )

    result = validate_llm_run_records(
        seal=seal, usage_rows=[usage], cache_observations=[cache]
    )
    assert result["cache_observations"][0]["lookup_status"] == "miss"


@pytest.mark.parametrize(
    "field,value,match",
    [
        ("source_id", "foreign_source_v1", "source, bucket or requested model"),
        ("source_revision", "foreign_revision_v1", "source, bucket or requested model"),
        ("physical_quota_bucket_id", "foreign-bucket", "source, bucket or requested model"),
        ("requested_model_id", "gpt-5.4", "source, bucket or requested model"),
        ("observed_model_id", "gpt-5.4-foreign", "observed model differs"),
    ],
)
def test_run_record_collection_rejects_foreign_usage(field, value, match) -> None:
    seal = _resolve()
    usage = _usage(seal)
    usage[field] = value
    with pytest.raises(ContractValidationError, match=match):
        validate_llm_run_records(seal=seal, usage_rows=[usage])


def test_run_record_collection_rejects_foreign_seal_on_every_record_type() -> None:
    seal = _resolve()
    usage = _usage(seal)
    usage["seal_sha256"] = SHA_A
    with pytest.raises(ContractValidationError, match="usage row.*foreign seal"):
        validate_llm_run_records(seal=seal, usage_rows=[usage])

    cache = _cache(seal)
    cache["seal_sha256"] = SHA_A
    with pytest.raises(ContractValidationError, match="cache observation.*foreign seal"):
        validate_llm_run_records(seal=seal, cache_observations=[cache])

    failed = _usage(
        seal,
        outcome="failed_after_request",
        error_id="semantic_contract_failure",
    )
    error = _error(seal, failed)
    error["seal_sha256"] = SHA_A
    with pytest.raises(ContractValidationError, match="error row.*foreign seal"):
        validate_llm_run_records(
            seal=seal, usage_rows=[failed], error_rows=[error]
        )


def test_run_record_collection_rejects_duplicate_physical_request() -> None:
    seal = _resolve()
    first = _usage(seal)
    second = _usage(
        seal,
        logical_request_id="request_2",
        physical_attempt_index=2,
    )
    second["request_id"] = first["request_id"]
    with pytest.raises(ContractValidationError, match="request_id"):
        validate_llm_run_records(seal=seal, usage_rows=[first, second])


def test_run_record_collection_binds_error_retry_to_sealed_policy() -> None:
    seal = _resolve()
    usage = _usage(
        seal,
        outcome="failed_after_request",
        error_id="semantic_contract_failure",
    )
    error = _error(seal, usage)
    with pytest.raises(ContractValidationError, match="sealed retry policy"):
        validate_llm_run_records(
            seal=seal, usage_rows=[usage], error_rows=[error]
        )


def test_run_record_collection_accepts_retry_only_when_profile_allows_it() -> None:
    profile = _profile()
    profile["role_bindings"][0]["semantic_retry"] = {
        "max_retries": 1,
        "retryable_categories": ["pipeline_semantic"],
    }
    seal = _resolve(profile)
    usage = _usage(
        seal,
        outcome="failed_after_request",
        error_id="semantic_contract_failure",
    )
    error = _error(seal, usage)
    result = validate_llm_run_records(
        seal=seal, usage_rows=[usage], error_rows=[error]
    )
    assert result["error_rows"][0]["retry_disposition"] == "semantic_retry_allowed"


def test_run_record_collection_enforces_semantic_retry_count_per_logical_request() -> None:
    profile = _profile()
    profile["role_bindings"][0]["semantic_retry"] = {
        "max_retries": 1,
        "retryable_categories": ["pipeline_semantic"],
    }
    seal = _resolve(profile)
    rows = []
    errors = []
    for index in (1, 2):
        error_id = f"semantic_failure_{index}"
        usage = _usage(
            seal,
            outcome="failed_after_request",
            error_id=error_id,
            semantic_attempt_index=index,
            physical_attempt_index=index,
        )
        rows.append(usage)
        errors.append(_error(seal, usage))
    rows.append(
        _usage(
            seal,
            semantic_attempt_index=3,
            physical_attempt_index=3,
        )
    )
    with pytest.raises(ContractValidationError, match="semantic attempts exceed"):
        validate_llm_run_records(seal=seal, usage_rows=rows, error_rows=errors)


def test_run_record_collection_rejects_unknown_usage_under_finite_caps() -> None:
    seal = _resolve()
    usage = _usage(seal)
    for field in (
        "prompt_tokens",
        "cached_input_tokens",
        "completion_tokens",
        "reasoning_tokens",
        "total_tokens",
    ):
        usage[field] = None
    usage.update(
        {
            "cost_usd": None,
            "cost_status": "unknown",
            "cost_provenance": {
                "kind": "unavailable",
                "reference_id": None,
                "reference_sha256": None,
            },
        }
    )
    with pytest.raises(ContractValidationError, match="cannot certify"):
        validate_llm_run_records(seal=seal, usage_rows=[usage])


def test_run_record_collection_rejects_foreign_cache_namespace() -> None:
    seal = _resolve()
    cache = _cache(seal)
    cache["cache_namespace"] = "literary.foreign.cache"
    with pytest.raises(ContractValidationError, match="foreign namespace"):
        validate_llm_run_records(seal=seal, cache_observations=[cache])


def test_checkpoint_reuse_rejects_foreign_producer_input() -> None:
    seal = _resolve()
    cache = _cache(seal)
    cache.update(
        {
            "cache_kind": "checkpoint_stage_reuse",
            "cache_key_sha256": derive_cache_key_sha256(
                seal=seal,
                logical_request_id="request_1",
                cache_kind="checkpoint_stage_reuse",
            ),
            "producer_input_bindings_sha256": SHA_A,
        }
    )
    receipt = _receipt(seal, artifact_kind="checkpoint_stage")
    cache["producer_artifact_receipt_sha256"] = receipt["receipt_sha256"]
    with pytest.raises(ContractValidationError, match="producer inputs"):
        validate_llm_run_records(
            seal=seal,
            cache_observations=[cache],
            reusable_artifact_receipts=[
                receipt
            ],
        )


def test_reusable_cache_requires_trusted_producer_receipt() -> None:
    seal = _resolve()
    with pytest.raises(ContractValidationError, match="receipt is not trusted"):
        validate_llm_run_records(seal=seal, cache_observations=[_cache(seal)])


@pytest.mark.parametrize("drift", ["stage", "profile"])
def test_reusable_cache_rejects_foreign_producer_material(drift) -> None:
    producer = _resolve(stage_id="stage_a")
    if drift == "stage":
        consumer = _resolve(stage_id="stage_b")
    else:
        profile = _profile()
        profile["profile_revision"] = "revision_v2"
        profile["role_bindings"][0]["generation"]["temperature"] = 0.3
        consumer = _resolve(profile, stage_id="stage_a")
    receipt = _receipt(producer)
    cache = _cache(consumer)
    cache.update(
        {
            "producer_seal_sha256": producer["seal_sha256"],
            "producer_input_bindings_sha256": producer["input_bindings_sha256"],
            "producer_artifact_receipt_sha256": receipt["receipt_sha256"],
        }
    )
    with pytest.raises(ContractValidationError, match="producer .* differs"):
        validate_llm_run_records(
            seal=consumer,
            cache_observations=[cache],
            producer_seals=[producer],
            reusable_artifact_receipts=[receipt],
        )


def test_reusable_cache_accepts_same_material_across_run_attempts() -> None:
    producer = _resolve(run_id="run_a", attempt_run_id="attempt_a")
    consumer = _resolve(run_id="run_b", attempt_run_id="attempt_b")
    receipt = _receipt(producer)
    cache = _cache(consumer)
    cache.update(
        {
            "producer_seal_sha256": producer["seal_sha256"],
            "producer_input_bindings_sha256": producer["input_bindings_sha256"],
            "producer_artifact_receipt_sha256": receipt["receipt_sha256"],
        }
    )
    result = validate_llm_run_records(
        seal=consumer,
        cache_observations=[cache],
        producer_seals=[producer],
        reusable_artifact_receipts=[receipt],
    )
    assert result["cache_observations"][0]["producer_seal_sha256"] == producer[
        "seal_sha256"
    ]


def test_run_record_collection_rejects_usage_above_stage_budget() -> None:
    profile = _profile()
    profile["role_bindings"][0]["limits"]["max_prompt_tokens"] = 100
    profile["role_bindings"][0]["generation"]["max_input_tokens"] = 100
    seal = _resolve(profile)
    first = _usage(seal)
    second = _usage(
        seal,
        logical_request_id="request_2",
        physical_attempt_index=2,
    )
    with pytest.raises(ContractValidationError, match="max_prompt_tokens"):
        validate_llm_run_records(seal=seal, usage_rows=[first, second])


def test_standalone_seal_validation_rejects_tampering() -> None:
    seal = _resolve()
    assert validate_resolved_llm_run_seal(seal) == seal

    tampered = deepcopy(seal)
    tampered["primary"]["source"]["route_id"] = "responses_create"
    with pytest.raises(ContractValidationError):
        validate_resolved_llm_run_seal(tampered)

    resealed = deepcopy(tampered)
    body = {key: value for key, value in resealed.items() if key != "seal_sha256"}
    resealed["seal_sha256"] = canonical_sha256(body)
    with pytest.raises(ContractValidationError, match="route_id differs"):
        validate_resolved_llm_run_seal(resealed)


def test_standalone_seal_rejects_unknown_field_even_if_resealed() -> None:
    seal = _resolve()
    seal["hidden_retry"] = 2
    body = {key: value for key, value in seal.items() if key != "seal_sha256"}
    seal["seal_sha256"] = canonical_sha256(body)
    with pytest.raises(ContractValidationError, match="extra"):
        validate_resolved_llm_run_seal(seal)


@pytest.mark.parametrize(
    "record,field,value,match",
    [
        ("source", "physical_quota_bucket_id", "forged-bucket", "source record hash"),
        ("capability", "observed_model_id", "forged-model", "capability record hash"),
    ],
)
def test_standalone_seal_rejects_resealed_catalog_record_drift(
    record, field, value, match
) -> None:
    seal = _resolve()
    seal["primary"][record][field] = value
    body = {key: item for key, item in seal.items() if key != "seal_sha256"}
    seal["seal_sha256"] = canonical_sha256(body)
    with pytest.raises(ContractValidationError, match=match):
        validate_resolved_llm_run_seal(seal)


def test_old_runtime_rows_cannot_be_relabelled_to_materially_different_seal() -> None:
    first = _resolve(inputs=[{"name": "source", "sha256": "1" * 64}])
    second = _resolve(inputs=[{"name": "source", "sha256": "2" * 64}])
    usage = _usage(first)
    usage["seal_sha256"] = second["seal_sha256"]
    with pytest.raises(ContractValidationError, match="logical request lineage"):
        validate_llm_run_records(seal=second, usage_rows=[usage])

    cache = _cache(first)
    cache["seal_sha256"] = second["seal_sha256"]
    cache["cache_namespace"] = second["cache_namespace"]
    with pytest.raises(ContractValidationError, match="logical request lineage"):
        validate_llm_run_records(seal=second, cache_observations=[cache])
