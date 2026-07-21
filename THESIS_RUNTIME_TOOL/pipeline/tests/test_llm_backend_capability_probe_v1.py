from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
from pathlib import Path
from urllib.error import HTTPError

import pytest

from pipeline.llm_backend import (
    ContentAddressedArtifactStore,
    ContractValidationError,
    MappingCredentialProvider,
    PhysicalQuotaScheduler,
    RawTransportResponse,
    SharedLlmAttemptLedger,
    SharedLlmCapabilityProbe,
    TransportCallError,
    UrllibTransportSender,
    canonical_json,
    canonical_sha256,
    create_capability_probe_seal,
    credential_commitment,
    prepare_capability_probe_transport_request,
    resolve_llm_run_seal,
    resolve_source_credential,
    validate_capability_probe_bundle,
)


FIXTURES = Path(__file__).parent / "fixtures" / "llm_backend_v1"
SECRET = "capability-probe-fixture-secret"
VALIDATOR_SHA = "d" * 64


def _source() -> dict:
    return {
        "schema_version": "api_source_v1",
        "source_id": "modelapi_shared_v1",
        "source_revision": "modelapi_profile_v1",
        "source_class": "remote_api",
        "adapter_id": "openai_python_v1",
        "protocol": "openai_chat_completions",
        "route_id": "chat_completions_create",
        "endpoint_class": "remote",
        "base_url": "https://modelapi.invalid/v1",
        "credential_ref": "credential.modelapi_shared_v1",
        "credential_commitment": credential_commitment(SECRET),
        "physical_quota_bucket_id": "modelapi-shared-v1",
        "enabled": True,
    }


def _schema() -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "entities": {
                "type": "array",
                "items": {"type": "string"},
            }
        },
        "required": ["entities"],
    }


def _request_body(schema: dict | None = None) -> dict:
    schema = schema or _schema()
    return {
        "model": "gpt-5.4",
        "messages": [
            {
                "role": "user",
                "content": "Return a valid empty entity inventory.",
            }
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "literary_b1_entity_inventory_v1",
                "strict": True,
                "schema": schema,
            },
        },
    }


def _intent(
    schema: dict | None = None,
    *,
    schema_dialect: str = "json_schema_2020_12",
) -> dict:
    schema = schema or _schema()
    return {
        "capability_id": "modelapi_gpt54_literary_b1_native_so_v1",
        "capability_revision": "probe_profile_v1",
        "requested_model_id": "gpt-5.4",
        "accepted_observed_model_ids": ["gpt-5.4"],
        "capability_kind": "native_structured_output",
        "schema_name": "literary_b1_entity_inventory_v1",
        "schema_dialect": schema_dialect,
        "schema_sha256": canonical_sha256(schema),
        "local_validator_id": "literary.b1.entity_inventory.validator",
        "local_validator_sha256": VALIDATOR_SHA,
    }


def _limits() -> dict:
    return {
        "max_calls": 1,
        "max_prompt_utf8_bytes": 8_000,
        "max_response_utf8_bytes": 8_000,
        "max_prompt_tokens": 100,
        "max_completion_tokens": 100,
        "max_total_tokens": 200,
        "request_timeout_ms": 10_000,
    }


def _implementation_binding() -> dict:
    return {
        "shared_core_revision": "1" * 40,
        "consumer_revision": "2" * 40,
        "consumer_implementation_sha256": "3" * 64,
    }


def _seal(
    *,
    probe_run_id: str = "literary_modelapi_b1_probe_001",
    schema_dialect: str = "json_schema_2020_12",
) -> tuple[dict, dict]:
    body = _request_body()
    return (
        create_capability_probe_seal(
            source=_source(),
            consumer_workstream="literary",
            role_id="literary.b1.entity_inventory",
            probe_run_id=probe_run_id,
            probe_profile_id="literary_b1_native_so_probe_v1",
            probe_profile_revision="v1",
            implementation_binding=_implementation_binding(),
            capability_intent=_intent(schema_dialect=schema_dialect),
            response_schema=_schema(),
            request_body=body,
            limits=_limits(),
            issued_at_utc="2026-07-20T00:00:00Z",
        ),
        body,
    )


