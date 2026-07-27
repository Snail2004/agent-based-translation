from __future__ import annotations

from copy import deepcopy
import json

from pipeline.llm_backend import (
    ContentAddressedArtifactStore,
    MappingCredentialProvider,
    PhysicalQuotaScheduler,
    RawTransportResponse,
    SharedLlmAttemptLedger,
    SharedLlmCapabilityProbe,
    canonical_json,
    credential_commitment,
)
from pipeline.literary.b3_temporal_prompts_v7 import b3_temporal_response_schema_v7
from pipeline.literary.b3_temporal_capability_contract_v4 import (
    empty_b3_probe_response_v3,
    synthetic_b3_probe_request_v7,
)
from pipeline.literary.modelapi_b3_json_object_capability_probe_v1 import (
    ROLE_ID,
    build_probe_plan_v1,
    execute_probe_once_v1,
    load_probe_profile_v1,
)
from pipeline.tests.literary_capability_probe_test_support import (
    model_facing_probe_payload_v1,
)


SECRET = "synthetic-modelapi-b3-secret"
BINDING = {
    "shared_core_revision": "a" * 40,
    "consumer_revision": "b" * 40,
    "consumer_implementation_sha256": "c" * 64,
}


class _Sender:
    def __init__(self, plan) -> None:
        self.plan = plan
        self.calls = 0

    def send(self, request):
        self.calls += 1
        assert request.url == "https://modelapi.vn/v1/chat/completions"
        body = json.loads(request.body)
        assert body["response_format"] == {"type": "json_object"}
        assert "json_schema" not in body["response_format"]
        canonical_request = synthetic_b3_probe_request_v7()
        payload = {
            "id": "modelapi-b3-probe",
            "model": "gpt-5.4",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": canonical_json(
                            model_facing_probe_payload_v1(
                                self.plan,
                                empty_b3_probe_response_v3(canonical_request),
                            )
                        )
                    },
                }
            ],
            "usage": {
                "prompt_tokens": 800,
                "completion_tokens": 40,
                "total_tokens": 840,
                "prompt_tokens_details": {"cached_tokens": 0},
                "completion_tokens_details": {"reasoning_tokens": 0},
            },
        }
        return RawTransportResponse(
            status_code=200,
            headers={"x-request-id": "modelapi-b3-probe"},
            body=canonical_json(payload).encode("utf-8"),
            request_id="modelapi-b3-probe",
        )


def _plan():
    return build_probe_plan_v1(
        probe_run_id="modelapi_b3_probe_fixture",
        credential_commitment_sha256=credential_commitment(SECRET),
        issued_at_utc="2026-07-21T00:00:00Z",
        implementation_binding=BINDING,
    )


def test_probe_uses_modelapi_json_object_without_native_schema() -> None:
    plan = _plan()
    assert plan.seal["role_id"] == ROLE_ID
    assert plan.source["source_id"] == "modelapi_shared_v1"
    assert plan.source["physical_quota_bucket_id"] == "modelapi-shared-v1"
    assert plan.request_body["response_format"] == {"type": "json_object"}
    assert plan.response_schema == b3_temporal_response_schema_v7()


def test_probe_qualifies_only_after_b3_local_validation(tmp_path) -> None:
    plan = _plan()
    sender = _Sender(plan)
    probe = SharedLlmCapabilityProbe(
        credential_provider=MappingCredentialProvider(
            {plan.source["credential_ref"]: SECRET}
        ),
        scheduler=PhysicalQuotaScheduler(tmp_path / "quota"),
        ledger=SharedLlmAttemptLedger(tmp_path / "attempts.sqlite3"),
        artifact_store=ContentAddressedArtifactStore(tmp_path / "artifacts"),
        sender=sender,
        implementation_binding=BINDING,
    )
    result = execute_probe_once_v1(probe=probe, plan=plan)
    assert result["status"] == "qualified"
    assert result["capability_evidence"]["verdict"] == "qualified"
    assert sender.calls == 1


def test_probe_profile_and_runtime_are_closed() -> None:
    profile = load_probe_profile_v1()
    changed = deepcopy(profile)
    changed["safety"]["fallback_enabled"] = True
    assert changed != profile
    assert profile["safety"]["fallback_enabled"] is False
    assert profile["safety"]["transport_retry_max"] == 0
    assert profile["safety"]["semantic_retry_max"] == 0
