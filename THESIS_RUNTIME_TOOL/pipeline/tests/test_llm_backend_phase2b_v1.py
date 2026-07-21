from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import pickle

import pytest

from pipeline.llm_backend import (
    ApplicationResponseCache,
    ContentAddressedArtifactStore,
    ContractValidationError,
    MappingCredentialProvider,
    PhysicalQuotaScheduler,
    PreparedTransportRequest,
    QuotaBusyError,
    RawTransportResponse,
    SharedLlmAttemptLedger,
    SharedLlmBackend,
    TransportCallError,
    UncertifiedAttemptError,
    canonical_json,
    canonical_sha256,
    credential_commitment,
    normalize_provider_response,
    prepare_transport_request,
    resolve_llm_run_seal,
    resolve_source_credential,
)


FIXTURES = Path(__file__).parent / "fixtures" / "llm_backend_v1"
SECRET = "phase2b-fixture-secret"
SHA_A = "a" * 64


def _load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _sealed(*, configure_role=None) -> tuple[dict, dict]:
    profile = _load("profile_four_workstreams.json")["profiles"][0]
    source = _load("source_local.json")
    capability = _load("capability_native.json")
    source["credential_commitment"] = credential_commitment(SECRET)
    role = profile["role_bindings"][0]
    if configure_role is not None:
        configure_role(role)
    role["primary"]["source_record_sha256"] = canonical_sha256(source)
    request_body = {
        "model": role["primary"]["requested_model_id"],
        "messages": [{"role": "user", "content": "fixture prompt"}],
    }
    seal = resolve_llm_run_seal(
        profile=profile,
        api_sources=[source],
        capability_evidence=[capability],
        role_id=role["role_id"],
        run_id="phase2b_run",
        attempt_run_id="phase2b_attempt",
        stage_id="phase2b_stage",
        input_bindings=[
            {
                "name": "transport_request_body",
                "sha256": canonical_sha256(request_body),
            }
        ],
    )
    return seal, request_body


def _sealed_with_transport_retry(*, sufficient_budget: bool) -> tuple[dict, dict]:
    def configure(role):
        role["transport_retry"] = {
            "max_retries": 1,
            "backoff_policy": "exponential",
            "initial_delay_ms": 100,
            "max_delay_ms": 100,
            "retryable_codes": ["server_unavailable"],
        }
        role["limits"].update(
            {
                "max_calls": 2,
                "max_prompt_tokens": 10_200 if sufficient_budget else 10_000,
                "max_completion_tokens": 4_200 if sufficient_budget else 4_096,
                "max_total_tokens": 14_400 if sufficient_budget else 14_096,
                "max_cost_usd": None,
            }
        )

    return _sealed(configure_role=configure)


