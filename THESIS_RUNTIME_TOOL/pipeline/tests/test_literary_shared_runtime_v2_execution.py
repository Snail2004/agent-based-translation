from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from pipeline.llm_backend import (
    ApplicationResponseCache,
    ContentAddressedArtifactStore,
    ContractValidationError,
    MappingCredentialProvider,
    PhysicalQuotaScheduler,
    RawTransportResponse,
    SharedLlmAttemptLedger,
    SharedLlmBackend,
    canonical_json,
    canonical_sha256,
    credential_commitment,
)
from pipeline.literary.chapter_cycle_shared_runtime_v1 import (
    LiterarySharedRunnerBindingsV1,
    LiterarySharedRunnerError,
    capability_binding_key,
)
from pipeline.literary.model_ref_transport_v1 import bind_model_ref_validator_v1
from pipeline.literary.shared_llm_adapter_v1 import LiterarySharedLlmAttemptAdapter
from pipeline.literary.shared_llm_adapter_v1 import LiterarySharedLlmAdapterError
from pipeline.literary.shared_runtime_profile_v2 import (
    DEFAULT_PROFILE_V2_PATH,
    load_literary_shared_runtime_profile_v2,
)
from pipeline.literary.structured_output_policy_v1 import (
    project_transport_schema_v1,
)


SECRET = "synthetic-shared-runtime-v2-secret"
ROLE_ID = "literary.b1.entity_inventory"
SCHEMA = {
    "type": "object",
    "properties": {
        "role": {"type": "string", "minLength": 1},
        "support_block_ids": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "minItems": 1,
            "uniqueItems": True,
        },
    },
    "required": ["role", "support_block_ids"],
    "additionalProperties": False,
}
SCHEMA_HASH = canonical_sha256(SCHEMA)
TRANSPORT_SCHEMA, TRANSPORT_OMISSIONS = project_transport_schema_v1(SCHEMA)
TRANSPORT_SCHEMA_HASH = canonical_sha256(TRANSPORT_SCHEMA)
VALIDATOR_HASH = "d" * 64


class _Sender:
    def __init__(self, semantic_payload: dict[str, Any] | None = None) -> None:
        self.calls = 0
        self.request_body: dict[str, Any] | None = None
        self.semantic_payload = semantic_payload or {
            "role": ROLE_ID,
            "support_block_ids": ["b001"],
        }

    def send(self, request) -> RawTransportResponse:
        self.calls += 1
        self.request_body = json.loads(request.body.decode("utf-8"))
        payload = {
            "id": "fake-v2",
            "model": "gpt-5.4",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": canonical_json(self.semantic_payload)},
                }
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 4,
                "total_tokens": 14,
                "prompt_tokens_details": {"cached_tokens": 0},
                "completion_tokens_details": {"reasoning_tokens": 0},
            },
        }
        return RawTransportResponse(
            status_code=200,
            headers={"x-request-id": "fake-v2"},
            body=canonical_json(payload).encode("utf-8"),
            request_id="fake-v2",
        )


def _source() -> dict[str, Any]:
    return {
        "schema_version": "api_source_v1",
        "source_id": "openai_official_row2_v1",
        "source_revision": "openai_key2_literary_20260719_v1",
        "source_class": "remote_api",
        "adapter_id": "openai_python_v1",
        "protocol": "openai_chat_completions",
        "route_id": "chat_completions_create",
        "endpoint_class": "remote",
        "base_url": "https://api.openai.com/v1",
        "credential_ref": "credential.openai_row2",
        "credential_commitment": credential_commitment(SECRET),
        "physical_quota_bucket_id": "openai-row2",
        "enabled": True,
    }


def _capability() -> dict[str, Any]:
    return {
        "schema_version": "capability_evidence_v1",
        "capability_id": "literary.b1.entity_inventory.openai_native_v1",
        "capability_revision": "openai_row2_gpt54_v1",
        "source_id": "openai_official_row2_v1",
        "source_revision": "openai_key2_literary_20260719_v1",
        "adapter_id": "openai_python_v1",
        "protocol": "openai_chat_completions",
        "route_id": "chat_completions_create",
        "base_url": "https://api.openai.com/v1",
        "requested_model_id": "gpt-5.4",
        "observed_model_id": "gpt-5.4",
        "capability_kind": "native_structured_output",
        "schema_dialect": "openai_strict_json_schema_subset_v1",
        "schema_sha256": TRANSPORT_SCHEMA_HASH,
        "local_validator_id": "literary.b1.entity_inventory.validator.v1",
        "local_validator_sha256": VALIDATOR_HASH,
        "probe_id": "literary.b1.entity_inventory.openai_probe_v1",
        "evidence_sha256": "e" * 64,
        "observed_at_utc": "2026-07-20T00:00:00Z",
        "verdict": "qualified",
    }


