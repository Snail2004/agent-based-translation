from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import pytest

from pipeline.eval.contracts_v1 import ContractValidationError
from pipeline.eval.live_pilot_capability_probe_v1 import (
    SHARED_CAPABILITY_PROBE_REVISION,
    build_evaluation_capability_probe_plan_v1,
    build_evaluation_json_object_capability_probe_plan_v1,
    execute_evaluation_capability_probe_once_v1,
    validate_evaluation_capability_payload_v1,
)
from pipeline.eval.llm_profiles_v1 import (
    PJ_JUDGE_ROLE_ID,
    SF_BT_BACK_TRANSLATOR_ROLE_ID,
    SF_BT_SEMANTIC_JUDGE_ROLE_ID,
    evaluation_role_contract_v1,
)
from pipeline.llm_backend import (
    ContentAddressedArtifactStore,
    ContractValidationError as SharedContractValidationError,
    MappingCredentialProvider,
    PhysicalQuotaScheduler,
    RawTransportResponse,
    SharedLlmAttemptLedger,
    SharedLlmCapabilityProbe,
    TransportCallError,
    canonical_json,
    credential_commitment,
)


SECRET = "evaluation-capability-probe-secret"
MODEL = "gemini-fixture"


def _source(**updates) -> dict:
    row = {
        "schema_version": "api_source_v1",
        "source_id": "gemini_free_row_fixture_v1",
        "source_revision": "gemini_free_row_fixture_revision_v1",
        "source_class": "remote_api",
        "adapter_id": "google_genai_rest_v1",
        "protocol": "google_genai_generate_content",
        "route_id": "models_generate_content",
        "endpoint_class": "remote",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "credential_ref": "credential.gemini_free_row_fixture_v1",
        "credential_commitment": credential_commitment(SECRET),
        "physical_quota_bucket_id": "gemini-free-row-fixture-v1",
        "enabled": True,
    }
    row.update(updates)
    return row


def _binding(**updates) -> dict:
    row = {
        "shared_core_revision": SHARED_CAPABILITY_PROBE_REVISION,
        "consumer_revision": "1" * 40,
        "consumer_implementation_sha256": "2" * 64,
    }
    row.update(updates)
    return row


def _ckey_source(**updates) -> dict:
    row = {
        "schema_version": "api_source_v1",
        "source_id": "ckey_fixture_v1",
        "source_revision": "ckey_fixture_revision_v1",
        "source_class": "remote_api",
        "adapter_id": "openai_compatible_chat_v1",
        "protocol": "openai_chat_completions",
        "route_id": "chat_completions",
        "endpoint_class": "remote",
        "base_url": "https://proxy.example.test/v1",
        "credential_ref": "credential.ckey_fixture_v1",
        "credential_commitment": credential_commitment(SECRET),
        "physical_quota_bucket_id": "ckey-fixture-v1",
        "enabled": True,
    }
    row.update(updates)
    return row


def _plan(role_id: str, **updates):
    arguments = {
        "role_id": role_id,
        "source": _source(),
        "requested_model_id": MODEL,
        "accepted_observed_model_ids": [MODEL],
        "probe_run_id": f"evaluation_probe_{role_id.replace('.', '_')}",
        "issued_at_utc": "2026-07-20T00:00:00Z",
        "implementation_binding": _binding(),
    }
    arguments.update(updates)
    return build_evaluation_capability_probe_plan_v1(**arguments)


def _valid_payload(role_id: str) -> dict:
    return {
        SF_BT_BACK_TRANSLATOR_ROLE_ID: {
            "back_translation": "The system stores three rows."
        },
        SF_BT_SEMANTIC_JUDGE_ROLE_ID: {
            "score": 100,
            "flags": [],
            "note": "The synthetic passages preserve the same meaning.",
        },
        PJ_JUDGE_ROLE_ID: {
            "overall_verdict": "tie",
            "style_verdict": "tie",
            "tags": [],
            "note": "no meaningful difference",
        },
    }[role_id]


class _Clock:
    def __init__(self) -> None:
        start = datetime(2026, 7, 20, tzinfo=timezone.utc)
        self.values = [start + timedelta(seconds=index) for index in range(8)]

    def __call__(self) -> datetime:
        return self.values.pop(0)