def _response_bytes(*, model: str = "gpt-5.4", content: str = '{"entities":[]}') -> bytes:
    return canonical_json(
        {
            "id": "probe-request-1",
            "model": model,
            "choices": [
                {"finish_reason": "stop", "message": {"content": content}}
            ],
            "usage": {
                "prompt_tokens": 20,
                "completion_tokens": 5,
                "total_tokens": 25,
                "prompt_tokens_details": {"cached_tokens": 0},
                "completion_tokens_details": {"reasoning_tokens": 0},
            },
        }
    ).encode("utf-8")


class _Clock:
    def __init__(self) -> None:
        start = datetime(2026, 7, 20, tzinfo=timezone.utc)
        self.values = [start + timedelta(seconds=index) for index in range(6)]

    def __call__(self) -> datetime:
        return self.values.pop(0)


class _SuccessSender:
    def __init__(self, *, model: str = "gpt-5.4", content: str = '{"entities":[]}') -> None:
        self.calls = 0
        self.model = model
        self.content = content

    def send(self, request):
        self.calls += 1
        assert request.url == "https://modelapi.invalid/v1/chat/completions"
        assert request.headers_for_transport()["authorization"] == f"Bearer {SECRET}"
        return RawTransportResponse(
            status_code=200,
            headers={"x-request-id": "probe-request-1"},
            body=_response_bytes(model=self.model, content=self.content),
            request_id="probe-request-1",
        )


class _GoogleReasoningSender:
    def __init__(self) -> None:
        self.calls = 0

    def send(self, request):
        self.calls += 1
        assert request.url == (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-2.5-flash:generateContent"
        )
        return RawTransportResponse(
            status_code=200,
            headers={},
            body=canonical_json(
                {
                    "responseId": "google-reasoning-response-1",
                    "modelVersion": "gemini-2.5-flash",
                    "candidates": [
                        {
                            "finishReason": "STOP",
                            "content": {
                                "parts": [
                                    {
                                        "text": (
                                            '{"back_translation":'
                                            '"Data storage system."}'
                                        )
                                    }
                                ],
                            },
                        }
                    ],
                    "usageMetadata": {
                        "promptTokenCount": 42,
                        "candidatesTokenCount": 11,
                        "thoughtsTokenCount": 60,
                        "totalTokenCount": 113,
                    },
                }
            ).encode("utf-8"),
            request_id="google-reasoning-response-1",
        )


class _GoogleHiddenUsageSender:
    def __init__(self) -> None:
        self.calls = 0

    def send(self, request):
        self.calls += 1
        return RawTransportResponse(
            status_code=200,
            headers={},
            body=canonical_json(
                {
                    "responseId": "google-hidden-usage-response-1",
                    "modelVersion": "gemini-2.5-flash",
                    "candidates": [
                        {
                            "finishReason": "STOP",
                            "content": {
                                "parts": [
                                    {
                                        "text": (
                                            '{"back_translation":'
                                            '"Data storage system."}'
                                        )
                                    }
                                ]
                            },
                        }
                    ],
                    "usageMetadata": {
                        "promptTokenCount": 4127,
                        "candidatesTokenCount": 19,
                        "totalTokenCount": 4528,
                    },
                }
            ).encode("utf-8"),
            request_id="google-hidden-usage-response-1",
        )


class _FailureSender:
    def __init__(self) -> None:
        self.calls = 0

    def send(self, request):
        self.calls += 1
        raise TransportCallError(
            code="http_503",
            status_code=503,
            safe_message="provider returned HTTP 503",
        )


class _HttpBodyFailureSender:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.calls = 0

    def send(self, request):
        self.calls += 1
        raise TransportCallError(
            code="http_400",
            status_code=400,
            safe_message="provider returned HTTP 400",
            response=RawTransportResponse(
                status_code=400,
                headers={"x-request-id": "provider-error-1"},
                body=self.body,
                request_id="provider-error-1",
            ),
        )