def _runtime(tmp_path: Path, sender: _Sender) -> LiterarySharedRunnerBindingsV1:
    store = ContentAddressedArtifactStore(tmp_path / "objects")
    backend = SharedLlmBackend(
        credential_provider=MappingCredentialProvider(
            {"credential.openai_row2": SECRET}
        ),
        scheduler=PhysicalQuotaScheduler(tmp_path / "quota"),
        ledger=SharedLlmAttemptLedger(tmp_path / "attempts.sqlite3"),
        response_cache=ApplicationResponseCache(
            index_path=tmp_path / "cache.sqlite3", artifact_store=store
        ),
        sender=sender,
    )
    return LiterarySharedRunnerBindingsV1(
        adapter=LiterarySharedLlmAttemptAdapter(backend=backend),
        api_source=None,
        capabilities={capability_binding_key(ROLE_ID, SCHEMA): _capability()},
        run_id="literary_runtime_v2_run",
        attempt_run_id="literary_runtime_v2_attempt",
        structured_output=None,
        runtime_profile=load_literary_shared_runtime_profile_v2(),
        api_sources_by_alias={"openai_official_row2": _source()},
    )


def _validate_semantic(row: dict[str, Any]) -> dict[str, Any]:
    Draft202012Validator(SCHEMA).validate(row)
    return dict(row)


def test_v2_runtime_resolves_role_source_envelope_and_shared_attempt(
    tmp_path: Path,
) -> None:
    sender = _Sender()
    runtime = _runtime(tmp_path, sender)
    identity = runtime.identity_payload()
    assert identity["schema_version"] == "literary_shared_runner_identity_v2"
    assert identity["output_envelope_by_role"][ROLE_ID]["mode"] == (
        "native_schema"
    )
    result = runtime.execute_accepted_request(
        role_id=ROLE_ID,
        stage_id="b1_fixture",
        logical_request_id="b1_fixture_request",
        request={
            "messages": [
                {"role": "system", "content": "Return the fixture."},
                {"role": "user", "content": "Fixture."},
            ],
            "response_schema": SCHEMA,
            "request_fingerprint": "a" * 64,
        },
        schema_name="literary_b1_fixture",
        semantic_validator=_validate_semantic,
        validator_ref={
            "id": "literary.b1.entity_inventory.validator.v1",
            "revision": "v1",
            "sha256": VALIDATOR_HASH,
        },
        application_contract_id="literary.b1.fixture_apply_v1",
        application_contract_revision="v1",
        output_dir=tmp_path / "output",
    )
    assert sender.calls == 1
    assert sender.request_body is not None
    assert sender.request_body["response_format"]["type"] == "json_schema"
    wire_schema = sender.request_body["response_format"]["json_schema"]["schema"]
    assert wire_schema == TRANSPORT_SCHEMA
    assert TRANSPORT_SCHEMA_HASH != SCHEMA_HASH
    assert {row["keyword"] for row in TRANSPORT_OMISSIONS} == {
        "minItems",
        "minLength",
        "uniqueItems",
    }
    assert result.semantic_payload == {
        "role": ROLE_ID,
        "support_block_ids": ["b001"],
    }