def _sealed_for_protocol(protocol: str) -> tuple[dict, dict]:
    profile = _load("profile_four_workstreams.json")["profiles"][0]
    role = profile["role_bindings"][0]
    source = _load("source_local.json")
    capability = _load("capability_native.json")
    if protocol == "openai_responses":
        source = _load("source_remote.json")
        source["credential_commitment"] = credential_commitment(SECRET)
        request_body = {
            "model": "gpt-5.5",
            "input": "fixture prompt",
        }
    elif protocol == "google_genai_generate_content":
        source.update(
            {
                "adapter_id": "google_genai_rest_v1",
                "base_url": "https://generativelanguage.googleapis.com/v1beta",
                "credential_commitment": credential_commitment(SECRET),
                "credential_ref": "credential.google_fixture_v1",
                "endpoint_class": "remote",
                "physical_quota_bucket_id": "google-fixture-v1",
                "protocol": protocol,
                "route_id": "models_generate_content",
                "source_class": "remote_api",
                "source_id": "google_fixture_v1",
                "source_revision": "google_fixture_revision_v1",
            }
        )
        request_body = {
            "contents": [{"role": "user", "parts": [{"text": "fixture prompt"}]}]
        }
    elif protocol == "local_in_process":
        source.update(
            {
                "adapter_id": "local_callback_v1",
                "base_url": None,
                "credential_commitment": None,
                "credential_ref": None,
                "endpoint_class": "in_process",
                "physical_quota_bucket_id": "local-callback-v1",
                "protocol": protocol,
                "route_id": "local_callback",
                "source_class": "local_in_process",
                "source_id": "local_callback_v1",
                "source_revision": "local_callback_revision_v1",
            }
        )
        request_body = {"prompt": "fixture prompt"}
    else:
        raise AssertionError(f"unsupported fixture protocol {protocol}")

    requested_model = (
        "gemini-fixture" if protocol == "google_genai_generate_content"
        else "local-fixture"
        if protocol == "local_in_process"
        else "gpt-5.5"
    )
    capability.update(
        {
            "adapter_id": source["adapter_id"],
            "base_url": source["base_url"],
            "capability_id": f"{source['source_id']}_native_so_v1",
            "capability_revision": "fixture_probe_v1",
            "observed_model_id": requested_model,
            "probe_id": f"{source['source_id']}_probe_v1",
            "protocol": source["protocol"],
            "requested_model_id": requested_model,
            "route_id": source["route_id"],
            "source_id": source["source_id"],
            "source_revision": source["source_revision"],
        }
    )
    role["primary"].update(
        {
            "capability_id": capability["capability_id"],
            "capability_record_sha256": canonical_sha256(capability),
            "capability_revision": capability["capability_revision"],
            "requested_model_id": requested_model,
            "source_id": source["source_id"],
            "source_record_sha256": canonical_sha256(source),
            "source_revision": source["source_revision"],
        }
    )
    seal = resolve_llm_run_seal(
        profile=profile,
        api_sources=[source],
        capability_evidence=[capability],
        role_id=role["role_id"],
        run_id=f"phase2b_{protocol}_run",
        attempt_run_id=f"phase2b_{protocol}_attempt",
        stage_id=f"phase2b_{protocol}_stage",
        input_bindings=[
            {
                "name": "transport_request_body",
                "sha256": canonical_sha256(request_body),
            }
        ],
    )
    return seal, request_body


def _response_bytes() -> bytes:
    return canonical_json(
        {
            "id": "provider-request-1",
            "model": "gpt-5.5",
            "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 30,
                "total_tokens": 130,
                "prompt_tokens_details": {"cached_tokens": 0},
                "completion_tokens_details": {"reasoning_tokens": 5},
            },
        }
    ).encode("utf-8")


def _cost_fact() -> dict:
    return {
        "cost_usd": 0.25,
        "cost_status": "calculated",
        "cost_provenance": {
            "kind": "pricing_manifest",
            "reference_id": "fixture_pricing_v1",
            "reference_sha256": SHA_A,
        },
    }


class _Clock:
    def __init__(self, count: int = 8) -> None:
        start = datetime(2026, 7, 19, tzinfo=timezone.utc)
        self.values = [start + timedelta(seconds=index) for index in range(count)]

    def __call__(self) -> datetime:
        return self.values.pop(0)


class _SuccessSender:
    def __init__(self) -> None:
        self.calls = 0

    def send(self, request):
        self.calls += 1
        assert "authorization" in request.headers_for_transport()
        return RawTransportResponse(
            status_code=200,
            headers={"x-request-id": "provider-request-1"},
            body=_response_bytes(),
            request_id="provider-request-1",
        )


class _FailureSender:
    def __init__(self) -> None:
        self.calls = 0

    def send(self, request):
        self.calls += 1
        raise TransportCallError(
            code="http_500",
            status_code=500,
            safe_message="provider returned HTTP 500",
        )


class _HttpErrorSender:
    def __init__(self, status_code: int) -> None:
        self.calls = 0
        self.status_code = status_code

    def send(self, request):
        self.calls += 1
        raise TransportCallError(
            code=f"http_{self.status_code}",
            status_code=self.status_code,
            safe_message=f"provider returned HTTP {self.status_code}",
        )


class _FailOnceSender:
    def __init__(self) -> None:
        self.calls = 0

    def send(self, request):
        self.calls += 1
        if self.calls == 1:
            raise TransportCallError(
                code="http_500",
                status_code=500,
                safe_message="provider returned HTTP 500",
            )
        return RawTransportResponse(
            status_code=200,
            headers={"x-request-id": "provider-request-1"},
            body=_response_bytes(),
            request_id="provider-request-1",
        )