class _GoogleSender:
    def __init__(
        self,
        *,
        role_id: str,
        observed_model: str = MODEL,
        content_payload: dict | None = None,
    ) -> None:
        self.role_id = role_id
        self.observed_model = observed_model
        self.content_payload = content_payload
        self.calls = 0

    def send(self, request):
        self.calls += 1
        assert request.url.endswith(f"/models/{MODEL}:generateContent")
        assert request.headers_for_transport()["x-goog-api-key"] == SECRET
        payload = canonical_json(
            {
                "responseId": "evaluation-probe-request-1",
                "modelVersion": self.observed_model,
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {
                            "parts": [
                                {
                                    "text": json.dumps(
                                        self.content_payload
                                        if self.content_payload is not None
                                        else _valid_payload(self.role_id),
                                        ensure_ascii=False,
                                    )
                                }
                            ]
                        },
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 90,
                    "candidatesTokenCount": 25,
                    "totalTokenCount": 115,
                },
            }
        ).encode("utf-8")
        return RawTransportResponse(
            status_code=200,
            headers={},
            body=payload,
            request_id="evaluation-probe-request-1",
        )


class _FailureSender:
    def __init__(self) -> None:
        self.calls = 0

    def send(self, _request):
        self.calls += 1
        raise TransportCallError(
            code="http_503",
            status_code=503,
            safe_message="provider returned HTTP 503",
        )


class _OpenAiCompatibleSender:
    def __init__(self, *, role_id: str, payload: dict | None = None) -> None:
        self.role_id = role_id
        self.payload = payload
        self.calls = 0

    def send(self, request):
        self.calls += 1
        assert request.url == "https://proxy.example.test/v1/chat/completions"
        assert request.headers_for_transport()["authorization"] == f"Bearer {SECRET}"
        body = json.loads(request.body.decode("utf-8"))
        assert body["response_format"] == {"type": "json_object"}
        assert body["max_completion_tokens"] == 512
        assert "max_tokens" not in body
        assert "json_schema" not in json.dumps(body)
        response = {
            "id": "ckey-probe-response-1",
            "model": MODEL,
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": json.dumps(
                            self.payload
                            if self.payload is not None
                            else _valid_payload(self.role_id),
                            ensure_ascii=False,
                        )
                    },
                }
            ],
            "usage": {
                "prompt_tokens": 80,
                "completion_tokens": 20,
                "total_tokens": 100,
            },
        }
        return RawTransportResponse(
            status_code=200,
            headers={},
            body=canonical_json(response).encode("utf-8"),
            request_id="ckey-probe-response-1",
        )


def _probe(tmp_path, sender, binding: dict | None = None):
    ledger = SharedLlmAttemptLedger(tmp_path / "ledger.jsonl")
    probe = SharedLlmCapabilityProbe(
        credential_provider=MappingCredentialProvider(
            {_source()["credential_ref"]: SECRET}
        ),
        scheduler=PhysicalQuotaScheduler(tmp_path / "leases"),
        ledger=ledger,
        artifact_store=ContentAddressedArtifactStore(tmp_path / "artifacts"),
        sender=sender,
        implementation_binding=binding or _binding(),
        clock=_Clock(),
    )
    return probe, ledger


def _ckey_probe(tmp_path, sender):
    ledger = SharedLlmAttemptLedger(tmp_path / "ledger.jsonl")
    probe = SharedLlmCapabilityProbe(
        credential_provider=MappingCredentialProvider(
            {_ckey_source()["credential_ref"]: SECRET}
        ),
        scheduler=PhysicalQuotaScheduler(tmp_path / "leases"),
        ledger=ledger,
        artifact_store=ContentAddressedArtifactStore(tmp_path / "artifacts"),
        sender=sender,
        implementation_binding=_binding(),
        clock=_Clock(),
    )
    return probe, ledger


@pytest.mark.parametrize(
    "role_id",
    [
        SF_BT_BACK_TRANSLATOR_ROLE_ID,
        SF_BT_SEMANTIC_JUDGE_ROLE_ID,
        PJ_JUDGE_ROLE_ID,
    ],
)
def test_each_evaluation_role_qualifies_once_with_its_existing_contract(
    tmp_path, role_id: str
) -> None:
    plan = _plan(role_id)
    original = deepcopy(plan.request_body)
    sender = _GoogleSender(role_id=role_id)
    probe, ledger = _probe(tmp_path, sender)
    result = execute_evaluation_capability_probe_once_v1(probe=probe, plan=plan)

    assert result["status"] == "qualified"
    assert result["provider_called"] is True
    assert sender.calls == 1
    assert plan.request_body == original
    assert result["capability_evidence"]["local_validator_id"] == (
        evaluation_role_contract_v1(role_id)["validator"]["id"]
    )
    assert result["capability_evidence"]["verdict"] == "qualified"
    assert ledger.count("capability_probe_seal") == 1
    assert ledger.count("capability_evidence") == 1