def test_v2_runtime_requires_model_ref_bound_validator_before_resolving_refs(
    tmp_path: Path,
) -> None:
    sender = _Sender({"role": "E1", "support_block_ids": ["b001"]})
    runtime = _runtime(tmp_path, sender)
    request = {
        "messages": [
            {
                "role": "user",
                "content": canonical_json(
                    {"role": ROLE_ID, "support_block_ids": ["b001"]}
                ),
            }
        ],
        "response_schema": SCHEMA,
        "request_fingerprint": "f" * 64,
    }
    validator_ref = {
        "id": "literary.b1.entity_inventory.validator.v1",
        "revision": "v1",
        "sha256": VALIDATOR_HASH,
    }
    with pytest.raises(ContractValidationError, match="validator binding"):
        runtime.execute_accepted_request(
            role_id=ROLE_ID,
            stage_id="b1_unbound_local_ref_fixture",
            logical_request_id="b1_unbound_local_ref_fixture_request",
            request=request,
            schema_name="literary_b1_local_ref_fixture",
            semantic_validator=_validate_semantic,
            validator_ref=validator_ref,
            application_contract_id="literary.b1.fixture_apply_v1",
            application_contract_revision="v1",
            output_dir=tmp_path / "unbound_local_ref_output",
            model_reference_mode="classified_request_local_v1",
            model_reference_fields={"entity": ("role",)},
        )
    assert sender.calls == 0

    bound_ref = bind_model_ref_validator_v1(validator_ref)
    bound_capability = {
        **_capability(),
        "local_validator_id": bound_ref["id"],
        "local_validator_sha256": bound_ref["sha256"],
    }
    runtime = replace(
        runtime,
        capabilities={capability_binding_key(ROLE_ID, SCHEMA): bound_capability},
    )
    result = runtime.execute_accepted_request(
        role_id=ROLE_ID,
        stage_id="b1_local_ref_fixture",
        logical_request_id="b1_local_ref_fixture_request",
        request=request,
        schema_name="literary_b1_local_ref_fixture",
        semantic_validator=_validate_semantic,
        validator_ref=validator_ref,
        application_contract_id="literary.b1.fixture_apply_v1",
        application_contract_revision="v1",
        output_dir=tmp_path / "local_ref_output",
        model_reference_mode="classified_request_local_v1",
        model_reference_fields={"entity": ("role",)},
    )

    assert sender.calls == 1
    assert sender.request_body is not None
    model_messages = sender.request_body["messages"]
    assert model_messages[0]["role"] == "system"
    assert "transport handles" in model_messages[0]["content"]
    assert json.loads(model_messages[1]["content"])["role"] == "E1"
    assert ROLE_ID not in canonical_json(model_messages)
    assert result.semantic_payload == {
        "role": ROLE_ID,
        "support_block_ids": ["b001"],
    }


def test_v2_runtime_keeps_omitted_transport_constraints_local_authority(
    tmp_path: Path,
) -> None:
    sender = _Sender(
        {"role": ROLE_ID, "support_block_ids": ["b001", "b001"]}
    )
    runtime = _runtime(tmp_path, sender)
    output = tmp_path / "rejected_output"
    with pytest.raises(
        LiterarySharedLlmAdapterError, match="ValidationError.*non-unique"
    ):
        runtime.execute_accepted_request(
            role_id=ROLE_ID,
            stage_id="b1_duplicate_fixture",
            logical_request_id="b1_duplicate_fixture_request",
            request={
                "messages": [{"role": "user", "content": "Fixture."}],
                "response_schema": SCHEMA,
                "request_fingerprint": "b" * 64,
            },
            schema_name="literary_b1_duplicate_fixture",
            semantic_validator=_validate_semantic,
            validator_ref={
                "id": "literary.b1.entity_inventory.validator.v1",
                "revision": "v1",
                "sha256": VALIDATOR_HASH,
            },
            application_contract_id="literary.b1.fixture_apply_v1",
            application_contract_revision="v1",
            output_dir=output,
        )
    assert sender.calls == 1
    diagnostic = json.loads(
        (output / "semantic_rejection.json").read_text(encoding="utf-8")
    )
    assert diagnostic["semantic_status"] == "rejected"
    assert diagnostic["validator_error"]["error_type"] == "ValidationError"
    assert "non-unique" in diagnostic["validator_error"]["message"]
    assert diagnostic["semantic_payload"]["sha256"] == canonical_sha256(
        {"role": ROLE_ID, "support_block_ids": ["b001", "b001"]}
    )
    assert '"support_block_ids":["b001","b001"]' in diagnostic[
        "semantic_payload"
    ]["excerpt"]
    assert diagnostic["semantic_payload"]["excerpt_truncated"] is False
    assert diagnostic["semantic_authority_granted"] is False
    assert diagnostic["application_response_cache"] == "disabled"
    assert diagnostic["accepted_application_receipt_written"] is False
    assert not (output / "shared_attempt_receipt.json").exists()


