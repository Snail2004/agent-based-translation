from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import json

import pytest

from pipeline.llm_backend import (
    ContractValidationError,
    MappingCredentialProvider,
    PhysicalQuotaScheduler,
    QuotaBusyError,
    RawTransportResponse,
    TransportCallError,
    canonical_sha256,
    credential_commitment,
)
from pipeline.prepass.d2l_shared_llm_adapter_v1 import (
    D2LSharedLlmAdapterError,
    D2LSharedLlmClient,
    D2LSharedLlmClientFactory,
    D2LSharedLlmAttemptAdapter,
    D2LSharedOpenAiTransportBridge,
    D2LTransportRetriesExhausted,
    render_google_generate_content_request,
    render_openai_chat_request,
)
from pipeline.prepass.d2l_shared_llm_profiles_v1 import get_role_preset
from pipeline.prepass.d2l_candidate_discovery_v2 import (
    RESPONSE_SCHEMA as DISCOVERY_RESPONSE_SCHEMA,
    render_discovery_messages,
)


SECRET = "test-only-shared-adapter-secret"
ROLE_ID = "d2l.b2.admission"


class _Clock:
    def __init__(self) -> None:
        self.tick = 0

    def __call__(self) -> datetime:
        self.tick += 1
        return datetime(2026, 7, 19, 0, 0, self.tick, tzinfo=timezone.utc)


class _OpenAiSender:
    def __init__(self) -> None:
        self.calls = 0
        self.requests = []

    def send(self, request) -> RawTransportResponse:
        self.calls += 1
        self.requests.append(request)
        payload = {
            "id": f"req_{self.calls}",
            "model": "gpt-5.5",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": '{"packet_id":"pkt","decisions":[]}'},
                }
            ],
            "usage": {
                "prompt_tokens": 12,
                "completion_tokens": 7,
                "total_tokens": 19,
                "prompt_tokens_details": {"cached_tokens": 0},
                "completion_tokens_details": {"reasoning_tokens": 0},
            },
        }
        return RawTransportResponse(
            status_code=200,
            headers={},
            body=json.dumps(payload, sort_keys=True).encode("utf-8"),
            request_id=f"req_{self.calls}",
        )


class _FailingSender:
    def send(self, request):
        raise TransportCallError(
            code="http_500",
            status_code=500,
            safe_message="provider returned HTTP 500",
        )


class _FailOnceSender(_OpenAiSender):
    def send(self, request) -> RawTransportResponse:
        if self.calls == 0:
            self.calls += 1
            raise TransportCallError(
                code="http_500",
                status_code=500,
                safe_message="provider returned HTTP 500",
            )
        return super().send(request)


class _GoogleCandidateSender:
    def __init__(self) -> None:
        self.calls = 0
        self.requests = []

    def send(self, request) -> RawTransportResponse:
        self.calls += 1
        self.requests.append(request)
        content = json.dumps(
            {
                "chapter_id": "chapter",
                "window_id": "window",
                "candidate_observations": [
                    {
                        "source_surface": "gradient descent",
                        "anchor_block_ids": ["block_1"],
                    }
                ],
            }
        )
        payload = {
            "responseId": f"gemini_{self.calls}",
            "modelVersion": "gemini-3.5-flash",
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {"parts": [{"text": content}]},
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 30,
                "cachedContentTokenCount": 0,
                "candidatesTokenCount": 20,
                "thoughtsTokenCount": 0,
                "totalTokenCount": 50,
            },
        }
        return RawTransportResponse(
            status_code=200,
            headers={},
            body=json.dumps(payload, sort_keys=True).encode("utf-8"),
            request_id=f"gemini_{self.calls}",
        )


def _source() -> dict:
    return {
        "schema_version": "api_source_v1",
        "source_id": "test_d2l_openai_source",
        "source_revision": "v1",
        "source_class": "remote_api",
        "adapter_id": "shared_urllib_openai_chat_v1",
        "protocol": "openai_chat_completions",
        "route_id": "chat_completions_create",
        "endpoint_class": "remote",
        "base_url": "https://provider.invalid/v1",
        "credential_ref": "credential.test_d2l_source",
        "credential_commitment": credential_commitment(SECRET),
        "physical_quota_bucket_id": "test-d2l-bucket-v1",
        "enabled": True,
    }


