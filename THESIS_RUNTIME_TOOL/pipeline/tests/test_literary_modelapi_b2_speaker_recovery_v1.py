from __future__ import annotations

from copy import deepcopy
import json
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
from pipeline.literary.b2_recovery_v1 import B2RecoveryContractError
from pipeline.literary.b2_recovery_batch_v1 import (
    REGISTRY_RECOVERY_BATCH_SYSTEM_PROMPT_V1,
)
from pipeline.literary.b2_slim_speaker_recovery_v1 import (
    make_b2_slim_speaker_recovery_validator_v1,
    render_b2_slim_speaker_recovery_request_v1,
)
from pipeline.literary.modelapi_b2_speaker_recovery_capability_probe_v1 import (
    ROLE_ID,
    build_probe_plan_v1,
    execute_probe_once_v1,
    load_probe_profile_v1,
    synthetic_probe_index_v1,
)
from pipeline.scripts.run_literary_b2_speaker_recovery_modelapi_v1 import (
    _consolidate_batch_decisions_v1,
)
from pipeline.tests.literary_capability_probe_test_support import (
    model_facing_probe_payload_v1,
)


SECRET = "synthetic-modelapi-b2-speaker-probe-secret"


def _binding() -> dict[str, str]:
    return {
        "shared_core_revision": "a" * 40,
        "consumer_revision": "b" * 40,
        "consumer_implementation_sha256": "c" * 64,
    }


def _response(action: str) -> dict:
    index = synthetic_probe_index_v1()
    request = render_b2_slim_speaker_recovery_request_v1(index)
    assert request is not None
    component = index["registry_components"][0]
    ticket = index["registry_gap_tickets"][0]
    return {
        "schema_version": "literary_b2_registry_recovery_batch_response_v1_1",
        "chapter_id": "probe_chapter",
        "batch_id": request.component_id,
        "component_results": [
            {
                "component_id": component["component_id"],
                "result": {
                    "schema_version": "literary_b2_registry_recovery_response_v1",
                    "chapter_id": "probe_chapter",
                    "component_id": component["component_id"],
                    "ticket_actions": [
                        {
                            "ticket_id": ticket["ticket_id"],
                            "action": action,
                            "target_candidate_card_id": (
                                "probe_ent_rowan" if action == "attach_existing" else None
                            ),
                            "provisional_group_key": None,
                            "canonical_surface": None,
                            "referent_kind": None,
                            "identity_summary": None,
                            "source_block_ids": ["probe_block"],
                            "pending_reason": (
                                "Insufficient evidence." if action == "keep_pending" else None
                            ),
                            "resolution_note": "Probe verdict remains model-owned.",
                            **(
                                {"narrowed_candidate_card_ids": []}
                                if action == "keep_pending"
                                else {}
                            ),
                        }
                    ],
                },
            }
        ],
    }


