from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Mapping

import pytest

from pipeline.literary.shared_llm_adapter_v1 import (
    LiterarySharedLlmAdapterError,
    LiterarySharedLlmAttemptAdapter,
    build_prompt_json_instruction,
    render_literary_request_body,
)
from pipeline.literary.shared_llm_profiles_v1 import ROLE_PRESETS
from pipeline.llm_backend import (
    ApplicationResponseCache,
    ContentAddressedArtifactStore,
    MappingCredentialProvider,
    PhysicalQuotaScheduler,
    RawTransportResponse,
    SharedLlmAttemptLedger,
    SharedLlmBackend,
    canonical_json,
    canonical_sha256,
    credential_commitment,
)


SECRET = "synthetic-literary-shared-secret"
SCHEMA = {
    "type": "object",
    "properties": {"role": {"type": "string"}},
    "required": ["role"],
    "additionalProperties": False,
}
SCHEMA_HASH = canonical_sha256(SCHEMA)
VALIDATOR_HASH = "d" * 64


class _RoleResponseSender:
    def __init__(self, role_id: str) -> None:
        self.role_id = role_id
        self.calls = 0
        self.requests = []

    def send(self, request) -> RawTransportResponse:
        self.calls += 1
        self.requests.append(request)
        body = json.loads(request.body.decode("utf-8"))
        assert body["model"] == "gpt-5.4"
        assert body["response_format"]["type"] == "json_schema"
        content = canonical_json({"role": self.role_id})
        payload = {
            "id": f"fake-{self.calls}",
            "model": "gpt-5.4",
            "choices": [
                {"finish_reason": "stop", "message": {"content": content}}
            ],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
                "prompt_tokens_details": {"cached_tokens": 0},
                "completion_tokens_details": {"reasoning_tokens": 0},
            },
        }
        return RawTransportResponse(
            status_code=200,
            headers={"x-request-id": f"fake-{self.calls}"},
            body=canonical_json(payload).encode("utf-8"),
            request_id=f"fake-{self.calls}",
        )


def _source() -> dict[str, Any]:
    return {
        "schema_version": "api_source_v1",
        "source_id": "literary_fake_openai_v1",
        "source_revision": "fake_transport_v1",
        "source_class": "remote_api",
        "adapter_id": "openai_python_v1",
        "protocol": "openai_chat_completions",
        "route_id": "chat_completions_create",
        "endpoint_class": "remote",
        "base_url": "https://provider.invalid/v1",
        "credential_ref": "credential.literary_fake_v1",
        "credential_commitment": credential_commitment(SECRET),
        "physical_quota_bucket_id": "literary-fake-v1",
        "enabled": True,
    }


def _capability(role_id: str) -> dict[str, Any]:
    return {
        "schema_version": "capability_evidence_v1",
        "capability_id": f"{role_id}.fake_native_so_v1",
        "capability_revision": "fake_transport_v1",
        "source_id": "literary_fake_openai_v1",
        "source_revision": "fake_transport_v1",
        "adapter_id": "openai_python_v1",
        "protocol": "openai_chat_completions",
        "route_id": "chat_completions_create",
        "base_url": "https://provider.invalid/v1",
        "requested_model_id": "gpt-5.4",
        "observed_model_id": "gpt-5.4",
        "capability_kind": "native_structured_output",
        "schema_dialect": "json_schema_2020_12",
        "schema_sha256": SCHEMA_HASH,
        "local_validator_id": f"{role_id}.validator_v1",
        "local_validator_sha256": VALIDATOR_HASH,
        "probe_id": f"{role_id}.fake_probe_v1",
        "evidence_sha256": "e" * 64,
        "observed_at_utc": "2026-07-19T00:00:00Z",
        "verdict": "qualified",
    }


def _ref(identifier: str, digest: str) -> dict[str, str]:
    return {"id": identifier, "revision": "v1", "sha256": digest}


def _backend(tmp_path: Path, sender: _RoleResponseSender):
    artifact_store = ContentAddressedArtifactStore(tmp_path / "objects")
    ledger = SharedLlmAttemptLedger(tmp_path / "attempt_ledger.sqlite3")
    cache = ApplicationResponseCache(
        index_path=tmp_path / "response_cache.sqlite3",
        artifact_store=artifact_store,
    )
    backend = SharedLlmBackend(
        credential_provider=MappingCredentialProvider(
            {"credential.literary_fake_v1": SECRET}
        ),
        scheduler=PhysicalQuotaScheduler(tmp_path / "quota_locks"),
        ledger=ledger,
        response_cache=cache,
        sender=sender,
    )
    return backend, ledger