def test_semantic_rejection_excerpt_is_bounded(tmp_path: Path) -> None:
    semantic_payload = {
        "role": "x" * 20_000,
        "support_block_ids": ["b001", "b001"],
    }
    sender = _Sender(semantic_payload)
    runtime = _runtime(tmp_path, sender)
    output = tmp_path / "bounded_rejection"

    with pytest.raises(LiterarySharedLlmAdapterError, match="non-unique"):
        runtime.execute_accepted_request(
            role_id=ROLE_ID,
            stage_id="b1_bounded_rejection_fixture",
            logical_request_id="b1_bounded_rejection_fixture_request",
            request={
                "messages": [{"role": "user", "content": "Fixture."}],
                "response_schema": SCHEMA,
                "request_fingerprint": "c" * 64,
            },
            schema_name="literary_b1_bounded_rejection_fixture",
            semantic_validator=_validate_semantic,
            validator_ref={
                "id": "literary.b1.entity_inventory.validator.v1",
                "revision": "v1",
                "sha256": VALIDATOR_HASH,
            },
            application_contract_id="literary.b1.fixture_apply_v1",
            application_contract_revision="v1",
            output_dir=output,
        )

    diagnostic = json.loads(
        (output / "semantic_rejection.json").read_text(encoding="utf-8")
    )
    assert diagnostic["semantic_payload"]["sha256"] == canonical_sha256(
        semantic_payload
    )
    assert diagnostic["semantic_payload"]["excerpt_utf8_bytes"] == 16_384
    assert diagnostic["semantic_payload"]["excerpt_truncated"] is True


def test_v2_runtime_rejects_capability_bound_to_canonical_not_wire_schema(
    tmp_path: Path,
) -> None:
    sender = _Sender()
    runtime = _runtime(tmp_path, sender)
    wrong_capability = {**_capability(), "schema_sha256": SCHEMA_HASH}
    runtime = replace(
        runtime,
        capabilities={capability_binding_key(ROLE_ID, SCHEMA): wrong_capability},
    )
    with pytest.raises(LiterarySharedRunnerError, match="schema binding drifted"):
        runtime.execute_accepted_request(
            role_id=ROLE_ID,
            stage_id="b1_wrong_capability_fixture",
            logical_request_id="b1_wrong_capability_fixture_request",
            request={
                "messages": [{"role": "user", "content": "Fixture."}],
                "response_schema": SCHEMA,
                "request_fingerprint": "c" * 64,
            },
            schema_name="literary_b1_wrong_capability_fixture",
            semantic_validator=_validate_semantic,
            validator_ref={
                "id": "literary.b1.entity_inventory.validator.v1",
                "revision": "v1",
                "sha256": VALIDATOR_HASH,
            },
            application_contract_id="literary.b1.fixture_apply_v1",
            application_contract_revision="v1",
            output_dir=tmp_path / "wrong_capability_output",
        )
    assert sender.calls == 0


def test_v2_runtime_rejects_source_drift_before_provider(tmp_path: Path) -> None:
    sender = _Sender()
    runtime = _runtime(tmp_path, sender)
    bad_source = dict(_source())
    bad_source["base_url"] = "https://proxy.invalid/v1"
    runtime = replace(
        runtime, api_sources_by_alias={"openai_official_row2": bad_source}
    )
    with pytest.raises(LiterarySharedRunnerError, match="base_url"):
        runtime.identity_payload()
    assert sender.calls == 0