def _refs() -> tuple[dict, dict, dict, dict]:
    schema = {
        "type": "object",
        "properties": {
            "packet_id": {"type": "string"},
            "decisions": {"type": "array"},
        },
        "required": ["packet_id", "decisions"],
        "additionalProperties": False,
    }
    schema_hash = canonical_sha256(schema)
    return (
        {"id": "d2l_test_prompt_v1", "revision": "v1", "sha256": "a" * 64},
        {"id": "d2l_test_schema_v1", "revision": "v1", "sha256": schema_hash},
        {"id": "d2l_test_validator_v1", "revision": "v1", "sha256": "b" * 64},
        {
            "id": "d2l_test_extension_v1",
            "schema_version": "v1",
            "sha256": "c" * 64,
        },
    )


def _capability() -> dict:
    _, schema_ref, validator_ref, _ = _refs()
    source = _source()
    return {
        "schema_version": "capability_evidence_v1",
        "capability_id": "test_d2l_gpt55_native_so",
        "capability_revision": "v1",
        "source_id": source["source_id"],
        "source_revision": source["source_revision"],
        "adapter_id": source["adapter_id"],
        "protocol": source["protocol"],
        "route_id": source["route_id"],
        "base_url": source["base_url"],
        "requested_model_id": "gpt-5.5",
        "observed_model_id": "gpt-5.5",
        "capability_kind": "native_structured_output",
        "schema_dialect": "json_schema_2020_12",
        "schema_sha256": schema_ref["sha256"],
        "local_validator_id": validator_ref["id"],
        "local_validator_sha256": validator_ref["sha256"],
        "probe_id": "test_d2l_shared_adapter_probe",
        "evidence_sha256": "d" * 64,
        "observed_at_utc": "2026-07-19T00:00:00Z",
        "verdict": "qualified",
    }


def _limits(*, max_calls: int = 2) -> dict:
    return {
        "max_calls": max_calls,
        "max_prompt_tokens": 6000 * max_calls,
        "max_completion_tokens": 4096 * max_calls,
        "max_total_tokens": 10096 * max_calls,
        "max_cost_usd": None,
        "request_timeout_ms": 300000,
    }


def _execute(adapter, *, logical_request_id="packet_one", **overrides):
    prompt_ref, schema_ref, validator_ref, extension_ref = _refs()
    preset = get_role_preset(ROLE_ID)
    request = render_openai_chat_request(
        preset=preset,
        messages=[
            {"role": "system", "content": "Return the required JSON."},
            {"role": "user", "content": "packet"},
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "d2l_test",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "packet_id": {"type": "string"},
                        "decisions": {"type": "array"},
                    },
                    "required": ["packet_id", "decisions"],
                    "additionalProperties": False,
                },
            },
        },
    )
    kwargs = {
        "preset": preset,
        "api_source": _source(),
        "capability": _capability(),
        "prompt_ref": prompt_ref,
        "response_schema_ref": schema_ref,
        "validator_ref": validator_ref,
        "semantic_extension_ref": extension_ref,
        "structured_output": {
            "mode": "required",
            "schema_dialect": "json_schema_2020_12",
        },
        "limits": _limits(),
        "run_id": "d2l_test_run",
        "attempt_run_id": "d2l_test_attempt",
        "stage_id": "b2_admission",
        "logical_request_id": logical_request_id,
        "semantic_attempt_index": 1,
        "transport_retry_ordinal": 0,
        "request_body": request,
        "cost_fact": None,
    }
    kwargs.update(overrides)
    return adapter.execute(**kwargs)


def _adapter(tmp_path, sender):
    return D2LSharedLlmAttemptAdapter(
        runtime_root=tmp_path / "shared_runtime",
        credential_provider=MappingCredentialProvider(
            {"credential.test_d2l_source": SECRET}
        ),
        sender=sender,
        clock=_Clock(),
    )


def test_success_is_one_physical_attempt_and_semantic_text_is_returned(tmp_path) -> None:
    sender = _OpenAiSender()
    adapter = _adapter(tmp_path, sender)
    result = _execute(adapter)
    assert result.status == "provider_succeeded"
    assert result.provider_called is True
    assert result.response_text == '{"packet_id":"pkt","decisions":[]}'
    assert result.observed_model_id == "gpt-5.5"
    assert result.finish_reason == "stop"
    assert sender.calls == 1
    assert adapter.ledger.count("usage") == 1
    assert adapter.ledger.count("error") == 0


