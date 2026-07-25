from __future__ import annotations

from copy import deepcopy
import json

import pytest

from pipeline.llm_backend import MappingCredentialProvider, RawTransportResponse
from pipeline.prepass import d2l_project_transport_v1 as transport
from pipeline.prepass.d2l_project_campaign_v2 import (
    initial_transport_sources,
    semantic_role_profiles,
)


SECRET = "test-only-d2l-project-transport"


class _OpenAiSender:
    def __init__(self) -> None:
        self.requests = []

    def send(self, request) -> RawTransportResponse:
        self.requests.append(request)
        payload = {
            "id": "req_d2l_project_1",
            "model": "gpt-5.4",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": '{"packet_id":"packet_1","decisions":[]}'
                    },
                }
            ],
            "usage": {
                "prompt_tokens": 20,
                "completion_tokens": 10,
                "total_tokens": 30,
                "prompt_tokens_details": {"cached_tokens": 0},
                "completion_tokens_details": {"reasoning_tokens": 0},
            },
        }
        return RawTransportResponse(
            status_code=200,
            headers={},
            body=json.dumps(payload, sort_keys=True).encode("utf-8"),
            request_id="req_d2l_project_1",
        )


def _provider(source: dict) -> MappingCredentialProvider:
    return MappingCredentialProvider({source["credential_ref"]: SECRET})


def _role(role_id: str = "d2l.b2.admission") -> dict:
    return next(
        row for row in semantic_role_profiles() if row["role_id"] == role_id
    )


def _loaded(role: dict) -> dict:
    attempts = 6
    return {
        "seal": {"integrity": {"payload_sha256": "a" * 64}},
        "config": {
            "component_run_id": "d2l_component_transport_test",
            "semantic_roles": [role],
            "transport_policy": {
                "version": transport.TRANSPORT_VERSION,
                "retry": deepcopy(transport.TRANSPORT_RETRY_POLICY),
            },
            "limits": {
                "hard_total_token_cap": 100_000,
                "roles": [
                    {
                        "role_id": role["role_id"],
                        "physical_attempt_cap": attempts,
                        "max_input_tokens_per_attempt": role["generation"][
                            "max_input_tokens"
                        ],
                        "max_output_tokens_per_attempt": role["generation"][
                            "max_output_tokens"
                        ],
                        "max_total_tokens": attempts
                        * (
                            role["generation"]["max_input_tokens"]
                            + role["generation"]["max_output_tokens"]
                        ),
                    }
                ],
            },
        },
    }


def _attempt(role: dict, source: dict) -> dict:
    return {
        "schema_version": "d2l_transport_attempt_seal_v2",
        "component_attempt_id": 1,
        "transport_attempt_index": 1,
        "role_id": role["role_id"],
        "model_id": role["model_id"],
        "source": source,
        "integrity": {"payload_sha256": "b" * 64},
    }


def test_runtime_source_binds_commitment_without_exposing_value() -> None:
    source = initial_transport_sources()["modelapi_shared_v1"]
    runtime = transport.build_runtime_api_source(
        source, credential_provider=_provider(source)
    )
    rendered = json.dumps(runtime, sort_keys=True)
    assert SECRET not in rendered
    assert runtime["credential_commitment"] != SECRET
    assert runtime["source_id"] == "modelapi_shared_v1"


def test_every_production_source_builds_a_schema_valid_runtime_record() -> None:
    sources = initial_transport_sources()
    credentials = {
        str(source["credential_ref"]): f"test-only-{source_id}"
        for source_id, source in sources.items()
    }
    provider = MappingCredentialProvider(credentials)

    for source_id, source in sources.items():
        runtime = transport.build_runtime_api_source(
            source,
            credential_provider=provider,
        )
        assert runtime["source_id"] == source_id
        assert runtime["physical_quota_bucket_id"] == source[
            "physical_quota_bucket_id"
        ]


def test_runtime_source_fails_when_credential_is_unavailable() -> None:
    source = initial_transport_sources()["modelapi_shared_v1"]
    with pytest.raises(transport.D2LProjectTransportError, match="unavailable"):
        transport.build_runtime_api_source(
            source, credential_provider=MappingCredentialProvider({})
        )


def test_capability_is_exact_source_revision_and_model() -> None:
    source = initial_transport_sources()["modelapi_shared_v1"]
    runtime = transport.build_runtime_api_source(
        source, credential_provider=_provider(source)
    )
    capability = transport.trusted_capability_for(runtime, model_id="gpt-5.4")
    assert capability["capability_kind"] == "text_generation"
    assert capability["schema_sha256"] is None
    with pytest.raises(transport.D2LProjectTransportError, match="lacks qualified"):
        transport.trusted_capability_for(runtime, model_id="gpt-5.6")