def _execute(tmp_path: Path, role_id: str, *, validator=None):
    sender = _RoleResponseSender(role_id)
    backend, ledger = _backend(tmp_path, sender)
    adapter = LiterarySharedLlmAttemptAdapter(backend=backend)
    result = adapter.execute(
        preset=ROLE_PRESETS[role_id],
        api_source=_source(),
        capability=_capability(role_id),
        messages=[
            {"role": "system", "content": "Return the requested JSON."},
            {"role": "user", "content": f"Process {role_id}."},
        ],
        response_schema=SCHEMA,
        schema_name=role_id.replace(".", "_"),
        prompt_ref=_ref(f"{role_id}.prompt_v1", "b" * 64),
        response_schema_ref=_ref(f"{role_id}.schema_v1", SCHEMA_HASH),
        validator_ref=_ref(f"{role_id}.validator_v1", VALIDATOR_HASH),
        semantic_extension_ref={
            "id": f"{role_id}.authority_v1",
            "schema_version": "literary_semantic_authority_v1",
            "sha256": "f" * 64,
        },
        structured_output={
            "mode": "required",
            "schema_dialect": "json_schema_2020_12",
        },
        semantic_validator=validator or (lambda row: dict(row)),
        run_id="literary_fake_run",
        attempt_run_id="literary_fake_attempt",
        stage_id=role_id.replace(".", "_"),
        logical_request_id=f"request_{role_id.replace('.', '_')}",
        additional_input_bindings=[
            {"name": "literary_context_packet", "sha256": "a" * 64}
        ],
    )
    return result, sender, ledger


@pytest.mark.parametrize("role_id", sorted(ROLE_PRESETS))
def test_all_active_roles_execute_exactly_one_fake_shared_attempt(
    tmp_path: Path, role_id: str
) -> None:
    result, sender, ledger = _execute(
        tmp_path / role_id.replace(".", "_"), role_id
    )
    assert sender.calls == 1
    assert result.status == "semantic_accepted"
    assert result.semantic_payload == {"role": role_id}
    assert result.seal["role_id"] == role_id
    assert result.seal["fallback_plan"] == {"enabled": False, "steps": []}
    assert result.cache_observation is None
    usage = ledger.list_records("usage")
    assert len(usage) == 1
    assert usage[0]["physical_quota_bucket_id"] == "literary-fake-v1"


def test_local_semantic_rejection_is_visible_and_not_cached(tmp_path: Path) -> None:
    def reject(_row: Mapping[str, Any]) -> Mapping[str, Any]:
        raise ValueError("synthetic semantic rejection")

    role_id = "literary.b2.interaction"
    sender = _RoleResponseSender(role_id)
    backend, ledger = _backend(tmp_path, sender)
    adapter = LiterarySharedLlmAttemptAdapter(backend=backend)
    with pytest.raises(
        LiterarySharedLlmAdapterError,
        match="ValueError: synthetic semantic rejection",
    ):
        adapter.execute(
            preset=ROLE_PRESETS[role_id],
            api_source=_source(),
            capability=_capability(role_id),
            messages=[{"role": "user", "content": "fixture"}],
            response_schema=SCHEMA,
            schema_name="literary_b2_interaction",
            prompt_ref=_ref(f"{role_id}.prompt_v1", "b" * 64),
            response_schema_ref=_ref(f"{role_id}.schema_v1", SCHEMA_HASH),
            validator_ref=_ref(f"{role_id}.validator_v1", VALIDATOR_HASH),
            semantic_extension_ref={
                "id": f"{role_id}.authority_v1",
                "schema_version": "literary_semantic_authority_v1",
                "sha256": "f" * 64,
            },
            structured_output={
                "mode": "required",
                "schema_dialect": "json_schema_2020_12",
            },
            semantic_validator=reject,
            run_id="literary_reject_run",
            attempt_run_id="literary_reject_attempt",
            stage_id="literary_b2_interaction",
            logical_request_id="request_reject",
        )
    assert sender.calls == 1
    assert len(ledger.list_records("usage")) == 1
    assert ledger.list_records("cache") == []


def test_schema_drift_and_raw_cache_enablement_fail_before_provider(
    tmp_path: Path,
) -> None:
    role_id = "literary.b1.entity_inventory"
    sender = _RoleResponseSender(role_id)
    backend, _ledger = _backend(tmp_path, sender)
    adapter = LiterarySharedLlmAttemptAdapter(backend=backend)
    common = {
        "preset": ROLE_PRESETS[role_id],
        "api_source": _source(),
        "capability": _capability(role_id),
        "messages": [{"role": "user", "content": "fixture"}],
        "response_schema": SCHEMA,
        "schema_name": "literary_b1_entity_inventory",
        "prompt_ref": _ref(f"{role_id}.prompt_v1", "b" * 64),
        "validator_ref": _ref(f"{role_id}.validator_v1", VALIDATOR_HASH),
        "semantic_extension_ref": {
            "id": f"{role_id}.authority_v1",
            "schema_version": "literary_semantic_authority_v1",
            "sha256": "f" * 64,
        },
        "structured_output": {
            "mode": "required",
            "schema_dialect": "json_schema_2020_12",
        },
        "semantic_validator": lambda row: dict(row),
        "run_id": "literary_guard_run",
        "attempt_run_id": "literary_guard_attempt",
        "stage_id": "literary_b1_entity_inventory",
        "logical_request_id": "request_guard",
    }
    with pytest.raises(LiterarySharedLlmAdapterError, match="schema reference"):
        adapter.execute(
            **common,
            response_schema_ref=_ref(f"{role_id}.schema_v1", "0" * 64),
        )
    with pytest.raises(LiterarySharedLlmAdapterError, match="cache remains disabled"):
        adapter.execute(
            **common,
            response_schema_ref=_ref(f"{role_id}.schema_v1", SCHEMA_HASH),
            allow_response_cache_write=True,
        )
    assert sender.calls == 0