def test_transport_failure_is_persisted_without_hidden_retry(tmp_path) -> None:
    adapter = _adapter(tmp_path, _FailingSender())
    with pytest.raises(TransportCallError, match="HTTP 500"):
        _execute(adapter)
    assert adapter.ledger.count("usage") == 1
    assert adapter.ledger.count("error") == 1
    error = adapter.ledger.list_records("error")[0]
    assert error["retry_disposition"] == "do_not_retry"


def test_trusted_cache_hit_avoids_second_provider_call(tmp_path) -> None:
    sender = _OpenAiSender()
    adapter = _adapter(tmp_path, sender)
    first = _execute(adapter)
    second = _execute(adapter)
    assert first.provider_called is True
    assert second.status == "cache_hit"
    assert second.provider_called is False
    assert second.response_text == first.response_text
    assert sender.calls == 1


def test_duplicate_attempt_is_rejected_when_cache_is_bypassed(tmp_path) -> None:
    sender = _OpenAiSender()
    adapter = _adapter(tmp_path, sender)
    _execute(adapter, allow_response_cache_read=False)
    with pytest.raises(ContractValidationError, match="already exists"):
        _execute(adapter, allow_response_cache_read=False)
    assert sender.calls == 1


def test_stale_capability_binding_is_rejected_before_transport(tmp_path) -> None:
    sender = _OpenAiSender()
    adapter = _adapter(tmp_path, sender)
    capability = deepcopy(_capability())
    capability["schema_sha256"] = "e" * 64
    with pytest.raises(ContractValidationError, match="schema mismatch"):
        _execute(adapter, capability=capability)
    assert sender.calls == 0


def test_busy_physical_bucket_prevents_provider_call(tmp_path) -> None:
    sender = _OpenAiSender()
    adapter = _adapter(tmp_path, sender)
    scheduler = PhysicalQuotaScheduler(tmp_path / "shared_runtime" / "quota_locks")
    with scheduler.acquire(
        physical_quota_bucket_id="test-d2l-bucket-v1",
        lease_id="foreign_lease",
        owner_id="foreign_attempt",
        acquired_at_utc="2026-07-19T00:00:00Z",
    ):
        with pytest.raises(QuotaBusyError):
            _execute(adapter)
    assert sender.calls == 0