class _OverCapSender:
    def __init__(self) -> None:
        self.calls = 0

    def send(self, request):
        self.calls += 1
        payload = json.loads(_response_bytes())
        payload["usage"].update(
            {"prompt_tokens": 20, "completion_tokens": 101, "total_tokens": 121}
        )
        return RawTransportResponse(
            status_code=200,
            headers={},
            body=canonical_json(payload).encode("utf-8"),
            request_id="probe-request-over-cap",
        )


def _probe(tmp_path: Path, sender) -> tuple[SharedLlmCapabilityProbe, SharedLlmAttemptLedger]:
    ledger = SharedLlmAttemptLedger(tmp_path / "probe_ledger.sqlite3")
    return (
        SharedLlmCapabilityProbe(
            credential_provider=MappingCredentialProvider(
                {
                    "credential.modelapi_shared_v1": SECRET,
                    "credential.google_fixture_v1": SECRET,
                }
            ),
            scheduler=PhysicalQuotaScheduler(tmp_path / "locks"),
            ledger=ledger,
            artifact_store=ContentAddressedArtifactStore(tmp_path / "artifacts"),
            sender=sender,
            implementation_binding=_implementation_binding(),
            clock=_Clock(),
        ),
        ledger,
    )


def _validator(payload) -> None:
    if payload != {"entities": []}:
        raise ContractValidationError("entity inventory differs")


def test_unknown_capability_cannot_use_normal_resolver_before_probe() -> None:
    source = _source()
    schema = _schema()
    intent = _intent(schema)
    unknown = {
        "schema_version": "capability_evidence_v1",
        "capability_id": intent["capability_id"],
        "capability_revision": intent["capability_revision"],
        "source_id": source["source_id"],
        "source_revision": source["source_revision"],
        "adapter_id": source["adapter_id"],
        "protocol": source["protocol"],
        "route_id": source["route_id"],
        "base_url": source["base_url"],
        "requested_model_id": intent["requested_model_id"],
        "observed_model_id": None,
        "capability_kind": intent["capability_kind"],
        "schema_dialect": intent["schema_dialect"],
        "schema_sha256": intent["schema_sha256"],
        "local_validator_id": intent["local_validator_id"],
        "local_validator_sha256": intent["local_validator_sha256"],
        "probe_id": "unqualified_declaration",
        "evidence_sha256": "a" * 64,
        "observed_at_utc": "2026-07-20T00:00:00Z",
        "verdict": "unknown",
    }
    profile = _literary_profile(source=source, capability=unknown)
    with pytest.raises(ContractValidationError, match="not qualified"):
        _resolve_profile(profile=profile, source=source, capability=unknown)


def test_probe_schema_dialect_allowlist_is_closed() -> None:
    seal, _ = _seal(schema_dialect="openai_strict_json_schema_subset_v1")
    assert (
        seal["capability_intent"]["schema_dialect"]
        == "openai_strict_json_schema_subset_v1"
    )

    with pytest.raises(ContractValidationError, match="schema_dialect"):
        _seal(schema_dialect="provider_specific_unsealed_subset_v1")


def test_probe_qualifies_exact_binding_and_normal_resolver_accepts_it(tmp_path) -> None:
    seal, body = _seal()
    sender = _SuccessSender()
    probe, ledger = _probe(tmp_path, sender)
    result = probe.execute_once(
        seal=seal,
        request_body=body,
        local_validator=_validator,
        local_validator_id="literary.b1.entity_inventory.validator",
        local_validator_sha256=VALIDATOR_SHA,
    )
    assert result["status"] == "qualified"
    assert result["provider_called"] is True
    assert "response_bytes" not in result
    assert sender.calls == 1
    assert ledger.count("capability_probe_seal") == 1
    assert ledger.count("capability_probe_receipt") == 1
    assert ledger.count("capability_evidence") == 1
    assert ledger.count("usage") == 0
    assert ledger.count("cache") == 0
    evidence = result["capability_evidence"]
    assert evidence["verdict"] == "qualified"
    profile = _literary_profile(source=_source(), capability=evidence)
    resolved = _resolve_profile(
        profile=profile, source=_source(), capability=evidence
    )
    assert resolved["primary"]["capability"]["verdict"] == "qualified"