def test_shopaikey_openai_route_reports_the_exact_requested_gemini_model() -> None:
    source = initial_transport_sources()["shopaikey_openai_proxy_v1"]
    runtime = transport.build_runtime_api_source(
        source, credential_provider=_provider(source)
    )
    capability = transport.trusted_capability_for(
        runtime, model_id="gemini-3.5-flash"
    )
    assert capability["observed_model_id"] == "gemini-3.5-flash"
    assert capability["protocol"] == "openai_chat_completions"
    assert capability["base_url"] == "https://api.shopaikey.com/v1"


def test_project_client_keeps_third_party_native_schema_off_wire(
    tmp_path, monkeypatch
) -> None:
    role = _role()
    source = deepcopy(initial_transport_sources()["modelapi_shared_v1"])
    loaded = _loaded(role)
    monkeypatch.setattr(transport, "load_campaign", lambda _root: loaded)
    monkeypatch.setattr(
        transport,
        "load_transport_attempt_seal",
        lambda *_args, **_kwargs: _attempt(role, source),
    )
    sender = _OpenAiSender()
    project = transport.D2LProjectTransport(
        campaign_root=tmp_path / "campaign",
        runtime_root=tmp_path / "runtime",
        credential_provider=_provider(source),
        sender=sender,
    )
    client = project.build_client(role["role_id"])
    result = client.call(
        [{"role": "user", "content": "Return JSON only."}],
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "must_not_reach_proxy", "schema": {}},
        },
        tag="packet_1",
    )
    assert result.parsed_json == {"packet_id": "packet_1", "decisions": []}
    wire = json.loads(sender.requests[0].body.decode("utf-8"))
    assert "response_format" not in wire
    assert wire["model"] == "gpt-5.4"


def test_transport_attempt_component_identity_is_fail_closed(
    tmp_path, monkeypatch
) -> None:
    role = _role()
    source = deepcopy(initial_transport_sources()["modelapi_shared_v1"])
    monkeypatch.setattr(transport, "load_campaign", lambda _root: _loaded(role))
    attempt = _attempt(role, source)
    attempt["component_attempt_id"] = 2
    monkeypatch.setattr(
        transport,
        "load_transport_attempt_seal",
        lambda *_args, **_kwargs: attempt,
    )
    project = transport.D2LProjectTransport(
        campaign_root=tmp_path / "campaign",
        runtime_root=tmp_path / "runtime",
        credential_provider=_provider(source),
        sender=_OpenAiSender(),
    )
    with pytest.raises(
        transport.D2LProjectTransportError, match="different component attempt"
    ):
        project.build_client(role["role_id"], component_attempt_id=1)


def test_default_transport_attempt_tracks_component_attempt(
    tmp_path, monkeypatch
) -> None:
    role = _role()
    source = deepcopy(initial_transport_sources()["modelapi_shared_v1"])
    monkeypatch.setattr(transport, "load_campaign", lambda _root: _loaded(role))
    seen: list[int] = []

    def load_attempt(*_args, **kwargs):
        seen.append(kwargs["transport_attempt_index"])
        attempt = _attempt(role, source)
        attempt["component_attempt_id"] = 2
        attempt["transport_attempt_index"] = 2
        return attempt

    monkeypatch.setattr(transport, "load_transport_attempt_seal", load_attempt)
    project = transport.D2LProjectTransport(
        campaign_root=tmp_path / "campaign",
        runtime_root=tmp_path / "runtime",
        credential_provider=_provider(source),
        sender=_OpenAiSender(),
    )
    project.build_client(role["role_id"], component_attempt_id=2)
    assert seen == [2]


def test_historical_campaign_without_transport_policy_fails_closed(
    tmp_path, monkeypatch
) -> None:
    role = _role()
    loaded = _loaded(role)
    del loaded["config"]["transport_policy"]
    monkeypatch.setattr(transport, "load_campaign", lambda _root: loaded)

    with pytest.raises(
        transport.D2LProjectTransportError,
        match="prepare a new campaign",
    ):
        transport.D2LProjectTransport(
            campaign_root=tmp_path / "campaign",
            runtime_root=tmp_path / "runtime",
            credential_provider=MappingCredentialProvider({}),
            sender=_OpenAiSender(),
        )


def test_role_namespaces_and_limits_are_revision_bound() -> None:
    role = _role()
    preset = transport.role_preset(role, "modelapi_shared_v1")
    assert role["semantic_role_sha256"][:16].lower() in preset.preset_id
    assert preset.transport_retry["max_retries"] == 2
    assert preset.transport_retry["backoff_policy"] == "exponential"
    assert len(set(preset.namespaces.values())) == 3
    limits = transport.role_limits(_loaded(role)["config"], role["role_id"])
    assert limits["max_calls"] == 6
    assert limits["max_total_tokens"] >= (
        role["generation"]["max_input_tokens"]
        + role["generation"]["max_output_tokens"]
    )