class _Sender:
    def __init__(self, plan) -> None:
        self.payload = model_facing_probe_payload_v1(
            plan, _response("attach_existing")
        )
        self.calls = 0

    def send(self, request):
        self.calls += 1
        body = canonical_json(
            {
                "id": "synthetic-modelapi-b2-speaker-probe",
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
            request_id="synthetic-modelapi-b2-speaker-probe",
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


def test_probe_uses_third_party_json_object_without_native_schema() -> None:
    plan = build_probe_plan_v1(
        probe_run_id="probe_b2_speaker",
        credential_commitment_sha256=credential_commitment(SECRET),
        issued_at_utc="2026-07-21T00:00:00.000Z",
        implementation_binding=_binding(),
    )
    assert plan.seal["role_id"] == ROLE_ID
    assert plan.request_body["response_format"] == {"type": "json_object"}
    assert "json_schema" not in plan.request_body["response_format"]


def test_probe_projects_source_frame_segment_id_to_a_frame_label() -> None:
    plan = build_probe_plan_v1(
        probe_run_id="probe_b2_speaker_frame_ref",
        credential_commitment_sha256=credential_commitment(SECRET),
        issued_at_utc="2026-07-21T00:00:00.000Z",
        implementation_binding=_binding(),
    )
    payload = json.loads(plan.request["messages"][1]["content"])
    ticket = payload["components"][0]["tickets"][0]

    assert ticket["source_frame_segment_id"] == "F1"
    assert ticket["source_window_id"] == "F1"
    assert "b2frm2_probe_frame" not in canonical_json(payload)


def test_probe_executes_the_local_ref_envelope_before_semantic_validation(
    tmp_path: Path,
) -> None:
    plan = build_probe_plan_v1(
        probe_run_id="probe_b2_speaker_execute",
        credential_commitment_sha256=credential_commitment(SECRET),
        issued_at_utc="2026-07-21T00:00:00.000Z",
        implementation_binding=_binding(),
    )
    sender = _Sender(plan)
    result = execute_probe_once_v1(
        probe=_probe(tmp_path, plan, sender),
        plan=plan,
    )
    assert result["status"] == "qualified"
    assert sender.calls == 1


def test_batch_prompt_states_action_dependent_null_contract() -> None:
    assert "literary_b2_registry_recovery_batch_audit_v1_2" in (
        REGISTRY_RECOVERY_BATCH_SYSTEM_PROMPT_V1
    )
    assert "attach_existing: set target_candidate_card_id" in (
        REGISTRY_RECOVERY_BATCH_SYSTEM_PROMPT_V1
    )
    assert "These are output-contract rules, not" in (
        REGISTRY_RECOVERY_BATCH_SYSTEM_PROMPT_V1
    )


def test_probe_validator_accepts_attach_or_pending_without_forcing_verdict() -> None:
    index = synthetic_probe_index_v1()
    request = render_b2_slim_speaker_recovery_request_v1(index)
    assert request is not None
    validator = make_b2_slim_speaker_recovery_validator_v1(
        index=index, request=request
    )
    assert validator(_response("attach_existing"))["component_decisions"][0][
        "ticket_actions"
    ][0]["action"] == "attach_existing"
    assert validator(_response("keep_pending"))["component_decisions"][0][
        "ticket_actions"
    ][0]["action"] == "keep_pending"


def test_probe_validator_accepts_empty_narrowing_on_decided_action_only() -> None:
    index = synthetic_probe_index_v1()
    request = render_b2_slim_speaker_recovery_request_v1(index)
    assert request is not None
    validator = make_b2_slim_speaker_recovery_validator_v1(
        index=index, request=request
    )
    empty = _response("attach_existing")
    action = empty["component_results"][0]["result"]["ticket_actions"][0]
    action["narrowed_candidate_card_ids"] = []
    accepted = validator(empty)
    assert accepted["component_decisions"][0]["ticket_actions"][0][
        "narrowed_candidate_card_ids"
    ] == []

    non_empty = deepcopy(empty)
    non_empty["component_results"][0]["result"]["ticket_actions"][0][
        "narrowed_candidate_card_ids"
    ] = ["probe_ent_rowan"]
    with pytest.raises(
        B2RecoveryContractError,
        match="non-pending action carries narrowed candidate card ids",
    ):
        validator(non_empty)


def test_probe_profile_is_closed() -> None:
    profile = load_probe_profile_v1()
    changed = deepcopy(profile)
    changed["safety"]["fallback_enabled"] = True
    assert changed != profile
    assert profile["safety"]["fallback_enabled"] is False


def _sealed_batch_decision(component_id: str) -> dict:
    body = {
        "schema_version": "literary_b2_registry_recovery_batch_decision_v1",
        "validator_version": "test",
        "recovery_index_hash": "index_hash",
        "chapter_id": "wh_ch03",
        "batch_id": f"batch_{component_id}",
        "request_fingerprint": f"request_{component_id}",
        "component_decisions": [
            {
                "component_id": component_id,
                "ticket_actions": [],
            }
        ],
        "contract_normalizations": [],
    }
    from pipeline.literary.checkpoint import canonical_hash

    return {**body, "batch_decision_hash": canonical_hash(body)}


def test_speaker_recovery_batch_fold_exact_covers_components() -> None:
    folded = _consolidate_batch_decisions_v1(
        chapter_id="wh_ch03",
        recovery_index_hash="index_hash",
        expected_component_ids=["c1", "c2", "c3", "c4", "c5"],
        batch_decisions=[
            _sealed_batch_decision("c1"),
            _sealed_batch_decision("c2"),
            _sealed_batch_decision("c3"),
            _sealed_batch_decision("c4"),
            _sealed_batch_decision("c5"),
        ],
    )
    assert folded["schema_version"] == (
        "literary_b2_registry_recovery_consolidated_decision_v1"
    )
    assert [
        row["component_id"] for row in folded["component_decisions"]
    ] == ["c1", "c2", "c3", "c4", "c5"]


def test_speaker_recovery_batch_fold_rejects_duplicate_component() -> None:
    with pytest.raises(
        SystemExit,
        match="do not exact-cover components",
    ):
        _consolidate_batch_decisions_v1(
            chapter_id="wh_ch03",
            recovery_index_hash="index_hash",
            expected_component_ids=["c1", "c2"],
            batch_decisions=[
                _sealed_batch_decision("c1"),
                _sealed_batch_decision("c1"),
            ],
        )