class _SubmillisecondClock:
    def __init__(self) -> None:
        start = datetime(2026, 7, 19, tzinfo=timezone.utc)
        self.values = [
            start,
            start + timedelta(seconds=1, microseconds=999_900),
            start + timedelta(seconds=2, microseconds=100),
        ]

    def __call__(self) -> datetime:
        return self.values.pop(0)


def _backend(tmp_path: Path, sender, *, clock=None):
    artifacts = ContentAddressedArtifactStore(tmp_path / "artifacts")
    cache = ApplicationResponseCache(
        index_path=tmp_path / "response_cache.sqlite3",
        artifact_store=artifacts,
    )
    ledger = SharedLlmAttemptLedger(tmp_path / "attempt_ledger.sqlite3")
    backend = SharedLlmBackend(
        credential_provider=MappingCredentialProvider(
            {"credential.local_gpt_gateway_v1": SECRET}
        ),
        scheduler=PhysicalQuotaScheduler(tmp_path / "quota_locks"),
        ledger=ledger,
        response_cache=cache,
        sender=sender,
        clock=clock or _Clock(),
    )
    return backend, ledger, cache


def test_credential_resolution_is_commitment_bound_and_redacted() -> None:
    seal, _ = _sealed()
    source = seal["primary"]["source"]
    resolved = resolve_source_credential(
        source=source,
        provider=MappingCredentialProvider(
            {"credential.local_gpt_gateway_v1": SECRET}
        ),
    )
    assert resolved.reveal_for_transport() == SECRET
    assert SECRET not in repr(resolved)
    assert SECRET not in str(resolved)
    with pytest.raises(ContractValidationError, match="commitment mismatch"):
        resolve_source_credential(
            source=source,
            provider=MappingCredentialProvider(
                {"credential.local_gpt_gateway_v1": "wrong-secret"}
            ),
        )


def test_physical_quota_scheduler_is_exclusive_and_owner_checked(tmp_path) -> None:
    scheduler = PhysicalQuotaScheduler(tmp_path / "locks")
    lease = scheduler.acquire(
        physical_quota_bucket_id="local-gpt-gateway-v1",
        lease_id="lease_one",
        owner_id="attempt_one",
        acquired_at_utc="2026-07-19T00:00:00Z",
    )
    with pytest.raises(QuotaBusyError):
        scheduler.acquire(
            physical_quota_bucket_id="local-gpt-gateway-v1",
            lease_id="lease_two",
            owner_id="attempt_two",
            acquired_at_utc="2026-07-19T00:00:01Z",
        )
    lease.release()
    with scheduler.acquire(
        physical_quota_bucket_id="local-gpt-gateway-v1",
        lease_id="lease_two",
        owner_id="attempt_two",
        acquired_at_utc="2026-07-19T00:00:01Z",
    ):
        pass


def test_artifact_store_detects_tampering(tmp_path) -> None:
    store = ContentAddressedArtifactStore(tmp_path / "objects")
    digest = store.put_bytes(b"original")
    assert store.get_bytes(digest) == b"original"
    store.path_for(digest).write_bytes(b"tampered")
    with pytest.raises(ContractValidationError, match="hash mismatch"):
        store.get_bytes(digest)


def test_transport_envelope_is_seal_bound_and_redacted() -> None:
    seal, body = _sealed()
    credential = resolve_source_credential(
        source=seal["primary"]["source"],
        provider=MappingCredentialProvider(
            {"credential.local_gpt_gateway_v1": SECRET}
        ),
    )
    request = prepare_transport_request(
        seal=seal,
        request_body=body,
        credential=credential,
        timeout_seconds=10,
    )
    assert request.url == "http://localhost:8317/v1/chat/completions"
    assert SECRET not in repr(request)
    assert request.headers_for_transport()["authorization"] == f"Bearer {SECRET}"
    with pytest.raises(TypeError, match="may not be serialized"):
        pickle.dumps(request)
    bad_body = deepcopy(body)
    bad_body["model"] = "foreign-model"
    with pytest.raises(ContractValidationError, match="model differs"):
        prepare_transport_request(
            seal=seal,
            request_body=bad_body,
            credential=credential,
            timeout_seconds=10,
        )


