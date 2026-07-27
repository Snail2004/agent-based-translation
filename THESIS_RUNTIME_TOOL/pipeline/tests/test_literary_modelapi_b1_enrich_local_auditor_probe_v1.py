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
from pipeline.literary.modelapi_b1_enrich_local_auditor_capability_probe_v1 import (
    build_probe_plan_v1,
    execute_probe_once_v1,
    synthetic_response_v1,
)
from pipeline.literary.model_ref_transport_v1 import (
    resolve_capability_probe_response_v1,
)
from pipeline.tests.literary_capability_probe_test_support import (
    model_facing_probe_payload_v1,
)


SECRET = "synthetic-literary-b1-enrich-local-auditor-secret"


def _plan(suffix="001"):
    return build_probe_plan_v1(
        probe_run_id=f"literary_b1_enrich_local_auditor_probe_{suffix}",
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
                "id": "b1-enrich-local-auditor-probe-request",
                "model": "gpt-5.4",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": canonical_json(self.payload)},
                    }
                ],
                "usage": {
                    "prompt_tokens": 900,
                    "completion_tokens": 180,
                    "total_tokens": 1080,
                },
            }
        ).encode("utf-8")
        return RawTransportResponse(
            status_code=200,
            headers={"x-request-id": "b1-enrich-local-auditor-probe-request"},
            body=body,
            request_id="b1-enrich-local-auditor-probe-request",
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
    assert plan.seal["role_id"] == "literary.audit.b1_enrich_local"


def test_valid_payload_qualifies_exact_binding(tmp_path: Path) -> None:
    plan = _plan()
    sender = _Sender(plan)
    result = execute_probe_once_v1(
        probe=_probe(tmp_path, plan, sender), plan=plan
    )
    assert result["status"] == "qualified"
    assert result["capability_evidence"]["verdict"] == "qualified"
    assert sender.calls == 1


def test_unasked_observation_refs_round_trip_through_local_labels() -> None:
    plan = _plan("unasked_ref_round_trip")
    persistent = deepcopy(synthetic_response_v1())
    persistent["unasked_same_referent_observations"] = [
        {
            "subject_ref": "scan:b1obs_traveler",
            "target_ref": "scan:b1obs_mara",
            "source_block_ids": ["literary_b1_enrich_local_audit_probe_b001"],
            "reason": "Transport-only round-trip fixture.",
        }
    ]

    model_payload = model_facing_probe_payload_v1(plan, persistent)
    model_row = model_payload["unasked_same_referent_observations"][0]
    assert model_row["subject_ref"].startswith("O")
    assert model_row["target_ref"].startswith("O")

    resolved = resolve_capability_probe_response_v1(
        projected_request=plan.request,
        response=model_payload,
    )
    resolved_row = resolved["unasked_same_referent_observations"][0]
    assert resolved_row["subject_ref"] == "scan:b1obs_traveler"
    assert resolved_row["target_ref"] == "scan:b1obs_mara"


def test_any_locally_valid_verdict_may_qualify(tmp_path: Path) -> None:
    payload = deepcopy(synthetic_response_v1())
    payload["decisions"][0]["action"] = "keep_pending"
    plan = _plan("pending")
    result = execute_probe_once_v1(
        probe=_probe(tmp_path, plan, _Sender(plan, payload)), plan=plan
    )
    assert result["status"] == "qualified"
    assert result["capability_evidence"]["verdict"] == "qualified"


def test_foreign_revision_does_not_qualify(tmp_path: Path) -> None:
    plan = _plan("foreign")
    sender = _Sender(plan)
    sender.payload["decisions"][0].update(
        {
            "action": "revise_proposal",
            "revised_relation": "resides_at",
            "revised_relation_note": None,
            "revised_target_ref": "O99",
        }
    )
    result = execute_probe_once_v1(
        probe=_probe(tmp_path, plan, sender), plan=plan
    )
    assert result["status"] == "failed"
    assert result["capability_evidence"]["verdict"] == "failed"