def test_request_renderers_keep_model_out_of_google_body() -> None:
    openai = render_openai_chat_request(
        preset=get_role_preset("d2l.b2.admission"),
        messages=[{"role": "user", "content": "x"}],
        response_format={"type": "json_object"},
    )
    assert openai["model"] == "gpt-5.5"
    assert openai["reasoning_effort"] == "none"
    google = render_google_generate_content_request(
        preset=get_role_preset("d2l.candidate_discovery"),
        messages=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": "user"},
        ],
        response_json_schema={"type": "object"},
    )
    assert "model" not in google
    assert google["systemInstruction"]["parts"] == [{"text": "system"}]
    assert google["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 0}


def test_google_prompt_validated_and_disabled_envelopes_never_send_native_schema() -> None:
    preset = get_role_preset("d2l.candidate_discovery")
    messages = [{"role": "user", "content": "Return JSON only."}]

    prompt_validated = render_google_generate_content_request(
        preset=preset,
        messages=messages,
        response_json_schema=None,
        structured_output_mode="prompt_validated",
    )
    assert prompt_validated["generationConfig"]["responseMimeType"] == (
        "application/json"
    )
    assert "responseJsonSchema" not in prompt_validated["generationConfig"]

    disabled = render_google_generate_content_request(
        preset=preset,
        messages=messages,
        response_json_schema=None,
        structured_output_mode="disabled",
    )
    assert "responseMimeType" not in disabled["generationConfig"]
    assert "responseJsonSchema" not in disabled["generationConfig"]


def test_google_non_native_mode_rejects_accidental_schema_injection() -> None:
    with pytest.raises(
        D2LSharedLlmAdapterError,
        match="may not send a response JSON schema",
    ):
        render_google_generate_content_request(
            preset=get_role_preset("d2l.candidate_discovery"),
            messages=[{"role": "user", "content": "Return JSON only."}],
            response_json_schema={"type": "object"},
            structured_output_mode="disabled",
        )


def test_openai_compatible_disabled_mode_does_not_send_native_schema(tmp_path) -> None:
    sender = _OpenAiSender()
    adapter = _adapter(tmp_path, sender)
    prompt_ref, schema_ref, validator_ref, extension_ref = _refs()
    client = D2LSharedLlmClient(
        adapter=adapter,
        config=object(),
        preset=get_role_preset(ROLE_ID),
        api_source=_source(),
        capability={
            **_capability(),
            "capability_kind": "text_generation",
            "schema_dialect": None,
            "schema_sha256": None,
            "local_validator_id": None,
            "local_validator_sha256": None,
        },
        prompt_ref=prompt_ref,
        response_schema_ref=schema_ref,
        validator_ref=validator_ref,
        semantic_extension_ref=extension_ref,
        structured_output={"mode": "disabled", "schema_dialect": None},
        limits=_limits(),
        run_id="d2l_disabled_openai_test",
        attempt_run_id="d2l_disabled_openai_attempt",
        stage_id="b2_admission",
    )
    client.call(
        [{"role": "user", "content": "Return JSON only."}],
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "must_not_reach_wire", "schema": {}},
        },
        tag="disabled_output",
    )
    stable_resume_identity = client.resume_transport_identity
    physical_attempt_identity = client.transport_identity
    client.attempt_run_id = "d2l_disabled_openai_attempt_2"
    assert client.resume_transport_identity == stable_resume_identity
    assert client.transport_identity != physical_attempt_identity
    wire = json.loads(sender.requests[0].body.decode("utf-8"))
    assert "response_format" not in wire


def test_google_disabled_client_uses_prompt_json_and_local_parse_only(tmp_path) -> None:
    secret = SECRET
    source = {
        "schema_version": "api_source_v1",
        "source_id": "test_d2l_google_prompt_json",
        "source_revision": "v1",
        "source_class": "remote_api",
        "adapter_id": "shared_urllib_google_genai_v1",
        "protocol": "google_genai_generate_content",
        "route_id": "generate_content",
        "endpoint_class": "remote",
        "base_url": "https://provider.invalid",
        "credential_ref": "credential.test_d2l_google_prompt_json",
        "credential_commitment": credential_commitment(secret),
        "physical_quota_bucket_id": "test-d2l-google-prompt-json-v1",
        "enabled": True,
    }
    base_preset = get_role_preset("d2l.candidate_discovery")
    role_id = "d2l.translator.s1.prompt_json_test"
    preset = replace(
        base_preset,
        role_id=role_id,
        preset_id=f"{role_id}.gemini35_disabled_v1",
        preset_revision="v1",
        source_choice=source["source_id"],
        namespaces={
            "output": f"{role_id}.output",
            "checkpoint": f"{role_id}.checkpoint",
            "cache": f"{role_id}.cache",
        },
    )
    capability = {
        "schema_version": "capability_evidence_v1",
        "capability_id": "test_d2l_google_text_generation",
        "capability_revision": "v1",
        "source_id": source["source_id"],
        "source_revision": source["source_revision"],
        "adapter_id": source["adapter_id"],
        "protocol": source["protocol"],
        "route_id": source["route_id"],
        "base_url": source["base_url"],
        "requested_model_id": preset.requested_model_id,
        "observed_model_id": preset.requested_model_id,
        "capability_kind": "text_generation",
        "schema_dialect": None,
        "schema_sha256": None,
        "local_validator_id": None,
        "local_validator_sha256": None,
        "probe_id": "test_prior_basic_generation",
        "evidence_sha256": "e" * 64,
        "observed_at_utc": "2026-07-19T00:00:00Z",
        "verdict": "qualified",
    }
    sender = _GoogleCandidateSender()
    adapter = D2LSharedLlmAttemptAdapter(
        runtime_root=tmp_path / "shared_runtime",
        credential_provider=MappingCredentialProvider(
            {source["credential_ref"]: secret}
        ),
        sender=sender,
        clock=_Clock(),
    )
    prompt_ref, _, validator_ref, extension_ref = _refs()
    client = D2LSharedLlmClient(
        adapter=adapter,
        config=object(),
        preset=preset,
        api_source=source,
        capability=capability,
        prompt_ref=prompt_ref,
        response_schema_ref=None,
        validator_ref=validator_ref,
        semantic_extension_ref=extension_ref,
        structured_output={"mode": "disabled", "schema_dialect": None},
        limits={
            "max_calls": 2,
            "max_prompt_tokens": 12_000,
            "max_completion_tokens": 12_288,
            "max_total_tokens": 24_288,
            "max_cost_usd": None,
            "request_timeout_ms": 300_000,
        },
        run_id="d2l_prompt_json_test",
        attempt_run_id="d2l_prompt_json_attempt",
        stage_id="translator",
        google_response_json_schema=None,
    )
    result = client.call(
        [{"role": "user", "content": "Return JSON only."}],
        response_format={"type": "json_object"},
        tag="prompt_json",
    )

    assert result.parsed_json["chapter_id"] == "chapter"
    assert sender.calls == 1
    wire = json.loads(sender.requests[0].body.decode("utf-8"))
    assert "responseMimeType" not in wire["generationConfig"]
    assert "responseJsonSchema" not in wire["generationConfig"]


