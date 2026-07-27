from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.llm_backend import (
    ContentAddressedArtifactStore,
    ContractValidationError,
    MappingCredentialProvider,
    PhysicalQuotaScheduler,
    RawTransportResponse,
    SharedLlmAttemptLedger,
    SharedLlmCapabilityProbe,
    canonical_json,
    credential_commitment,
)
from pipeline.literary.openai_b1_scan_capability_probe_v1 import (
    MODELAPI_PROFILE_PATH,
    MODELAPI_RUNTIME_PROFILE_PATH,
    build_probe_plan_v1,
    execute_probe_once_v1,
    synthetic_b1_scan_response_v1,
)
from pipeline.tests.literary_capability_probe_test_support import (
    model_facing_probe_payload_v1,
)


SECRET = "synthetic-literary-b1-scan-secret"


def _plan(suffix: str = "001"):
    return build_probe_plan_v1(
        probe_run_id=f"literary_b1_scan_probe_{suffix}",
        credential_commitment_sha256=credential_commitment(SECRET),
        issued_at_utc="2026-07-21T00:00:00Z",
        implementation_binding={
            "shared_core_revision": "1" * 40,
            "consumer_revision": "2" * 40,
            "consumer_implementation_sha256": "3" * 64,
        },
    )


class _Sender:
    def __init__(self, plan, *, content: str | None = None) -> None:
        self.content = content or canonical_json(
            model_facing_probe_payload_v1(plan, synthetic_b1_scan_response_v1())
        )
        self.calls = 0

    def send(self, request):
        self.calls += 1
        assert request.url == "https://api.openai.com/v1/chat/completions"
        assert request.headers_for_transport()["authorization"] == f"Bearer {SECRET}"
        content = self.content
        body = canonical_json(
            {
                "id": "b1-scan-probe-request",
                "model": "gpt-5.4-2026-03-05",
                "choices": [
                    {"finish_reason": "stop", "message": {"content": content}}
                ],
                "usage": {
                    "prompt_tokens": 900,
                    "completion_tokens": 80,
                    "total_tokens": 980,
                    "prompt_tokens_details": {"cached_tokens": 0},
                    "completion_tokens_details": {"reasoning_tokens": 0},
                },
            }
        ).encode("utf-8")
        return RawTransportResponse(
            status_code=200,
            headers={"x-request-id": "b1-scan-probe-request"},
            body=body,
            request_id="b1-scan-probe-request",
        )


def _probe(tmp_path: Path, plan, sender: _Sender) -> SharedLlmCapabilityProbe:
    return SharedLlmCapabilityProbe(
        credential_provider=MappingCredentialProvider(
            {"credential.openai_row2": SECRET}
        ),
        scheduler=PhysicalQuotaScheduler(tmp_path / "locks"),
        ledger=SharedLlmAttemptLedger(tmp_path / "ledger.sqlite3"),
        artifact_store=ContentAddressedArtifactStore(tmp_path / "artifacts"),
        sender=sender,
        implementation_binding=plan.implementation_binding,
    )


def test_probe_plan_uses_json_object_and_exact_local_schema() -> None:
    plan = _plan()
    assert plan.request_body["response_format"] == {"type": "json_object"}
    assert "json_schema" not in plan.request_body["response_format"]
    assert plan.seal["role_id"] == "literary.b1.scan"
    assert plan.seal["capability_intent"]["capability_kind"] == "json_object"


def test_modelapi_profile_keeps_same_model_and_non_native_json_object() -> None:
    plan = build_probe_plan_v1(
        probe_run_id="literary_b1_scan_modelapi_probe_001",
        credential_commitment_sha256=credential_commitment(SECRET),
        issued_at_utc="2026-07-21T00:00:00Z",
        implementation_binding={
            "shared_core_revision": "1" * 40,
            "consumer_revision": "2" * 40,
            "consumer_implementation_sha256": "3" * 64,
        },
        profile_path=MODELAPI_PROFILE_PATH,
        runtime_profile_path=MODELAPI_RUNTIME_PROFILE_PATH,
    )
    assert plan.source["source_id"] == "modelapi_shared_v1"
    assert plan.source["base_url"] == "https://modelapi.vn/v1"
    assert plan.seal["capability_intent"]["requested_model_id"] == "gpt-5.4"
    assert plan.request_body["response_format"] == {"type": "json_object"}
    assert "json_schema" not in plan.request_body["response_format"]


def test_valid_payload_qualifies_once(tmp_path: Path) -> None:
    plan = _plan()
    sender = _Sender(plan)
    probe = _probe(tmp_path, plan, sender)
    result = execute_probe_once_v1(probe=probe, plan=plan)
    assert result["status"] == "qualified"
    assert result["capability_evidence"]["verdict"] == "qualified"
    assert sender.calls == 1
    with pytest.raises(ContractValidationError, match="already reserved"):
        execute_probe_once_v1(probe=probe, plan=plan)
    assert sender.calls == 1


def test_invalid_json_stays_failed(tmp_path: Path) -> None:
    plan = _plan("invalid-json")
    sender = _Sender(plan, content="not-json")
    result = execute_probe_once_v1(
        probe=_probe(tmp_path, plan, sender), plan=plan
    )
    assert result["status"] == "failed"
    assert result["receipt"]["failure"]["code"] == "response_json_invalid"
    assert result["capability_evidence"]["verdict"] == "failed"
    assert sender.calls == 1