def test_google_request_groups_exact_schema_with_one_synthetic_prompt() -> None:
    plan = _plan(PJ_JUDGE_ROLE_ID)
    assert "model" not in plan.request_body
    assert plan.request_body["generationConfig"]["responseMimeType"] == (
        "application/json"
    )
    assert plan.request_body["generationConfig"]["responseJsonSchema"] == (
        plan.response_schema
    )
    assert "responseSchema" not in plan.request_body["generationConfig"]
    assert plan.request_body["generationConfig"]["thinkingConfig"] == {
        "thinkingBudget": 0
    }
    assert plan.seal["limits"]["max_completion_tokens"] == (
        plan.request_body["generationConfig"]["maxOutputTokens"]
    )
    assert len(plan.request_body["contents"]) == 1
    assert "at most 25 English words" in (
        plan.request_body["contents"][0]["parts"][0]["text"]
    )
    encoded = canonical_json(plan.request_body).casefold()
    for forbidden in (
        "gold",
        "oracle",
        "human_reference",
        "reference_translation",
        "result_callback",
    ):
        assert forbidden not in encoded


def test_third_party_json_object_probe_uses_no_native_schema_and_local_validation(
    tmp_path,
) -> None:
    role_id = SF_BT_SEMANTIC_JUDGE_ROLE_ID
    plan = build_evaluation_json_object_capability_probe_plan_v1(
        role_id=role_id,
        source=_ckey_source(),
        requested_model_id=MODEL,
        accepted_observed_model_ids=[MODEL],
        probe_run_id="evaluation_ckey_json_object_probe_v1",
        issued_at_utc="2026-07-20T00:00:00Z",
        implementation_binding=_binding(),
    )
    assert plan.seal["capability_intent"]["capability_kind"] == "json_object"
    assert plan.seal["limits"]["max_prompt_tokens"] == 8_192
    assert plan.seal["limits"]["max_completion_tokens"] == 2_048
    assert plan.seal["limits"]["max_total_tokens"] == 10_240
    assert plan.request_body["max_completion_tokens"] == 512
    assert plan.request_body["response_format"] == {"type": "json_object"}
    assert "json_schema" not in canonical_json(plan.request_body)
    prompt = plan.request_body["messages"][0]["content"]
    assert "Return JSON only" in prompt
    assert "Do not use Markdown code fences" in prompt
    assert '{"score":100,"flags":[],"note":"one short English sentence"}' in prompt
    assert "score must be one of 0, 25, 50, 75, or 100" in prompt

    sender = _OpenAiCompatibleSender(role_id=role_id)
    probe, ledger = _ckey_probe(tmp_path, sender)
    result = execute_evaluation_capability_probe_once_v1(probe=probe, plan=plan)
    assert result["status"] == "qualified"
    assert result["capability_evidence"]["capability_kind"] == "json_object"
    assert result["capability_evidence"]["local_validator_id"] == (
        evaluation_role_contract_v1(role_id)["validator"]["id"]
    )
    assert sender.calls == 1
    assert ledger.count("capability_evidence") == 1


def test_official_probe_retains_the_original_prompt_certification_cap() -> None:
    plan = _plan(SF_BT_SEMANTIC_JUDGE_ROLE_ID)
    assert plan.seal["limits"]["max_prompt_tokens"] == 4_096
    assert plan.seal["limits"]["max_completion_tokens"] == 512
    assert plan.seal["limits"]["max_total_tokens"] == 4_608