def test_llm_client_compatibility_projection_uses_shared_cache(tmp_path) -> None:
    sender = _OpenAiSender()
    adapter = _adapter(tmp_path, sender)
    prompt_ref, schema_ref, validator_ref, extension_ref = _refs()
    preset = get_role_preset(ROLE_ID)
    config = type(
        "Config",
        (),
        {
            "model": "gpt-5.5",
            "temperature": 1.0,
            "seed": 20260718,
        },
    )()
    client = D2LSharedLlmClient(
        adapter=adapter,
        config=config,
        preset=preset,
        api_source=_source(),
        capability=_capability(),
        prompt_ref=prompt_ref,
        response_schema_ref=schema_ref,
        validator_ref=validator_ref,
        semantic_extension_ref=extension_ref,
        structured_output={
            "mode": "required",
            "schema_dialect": "json_schema_2020_12",
        },
        limits=_limits(),
        run_id="d2l_client_test",
        attempt_run_id="d2l_client_attempt",
        stage_id="b2_admission",
    )
    messages = [{"role": "user", "content": "packet"}]
    response_format = {
        "type": "json_schema",
        "json_schema": {
            "name": "d2l_test",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "packet_id": {"type": "string"},
                    "decisions": {"type": "array"},
                },
                "required": ["packet_id", "decisions"],
                "additionalProperties": False,
            },
        },
    }
    first = client.call(messages, response_format=response_format, tag="packet")
    second = client.call(messages, response_format=response_format, tag="packet")
    assert first.parsed_json == {"packet_id": "pkt", "decisions": []}
    assert first.from_cache is False
    assert first.cost_usd is None
    assert first.cost_status == "unknown"
    assert second.from_cache is True
    assert second.cost_usd is None
    assert second.cost_status == "cache_reuse"
    assert first.seal_sha256 == second.seal_sha256
    assert first.logical_request_id == second.logical_request_id
    assert first.physical_attempt_index == 1
    assert first.provider_id == first.source_id == "test_d2l_openai_source"
    assert first.masked_quota_bucket == "test-d2l...***"
    assert first.finish_reason == "stop"
    assert first.cache_status == "miss"
    assert first.cache_mechanism == "local_exact_cache"
    assert second.cache_status == "hit"
    assert second.cache_mechanism == "local_exact_cache"
    assert sender.calls == 1