@pytest.mark.parametrize(
    ("protocol", "expected_url", "expected_header"),
    [
        (
            "openai_responses",
            "https://provider.invalid/v1/responses",
            "authorization",
        ),
        (
            "google_genai_generate_content",
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-fixture:generateContent",
            "x-goog-api-key",
        ),
        ("local_in_process", None, None),
    ],
)
def test_all_declared_transport_envelopes_are_seal_bound(
    protocol: str,
    expected_url: str | None,
    expected_header: str | None,
) -> None:
    seal, body = _sealed_for_protocol(protocol)
    credential = resolve_source_credential(
        source=seal["primary"]["source"],
        provider=MappingCredentialProvider(
            {
                "credential.remote_fixture_v1": SECRET,
                "credential.google_fixture_v1": SECRET,
            }
        ),
    )
    request = prepare_transport_request(
        seal=seal,
        request_body=body,
        credential=credential,
        timeout_seconds=10,
    )
    assert request.protocol == protocol
    assert request.url == expected_url
    headers = request.headers_for_transport()
    if expected_header is None:
        assert headers == {"content-type": "application/json"}
    else:
        assert headers[expected_header]
        assert SECRET not in repr(request)


@pytest.mark.parametrize(
    ("protocol", "payload", "expected"),
    [
        (
            "openai_responses",
            {
                "id": "response-1",
                "model": "gpt-5.5",
                "status": "completed",
                "usage": {
                    "input_tokens": 20,
                    "output_tokens": 8,
                    "total_tokens": 28,
                    "input_tokens_details": {"cached_tokens": 4},
                    "output_tokens_details": {"reasoning_tokens": 3},
                },
            },
            ("stop", 20, 8, 3, 28),
        ),
        (
            "google_genai_generate_content",
            {
                "responseId": "google-response-1",
                "modelVersion": "gemini-2.5-flash",
                "candidates": [{"finishReason": "STOP"}],
                "usageMetadata": {
                    "promptTokenCount": 42,
                    "cachedContentTokenCount": 5,
                    "candidatesTokenCount": 11,
                    "thoughtsTokenCount": 60,
                    "totalTokenCount": 113,
                },
            },
            ("stop", 42, 71, 60, 113),
        ),
        (
            "google_genai_generate_content",
            {
                "responseId": "google-hidden-usage-response-1",
                "modelVersion": "gemini-3.5-flash",
                "candidates": [{"finishReason": "STOP"}],
                "usageMetadata": {
                    "promptTokenCount": 4127,
                    "candidatesTokenCount": 19,
                    "totalTokenCount": 4528,
                },
            },
            ("stop", 4127, 401, None, 4528),
        ),
        (
            "local_in_process",
            {
                "request_id": "local-response-1",
                "model": "local-fixture",
                "finish_reason": "stop",
                "usage": {
                    "prompt_tokens": 15,
                    "cached_input_tokens": 0,
                    "completion_tokens": 6,
                    "reasoning_tokens": 1,
                    "total_tokens": 21,
                },
            },
            ("stop", 15, 6, 1, 21),
        ),
    ],
)
def test_all_declared_response_protocols_normalize_usage_without_double_counting(
    protocol: str,
    payload: dict,
    expected: tuple,
) -> None:
    request = PreparedTransportRequest(
        method="POST",
        url=None if protocol == "local_in_process" else "https://provider.invalid",
        protocol=protocol,
        source_id="fixture_source",
        requested_model_id=payload.get("model")
        or payload.get("modelVersion")
        or "fixture-model",
        headers={},
        body=b"{}",
        timeout_seconds=10,
    )
    facts = normalize_provider_response(
        request=request,
        response=RawTransportResponse(
            status_code=200,
            headers={},
            body=canonical_json(payload).encode("utf-8"),
        ),
    )
    assert (
        facts["finish_reason"],
        facts["prompt_tokens"],
        facts["completion_tokens"],
        facts["reasoning_tokens"],
        facts["total_tokens"],
    ) == expected


