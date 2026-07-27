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
from pipeline.literary.b3_temporal_auditor_v1 import (
    B3_TEMPORAL_AUDITOR_MODEL_REF_FIELDS_V1,
    B3TemporalAuditorError,
    REVIEW_ROUTE_DESTINATIONS,
    STATE_REVIEW_ROUTES,
    SYSTEM_PROMPT,
    build_b3_temporal_review_overlay_v1,
    classify_b3_review_selection_v1,
    render_b3_temporal_audit_request_v1,
    synthetic_b3_temporal_review_packet_v1,
    synthetic_keep_pending_response_v1,
    validate_b3_temporal_audit_response_v1,
    verify_b3_temporal_review_overlay_v1,
)
from pipeline.literary.checkpoint import canonical_hash
from pipeline.literary.model_ref_v1 import project_model_request_v1
from pipeline.literary.modelapi_b3_temporal_auditor_capability_probe_v1 import (
    build_probe_plan_v1,
    execute_probe_once_v1,
    load_probe_profile_v1,
    validator_ref_v1,
)
from pipeline.literary.shared_runtime_profile_v2 import (
    load_literary_shared_runtime_profile_v2,
)
from pipeline.literary.modelapi_b3_temporal_auditor_capability_probe_v1 import (
    RUNTIME_PROFILE_PATH,
)
from pipeline.tests.literary_capability_probe_test_support import (
    model_facing_probe_payload_v1,
)
from pipeline.scripts import run_literary_b3_temporal_auditor_modelapi_v1 as runner


SECRET = "synthetic-b3-temporal-auditor-secret"


def test_auditor_transport_projects_consolidated_state_ids() -> None:
    request = {
        "messages": [
            {"role": "system", "content": "Review one supplied state."},
            {
                "role": "user",
                "content": canonical_json(
                    {
                        "state_id": "b3state1_primary",
                        "corroborating_state_ids": ["b3state1_corroborating"],
                    }
                ),
            },
        ],
        "response_schema": {"type": "object", "properties": {}},
    }

    projected, _ref_map = project_model_request_v1(
        request,
        field_names_by_namespace=B3_TEMPORAL_AUDITOR_MODEL_REF_FIELDS_V1,
    )
    payload = json.loads(projected["messages"][1]["content"])

    assert payload["state_id"].startswith("S")
    assert payload["corroborating_state_ids"][0].startswith("S")
    assert "b3state1_" not in projected["messages"][1]["content"]


def _pending_case(case_id: str, route: str) -> dict[str, str]:
    return {
        "pending_case_id": case_id,
        "review_route": route,
        "authority_status": "pending_review",
    }


def _routing_report(pending_cases, pending_case_id=None):
    selection = classify_b3_review_selection_v1(
        pending_cases=pending_cases,
        pending_case_id=pending_case_id,
    )
    selection.pop("schema_version")
    selection.pop("selection_hash")
    body = {
        "schema_version": "literary_b3_review_routing_report_v1",
        "chapter_id": "probe_chapter",
        "source_b3_root": "synthetic",
        "source_b3_tree_hash": "1" * 64,
        "source_b3_artifact_path": "chapter_temporal_artifact.json",
        "source_b3_artifact_hash": "2" * 64,
        **selection,
    }
    return {**body, "routing_report_hash": canonical_hash(body)}