def test_third_party_prompt_json_executes_without_native_schema_parameter(
    tmp_path: Path,
) -> None:
    profile_payload = json.loads(
        DEFAULT_PROFILE_V2_PATH.read_text(encoding="utf-8")
    )
    profile_payload["profile_revision"] = "modelapi_prompt_json_fixture_v1"
    profile_payload["sources"].append(
        {
            "source_alias": "modelapi_prompt_json",
            "source_id": "modelapi_shared_v1",
            "source_revision": "modelapi_profile_v1",
            "authority_class": "third_party",
            "source_class": "remote_api",
            "adapter_id": "openai_python_v1",
            "protocol": "openai_chat_completions",
            "route_id": "chat_completions_create",
            "endpoint_class": "remote",
            "base_url": "https://modelapi.vn/v1",
            "credential_ref": "credential.modelapi_shared_v1",
            "physical_quota_bucket_id": "modelapi-shared-v1",
            "selection_mode": "host_resolved_exact_source",
            "fallback_enabled": False,
        }
    )
    target = next(
        row for row in profile_payload["roles"] if row["role_id"] == ROLE_ID
    )
    target["source_alias"] = "modelapi_prompt_json"
    target["preset_id"] = "literary.b1.entity_inventory.prompt_json_v1"
    target["preset_revision"] = "v1"
    target["output_envelope"] = {
        "mode": "prompt_json",
        "schema_dialect": None,
        "instruction_id": "literary.json_only_output_instruction",
        "instruction_revision": "v2",
    }
    profile_path = tmp_path / "prompt_profile.json"
    profile_path.write_text(json.dumps(profile_payload), encoding="utf-8")
    profile = load_literary_shared_runtime_profile_v2(profile_path)
    source = {
        "schema_version": "api_source_v1",
        "source_id": "modelapi_shared_v1",
        "source_revision": "modelapi_profile_v1",
        "source_class": "remote_api",
        "adapter_id": "openai_python_v1",
        "protocol": "openai_chat_completions",
        "route_id": "chat_completions_create",
        "endpoint_class": "remote",
        "base_url": "https://modelapi.vn/v1",
        "credential_ref": "credential.modelapi_shared_v1",
        "credential_commitment": credential_commitment(SECRET),
        "physical_quota_bucket_id": "modelapi-shared-v1",
        "enabled": True,
    }
    capability = {
        **_capability(),
        "capability_id": "literary.b1.entity_inventory.modelapi_text_v1",
        "capability_revision": "modelapi_text_v1",
        "source_id": "modelapi_shared_v1",
        "source_revision": "modelapi_profile_v1",
        "base_url": "https://modelapi.vn/v1",
        "capability_kind": "text_generation",
        "schema_dialect": None,
        "schema_sha256": None,
        "local_validator_id": None,
        "local_validator_sha256": None,
    }
    sender = _Sender()
    store = ContentAddressedArtifactStore(tmp_path / "prompt_objects")
    backend = SharedLlmBackend(
        credential_provider=MappingCredentialProvider(
            {"credential.modelapi_shared_v1": SECRET}
        ),
        scheduler=PhysicalQuotaScheduler(tmp_path / "prompt_quota"),
        ledger=SharedLlmAttemptLedger(tmp_path / "prompt_attempts.sqlite3"),
        response_cache=ApplicationResponseCache(
            index_path=tmp_path / "prompt_cache.sqlite3", artifact_store=store
        ),
        sender=sender,
    )
    runtime = LiterarySharedRunnerBindingsV1(
        adapter=LiterarySharedLlmAttemptAdapter(backend=backend),
        api_source=None,
        capabilities={capability_binding_key(ROLE_ID, SCHEMA): capability},
        run_id="literary_prompt_json_run",
        attempt_run_id="literary_prompt_json_attempt",
        structured_output=None,
        runtime_profile=profile,
        api_sources_by_alias={"modelapi_prompt_json": source},
    )
    result = runtime.execute_accepted_request(
        role_id=ROLE_ID,
        stage_id="b1_prompt_fixture",
        logical_request_id="b1_prompt_fixture_request",
        request={
            "messages": [{"role": "user", "content": "Fixture."}],
            "response_schema": SCHEMA,
            "request_fingerprint": "b" * 64,
        },
        schema_name="literary_b1_fixture",
        semantic_validator=_validate_semantic,
        validator_ref={
            "id": "literary.b1.entity_inventory.validator.v1",
            "revision": "v1",
            "sha256": VALIDATOR_HASH,
        },
        application_contract_id="literary.b1.fixture_apply_v1",
        application_contract_revision="v1",
        output_dir=tmp_path / "prompt_output",
    )
    assert result.semantic_payload == {
        "role": ROLE_ID,
        "support_block_ids": ["b001"],
    }
    assert sender.request_body is not None
    assert "response_format" not in sender.request_body
    assert any(
        row["content"].startswith("OUTPUT ENVELOPE")
        for row in sender.request_body["messages"]
    )