def test_google_probe_folds_thoughts_into_completion_usage(tmp_path) -> None:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"back_translation": {"type": "string"}},
        "required": ["back_translation"],
    }
    source = {
        **_source(),
        "source_id": "google_fixture_v1",
        "source_revision": "google_fixture_revision_v1",
        "adapter_id": "google_genai_rest_v1",
        "protocol": "google_genai_generate_content",
        "route_id": "models_generate_content",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "credential_ref": "credential.google_fixture_v1",
        "physical_quota_bucket_id": "google-fixture-v1",
    }
    body = {
        "contents": [{"role": "user", "parts": [{"text": "Return JSON."}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseJsonSchema": schema,
        },
    }
    intent = {
        **_intent(schema),
        "capability_id": "google_fixture_native_so_v1",
        "requested_model_id": "gemini-2.5-flash",
        "accepted_observed_model_ids": ["gemini-2.5-flash"],
        "local_validator_id": "evaluation.sf_bt.back_translator.validator",
    }
    seal = create_capability_probe_seal(
        source=source,
        consumer_workstream="evaluation",
        role_id="evaluation.sf_bt.back_translator",
        probe_run_id="google_reasoning_usage_probe_001",
        probe_profile_id="evaluation_google_native_so_probe_v1",
        probe_profile_revision="v1",
        implementation_binding=_implementation_binding(),
        capability_intent=intent,
        response_schema=schema,
        request_body=body,
        limits=_limits(),
        issued_at_utc="2026-07-20T00:00:00Z",
    )
    sender = _GoogleReasoningSender()
    probe, ledger = _probe(tmp_path, sender)

    def validate_back_translation(payload) -> None:
        if payload != {"back_translation": "Data storage system."}:
            raise ContractValidationError("back translation differs")

    result = probe.execute_once(
        seal=seal,
        request_body=body,
        local_validator=validate_back_translation,
        local_validator_id="evaluation.sf_bt.back_translator.validator",
        local_validator_sha256=VALIDATOR_SHA,
    )

    assert result["status"] == "qualified"
    assert sender.calls == 1
    assert result["receipt"]["prompt_tokens"] == 42
    assert result["receipt"]["completion_tokens"] == 71
    assert result["receipt"]["reasoning_tokens"] == 60
    assert result["receipt"]["total_tokens"] == 113
    assert ledger.count("capability_probe_receipt") == 1
    assert ledger.count("capability_evidence") == 1


def test_google_probe_accounts_unlabeled_hidden_output_in_completion(tmp_path) -> None:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"back_translation": {"type": "string"}},
        "required": ["back_translation"],
    }
    source = {
        **_source(),
        "source_id": "google_fixture_v1",
        "source_revision": "google_fixture_revision_v1",
        "adapter_id": "google_genai_rest_v1",
        "protocol": "google_genai_generate_content",
        "route_id": "models_generate_content",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "credential_ref": "credential.google_fixture_v1",
        "physical_quota_bucket_id": "google-fixture-v1",
    }
    body = {
        "contents": [{"role": "user", "parts": [{"text": "Return JSON."}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseJsonSchema": schema,
        },
    }
    intent = {
        **_intent(schema),
        "capability_id": "google_fixture_native_so_v1",
        "requested_model_id": "gemini-2.5-flash",
        "accepted_observed_model_ids": ["gemini-2.5-flash"],
        "local_validator_id": "evaluation.sf_bt.back_translator.validator",
    }
    seal = create_capability_probe_seal(
        source=source,
        consumer_workstream="evaluation",
        role_id="evaluation.sf_bt.back_translator",
        probe_run_id="google_hidden_usage_probe_001",
        probe_profile_id="evaluation_google_native_so_probe_v1",
        probe_profile_revision="v1",
        implementation_binding=_implementation_binding(),
        capability_intent=intent,
        response_schema=schema,
        request_body=body,
        limits={
            **_limits(),
            "max_prompt_tokens": 5000,
            "max_completion_tokens": 500,
            "max_total_tokens": 5500,
        },
        issued_at_utc="2026-07-20T00:00:00Z",
    )
    sender = _GoogleHiddenUsageSender()
    probe, ledger = _probe(tmp_path, sender)

    result = probe.execute_once(
        seal=seal,
        request_body=body,
        local_validator=lambda payload: None,
        local_validator_id="evaluation.sf_bt.back_translator.validator",
        local_validator_sha256=VALIDATOR_SHA,
    )

    assert result["status"] == "qualified"
    assert result["receipt"]["prompt_tokens"] == 4127
    assert result["receipt"]["completion_tokens"] == 401
    assert result["receipt"]["reasoning_tokens"] is None
    assert result["receipt"]["total_tokens"] == 4528
    assert ledger.count("capability_probe_receipt") == 1
    assert ledger.count("capability_evidence") == 1


def test_same_probe_run_id_can_never_call_twice(tmp_path) -> None:
    seal, body = _seal()
    sender = _SuccessSender()
    probe, _ = _probe(tmp_path, sender)
    first = probe.execute_once(
        seal=seal,
        request_body=body,
        local_validator=_validator,
        local_validator_id="literary.b1.entity_inventory.validator",
        local_validator_sha256=VALIDATOR_SHA,
    )
    assert first["status"] == "qualified"
    with pytest.raises(ContractValidationError, match="already reserved"):
        probe.execute_once(
            seal=seal,
            request_body=body,
            local_validator=_validator,
            local_validator_id="literary.b1.entity_inventory.validator",
            local_validator_sha256=VALIDATOR_SHA,
        )
    assert sender.calls == 1


def test_transport_failure_persists_failed_evidence_without_retry(tmp_path) -> None:
    seal, body = _seal()
    sender = _FailureSender()
    probe, ledger = _probe(tmp_path, sender)
    result = probe.execute_once(
        seal=seal,
        request_body=body,
        local_validator=_validator,
        local_validator_id="literary.b1.entity_inventory.validator",
        local_validator_sha256=VALIDATOR_SHA,
    )
    assert result["status"] == "failed"
    assert result["capability_evidence"]["verdict"] == "failed"
    assert result["receipt"]["failure"]["code"] == "http_503"
    assert result["receipt"]["prompt_tokens"] is None
    assert sender.calls == 1
    assert ledger.count("capability_probe_receipt") == 1


def test_http_failure_body_is_content_addressed_without_pipeline_authority(
    tmp_path,
) -> None:
    seal, body = _seal()
    error_body = b'{"error":{"message":"unsupported parameter: seed"}}'
    sender = _HttpBodyFailureSender(error_body)
    probe, ledger = _probe(tmp_path, sender)
    result = probe.execute_once(
        seal=seal,
        request_body=body,
        local_validator=_validator,
        local_validator_id="literary.b1.entity_inventory.validator",
        local_validator_sha256=VALIDATOR_SHA,
    )
    receipt = result["receipt"]
    digest = receipt["response_artifact_sha256"]
    assert result["status"] == "failed"
    assert result["capability_evidence"]["verdict"] == "failed"
    assert receipt["failure"]["code"] == "http_400"
    assert receipt["request_id"] == "provider-error-1"
    assert receipt["raw_response_sha256"] == digest
    assert ContentAddressedArtifactStore(tmp_path / "artifacts").get_bytes(
        digest
    ) == error_body
    assert ledger.count("capability_evidence") == 1


def test_urllib_sender_bounds_http_error_body_to_request_cap(monkeypatch) -> None:
    seal, body = _seal()
    credential = resolve_source_credential(
        source=_source(),
        provider=MappingCredentialProvider(
            {"credential.modelapi_shared_v1": SECRET}
        ),
    )
    request = prepare_capability_probe_transport_request(
        probe_seal=seal,
        request_body=body,
        credential=credential,
        timeout_seconds=10,
    )
    error_body = b"x" * (_limits()["max_response_utf8_bytes"] + 1)

    def fail_urlopen(*_args, **_kwargs):
        raise HTTPError(
            request.url,
            400,
            "Bad Request",
            {"X-Request-ID": "provider-error-2"},
            BytesIO(error_body),
        )

    monkeypatch.setattr("pipeline.llm_backend.transport_v1.urlopen", fail_urlopen)
    with pytest.raises(TransportCallError) as caught:
        UrllibTransportSender().send(request)
    error = caught.value
    assert error.code == "http_400"
    assert error.response_body_truncated is True
    assert error.response is not None
    assert len(error.response.body) == _limits()["max_response_utf8_bytes"]
    assert error.response.request_id == "provider-error-2"


def test_failed_capability_revision_requires_a_new_revision_before_reprobe(tmp_path) -> None:
    seal, body = _seal()
    probe, ledger = _probe(tmp_path, _FailureSender())
    result = probe.execute_once(
        seal=seal,
        request_body=body,
        local_validator=_validator,
        local_validator_id="literary.b1.entity_inventory.validator",
        local_validator_sha256=VALIDATOR_SHA,
    )
    assert result["status"] == "failed"
    new_seal, _ = _seal(probe_run_id="literary_modelapi_b1_probe_002")
    with pytest.raises(ContractValidationError, match="terminal probe evidence"):
        ledger.reserve_capability_probe(new_seal)


def test_over_cap_usage_is_failed_evidence_not_qualification(tmp_path) -> None:
    seal, body = _seal()
    sender = _OverCapSender()
    probe, _ = _probe(tmp_path, sender)
    result = probe.execute_once(
        seal=seal,
        request_body=body,
        local_validator=_validator,
        local_validator_id="literary.b1.entity_inventory.validator",
        local_validator_sha256=VALIDATOR_SHA,
    )
    assert result["status"] == "failed"
    assert result["receipt"]["failure"]["code"] == "token_cap_exceeded"
    assert result["capability_evidence"]["verdict"] == "failed"
    assert sender.calls == 1


@pytest.mark.parametrize(
    ("model", "content", "expected_code"),
    [
        ("foreign-model", '{"entities":[]}', "observed_model_mismatch"),
        ("gpt-5.4", "not json", "response_json_invalid"),
        ("gpt-5.4", '{"entities":["invented"]}', "local_validator_rejected"),
    ],
)
def test_model_parse_and_local_validation_failures_never_qualify(
    tmp_path, model: str, content: str, expected_code: str
) -> None:
    seal, body = _seal(probe_run_id=f"probe_{expected_code}")
    sender = _SuccessSender(model=model, content=content)
    probe, _ = _probe(tmp_path, sender)
    result = probe.execute_once(
        seal=seal,
        request_body=body,
        local_validator=_validator,
        local_validator_id="literary.b1.entity_inventory.validator",
        local_validator_sha256=VALIDATOR_SHA,
    )
    assert result["status"] == "failed"
    assert result["receipt"]["failure"]["code"] == expected_code
    assert result["capability_evidence"]["verdict"] == "failed"
    if expected_code == "local_validator_rejected":
        assert result["receipt"]["parsed_content_sha256"] is not None


def test_schema_body_validator_and_seal_tampering_fail_before_transport(tmp_path) -> None:
    seal, body = _seal()
    sender = _SuccessSender()
    probe, ledger = _probe(tmp_path, sender)
    foreign_body = deepcopy(body)
    foreign_body["response_format"]["json_schema"]["strict"] = False
    with pytest.raises(ContractValidationError):
        probe.execute_once(
            seal=seal,
            request_body=foreign_body,
            local_validator=_validator,
            local_validator_id="literary.b1.entity_inventory.validator",
            local_validator_sha256=VALIDATOR_SHA,
        )
    with pytest.raises(ContractValidationError, match="validator identity"):
        probe.execute_once(
            seal=seal,
            request_body=body,
            local_validator=_validator,
            local_validator_id="literary.b1.entity_inventory.validator",
            local_validator_sha256="e" * 64,
        )
    probe.implementation_binding = {
        **_implementation_binding(),
        "consumer_revision": "4" * 40,
    }
    with pytest.raises(ContractValidationError, match="implementation identity"):
        probe.execute_once(
            seal=seal,
            request_body=body,
            local_validator=_validator,
            local_validator_id="literary.b1.entity_inventory.validator",
            local_validator_sha256=VALIDATOR_SHA,
        )
    probe.implementation_binding = _implementation_binding()
    tampered = deepcopy(seal)
    tampered["source_binding"]["record"]["physical_quota_bucket_id"] = "forged-bucket"
    with pytest.raises(ContractValidationError, match="source record hash mismatch"):
        probe.execute_once(
            seal=tampered,
            request_body=body,
            local_validator=_validator,
            local_validator_id="literary.b1.entity_inventory.validator",
            local_validator_sha256=VALIDATOR_SHA,
        )
    assert sender.calls == 0
    assert ledger.count("capability_probe_seal") == 0


@pytest.mark.parametrize(
    ("protocol", "requested_model", "expected_url"),
    [
        (
            "openai_responses",
            "gpt-5.4",
            "https://modelapi.invalid/v1/responses",
        ),
        (
            "google_genai_generate_content",
            "gemini-fixture",
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-fixture:generateContent",
        ),
        ("local_in_process", "local-fixture", None),
    ],
)
def test_probe_reuses_all_declared_transport_envelopes(
    protocol: str, requested_model: str, expected_url: str | None
) -> None:
    schema = _schema()
    source = _source()
    if protocol == "openai_responses":
        source.update({"protocol": protocol, "route_id": "responses_create"})
        body = {
            "model": requested_model,
            "input": "Return a valid empty entity inventory.",
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "literary_b1_entity_inventory_v1",
                    "strict": True,
                    "schema": schema,
                }
            },
        }
    elif protocol == "google_genai_generate_content":
        source.update(
            {
                "source_id": "google_fixture_v1",
                "source_revision": "google_fixture_revision_v1",
                "adapter_id": "google_genai_rest_v1",
                "protocol": protocol,
                "route_id": "models_generate_content",
                "base_url": "https://generativelanguage.googleapis.com/v1beta",
                "credential_ref": "credential.google_fixture_v1",
                "physical_quota_bucket_id": "google-fixture-v1",
            }
        )
        body = {
            "contents": [{"role": "user", "parts": [{"text": "Return JSON."}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseJsonSchema": schema,
            },
        }
    else:
        source.update(
            {
                "source_id": "local_fixture_v1",
                "source_revision": "local_fixture_revision_v1",
                "source_class": "local_in_process",
                "adapter_id": "local_callback_v1",
                "protocol": protocol,
                "route_id": "local_callback",
                "endpoint_class": "in_process",
                "base_url": None,
                "credential_ref": None,
                "credential_commitment": None,
                "physical_quota_bucket_id": "local-fixture-v1",
            }
        )
        body = {"prompt": "Return JSON.", "response_schema": schema, "strict": True}
    intent = _intent(schema)
    intent.update(
        {
            "capability_id": f"{source['source_id']}_native_so_v1",
            "requested_model_id": requested_model,
            "accepted_observed_model_ids": [requested_model],
        }
    )
    seal = create_capability_probe_seal(
        source=source,
        consumer_workstream="literary",
        role_id="literary.b1.entity_inventory",
        probe_run_id=f"probe_{protocol}",
        probe_profile_id="literary_b1_native_so_probe_v1",
        probe_profile_revision="v1",
        implementation_binding=_implementation_binding(),
        capability_intent=intent,
        response_schema=schema,
        request_body=body,
        limits=_limits(),
        issued_at_utc="2026-07-20T00:00:00Z",
    )
    credential = resolve_source_credential(
        source=source,
        provider=MappingCredentialProvider(
            {
                "credential.modelapi_shared_v1": SECRET,
                "credential.google_fixture_v1": SECRET,
            }
        ),
    )
    request = prepare_capability_probe_transport_request(
        probe_seal=seal,
        request_body=body,
        credential=credential,
        timeout_seconds=10,
    )
    assert request.protocol == protocol
    assert request.url == expected_url
    if protocol == "google_genai_generate_content":
        legacy_body = deepcopy(body)
        generation = legacy_body["generationConfig"]
        generation["responseSchema"] = generation.pop("responseJsonSchema")
        with pytest.raises(ContractValidationError, match="Google probe schema differs"):
            prepare_capability_probe_transport_request(
                probe_seal=seal,
                request_body=legacy_body,
                credential=credential,
                timeout_seconds=10,
            )


def test_receipt_or_evidence_cannot_be_resealed_as_qualified(tmp_path) -> None:
    seal, body = _seal()
    probe, _ = _probe(tmp_path, _FailureSender())
    result = probe.execute_once(
        seal=seal,
        request_body=body,
        local_validator=_validator,
        local_validator_id="literary.b1.entity_inventory.validator",
        local_validator_sha256=VALIDATOR_SHA,
    )
    forged_receipt = deepcopy(result["receipt"])
    forged_receipt["outcome"] = "qualified"
    forged_receipt["failure"] = None
    forged_receipt["response_contract_validated"] = True
    forged_receipt["receipt_sha256"] = canonical_sha256(
        {key: value for key, value in forged_receipt.items() if key != "receipt_sha256"}
    )
    forged_evidence = deepcopy(result["capability_evidence"])
    forged_evidence["verdict"] = "qualified"
    forged_evidence["evidence_sha256"] = forged_receipt["receipt_sha256"]
    with pytest.raises(ContractValidationError):
        validate_capability_probe_bundle(
            seal=seal,
            receipt=forged_receipt,
            capability_evidence=forged_evidence,
        )


def _literary_profile(*, source: dict, capability: dict) -> dict:
    fixture = json.loads(
        (FIXTURES / "profile_four_workstreams.json").read_text(encoding="utf-8")
    )
    profile = deepcopy(fixture["profiles"][1])
    role = profile["role_bindings"][0]
    role["role_id"] = "literary.b1.entity_inventory"
    role["preset_id"] = "literary.b1.entity_inventory.probe_fixture_v1"
    role["namespaces"] = {
        "output": "literary.b1.entity_inventory.output",
        "checkpoint": "literary.b1.entity_inventory.checkpoint",
        "cache": "literary.b1.entity_inventory.cache",
    }
    role["response_schema"]["sha256"] = capability["schema_sha256"]
    role["validator"].update(
        {
            "id": capability["local_validator_id"],
            "sha256": capability["local_validator_sha256"],
        }
    )
    role["primary"].update(
        {
            "source_id": source["source_id"],
            "source_revision": source["source_revision"],
            "source_record_sha256": canonical_sha256(source),
            "capability_id": capability["capability_id"],
            "capability_revision": capability["capability_revision"],
            "capability_record_sha256": canonical_sha256(capability),
            "requested_model_id": capability["requested_model_id"],
        }
    )
    return profile


def _resolve_profile(*, profile: dict, source: dict, capability: dict) -> dict:
    body = _request_body()
    return resolve_llm_run_seal(
        profile=profile,
        api_sources=[source],
        capability_evidence=[capability],
        role_id="literary.b1.entity_inventory",
        run_id="literary_probe_consumer_run",
        attempt_run_id="literary_probe_consumer_attempt",
        stage_id="literary_b1",
        input_bindings=[
            {"name": "transport_request_body", "sha256": canonical_sha256(body)}
        ],
    )
