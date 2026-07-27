from __future__ import annotations

from copy import deepcopy
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
from pipeline.literary.modelapi_b1_enrich_capability_probe_v1 import (
    ROLE_ID,
    RUNTIME_PROFILE_PATH,
    build_probe_plan_v1,
    execute_probe_once_v1,
    synthetic_response_v1,
)
from pipeline.literary.shared_runtime_profile_v2 import (
    load_literary_shared_runtime_profile_v2,
)
from pipeline.tests.literary_capability_probe_test_support import (
    model_facing_probe_payload_v1,
)


SECRET = "synthetic-literary-b1-enrich-secret"


def _plan(suffix="001"):
    return build_probe_plan_v1(
        probe_run_id=f"literary_b1_enrich_probe_{suffix}",
        credential_commitment_sha256=credential_commitment(SECRET),
        issued_at_utc="2026-07-21T00:00:00Z",
        implementation_binding={
            "shared_core_revision": "1" * 40,
            "consumer_revision": "2" * 40,
            "consumer_implementation_sha256": "3" * 64,
        },
    )


class _Sender:
    def __init__(self, plan, payload=None):
        self.payload = model_facing_probe_payload_v1(
            plan, payload or synthetic_response_v1()
        )
        self.calls = 0

    def send(self, request):
        self.calls += 1
        assert request.url == "https://modelapi.vn/v1/chat/completions"
        assert request.headers_for_transport()["authorization"] == f"Bearer {SECRET}"
        body = canonical_json(
            {
                "id": "b1-enrich-probe-request",
                "model": "gpt-5.4",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": canonical_json(self.payload)},
                    }
                ],
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 200,
                    "total_tokens": 1200,
                },
            }
        ).encode("utf-8")
        return RawTransportResponse(
            status_code=200,
            headers={"x-request-id": "b1-enrich-probe-request"},
            body=body,
            request_id="b1-enrich-probe-request",
        )


def _probe(tmp_path: Path, plan, sender):
    return SharedLlmCapabilityProbe(
        credential_provider=MappingCredentialProvider(
            {"credential.modelapi_shared_v1": SECRET}
        ),
        scheduler=PhysicalQuotaScheduler(tmp_path / "locks"),
        ledger=SharedLlmAttemptLedger(tmp_path / "ledger.sqlite3"),
        artifact_store=ContentAddressedArtifactStore(tmp_path / "artifacts"),
        sender=sender,
        implementation_binding=plan.implementation_binding,
    )


def test_plan_uses_modelapi_json_object_without_native_schema() -> None:
    plan = _plan()
    assert plan.source["source_id"] == "modelapi_shared_v1"
    assert plan.request_body["response_format"] == {"type": "json_object"}
    assert "json_schema" not in plan.request_body["response_format"]
    assert plan.seal["role_id"] == "literary.b1.enrich"


def test_runtime_profile_v2_raises_only_b1_enrich_input_reserve() -> None:
    profile = load_literary_shared_runtime_profile_v2(
        RUNTIME_PROFILE_PATH, expected_role_ids={ROLE_ID}
    )
    preset = profile.role_presets[ROLE_ID]
    assert preset.generation["max_input_tokens"] == 20_000
    assert preset.generation["max_output_tokens"] == 8_192
    assert preset.limits["max_prompt_tokens"] == 20_000
    assert preset.limits["max_completion_tokens"] == 8_192
    assert preset.limits["max_total_tokens"] == 28_192
    assert preset.transport_retry["max_retries"] == 0
    assert preset.semantic_retry["max_retries"] == 0


def test_valid_payload_qualifies_exact_binding(tmp_path: Path) -> None:
    plan = _plan()
    sender = _Sender(plan)
    result = execute_probe_once_v1(
        probe=_probe(tmp_path, plan, sender), plan=plan
    )
    assert result["status"] == "qualified"
    assert result["capability_evidence"]["verdict"] == "qualified"
    assert sender.calls == 1


def test_semantically_invalid_payload_does_not_qualify(tmp_path: Path) -> None:
    payload = deepcopy(synthetic_response_v1())
    payload["entities"][0]["claims"] = []
    plan = _plan("invalid")
    result = execute_probe_once_v1(
        probe=_probe(tmp_path, plan, _Sender(plan, payload)), plan=plan
    )
    assert result["status"] == "failed"
    assert result["capability_evidence"]["verdict"] == "failed"
