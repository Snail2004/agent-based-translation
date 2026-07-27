from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

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
    canonical_json,
    canonical_sha256,
    credential_commitment,
    resolve_llm_run_seal,
)
from pipeline.literary import openai_identity_capability_probe_v1 as probe_module
from pipeline.literary.openai_identity_capability_probe_v1 import (
    CANONICAL_SCHEMA_SHA256,
    OMISSION_SET_SHA256,
    ROLE_ID,
    SHARED_CORE_REVISION,
    TRANSPORT_SCHEMA_SHA256,
    VALIDATOR_ID,
    VALIDATOR_SHA256,
    LiteraryOpenAiIdentityCapabilityProbeError,
    build_clean_implementation_binding_v1,
    build_literary_openai_identity_probe_plan_v1,
    execute_literary_openai_identity_probe_once_v1,
    implementation_sha256_v1,
    load_literary_openai_identity_probe_profile_v1,
    synthetic_identity_probe_index_v1,
    synthetic_identity_probe_response_v1,
    validate_literary_openai_identity_probe_payload_v1,
)
from pipeline.literary.shared_llm_profiles_v1 import (
    build_literary_pipeline_profile,
)
from pipeline.literary.shared_runtime_profile_v2 import (
    DEFAULT_PROFILE_V2_PATH,
    load_literary_shared_runtime_profile_v2,
)


SECRET = "literary-openai-identity-probe-fixture-secret"
CONSUMER_REVISION = "a" * 40
ISSUED_AT = "2026-07-20T00:00:00Z"


def _implementation_binding() -> dict[str, str]:
    return {
        "shared_core_revision": SHARED_CORE_REVISION,
        "consumer_revision": CONSUMER_REVISION,
        "consumer_implementation_sha256": implementation_sha256_v1(),
    }


def _plan(probe_run_id: str = "literary_identity_probe_test_001"):
    return build_literary_openai_identity_probe_plan_v1(
        probe_run_id=probe_run_id,
        credential_commitment_sha256=credential_commitment(SECRET),
        issued_at_utc=ISSUED_AT,
        implementation_binding=_implementation_binding(),
    )


def _provider_body(*, model: str = "gpt-5.4-2026-03-05", content=None) -> bytes:
    payload = content or canonical_json(synthetic_identity_probe_response_v1())
    return canonical_json(
        {
            "id": "identity-probe-request-1",
            "model": model,
            "choices": [
                {"finish_reason": "stop", "message": {"content": payload}}
            ],
            "usage": {
                "prompt_tokens": 1200,
                "completion_tokens": 120,
                "total_tokens": 1320,
                "prompt_tokens_details": {"cached_tokens": 0},
                "completion_tokens_details": {"reasoning_tokens": 0},
            },
        }
    ).encode("utf-8")


class _Clock:
    def __init__(self) -> None:
        start = datetime(2026, 7, 20, tzinfo=timezone.utc)
        self.values = [start + timedelta(seconds=index) for index in range(8)]

    def __call__(self) -> datetime:
        return self.values.pop(0)


class _Sender:
    def __init__(self, *, model="gpt-5.4-2026-03-05", content=None, fail=False):
        self.model = model
        self.content = content
        self.fail = fail
        self.calls = 0

    def send(self, request):
        self.calls += 1
        assert request.url == "https://api.openai.com/v1/chat/completions"
        assert request.headers_for_transport()["authorization"] == f"Bearer {SECRET}"
        assert request.source_id == "openai_official_row2_v1"
        if self.fail:
            raise TransportCallError(
                code="http_503",
                status_code=503,
                safe_message="provider returned HTTP 503",
            )
        return RawTransportResponse(
            status_code=200,
            headers={"x-request-id": "identity-probe-request-1"},
            body=_provider_body(model=self.model, content=self.content),
            request_id="identity-probe-request-1",
        )


def _probe(tmp_path: Path, sender: _Sender):
    ledger = SharedLlmAttemptLedger(tmp_path / "probe_ledger.sqlite3")
    probe = SharedLlmCapabilityProbe(
        credential_provider=MappingCredentialProvider(
            {"credential.openai_row2": SECRET}
        ),
        scheduler=PhysicalQuotaScheduler(tmp_path / "locks"),
        ledger=ledger,
        artifact_store=ContentAddressedArtifactStore(tmp_path / "artifacts"),
        sender=sender,
        implementation_binding=_implementation_binding(),
        clock=_Clock(),
    )
    return probe, ledger


def _normal_seal(plan, evidence):
    runtime = load_literary_shared_runtime_profile_v2(DEFAULT_PROFILE_V2_PATH)
    validator_ref = plan.validator_ref
    profile = build_literary_pipeline_profile(
        preset=runtime.role_presets[ROLE_ID],
        api_source=plan.source,
        capability=evidence,
        prompt_ref={
            "id": "literary.audit.identity_surface.probe_prompt",
            "revision": "v1",
            "sha256": canonical_sha256(plan.request_body["messages"]),
        },
        response_schema_ref={
            "id": "literary.audit.identity_surface.response_schema.transport",
            "revision": "openai_projection_v1",
            "sha256": TRANSPORT_SCHEMA_SHA256,
        },
        validator_ref=validator_ref,
        semantic_extension_ref={
            "id": "literary.audit.identity_surface.decision_v1",
            "schema_version": "literary_semantic_authority_v1",
            "sha256": "b" * 64,
        },
        structured_output=runtime.shared_structured_output_for(ROLE_ID),
        profile_id="literary_shared_llm_openai_official_v2",
        profile_revision="openai_row2_gpt54_native_v1",
    )
    return resolve_llm_run_seal(
        profile=profile,
        api_sources=[plan.source],
        capability_evidence=[evidence],
        role_id=ROLE_ID,
        run_id="literary_identity_probe_consumer",
        attempt_run_id="literary_identity_probe_consumer_attempt",
        stage_id="identity_surface",
        input_bindings=[
            {"name": "transport_request", "sha256": canonical_sha256(plan.request_body)}
        ],
    )