def test_google_response_rejects_incoherent_hidden_usage_total() -> None:
    payload = {
        "responseId": "google-invalid-usage-response-1",
        "modelVersion": "gemini-3.5-flash",
        "candidates": [{"finishReason": "STOP"}],
        "usageMetadata": {
            "promptTokenCount": 100,
            "candidatesTokenCount": 20,
            "totalTokenCount": 119,
        },
    }
    request = PreparedTransportRequest(
        method="POST",
        url="https://provider.invalid",
        protocol="google_genai_generate_content",
        source_id="fixture_source",
        requested_model_id="gemini-3.5-flash",
        headers={},
        body=b"{}",
        timeout_seconds=10,
    )
    with pytest.raises(TransportCallError, match="smaller than prompt plus"):
        normalize_provider_response(
            request=request,
            response=RawTransportResponse(
                status_code=200,
                headers={},
                body=canonical_json(payload).encode("utf-8"),
            ),
        )


def test_openai_response_normalization_keeps_reasoning_as_completion_subset() -> None:
    seal, body = _sealed()
    credential = resolve_source_credential(
        source=seal["primary"]["source"],
        provider=MappingCredentialProvider(
            {"credential.local_gpt_gateway_v1": SECRET}
        ),
    )
    request = prepare_transport_request(
        seal=seal,
        request_body=body,
        credential=credential,
        timeout_seconds=10,
    )
    facts = normalize_provider_response(
        request=request,
        response=RawTransportResponse(
            status_code=200, headers={}, body=_response_bytes()
        ),
    )
    assert facts["total_tokens"] == 130
    assert facts["reasoning_tokens"] == 5
    assert facts["completion_tokens"] == 30


def test_backend_calls_provider_once_then_uses_trusted_cache(tmp_path) -> None:
    seal, body = _sealed()
    sender = _SuccessSender()
    backend, ledger, _ = _backend(tmp_path, sender, clock=_Clock())
    first = backend.execute_one_attempt(
        seal=seal,
        logical_request_id="packet_1",
        semantic_attempt_index=1,
        transport_retry_ordinal=0,
        request_body=body,
        cost_fact=_cost_fact(),
    )
    second = backend.execute_one_attempt(
        seal=seal,
        logical_request_id="packet_1",
        semantic_attempt_index=1,
        transport_retry_ordinal=0,
        request_body=body,
        cost_fact=_cost_fact(),
    )
    assert first["status"] == "provider_succeeded"
    assert second["status"] == "cache_hit"
    assert sender.calls == 1
    assert second["response_bytes"] == _response_bytes()
    assert ledger.count("usage") == 1
    assert ledger.count("artifact_receipt") == 1
    assert ledger.count("cache") == 2


def test_cache_hit_still_requires_the_exact_sealed_request_body(tmp_path) -> None:
    seal, body = _sealed()
    sender = _SuccessSender()
    backend, _, _ = _backend(tmp_path, sender, clock=_Clock())
    backend.execute_one_attempt(
        seal=seal,
        logical_request_id="packet_1",
        semantic_attempt_index=1,
        transport_retry_ordinal=0,
        request_body=body,
        cost_fact=_cost_fact(),
    )
    foreign_body = deepcopy(body)
    foreign_body["messages"][0]["content"] = "foreign prompt"
    with pytest.raises(ContractValidationError, match="not present in sealed input"):
        backend.execute_one_attempt(
            seal=seal,
            logical_request_id="packet_1",
            semantic_attempt_index=1,
            transport_retry_ordinal=0,
            request_body=foreign_body,
            cost_fact=_cost_fact(),
        )
    assert sender.calls == 1


def test_existing_attempt_without_cache_fails_before_a_duplicate_provider_call(
    tmp_path,
) -> None:
    seal, body = _sealed()
    sender = _SuccessSender()
    backend, _, _ = _backend(tmp_path, sender, clock=_Clock())
    backend.execute_one_attempt(
        seal=seal,
        logical_request_id="packet_1",
        semantic_attempt_index=1,
        transport_retry_ordinal=0,
        request_body=body,
        allow_response_cache_read=False,
        allow_response_cache_write=False,
        cost_fact=_cost_fact(),
    )
    with pytest.raises(ContractValidationError, match="already exists"):
        backend.execute_one_attempt(
            seal=seal,
            logical_request_id="packet_1",
            semantic_attempt_index=1,
            transport_retry_ordinal=0,
            request_body=body,
            allow_response_cache_read=False,
            allow_response_cache_write=False,
            cost_fact=_cost_fact(),
        )
    assert sender.calls == 1