def _probe_plan():
    return build_probe_plan_v1(
        probe_run_id="synthetic_b3_temporal_auditor_probe",
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
        packet = synthetic_b3_temporal_review_packet_v1()
        payload = model_facing_probe_payload_v1(
            self.plan, synthetic_keep_pending_response_v1(packet)
        )
        body = canonical_json(
            {
                "id": "synthetic-b3-temporal-auditor-probe",
                "model": "gpt-5.4",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": canonical_json(payload)},
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
            request_id="synthetic-b3-temporal-auditor-probe",
        )


def test_keep_pending_stays_non_authoritative() -> None:
    packet = synthetic_b3_temporal_review_packet_v1()
    decision = validate_b3_temporal_audit_response_v1(
        packet=packet, response=synthetic_keep_pending_response_v1(packet)
    )
    overlay = build_b3_temporal_review_overlay_v1(
        packet=packet, decision=decision
    )
    assert overlay["confirmed_state_rows"] == []
    assert overlay["retained_pending_case_ids"] == [
        packet["pending_cases"][0]["pending_case_id"]
    ]
    assert verify_b3_temporal_review_overlay_v1(overlay, packet=packet) == overlay


def test_temporal_review_route_uses_the_state_auditor_without_relabelling() -> None:
    packet = synthetic_b3_temporal_review_packet_v1(review_route="temporal_review")
    rendered = render_b3_temporal_audit_request_v1(packet)
    decision = validate_b3_temporal_audit_response_v1(
        packet=packet, response=synthetic_keep_pending_response_v1(packet)
    )
    overlay = build_b3_temporal_review_overlay_v1(
        packet=packet, decision=decision
    )

    assert rendered.packet["pending_cases"][0]["review_route"] == "temporal_review"
    assert "temporal_review asks whether" in rendered.messages[0]["content"]
    assert overlay["retained_pending_case_ids"] == [
        packet["pending_cases"][0]["pending_case_id"]
    ]


def test_every_b3_review_route_has_an_explicit_destination() -> None:
    assert set(REVIEW_ROUTE_DESTINATIONS) == {
        "inherited_identity_block",
        "identity_review",
        "stable_claim_review",
        "temporal_review",
    }
    assert STATE_REVIEW_ROUTES == {"stable_claim_review", "temporal_review"}
    assert (
        REVIEW_ROUTE_DESTINATIONS["identity_review"]["lifecycle_state"]
        == "parked_pending_adapter"
    )
    assert (
        REVIEW_ROUTE_DESTINATIONS["inherited_identity_block"][
            "implementation_status"
        ]
        == "inherited_holding_no_consumer"
    )


def test_temporal_case_is_selected_when_it_is_the_only_state_review() -> None:
    selection = classify_b3_review_selection_v1(
        pending_cases=[_pending_case("case_temporal", "temporal_review")]
    )

    assert selection["status"] == "ready"
    assert selection["selected_pending_case_id"] == "case_temporal"
    assert selection["selected_review_route"] == "temporal_review"
    assert selection["provider_call_allowed"] is True


def test_identity_only_queue_is_a_reportable_no_match() -> None:
    selection = classify_b3_review_selection_v1(
        pending_cases=[_pending_case("case_identity", "identity_review")]
    )

    assert selection["status"] == "no_matching_cases"
    assert selection["provider_call_allowed"] is False
    assert selection["pending_case_ids_by_route"]["identity_review"] == [
        "case_identity"
    ]
    identity_destination = next(
        row
        for row in selection["route_destinations"]
        if row["review_route"] == "identity_review"
    )
    assert identity_destination["implementation_status"] == "adapter_required"


def test_explicit_identity_case_reports_the_unserved_route() -> None:
    selection = classify_b3_review_selection_v1(
        pending_cases=[_pending_case("case_identity", "identity_review")],
        pending_case_id="case_identity",
    )

    assert selection["status"] == "route_not_supported"
    assert selection["selected_review_route"] == "identity_review"
    assert selection["selected_destination"]["destination_id"] == (
        "literary.audit.cross_chapter_identity"
    )
    assert "does not serve identity_review" in selection["reason"]


def test_multiple_state_cases_require_an_explicit_selection() -> None:
    selection = classify_b3_review_selection_v1(
        pending_cases=[
            _pending_case("case_stable", "stable_claim_review"),
            _pending_case("case_temporal", "temporal_review"),
        ]
    )

    assert selection["status"] == "selection_required"
    assert selection["selected_pending_case_id"] is None
    assert selection["provider_call_allowed"] is False


def test_unknown_pending_case_id_is_distinct_from_a_clean_no_match() -> None:
    selection = classify_b3_review_selection_v1(
        pending_cases=[_pending_case("case_temporal", "temporal_review")],
        pending_case_id="missing",
    )

    assert selection["status"] == "pending_case_not_found"
    assert selection["provider_call_allowed"] is False


def test_unknown_review_route_fails_closed() -> None:
    with pytest.raises(B3TemporalAuditorError, match="unknown B3 review route"):
        classify_b3_review_selection_v1(
            pending_cases=[_pending_case("case_unknown", "unknown_review")]
        )


@pytest.mark.parametrize(
    ("pending_case_id", "expected_status", "expected_exit_code"),
    [
        (None, "no_matching_cases", 0),
        ("case_identity", "route_not_supported", 3),
    ],
)
def test_unserved_route_writes_a_no_call_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pending_case_id: str | None,
    expected_status: str,
    expected_exit_code: int,
) -> None:
    routing = _routing_report(
        [_pending_case("case_identity", "identity_review")],
        pending_case_id=pending_case_id,
    )
    monkeypatch.setattr(runner, "_clean_head", lambda: "a" * 40)
    monkeypatch.setattr(
        runner, "build_b3_review_routing_report_v1", lambda **_kwargs: routing
    )
    monkeypatch.setattr(
        runner,
        "_credential",
        lambda *_args, **_kwargs: pytest.fail("routing no-op read a credential"),
    )
    output_root = tmp_path / "routing-only"

    argv = [
        "audit",
        "--b3-root",
        str(tmp_path / "unused-b3"),
        "--output-root",
        str(output_root),
        "--capability-root",
        str(tmp_path / "unused-capability"),
        "--run-id",
        "routing_only",
        "--attempt-run-id",
        "routing_only_attempt",
    ]
    if pending_case_id is not None:
        argv.extend(["--pending-case-id", pending_case_id])

    exit_code = runner.main(argv)

    report = json.loads((output_root / "audit_report.json").read_text("utf-8"))
    assert exit_code == expected_exit_code
    assert report["status"] == expected_status
    assert report["provider_called"] is False
    assert report["credential_read_performed"] is False
    assert report["capability_evidence_read_performed"] is False
    assert report["pending_case_ids_by_route"]["identity_review"] == [
        "case_identity"
    ]
    assert not Path(f"{output_root}-shared").exists()


def test_wrong_chapter_echo_keeps_pending_audit_decision() -> None:
    packet = synthetic_b3_temporal_review_packet_v1()
    response = synthetic_keep_pending_response_v1(packet)
    response["chapter_id"] = "copied_example_chapter"

    decision = validate_b3_temporal_audit_response_v1(
        packet=packet, response=response
    )

    assert decision["chapter_id"] == packet["chapter_id"]
    assert len(decision["case_decisions"]) == 1
    assert decision["response_normalization_notes"][0]["field"] == "chapter_id"
    overlay = build_b3_temporal_review_overlay_v1(
        packet=packet, decision=decision
    )
    assert verify_b3_temporal_review_overlay_v1(overlay, packet=packet) == overlay


def test_confirm_state_is_model_supplied_and_grounded() -> None:
    packet = synthetic_b3_temporal_review_packet_v1()
    case_id = packet["pending_cases"][0]["pending_case_id"]
    raw = {
        "schema_version": "literary_b3_temporal_review_response_v1",
        "chapter_id": "probe_chapter",
        "case_decisions": [
            {
                "pending_case_id": case_id,
                "disposition": "confirm_state",
                "resolved_action": {
                    "operation": "open_state",
                    "state_domain": "ownership",
                    "subject_referent_refs": ["probe_owner"],
                    "counterpart_referent_refs": ["probe_place"],
                    "state_value": "owns",
                    "event_status": "occurred",
                    "temporal_position": "current_progression",
                    "source_event_ids": ["probe_event"],
                    "source_turn_ids": [],
                    "source_block_ids": ["probe_block"],
                    "frame_segment_ids": ["probe_frame"],
                    "reason": "The supplied statement is accepted as a durable claim.",
                },
                "cited_source_block_ids": ["probe_block"],
                "reason": "The evidence is sufficient for this synthetic decision.",
                "pending_reason_code": None,
            }
        ],
    }
    overlay = build_b3_temporal_review_overlay_v1(packet=packet, decision=raw)
    assert len(overlay["confirmed_state_rows"]) == 1
    assert overlay["confirmed_state_rows"][0]["authority_status"] == "effective"
    assert overlay["identity_mutation_performed"] is False


def test_confirm_can_cite_supplied_context_beyond_action_anchor() -> None:
    assert "cited_source_block_ids" in SYSTEM_PROMPT
    assert "not direct anchors of the final action" in SYSTEM_PROMPT
    packet = synthetic_b3_temporal_review_packet_v1()
    packet_body = deepcopy(packet)
    packet_body.pop("packet_hash")
    packet_body["source_blocks"].append(
        {"block_id": "probe_neighbor", "text": "A neighboring context block."}
    )
    event = packet_body["component"]["salient_events"][0]
    event["source_block_ids"].append("probe_neighbor")
    packet_body["frame_packets"][0]["frame"]["relevant_block_ids"].append(
        "probe_neighbor"
    )
    packet = {**packet_body, "packet_hash": canonical_hash(packet_body)}
    raw = {
        "schema_version": "literary_b3_temporal_review_response_v1",
        "chapter_id": "probe_chapter",
        "case_decisions": [
            {
                "pending_case_id": packet["pending_cases"][0]["pending_case_id"],
                "disposition": "confirm_state",
                "resolved_action": {
                    "operation": "open_state",
                    "state_domain": "ownership",
                    "subject_referent_refs": ["probe_owner"],
                    "counterpart_referent_refs": ["probe_place"],
                    "state_value": "owns",
                    "event_status": "occurred",
                    "temporal_position": "current_progression",
                    "source_event_ids": ["probe_event"],
                    "source_turn_ids": [],
                    "source_block_ids": ["probe_block"],
                    "frame_segment_ids": ["probe_frame"],
                    "reason": "The neighboring block was consulted for comparison.",
                },
                "cited_source_block_ids": ["probe_block", "probe_neighbor"],
                "reason": "The direct statement is in the first block and the neighboring block was checked.",
                "pending_reason_code": None,
            }
        ],
    }
    overlay = build_b3_temporal_review_overlay_v1(packet=packet, decision=raw)
    assert len(overlay["confirmed_state_rows"]) == 1


def test_refer_identity_requires_a_null_pending_reason_code() -> None:
    assert "pending_reason_code` is non-null only for keep_pending" in SYSTEM_PROMPT
    packet = synthetic_b3_temporal_review_packet_v1()
    raw = synthetic_keep_pending_response_v1(packet)
    row = raw["case_decisions"][0]
    row["disposition"] = "refer_identity"

    with pytest.raises(B3TemporalAuditorError, match="schema failure"):
        validate_b3_temporal_audit_response_v1(packet=packet, response=raw)

    row["pending_reason_code"] = None
    decision = validate_b3_temporal_audit_response_v1(
        packet=packet,
        response=raw,
    )
    overlay = build_b3_temporal_review_overlay_v1(
        packet=packet,
        decision=decision,
    )
    assert overlay["identity_referral_case_ids"] == [
        packet["pending_cases"][0]["pending_case_id"]
    ]
    assert overlay["retained_pending_case_ids"] == [
        packet["pending_cases"][0]["pending_case_id"]
    ]


def test_non_pending_decision_omitted_reason_code_gets_forced_null() -> None:
    packet = synthetic_b3_temporal_review_packet_v1()
    raw = synthetic_keep_pending_response_v1(packet)
    row = raw["case_decisions"][0]
    row["disposition"] = "reject_claim"
    row["resolved_action"] = None
    row.pop("pending_reason_code")

    decision = validate_b3_temporal_audit_response_v1(
        packet=packet,
        response=raw,
    )

    assert decision["case_decisions"][0]["pending_reason_code"] is None


def test_keep_pending_omitted_reason_code_still_fails_closed() -> None:
    packet = synthetic_b3_temporal_review_packet_v1()
    raw = synthetic_keep_pending_response_v1(packet)
    raw["case_decisions"][0].pop("pending_reason_code")

    with pytest.raises(B3TemporalAuditorError, match="schema failure"):
        validate_b3_temporal_audit_response_v1(packet=packet, response=raw)


def test_foreign_source_reference_fails_closed() -> None:
    packet = synthetic_b3_temporal_review_packet_v1()
    raw = synthetic_keep_pending_response_v1(packet)
    raw["case_decisions"][0]["cited_source_block_ids"] = ["foreign"]
    with pytest.raises(B3TemporalAuditorError, match="foreign source"):
        validate_b3_temporal_audit_response_v1(packet=packet, response=raw)


def test_decision_hash_tamper_fails_closed() -> None:
    packet = synthetic_b3_temporal_review_packet_v1()
    decision = validate_b3_temporal_audit_response_v1(
        packet=packet, response=synthetic_keep_pending_response_v1(packet)
    )
    tampered = deepcopy(decision)
    tampered["decision_hash"] = "0" * 64
    with pytest.raises(B3TemporalAuditorError, match="decision hash"):
        build_b3_temporal_review_overlay_v1(packet=packet, decision=tampered)


def test_modelapi_profiles_are_closed_and_single_call() -> None:
    probe = load_probe_profile_v1()
    assert probe["profile_revision"] == (
        "modelapi_gpt54_b3_temporal_auditor_json_object_v4"
    )
    assert probe["limits"]["max_calls"] == 1
    assert probe["safety"]["fallback_enabled"] is False
    assert validator_ref_v1()["revision"] == "v1_3"
    runtime = load_literary_shared_runtime_profile_v2(
        RUNTIME_PROFILE_PATH,
        expected_role_ids={"literary.audit.b3_stable_claim"},
    )
    role = runtime.role_presets["literary.audit.b3_stable_claim"]
    assert runtime.profile_revision == (
        "modelapi_gpt54_b3_state_claim_auditor_prompt_validated_v4"
    )
    assert role.requested_model_id == "gpt-5.4"
    assert role.limits["max_calls"] == 1
    assert runtime.source_binding_for(role.role_id)["authority_class"] == "third_party"


def test_modelapi_fake_probe_qualifies_exact_local_contract(tmp_path: Path) -> None:
    plan = _probe_plan()
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
