from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

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
from pipeline.literary.modelapi_b2_json_object_capability_probe_v1 import (
    LiteraryModelApiB2ProbeError,
    build_modelapi_b2_probe_plan_v1,
    execute_modelapi_b2_probe_once_v1,
    load_modelapi_b2_probe_profile_v1,
    validate_modelapi_b2_probe_payload_v1,
)
from pipeline.literary.openai_b2_json_object_capability_probe_v1 import (
    empty_b2_probe_response_v1,
    synthetic_b2_probe_request_v1,
)


SECRET = "synthetic-modelapi-b2-probe-secret"
from pipeline.literary.model_ref_transport_v1 import (
    resolve_capability_probe_response_v1,
)
from pipeline.tests.literary_capability_probe_test_support import (
    model_facing_probe_payload_v1,
)
from pipeline.scripts.run_literary_b2_registry_modelapi_v1 import (
    _canary_summary,
    _validate_frozen_db,
)
import pipeline.scripts.run_literary_b2_registry_modelapi_v1 as modelapi_runner


def _binding() -> dict[str, str]:
    return {
        "shared_core_revision": "a" * 40,
        "consumer_revision": "b" * 40,
        "consumer_implementation_sha256": "c" * 64,
    }


def test_modelapi_runner_forwards_preceding_b2_root_without_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr(modelapi_runner, "_clean_head", lambda: "head")
    monkeypatch.setattr(modelapi_runner, "_validate_frozen_db", lambda path: None)
    monkeypatch.setattr(modelapi_runner, "_credential", lambda path: "synthetic")

    def fake_run_canary(**kwargs):
        captured.update(kwargs)
        return {"status": "complete"}

    monkeypatch.setattr(modelapi_runner, "_run_canary", fake_run_canary)
    prior_root = tmp_path / "prior-b2"
    runtime_profile = tmp_path / "runtime-profile.json"
    rc = modelapi_runner.main(
        [
            "run",
            "--source-run-root",
            str(tmp_path / "source"),
            "--output-root",
            str(tmp_path / "output"),
            "--frame-capability-root",
            str(tmp_path / "frame-capability"),
            "--interaction-capability-root",
            str(tmp_path / "interaction-capability"),
            "--run-id",
            "run-2",
            "--attempt-run-id",
            "attempt-2",
            "--canary-profile",
            str(tmp_path / "ch2-profile.json"),
            "--runtime-profile",
            str(runtime_profile),
            "--prior-b2-root",
            str(prior_root),
            "--frozen-db",
            str(tmp_path / "frozen.sqlite3"),
        ]
    )

    assert rc == 0
    assert captured["prior_b2_root"] == prior_root
    assert captured["runtime_profile"] == runtime_profile


class _Sender:
    def __init__(self, plan, probe_name: str) -> None:
        canonical_request = synthetic_b2_probe_request_v1(probe_name)
        persistent = empty_b2_probe_response_v1(
            probe_name, request=canonical_request
        )
        self.payload = model_facing_probe_payload_v1(plan, persistent)
        self.calls = 0

    def send(self, request):
        self.calls += 1
        body = canonical_json(
            {
                "id": "synthetic-modelapi-b2-probe",
                "model": "gpt-5.4",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": canonical_json(self.payload)},
                    }
                ],
                "usage": {
                    "prompt_tokens": 500,
                    "completion_tokens": 100,
                    "total_tokens": 600,
                },
            }
        ).encode("utf-8")
        return RawTransportResponse(
            status_code=200,
            headers={},
            body=body,
            request_id="synthetic-modelapi-b2-probe",
        )


def _probe(tmp_path: Path, plan, sender: _Sender) -> SharedLlmCapabilityProbe:
    return SharedLlmCapabilityProbe(
        credential_provider=MappingCredentialProvider(
            {plan.source["credential_ref"]: SECRET}
        ),
        scheduler=PhysicalQuotaScheduler(tmp_path / "locks"),
        ledger=SharedLlmAttemptLedger(tmp_path / "ledger.sqlite3"),
        artifact_store=ContentAddressedArtifactStore(tmp_path / "artifacts"),
        sender=sender,
        implementation_binding=plan.implementation_binding,
    )