def test_google_renderer_keeps_schema_and_thinking_policy_explicit() -> None:
    role_id = "literary.b2.frame"
    capability = _capability(role_id)
    capability.update(
        {
            "capability_kind": "native_structured_output",
            "protocol": "google_genai_generate_content",
        }
    )
    body = render_literary_request_body(
        preset=ROLE_PRESETS[role_id],
        protocol="google_genai_generate_content",
        capability=capability,
        messages=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": "user"},
        ],
        response_schema=SCHEMA,
        schema_name="literary_b2_frame",
        structured_output={
            "mode": "required",
            "schema_dialect": "json_schema_2020_12",
        },
    )
    assert body["systemInstruction"] == {"parts": [{"text": "system"}]}
    assert body["generationConfig"]["responseJsonSchema"] == SCHEMA
    assert body["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 0}


def test_adapter_source_contains_no_provider_sdk_or_credential_loader() -> None:
    source = Path(
        "pipeline/literary/shared_llm_adapter_v1.py"
    ).read_text(encoding="utf-8")
    forbidden = ("from openai", "import openai", "google.genai", "urlopen", "api_key")
    assert not any(marker in source for marker in forbidden)
    assert "execute_one_attempt" in source


def test_prompt_json_uses_no_provider_response_format_and_adds_one_instruction() -> None:
    role_id = "literary.b2.interaction"
    body = render_literary_request_body(
        preset=ROLE_PRESETS[role_id],
        protocol="openai_chat_completions",
        capability={"capability_kind": "text_generation"},
        messages=[
            {"role": "system", "content": "Semantic task contract."},
            {"role": "user", "content": "Fixture."},
        ],
        response_schema=SCHEMA,
        schema_name="literary_b2_interaction",
        structured_output={"mode": "disabled", "schema_dialect": None},
        output_envelope={
            "mode": "prompt_json",
            "schema_dialect": None,
            "instruction_id": "literary.json_only_output_instruction",
            "instruction_revision": "v2",
        },
        base_url="https://modelapi.vn/v1",
    )
    assert "response_format" not in body
    instructions = [
        row["content"]
        for row in body["messages"]
        if row["role"] == "system" and row["content"].startswith("OUTPUT ENVELOPE")
    ]
    assert instructions == [build_prompt_json_instruction(SCHEMA)]
    assert body["messages"][-1] == {"role": "user", "content": "Fixture."}


def test_json_object_is_only_a_syntax_aid_and_keeps_local_instruction() -> None:
    role_id = "literary.b2.frame"
    body = render_literary_request_body(
        preset=ROLE_PRESETS[role_id],
        protocol="openai_chat_completions",
        capability={"capability_kind": "json_object"},
        messages=[{"role": "user", "content": "Fixture."}],
        response_schema=SCHEMA,
        schema_name="literary_b2_frame",
        structured_output={
            "mode": "prompt_validated",
            "schema_dialect": "json_schema_2020_12",
        },
        output_envelope={
            "mode": "json_object",
            "schema_dialect": "json_schema_2020_12",
            "instruction_id": "literary.json_only_output_instruction",
            "instruction_revision": "v2",
        },
        base_url="https://third-party.invalid/v1",
    )
    assert body["response_format"] == {"type": "json_object"}
    assert "json_schema" not in body["response_format"]
    assert any(
        row["content"].startswith("OUTPUT ENVELOPE")
        for row in body["messages"]
    )


def test_explicit_native_schema_rejects_third_party_and_lookalike_sources() -> None:
    role_id = "literary.b1.entity_inventory"
    common = {
        "preset": ROLE_PRESETS[role_id],
        "protocol": "openai_chat_completions",
        "capability": {"capability_kind": "native_structured_output"},
        "messages": [{"role": "user", "content": "Fixture."}],
        "response_schema": SCHEMA,
        "schema_name": "literary_b1_entity_inventory",
        "structured_output": {
            "mode": "required",
            "schema_dialect": "json_schema_2020_12",
        },
        "output_envelope": {
            "mode": "native_schema",
            "schema_dialect": "json_schema_2020_12",
            "instruction_id": None,
            "instruction_revision": None,
        },
    }
    for base_url in (
        "https://modelapi.vn/v1",
        "https://api.openai.com.example/v1",
        "https://api.openai.com:443/v1",
        "https://user@api.openai.com/v1",
        "https://api.openai.com/v1?mode=native",
        "https://api.openai.com/v1\n",
    ):
        with pytest.raises(
            LiterarySharedLlmAdapterError, match="direct official sources"
        ):
            render_literary_request_body(**common, base_url=base_url)

    body = render_literary_request_body(
        **common, base_url="https://api.openai.com/v1"
    )
    assert body["response_format"]["type"] == "json_schema"
