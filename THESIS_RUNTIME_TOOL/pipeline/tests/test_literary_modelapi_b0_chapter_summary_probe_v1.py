from __future__ import annotations

from pathlib import Path

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
from pipeline.literary.b0_chapter_summary_v1 import (
    ROLE_ID,
    synthetic_b0_context_v1,
    synthetic_b0_response_v1,
)
from pipeline.literary.modelapi_b0_chapter_summary_capability_probe_v1 import (
    RUNTIME_PROFILE_PATH,
    build_probe_plan_v1,
    execute_probe_once_v1,
    load_probe_profile_v1,
)
from pipeline.literary.shared_runtime_profile_v2 import (
    load_literary_shared_runtime_profile_v2,
)
from pipeline.tests.literary_capability_probe_test_support import (
    model_facing_probe_payload_v1,
)


SECRET = "synthetic-b0-summary-secret"


def _plan():
    return build_probe_plan_v1(
        probe_run_id="synthetic_b0_summary_probe",
        credential_commitment_sha256=credential_commitment(SECRET),
        issued_at_utc="2026-07-21T00:00:00Z",
        implementation_binding={
            "shared_core_revision": "1" * 40,
            "consumer_revision": "2" * 40,
            "consumer_implementation_sha256": "3" * 64,
        },
    )


class _Sender:
    def __init__(self, plan) -> None:
        self.plan = plan
        self.calls = 0

    def send(self, request):
        self.calls += 1
        payload = model_facing_probe_payload_v1(
            self.plan, synthetic_b0_response_v1(synthetic_b0_context_v1())
        )
        body = canonical_json(
            {
                "id": "synthetic-b0-summary-probe",
                "model": "gpt-5.4",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": canonical_json(payload)},
                    }
                ],
                "usage": {
                    "prompt_tokens": 800,
                    "completion_tokens": 180,
                    "total_tokens": 980,
                },
            }
        ).encode("utf-8")
        return RawTransportResponse(
            status_code=200,
            headers={},
            body=body,
            request_id="synthetic-b0-summary-probe",
        )


def test_modelapi_profiles_are_closed_prompt_validated_json() -> None:
    probe = load_probe_profile_v1()
    assert probe["limits"]["max_calls"] == 1
    assert probe["safety"]["fallback_enabled"] is False
    runtime = load_literary_shared_runtime_profile_v2(
        RUNTIME_PROFILE_PATH, expected_role_ids={ROLE_ID}
    )
    role = runtime.role_presets[ROLE_ID]
    assert (
        runtime.profile_revision
        == "modelapi_gpt54_b0_chapter_summary_prompt_validated_v2"
    )
    assert role.requested_model_id == "gpt-5.4"
    assert role.preset_revision == "v2"
    assert role.generation["max_input_tokens"] == 24_000
    assert role.limits["max_calls"] == 1
    assert role.limits["max_prompt_tokens"] == 24_000
    assert role.limits["max_total_tokens"] == 26_500
    assert runtime.source_binding_for(ROLE_ID)["authority_class"] == "third_party"
    assert runtime.output_envelope_for(ROLE_ID)["mode"] == "json_object"


def test_modelapi_fake_probe_qualifies_exact_local_contract(tmp_path: Path) -> None:
    plan = _plan()
    assert plan.request_body["response_format"] == {"type": "json_object"}
    sender = _Sender(plan)
    probe = SharedLlmCapabilityProbe(
        credential_provider=MappingCredentialProvider(
            {"credential.modelapi_shared_v1": SECRET}
        ),
        scheduler=PhysicalQuotaScheduler(tmp_path / "locks"),
        ledger=SharedLlmAttemptLedger(tmp_path / "ledger.sqlite3"),
        artifact_store=ContentAddressedArtifactStore(tmp_path / "artifacts"),
        sender=sender,
        implementation_binding=plan.implementation_binding,
    )
    result = execute_probe_once_v1(probe=probe, plan=plan)
    assert result["status"] == "qualified"
    assert result["capability_evidence"]["verdict"] == "qualified"
    assert sender.calls == 1