@pytest.mark.parametrize("probe_name", ["frame", "interaction"])
def test_modelapi_b2_probe_uses_json_object_and_local_validator(
    probe_name: str,
) -> None:
    plan = build_modelapi_b2_probe_plan_v1(
        probe_name=probe_name,
        probe_run_id=f"probe_{probe_name}",
        credential_commitment_sha256=credential_commitment(SECRET),
        issued_at_utc="2026-07-21T00:00:00.000Z",
        implementation_binding=_binding(),
    )
    assert plan.source["source_id"] == "modelapi_shared_v1"
    assert plan.source["physical_quota_bucket_id"] == "modelapi-shared-v1"
    assert plan.request_body["response_format"] == {"type": "json_object"}
    assert "json_schema" not in plan.request_body["response_format"]
    canonical_request = synthetic_b2_probe_request_v1(probe_name)
    model_payload = model_facing_probe_payload_v1(
        plan, empty_b2_probe_response_v1(probe_name, request=canonical_request)
    )
    resolved = resolve_capability_probe_response_v1(
        projected_request=plan.request,
        response=model_payload,
    )
    normalized = validate_modelapi_b2_probe_payload_v1(
        probe_name=probe_name,
        request=canonical_request,
        payload=resolved,
    )
    assert normalized["chapter_id"] == canonical_request["chapter_id"]


def test_modelapi_b2_probe_rejects_unknown_probe_name() -> None:
    with pytest.raises(LiteraryModelApiB2ProbeError, match="unknown"):
        build_modelapi_b2_probe_plan_v1(
            probe_name="foreign",
            probe_run_id="probe_foreign",
            credential_commitment_sha256="d" * 64,
            issued_at_utc="2026-07-21T00:00:00.000Z",
            implementation_binding=_binding(),
        )


@pytest.mark.parametrize("probe_name", ["frame", "interaction"])
def test_modelapi_b2_fake_probe_uses_local_refs_before_semantic_validation(
    probe_name: str, tmp_path: Path
) -> None:
    plan = build_modelapi_b2_probe_plan_v1(
        probe_name=probe_name,
        probe_run_id=f"probe_execute_{probe_name}",
        credential_commitment_sha256=credential_commitment(SECRET),
        issued_at_utc="2026-07-21T00:00:00.000Z",
        implementation_binding=_binding(),
    )
    sender = _Sender(plan, probe_name)
    result = execute_modelapi_b2_probe_once_v1(
        probe=_probe(tmp_path / probe_name, plan, sender),
        plan=plan,
    )
    assert result["status"] == "qualified"
    assert sender.calls == 1


def test_probe_profile_is_closed() -> None:
    profile = load_modelapi_b2_probe_profile_v1()
    changed = deepcopy(profile)
    changed["safety"]["fallback_enabled"] = True
    assert changed != profile
    assert profile["safety"]["fallback_enabled"] is False


def test_canary_summary_uses_slim_salient_event_field() -> None:
    summary = _canary_summary(
        frame={"artifact_hash": "f" * 64, "frame_segments": [{}, {}]},
        chapter={
            "chapter_id": "wh_ch01",
            "artifact_hash": "c" * 64,
            "speaker_turns": [{}, {}, {}],
            "salient_events": [{}],
            "review_requests": [{}, {}],
        },
    )
    assert summary["salient_events"] == 1
    assert summary["speaker_turns"] == 3
    assert summary["review_requests"] == 2


def test_frozen_db_preflight_rejects_missing_path(tmp_path) -> None:
    with pytest.raises(FileNotFoundError, match="frozen DB does not exist"):
        _validate_frozen_db(tmp_path / "missing.sqlite3")