def test_profile_plan_and_fixture_are_closed_and_book_neutral() -> None:
    profile = load_literary_openai_identity_probe_profile_v1()
    plan = _plan()
    assert profile["capability_intent"]["role_id"] == ROLE_ID
    assert plan.source["source_id"] == "openai_official_row2_v1"
    assert plan.source["physical_quota_bucket_id"] == "openai-row2"
    assert canonical_sha256(plan.canonical_schema) == CANONICAL_SCHEMA_SHA256
    assert canonical_sha256(plan.response_schema) == TRANSPORT_SCHEMA_SHA256
    assert canonical_sha256(list(plan.omitted_transport_constraints)) == (
        OMISSION_SET_SHA256
    )
    assert len(plan.omitted_transport_constraints) == 8
    assert plan.validator_ref == {
        "id": VALIDATOR_ID,
        "revision": "v1",
        "sha256": VALIDATOR_SHA256,
    }
    response_format = plan.request_body["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["schema"] == plan.response_schema
    serialized = canonical_json(plan.request_body)
    assert "Hareton" not in serialized
    assert "Heathcliff" not in serialized
    assert SECRET not in serialized


def test_clean_binding_rejects_dirty_tracked_tree(monkeypatch) -> None:
    def dirty_git(_root, *args):
        if args[0] == "status":
            return " M THESIS_RUNTIME_TOOL/pipeline/literary/example.py"
        raise AssertionError(args)

    monkeypatch.setattr(probe_module, "_git_text", dirty_git)
    with pytest.raises(
        LiteraryOpenAiIdentityCapabilityProbeError, match="clean tracked"
    ):
        build_clean_implementation_binding_v1()


def test_exact_probe_qualifies_and_normal_resolver_accepts(tmp_path) -> None:
    plan = _plan()
    sender = _Sender()
    probe, ledger = _probe(tmp_path, sender)
    result = execute_literary_openai_identity_probe_once_v1(
        probe=probe, plan=plan
    )
    assert result["status"] == "qualified"
    assert result["capability_evidence"]["verdict"] == "qualified"
    assert sender.calls == 1
    assert ledger.count("capability_probe_seal") == 1
    assert ledger.count("capability_probe_receipt") == 1
    assert ledger.count("capability_evidence") == 1
    assert ledger.count("usage") == 0
    seal = _normal_seal(plan, result["capability_evidence"])
    assert seal["primary"]["capability"]["verdict"] == "qualified"


@pytest.mark.parametrize(
    ("sender", "failure_code"),
    [
        (_Sender(content="not json"), "response_json_invalid"),
        (_Sender(content='{"component_id":"foreign"}'), "local_validator_rejected"),
        (_Sender(model="foreign-model"), "observed_model_mismatch"),
        (_Sender(fail=True), "http_503"),
    ],
)
def test_invalid_json_semantics_model_and_http_fail_closed(
    tmp_path, sender, failure_code
) -> None:
    plan = _plan(f"literary_identity_probe_{failure_code}")
    probe, _ = _probe(tmp_path, sender)
    result = execute_literary_openai_identity_probe_once_v1(
        probe=probe, plan=plan
    )
    assert result["status"] == "failed"
    assert result["receipt"]["failure"]["code"] == failure_code
    assert sender.calls == 1


def test_same_probe_revision_cannot_call_twice(tmp_path) -> None:
    plan = _plan()
    sender = _Sender()
    probe, _ = _probe(tmp_path, sender)
    assert execute_literary_openai_identity_probe_once_v1(
        probe=probe, plan=plan
    )["status"] == "qualified"
    with pytest.raises(ContractValidationError, match="already reserved"):
        execute_literary_openai_identity_probe_once_v1(probe=probe, plan=plan)
    assert sender.calls == 1


def test_local_validator_retains_omitted_constraints() -> None:
    index = synthetic_identity_probe_index_v1()
    valid = synthetic_identity_probe_response_v1(index)
    duplicate = deepcopy(valid)
    duplicate["candidate_actions"][0]["source_block_ids"] *= 2
    with pytest.raises(Exception, match="non-unique|duplicate"):
        validate_literary_openai_identity_probe_payload_v1(
            index=index, payload=duplicate
        )
    empty = deepcopy(valid)
    empty["candidate_actions"][0]["source_block_ids"] = []
    with pytest.raises(Exception, match="non-empty|at least one"):
        validate_literary_openai_identity_probe_payload_v1(index=index, payload=empty)


def test_schema_projection_and_validator_drift_fail_before_transport(tmp_path) -> None:
    plan = _plan("literary_identity_probe_tamper")
    tampered = deepcopy(plan.request_body)
    tampered["response_format"]["json_schema"]["schema"] = deepcopy(
        plan.canonical_schema
    )
    sender = _Sender()
    probe, _ = _probe(tmp_path, sender)
    with pytest.raises(ContractValidationError, match="schema differs|request body differs"):
        probe.execute_once(
            seal=plan.seal,
            request_body=tampered,
            local_validator=lambda payload: payload,
            local_validator_id=VALIDATOR_ID,
            local_validator_sha256=VALIDATOR_SHA256,
        )
    assert sender.calls == 0

    with patch.object(probe_module, "VALIDATOR_SHA256", "f" * 64), pytest.raises(
        LiteraryOpenAiIdentityCapabilityProbeError, match="validator hash"
    ):
        probe_module.identity_validator_ref_v1()