def test_candidate_discovery_google_client_preserves_exact_contract(
    tmp_path,
) -> None:
    preset = get_role_preset("d2l.candidate_discovery")
    source = _source()
    source.update(
        {
            "source_id": "test_d2l_google_source",
            "source_revision": "v1",
            "adapter_id": "google_genai_rest_v1",
            "protocol": "google_genai_generate_content",
            "route_id": "models_generate_content",
            "base_url": "https://provider.invalid/v1beta",
            "credential_ref": "credential.test_d2l_google",
            "credential_commitment": credential_commitment(SECRET),
            "physical_quota_bucket_id": "test-d2l-google-bucket-v1",
        }
    )
    schema_hash = canonical_sha256(DISCOVERY_RESPONSE_SCHEMA)
    prompt_ref = {
        "id": "d2l_candidate_discovery_v2",
        "revision": "v2",
        "sha256": "a" * 64,
    }
    schema_ref = {
        "id": "d2l_candidate_discovery_schema_v2",
        "revision": "v2",
        "sha256": schema_hash,
    }
    validator_ref = {
        "id": "d2l_candidate_discovery_validator_v2",
        "revision": "v2",
        "sha256": "b" * 64,
    }
    extension_ref = {
        "id": "d2l_candidate_discovery_extension_v1",
        "schema_version": "v1",
        "sha256": "c" * 64,
    }
    capability = _capability()
    capability.update(
        {
            "capability_id": "test_d2l_google_candidate_so",
            "capability_revision": "v1",
            "source_id": source["source_id"],
            "source_revision": source["source_revision"],
            "adapter_id": source["adapter_id"],
            "protocol": source["protocol"],
            "route_id": source["route_id"],
            "base_url": source["base_url"],
            "requested_model_id": preset.requested_model_id,
            "observed_model_id": preset.requested_model_id,
            "schema_sha256": schema_hash,
            "local_validator_id": validator_ref["id"],
            "local_validator_sha256": validator_ref["sha256"],
        }
    )
    sender = _GoogleCandidateSender()
    adapter = D2LSharedLlmAttemptAdapter(
        runtime_root=tmp_path / "shared_runtime",
        credential_provider=MappingCredentialProvider(
            {"credential.test_d2l_google": SECRET}
        ),
        sender=sender,
        clock=_Clock(),
    )
    client = D2LSharedLlmClient(
        adapter=adapter,
        config=type(
            "Config",
            (),
            {
                "model": preset.requested_model_id,
                "temperature": 1.0,
                "seed": 20260612,
            },
        )(),
        preset=preset,
        api_source=source,
        capability=capability,
        prompt_ref=prompt_ref,
        response_schema_ref=schema_ref,
        validator_ref=validator_ref,
        semantic_extension_ref=extension_ref,
        structured_output={
            "mode": "required",
            "schema_dialect": "json_schema_2020_12",
        },
        limits={
            "max_calls": 3,
            "max_prompt_tokens": 18_000,
            "max_completion_tokens": 18_432,
            "max_total_tokens": 36_432,
            "max_cost_usd": None,
            "request_timeout_ms": 300_000,
        },
        run_id="d2l_candidate_test",
        attempt_run_id="d2l_candidate_attempt",
        stage_id="candidate_discovery",
        google_response_json_schema=DISCOVERY_RESPONSE_SCHEMA,
    )
    config = type(
        "Config",
        (),
        {
            "model": preset.requested_model_id,
            "temperature": 1.0,
            "seed": 20260612,
            "reasoning_effort": "none",
            "verbosity": "low",
            "max_output_tokens": 6144,
            "prompt_token_cap": 6000,
        },
    )()
    factory = D2LSharedLlmClientFactory(client)
    assert factory(config, tmp_path / "ignored.sqlite3") is client

    result = client.call(
        render_discovery_messages(
            chapter_id="chapter",
            window_id="window",
            source_blocks=[("block_1", "gradient descent")],
        ),
        response_format={"type": "json_object"},
        tag="d2l_candidate_discovery_v2:window:attempt-1",
    )

    assert result.parsed_json == {
        "chapter_id": "chapter",
        "window_id": "window",
        "candidate_observations": [
            {
                "source_surface": "gradient descent",
                "anchor_block_ids": ["block_1"],
            }
        ],
    }
    assert result.model == "gemini-3.5-flash"
    assert result.cost_usd is None
    assert result.cost_status == "unknown"
    assert sender.calls == 1

    config.max_output_tokens = 4096
    with pytest.raises(
        D2LSharedLlmAdapterError,
        match="differs from the sealed D2L role preset",
    ):
        factory(config, tmp_path / "ignored.sqlite3")