def test_attempt_ledger_is_idempotent_and_rejects_identity_reuse(
    tmp_path,
) -> None:
    seal, body = _sealed()
    sender = _SuccessSender()
    backend, ledger, _ = _backend(tmp_path, sender, clock=_Clock())
    backend.execute_one_attempt(
        seal=seal,
        logical_request_id="packet_1",
        semantic_attempt_index=1,
        transport_retry_ordinal=0,
        request_body=body,
        cost_fact=_cost_fact(),
    )
    usage = ledger.list_records("usage")
    observations = ledger.list_records("cache")
    receipts = ledger.list_records("artifact_receipt")
    ledger.append_bundle(
        seal=seal,
        usage_rows=usage,
        cache_observations=observations,
        reusable_artifact_receipts=receipts,
    )
    assert ledger.count("usage") == 1
    assert ledger.count("cache") == 1
    assert ledger.count("artifact_receipt") == 1

    conflicting = deepcopy(usage[0])
    conflicting["cost_usd"] = 0.5
    with pytest.raises(ContractValidationError, match="conflicting bytes"):
        ledger.append_bundle(seal=seal, usage_rows=[conflicting])


def test_cache_index_without_ledger_receipt_is_not_authoritative(tmp_path) -> None:
    seal, _ = _sealed()
    sender = _SuccessSender()
    backend, _, cache = _backend(tmp_path, sender)
    cache.store(
        producer_seal=seal,
        logical_request_id="packet_1",
        response_bytes=_response_bytes(),
        created_at_utc="2026-07-19T00:00:00Z",
    )
    with pytest.raises(ContractValidationError, match="trusted producer"):
        backend.execute_one_attempt(
            seal=seal,
            logical_request_id="packet_1",
            semantic_attempt_index=1,
            transport_retry_ordinal=0,
            request_body=_sealed()[1],
            cost_fact=_cost_fact(),
        )
    assert sender.calls == 0


def test_transport_failure_is_persisted_once_without_hidden_retry(tmp_path) -> None:
    seal, body = _sealed()
    sender = _FailureSender()
    backend, ledger, _ = _backend(tmp_path, sender, clock=_Clock())
    with pytest.raises(TransportCallError, match="HTTP 500"):
        backend.execute_one_attempt(
            seal=seal,
            logical_request_id="packet_1",
            semantic_attempt_index=1,
            transport_retry_ordinal=0,
            request_body=body,
            cost_fact=_cost_fact(),
        )
    assert sender.calls == 1
    assert ledger.count("usage") == 1
    assert ledger.count("error") == 1
    assert not list((tmp_path / "quota_locks").glob("*.lock"))


def test_latency_uses_the_same_millisecond_precision_as_persisted_timestamps(
    tmp_path,
) -> None:
    seal, body = _sealed()
    backend, ledger, _ = _backend(
        tmp_path, _SuccessSender(), clock=_SubmillisecondClock()
    )
    result = backend.execute_one_attempt(
        seal=seal,
        logical_request_id="packet_1",
        semantic_attempt_index=1,
        transport_retry_ordinal=0,
        request_body=body,
        cost_fact=_cost_fact(),
    )
    assert result["status"] == "provider_succeeded"
    usage = ledger.list_records("usage")[0]
    assert usage["started_at_utc"].endswith("01.999Z")
    assert usage["finished_at_utc"].endswith("02.000Z")
    assert usage["latency_ms"] == 1


@pytest.mark.parametrize(
    ("status_code", "retry_class"),
    [(402, "authorization"), (408, "timeout")],
)
def test_http_error_class_matches_the_closed_contract(
    tmp_path, status_code, retry_class
) -> None:
    seal, body = _sealed()
    sender = _HttpErrorSender(status_code)
    backend, ledger, _ = _backend(tmp_path, sender)
    with pytest.raises(TransportCallError, match=f"HTTP {status_code}"):
        backend.execute_one_attempt(
            seal=seal,
            logical_request_id="packet_1",
            semantic_attempt_index=1,
            transport_retry_ordinal=0,
            request_body=body,
            cost_fact=None,
        )
    assert sender.calls == 1
    error = ledger.list_records("error")[0]
    assert error["retry_class"] == retry_class
    assert error["retry_disposition"] == "do_not_retry"


