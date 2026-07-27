from __future__ import annotations

import json
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
from pipeline.literary.b4_editorial_live_modelapi_v1 import (
    EDITORIAL_RUNTIME_ROLE_IDS,
    run_editorial_review_live_v1,
)
from pipeline.literary.b4_editorial_review_v1 import ROLE_ID
from pipeline.literary.modelapi_b4_editorial_capability_probe_v1 import (
    RUNTIME_PROFILE_PATH,
    STYLE_PROFILE,
    build_probe_plan_v1,
    execute_probe_once_v1,
    synthetic_probe_rendered_v1,
    synthetic_probe_response_v1,
)
from pipeline.literary.shared_runtime_profile_v2 import (
    load_literary_shared_runtime_profile_v2,
)


SECRET = "synthetic-b4-editorial-secret"


class _Sender:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls = 0

    def send(self, _request):
        self.calls += 1
        body = canonical_json(
            {
                "id": f"synthetic-editorial-{self.calls}",
                "model": "gpt-5.4",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": canonical_json(self.payload)
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 1_200,
                    "completion_tokens": 180,
                    "total_tokens": 1_380,
                    "prompt_tokens_details": {"cached_tokens": 0},
                },
            }
        ).encode("utf-8")
        return RawTransportResponse(
            status_code=200,
            headers={},
            body=body,
            request_id=f"synthetic-editorial-{self.calls}",
        )


def _binding() -> dict[str, str]:
    return {
        "shared_core_revision": "1" * 40,
        "consumer_revision": "2" * 40,
        "consumer_implementation_sha256": "3" * 64,
    }


def _qualified_evidence(tmp_path: Path) -> dict:
    plan = build_probe_plan_v1(
        probe_run_id="editorial_probe_test",
        credential_commitment_sha256=credential_commitment(SECRET),
        issued_at_utc="2026-07-27T00:00:00Z",
        implementation_binding=_binding(),
    )
    sender = _Sender(synthetic_probe_response_v1())
    probe = SharedLlmCapabilityProbe(
        credential_provider=MappingCredentialProvider(
            {plan.source["credential_ref"]: SECRET}
        ),
        scheduler=PhysicalQuotaScheduler(tmp_path / "locks"),
        ledger=SharedLlmAttemptLedger(tmp_path / "ledger.sqlite3"),
        artifact_store=ContentAddressedArtifactStore(
            tmp_path / "artifacts"
        ),
        sender=sender,
        implementation_binding=plan.implementation_binding,
    )
    result = execute_probe_once_v1(probe=probe, plan=plan)
    assert result["status"] == "qualified"
    assert sender.calls == 1
    return result["capability_evidence"]


def test_editorial_runtime_registers_one_fail_closed_role() -> None:
    runtime = load_literary_shared_runtime_profile_v2(
        RUNTIME_PROFILE_PATH,
        expected_role_ids=EDITORIAL_RUNTIME_ROLE_IDS,
    )

    assert set(runtime.role_bindings) == {ROLE_ID}
    assert runtime.role_presets[ROLE_ID].limits["max_calls"] == 24
    assert runtime.role_presets[ROLE_ID].transport_retry["max_retries"] == 0
    assert runtime.output_envelope_for(ROLE_ID)["mode"] == "json_object"


def test_editorial_probe_and_live_call_persist_review(tmp_path: Path) -> None:
    evidence = _qualified_evidence(tmp_path / "probe")
    rendered = synthetic_probe_rendered_v1()
    sender = _Sender(synthetic_probe_response_v1())
    output = tmp_path / "live"

    report = run_editorial_review_live_v1(
        review_packet=rendered.packet,
        style_profile=STYLE_PROFILE,
        capability_evidence=evidence,
        output_root=output,
        shared_root=tmp_path / "shared",
        scheduler_root=tmp_path / "locks-live",
        secret=SECRET,
        credential_commitment_sha256=credential_commitment(SECRET),
        run_id="editorial_live_test",
        attempt_run_id="editorial_live_test_a1",
        current_git_head="4" * 40,
        sender=sender,
    )

    assert sender.calls == 1
    assert report["provider_called"] is True
    assert report["action_counts"] == {"accept": 1}
    assert report["provider_retries"] == 0
    assert (output / "shared_attempt_receipt.json").is_file()
    artifact = json.loads(
        (output / "editorial_review.json").read_text(encoding="utf-8")
    )
    assert artifact["translation_text_mutation_performed"] is False
    assert artifact["blocks"][0]["suggested_action"] == "accept"