def test_openai_bridge_rejects_wire_drift_and_records_attempt(tmp_path) -> None:
    sender = _OpenAiSender()
    adapter = _adapter(tmp_path, sender)
    prompt_ref, schema_ref, validator_ref, extension_ref = _refs()
    preset = get_role_preset(ROLE_ID)
    config = type(
        "Config",
        (),
        {"model": "gpt-5.5", "temperature": 1.0, "seed": 20260718},
    )()
    client = D2LSharedLlmClient(
        adapter=adapter,
        config=config,
        preset=preset,
        api_source=_source(),
        capability=_capability(),
        prompt_ref=prompt_ref,
        response_schema_ref=schema_ref,
        validator_ref=validator_ref,
        semantic_extension_ref=extension_ref,
        structured_output={
            "mode": "required",
            "schema_dialect": "json_schema_2020_12",
        },
        limits=_limits(),
        run_id="d2l_bridge_test",
        attempt_run_id="d2l_bridge_attempt",
        stage_id="b2_admission",
    )
    bridge = D2LSharedOpenAiTransportBridge(client)
    request = render_openai_chat_request(
        preset=preset,
        messages=[{"role": "user", "content": "packet"}],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "d2l_test",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "packet_id": {"type": "string"},
                        "decisions": {"type": "array"},
                    },
                    "required": ["packet_id", "decisions"],
                    "additionalProperties": False,
                },
            },
        },
    )
    payload = bridge(**request)
    assert payload["model"] == "gpt-5.5"
    assert bridge.last_attempt_metadata["provider_called"] is True
    drifted = dict(request)
    drifted["temperature"] = 0.0
    with pytest.raises(
        Exception, match="differs from sealed shared renderer"
    ):
        bridge(**drifted)


def test_client_retries_retryable_transport_and_reports_recovery(
    tmp_path,
) -> None:
    sender = _FailOnceSender()
    adapter = _adapter(tmp_path, sender)
    prompt_ref, schema_ref, validator_ref, extension_ref = _refs()
    base_preset = get_role_preset(ROLE_ID)
    preset = replace(
        base_preset,
        transport_retry={
            "max_retries": 2,
            "backoff_policy": "exponential",
            "initial_delay_ms": 1000,
            "max_delay_ms": 4000,
            "retryable_codes": [
                "connection",
                "rate_limit",
                "server_unavailable",
                "timeout",
            ],
        },
    )
    config = type(
        "Config",
        (),
        {"model": "gpt-5.5", "temperature": 1.0, "seed": 20260718},
    )()
    sleeps: list[float] = []
    client = D2LSharedLlmClient(
        adapter=adapter,
        config=config,
        preset=preset,
        api_source=_source(),
        capability=_capability(),
        prompt_ref=prompt_ref,
        response_schema_ref=schema_ref,
        validator_ref=validator_ref,
        semantic_extension_ref=extension_ref,
        structured_output={
            "mode": "required",
            "schema_dialect": "json_schema_2020_12",
        },
        limits=_limits(),
        run_id="d2l_retry_test",
        attempt_run_id="d2l_retry_attempt",
        stage_id="b2_admission",
        sleeper=sleeps.append,
    )
    messages = [{"role": "user", "content": "packet"}]
    response_format = {
        "type": "json_schema",
        "json_schema": {
            "name": "d2l_test",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "packet_id": {"type": "string"},
                    "decisions": {"type": "array"},
                },
                "required": ["packet_id", "decisions"],
                "additionalProperties": False,
            },
        },
    }
    observations: list[tuple[str, dict]] = []
    result = client.call(
        messages,
        response_format=response_format,
        tag="same",
        transport_observer=lambda event, payload: observations.append(
            (event, dict(payload))
        ),
    )
    assert result.parsed_json == {"packet_id": "pkt", "decisions": []}
    assert result.transport_retry_summary == {
        "logical_request_id": result.logical_request_id,
        "retry_count": 1,
        "outcome": "recovered",
        "reason_codes": ["server_unavailable"],
    }
    assert sleeps == [1.0]
    assert [event for event, _payload in observations] == [
        "attempt_failed",
        "retry_scheduled",
    ]
    rows = sorted(
        adapter.ledger.list_records("usage"),
        key=lambda row: row["physical_attempt_index"],
    )
    assert [row["transport_retry_ordinal"] for row in rows] == [0, 1]