@pytest.mark.parametrize(
    ("role_id", "required_shape"),
    [
        (
            SF_BT_BACK_TRANSLATOR_ROLE_ID,
            '{"back_translation":"English translation"}',
        ),
        (
            SF_BT_SEMANTIC_JUDGE_ROLE_ID,
            '{"score":100,"flags":[],"note":"one short English sentence"}',
        ),
        (
            PJ_JUDGE_ROLE_ID,
            '{"overall_verdict":"tie","style_verdict":"tie","tags":[],'
            '"note":"one short English sentence"}',
        ),
    ],
)
def test_third_party_json_object_probe_prompt_declares_exact_shape(
    role_id: str,
    required_shape: str,
) -> None:
    plan = build_evaluation_json_object_capability_probe_plan_v1(
        role_id=role_id,
        source=_ckey_source(),
        requested_model_id=MODEL,
        accepted_observed_model_ids=[MODEL],
        probe_run_id=f"evaluation_ckey_shape_{role_id.replace('.', '_')}",
        issued_at_utc="2026-07-20T00:00:00Z",
        implementation_binding=_binding(),
    )
    prompt = plan.request_body["messages"][0]["content"]
    assert "Return JSON only" in prompt
    assert required_shape in prompt
    assert "response schema" not in prompt
    assert "gold" not in prompt.casefold()
    assert "oracle" not in prompt.casefold()


def test_third_party_json_object_probe_rejects_semantically_invalid_json(
    tmp_path,
) -> None:
    role_id = SF_BT_SEMANTIC_JUDGE_ROLE_ID
    plan = build_evaluation_json_object_capability_probe_plan_v1(
        role_id=role_id,
        source=_ckey_source(),
        requested_model_id=MODEL,
        accepted_observed_model_ids=[MODEL],
        probe_run_id="evaluation_ckey_invalid_semantics_v1",
        issued_at_utc="2026-07-20T00:00:00Z",
        implementation_binding=_binding(),
    )
    sender = _OpenAiCompatibleSender(
        role_id=role_id,
        payload={"score": 58, "flags": [], "note": "invalid band"},
    )
    probe, _ = _ckey_probe(tmp_path, sender)
    result = execute_evaluation_capability_probe_once_v1(probe=probe, plan=plan)
    assert result["status"] == "failed"
    assert result["receipt"]["failure"]["code"] == "local_validator_rejected"
    assert sender.calls == 1


def test_third_party_google_compatible_probe_uses_json_mime_without_schema(
    tmp_path,
) -> None:
    role_id = SF_BT_BACK_TRANSLATOR_ROLE_ID
    source = _ckey_source(
        adapter_id="google_genai_rest_v1",
        protocol="google_genai_generate_content",
        route_id="models_generate_content",
        base_url="https://proxy.example.test/v1beta",
    )
    plan = build_evaluation_json_object_capability_probe_plan_v1(
        role_id=role_id,
        source=source,
        requested_model_id=MODEL,
        accepted_observed_model_ids=[MODEL],
        probe_run_id="evaluation_ckey_google_json_probe_v1",
        issued_at_utc="2026-07-20T00:00:00Z",
        implementation_binding=_binding(),
    )
    generation = plan.request_body["generationConfig"]
    assert generation["maxOutputTokens"] == 512
    assert generation["responseMimeType"] == "application/json"
    assert "responseJsonSchema" not in generation
    assert "responseSchema" not in generation

    sender = _GoogleSender(role_id=role_id)
    probe, ledger = _ckey_probe(tmp_path, sender)
    result = execute_evaluation_capability_probe_once_v1(probe=probe, plan=plan)
    assert result["status"] == "qualified"
    assert result["capability_evidence"]["capability_kind"] == "json_object"
    assert ledger.count("capability_evidence") == 1


def test_native_and_third_party_probe_authorities_cannot_be_relabelled() -> None:
    with pytest.raises(ContractValidationError, match="direct official"):
        _plan(SF_BT_SEMANTIC_JUDGE_ROLE_ID, source=_ckey_source())
    with pytest.raises(
        ContractValidationError, match="non-official OpenAI-compatible"
    ):
        build_evaluation_json_object_capability_probe_plan_v1(
            role_id=SF_BT_SEMANTIC_JUDGE_ROLE_ID,
            source=_source(),
            requested_model_id=MODEL,
            accepted_observed_model_ids=[MODEL],
            probe_run_id="evaluation_official_relabel_probe_v1",
            issued_at_utc="2026-07-20T00:00:00Z",
            implementation_binding=_binding(),
        )