def test_explicit_retry_reserves_unknown_failed_tokens_from_sealed_per_call_caps(
    tmp_path,
) -> None:
    seal, body = _sealed_with_transport_retry(sufficient_budget=True)
    sender = _FailOnceSender()
    backend, ledger, _ = _backend(tmp_path, sender)
    with pytest.raises(TransportCallError, match="HTTP 500"):
        backend.execute_one_attempt(
            seal=seal,
            logical_request_id="packet_1",
            semantic_attempt_index=1,
            transport_retry_ordinal=0,
            request_body=body,
            cost_fact=None,
        )
    result = backend.execute_one_attempt(
        seal=seal,
        logical_request_id="packet_1",
        semantic_attempt_index=1,
        transport_retry_ordinal=1,
        request_body=body,
        cost_fact=None,
    )
    assert result["status"] == "provider_succeeded"
    usage = sorted(
        ledger.list_records("usage"),
        key=lambda row: row["physical_attempt_index"],
    )
    assert sender.calls == 2
    assert usage[0]["prompt_tokens"] is None
    assert usage[0]["completion_tokens"] is None
    assert usage[0]["total_tokens"] is None
    assert usage[1]["total_tokens"] == 130


def test_explicit_retry_does_not_treat_unknown_failed_tokens_as_zero(tmp_path) -> None:
    seal, body = _sealed_with_transport_retry(sufficient_budget=False)
    sender = _FailOnceSender()
    backend, ledger, cache = _backend(tmp_path, sender)
    with pytest.raises(TransportCallError, match="HTTP 500"):
        backend.execute_one_attempt(
            seal=seal,
            logical_request_id="packet_1",
            semantic_attempt_index=1,
            transport_retry_ordinal=0,
            request_body=body,
            cost_fact=None,
        )
    with pytest.raises(UncertifiedAttemptError, match="cannot be certified"):
        backend.execute_one_attempt(
            seal=seal,
            logical_request_id="packet_1",
            semantic_attempt_index=1,
            transport_retry_ordinal=1,
            request_body=body,
            cost_fact=None,
        )
    assert sender.calls == 2
    assert ledger.count("usage") == 2
    assert cache.lookup(consumer_seal=seal, logical_request_id="packet_1") is None


def test_unknown_cost_persists_evidence_but_cannot_certify_finite_cap(tmp_path) -> None:
    seal, body = _sealed()
    sender = _SuccessSender()
    backend, ledger, cache = _backend(tmp_path, sender, clock=_Clock())
    with pytest.raises(UncertifiedAttemptError, match="cannot be certified"):
        backend.execute_one_attempt(
            seal=seal,
            logical_request_id="packet_1",
            semantic_attempt_index=1,
            transport_retry_ordinal=0,
            request_body=body,
            cost_fact=None,
        )
    assert sender.calls == 1
    assert ledger.count("usage") == 1
    assert ledger.list_records("usage")[0]["cost_usd"] is None
    assert cache.lookup(consumer_seal=seal, logical_request_id="packet_1") is None


def test_over_cap_usage_is_persisted_but_never_published_to_cache(tmp_path) -> None:
    seal, body = _sealed()
    sender = _SuccessSender()
    backend, ledger, cache = _backend(tmp_path, sender, clock=_Clock())
    over_cap = _cost_fact()
    over_cap["cost_usd"] = 4.0
    with pytest.raises(UncertifiedAttemptError, match="cannot be certified"):
        backend.execute_one_attempt(
            seal=seal,
            logical_request_id="packet_1",
            semantic_attempt_index=1,
            transport_retry_ordinal=0,
            request_body=body,
            cost_fact=over_cap,
        )
    assert sender.calls == 1
    assert ledger.list_records("usage")[0]["cost_usd"] == 4.0
    assert cache.lookup(consumer_seal=seal, logical_request_id="packet_1") is None
    assert not list((tmp_path / "quota_locks").glob("*.lock"))