def test_client_exhaustion_is_bounded_and_preserves_every_attempt(
    tmp_path,
) -> None:
    sender = _FailingSender()
    adapter = _adapter(tmp_path, sender)
    prompt_ref, schema_ref, validator_ref, extension_ref = _refs()
    preset = replace(
        get_role_preset(ROLE_ID),
        transport_retry={
            "max_retries": 2,
            "backoff_policy": "exponential",
            "initial_delay_ms": 1000,
            "max_delay_ms": 4000,
            "retryable_codes": [
                "connection",
                "rate_limit",
                "server_unavailable",
                "timeout",
            ],
        },
    )
    sleeps: list[float] = []
    client = D2LSharedLlmClient(
        adapter=adapter,
        config=type(
            "Config",
            (),
            {"model": "gpt-5.5", "temperature": 1.0, "seed": 20260718},
        )(),
        preset=preset,
        api_source=_source(),
        capability=_capability(),
        prompt_ref=prompt_ref,
        response_schema_ref=schema_ref,
        validator_ref=validator_ref,
        semantic_extension_ref=extension_ref,
        structured_output={
            "mode": "required",
            "schema_dialect": "json_schema_2020_12",
        },
        limits=_limits(max_calls=3),
        run_id="d2l_retry_exhausted_test",
        attempt_run_id="d2l_retry_exhausted_attempt",
        stage_id="b2_admission",
        sleeper=sleeps.append,
    )
    observations: list[tuple[str, dict]] = []
    with pytest.raises(D2LTransportRetriesExhausted) as caught:
        client.call(
            [{"role": "user", "content": "packet"}],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "d2l_test",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "packet_id": {"type": "string"},
                            "decisions": {"type": "array"},
                        },
                        "required": ["packet_id", "decisions"],
                        "additionalProperties": False,
                    },
                },
            },
            tag="same",
            transport_observer=lambda event, payload: observations.append(
                (event, dict(payload))
            ),
        )

    assert caught.value.retry_summary["retry_count"] == 2
    assert caught.value.retry_summary["outcome"] == "exhausted"
    assert sleeps == [1.0, 2.0]
    assert [event for event, _payload in observations] == [
        "attempt_failed",
        "retry_scheduled",
        "attempt_failed",
        "retry_scheduled",
        "attempt_failed",
        "retry_summary",
    ]
    rows = sorted(
        adapter.ledger.list_records("usage"),
        key=lambda row: row["physical_attempt_index"],
    )
    assert [row["transport_retry_ordinal"] for row in rows] == [0, 1, 2]
    assert all(row["cost_usd"] is None for row in rows)


def test_pipeline_reask_with_same_tag_is_a_new_logical_request(tmp_path) -> None:
    sender = _OpenAiSender()
    adapter = _adapter(tmp_path, sender)
    prompt_ref, schema_ref, validator_ref, extension_ref = _refs()
    preset = get_role_preset(ROLE_ID)
    config = type(
        "Config",
        (),
        {"model": "gpt-5.5", "temperature": 1.0, "seed": 20260718},
    )()
    client = D2LSharedLlmClient(
        adapter=adapter,
        config=config,
        preset=preset,
        api_source=_source(),
        capability=_capability(),
        prompt_ref=prompt_ref,
        response_schema_ref=schema_ref,
        validator_ref=validator_ref,
        semantic_extension_ref=extension_ref,
        structured_output={
            "mode": "required",
            "schema_dialect": "json_schema_2020_12",
        },
        limits=_limits(),
        run_id="d2l_semantic_reask_test",
        attempt_run_id="d2l_semantic_reask_attempt",
        stage_id="b2_admission",
    )
    response_format = {
        "type": "json_schema",
        "json_schema": {
            "name": "d2l_test",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "packet_id": {"type": "string"},
                    "decisions": {"type": "array"},
                },
                "required": ["packet_id", "decisions"],
                "additionalProperties": False,
            },
        },
    }

    client.call(
        [{"role": "user", "content": "initial packet"}],
        response_format=response_format,
        tag="same_pipeline_semantic_request",
    )
    client.call(
        [{"role": "user", "content": "re-ask with validator feedback"}],
        response_format=response_format,
        tag="same_pipeline_semantic_request",
    )

    rows = adapter.ledger.list_records("usage")
    assert sender.calls == 2
    assert len({row["logical_request_id"] for row in rows}) == 2
    assert {row["transport_retry_ordinal"] for row in rows} == {0}