@pytest.mark.parametrize(
    "protocol",
    ["openai_chat_completions", "openai_responses"],
)
def test_probe_request_shape_tracks_the_declared_protocol(protocol: str) -> None:
    source = _source()
    if protocol == "openai_chat_completions":
        source.update(
            {
                "source_id": "openai_fixture_v1",
                "source_revision": "openai_fixture_revision_v1",
                "adapter_id": "openai_python_v1",
                "protocol": protocol,
                "route_id": "chat_completions_create",
                "base_url": "https://api.openai.com/v1",
                "credential_ref": "credential.openai_fixture_v1",
                "physical_quota_bucket_id": "openai-fixture-v1",
            }
        )
    elif protocol == "openai_responses":
        source.update(
            {
                "source_id": "responses_fixture_v1",
                "source_revision": "responses_fixture_revision_v1",
                "adapter_id": "openai_responses_v1",
                "protocol": protocol,
                "route_id": "responses_create",
                "base_url": "https://api.openai.com/v1",
                "credential_ref": "credential.responses_fixture_v1",
                "physical_quota_bucket_id": "responses-fixture-v1",
            }
        )
    plan = _plan(SF_BT_BACK_TRANSLATOR_ROLE_ID, source=source)
    if protocol == "openai_chat_completions":
        assert plan.request_body["model"] == MODEL
        assert plan.request_body["response_format"]["type"] == "json_schema"
    elif protocol == "openai_responses":
        assert plan.request_body["model"] == MODEL
        assert plan.request_body["text"]["format"]["type"] == "json_schema"


@pytest.mark.parametrize(
    "source",
    [
        _source(base_url="https://api.shopaikey.com"),
        _source(
            source_id="modelapi_proxy_v1",
            source_revision="modelapi_proxy_revision_v1",
            adapter_id="openai_python_v1",
            protocol="openai_chat_completions",
            route_id="chat_completions_create",
            base_url="https://modelapi.invalid/v1",
        ),
        _source(
            source_id="local_fixture_v1",
            source_revision="local_fixture_revision_v1",
            source_class="local_in_process",
            adapter_id="local_callback_v1",
            protocol="local_in_process",
            route_id="local_callback",
            endpoint_class="in_process",
            base_url=None,
            credential_ref=None,
            credential_commitment=None,
            physical_quota_bucket_id="local-fixture-v1",
        ),
    ],
)
def test_proxy_and_local_sources_cannot_claim_native_structured_output(
    source: dict,
) -> None:
    with pytest.raises(
        ContractValidationError, match="direct official Google or OpenAI"
    ):
        _plan(SF_BT_BACK_TRANSLATOR_ROLE_ID, source=source)


def test_source_model_schema_and_implementation_tampering_fail_closed(
    tmp_path,
) -> None:
    plan = _plan(SF_BT_SEMANTIC_JUDGE_ROLE_ID)
    sender = _GoogleSender(role_id=SF_BT_SEMANTIC_JUDGE_ROLE_ID)
    probe, ledger = _probe(tmp_path, sender)

    foreign_body = deepcopy(plan.request_body)
    foreign_body["generationConfig"]["responseJsonSchema"]["properties"][
        "score"
    ] = {"type": "number"}
    foreign_plan = replace(plan, request_body=foreign_body)
    with pytest.raises(SharedContractValidationError):
        execute_evaluation_capability_probe_once_v1(
            probe=probe, plan=foreign_plan
        )

    probe.implementation_binding = _binding(consumer_revision="3" * 40)
    with pytest.raises(SharedContractValidationError, match="implementation identity"):
        execute_evaluation_capability_probe_once_v1(probe=probe, plan=plan)
    probe.implementation_binding = _binding()

    tampered = deepcopy(plan.seal)
    tampered["source_binding"]["record"]["physical_quota_bucket_id"] = (
        "foreign-bucket"
    )
    tampered_plan = replace(plan, seal=tampered)
    with pytest.raises(SharedContractValidationError, match="source record hash mismatch"):
        execute_evaluation_capability_probe_once_v1(
            probe=probe, plan=tampered_plan
        )
    assert sender.calls == 0
    assert ledger.count("capability_probe_seal") == 0


def test_observed_model_mismatch_and_transport_failure_are_terminal_evidence(
    tmp_path,
) -> None:
    role_id = SF_BT_BACK_TRANSLATOR_ROLE_ID
    mismatch_plan = _plan(role_id, probe_run_id="evaluation_probe_model_mismatch")
    mismatch_sender = _GoogleSender(
        role_id=role_id, observed_model="foreign-model"
    )
    mismatch_probe, _ = _probe(tmp_path / "mismatch", mismatch_sender)
    mismatch = execute_evaluation_capability_probe_once_v1(
        probe=mismatch_probe, plan=mismatch_plan
    )
    assert mismatch["status"] == "failed"
    assert mismatch["receipt"]["failure"]["code"] == "observed_model_mismatch"
    assert mismatch_sender.calls == 1

    failed_plan = _plan(role_id, probe_run_id="evaluation_probe_http_failure")
    failure_sender = _FailureSender()
    failure_probe, _ = _probe(tmp_path / "failure", failure_sender)
    failed = execute_evaluation_capability_probe_once_v1(
        probe=failure_probe, plan=failed_plan
    )
    assert failed["status"] == "failed"
    assert failed["receipt"]["failure"]["code"] == "http_503"
    assert failed["receipt"]["cost_usd"] is None
    assert failure_sender.calls == 1


def test_duplicate_probe_is_rejected_without_a_second_provider_call(tmp_path) -> None:
    role_id = PJ_JUDGE_ROLE_ID
    plan = _plan(role_id)
    sender = _GoogleSender(role_id=role_id)
    probe, _ = _probe(tmp_path, sender)
    first = execute_evaluation_capability_probe_once_v1(probe=probe, plan=plan)
    assert first["status"] == "qualified"
    with pytest.raises(SharedContractValidationError):
        execute_evaluation_capability_probe_once_v1(probe=probe, plan=plan)
    assert sender.calls == 1


def test_semantically_invalid_json_becomes_failed_evidence(tmp_path) -> None:
    role_id = SF_BT_SEMANTIC_JUDGE_ROLE_ID
    plan = _plan(role_id, probe_run_id="evaluation_probe_invalid_semantics")
    sender = _GoogleSender(
        role_id=role_id,
        content_payload={"score": 90, "flags": [], "note": "invalid band"},
    )
    probe, _ = _probe(tmp_path, sender)
    result = execute_evaluation_capability_probe_once_v1(probe=probe, plan=plan)
    assert result["status"] == "failed"
    assert result["receipt"]["failure"]["code"] == "local_validator_rejected"
    assert result["capability_evidence"]["verdict"] == "failed"
    assert sender.calls == 1


@pytest.mark.parametrize(
    "role_id,bad_payload",
    [
        (SF_BT_BACK_TRANSLATOR_ROLE_ID, {"back_translation": ""}),
        (
            SF_BT_SEMANTIC_JUDGE_ROLE_ID,
            {"score": 90, "flags": [], "note": "invalid band"},
        ),
        (
            PJ_JUDGE_ROLE_ID,
            {
                "overall_verdict": "candidate_3",
                "style_verdict": "tie",
                "tags": [],
                "note": "invalid candidate",
            },
        ),
    ],
)
def test_local_validator_rejects_payloads_outside_existing_semantics(
    role_id: str, bad_payload: dict
) -> None:
    with pytest.raises(ContractValidationError):
        validate_evaluation_capability_payload_v1(role_id, bad_payload)


def test_forbidden_authority_and_foreign_core_never_reach_a_probe() -> None:
    with pytest.raises(ContractValidationError, match="forbidden authority"):
        _plan(
            PJ_JUDGE_ROLE_ID,
            source=_source(source_id="evaluation-human-reference-source-v1"),
        )
    with pytest.raises(ContractValidationError, match="forbidden authority"):
        _plan(
            PJ_JUDGE_ROLE_ID,
            requested_model_id="result.callback.model",
            accepted_observed_model_ids=["result.callback.model"],
        )
    with pytest.raises(ContractValidationError, match="accepted shared capability"):
        _plan(
            PJ_JUDGE_ROLE_ID,
            implementation_binding=_binding(shared_core_revision="4" * 40),
        )


def test_plan_builder_is_immutable_and_has_no_hidden_model_default() -> None:
    source = _source()
    binding = _binding()
    source_before = deepcopy(source)
    binding_before = deepcopy(binding)
    plan = _plan(PJ_JUDGE_ROLE_ID, source=source, implementation_binding=binding)
    assert source == source_before
    assert binding == binding_before
    assert plan.seal["capability_intent"]["requested_model_id"] == MODEL
    with pytest.raises(ContractValidationError):
        _plan(PJ_JUDGE_ROLE_ID, requested_model_id="")
