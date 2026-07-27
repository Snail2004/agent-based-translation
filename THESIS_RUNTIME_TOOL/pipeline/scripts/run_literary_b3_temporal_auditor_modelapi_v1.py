from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from pipeline.llm_backend import (
    ApplicationResponseCache,
    ContentAddressedArtifactStore,
    MappingCredentialProvider,
    PhysicalQuotaScheduler,
    SharedLlmAttemptLedger,
    SharedLlmBackend,
    SharedLlmCapabilityProbe,
    UrllibTransportSender,
    credential_commitment,
    validate_capability_evidence,
)
from pipeline.literary.b3_temporal_auditor_v1 import (
    B3_TEMPORAL_AUDITOR_MODEL_REF_FIELDS_V1,
    ROLE_ID,
    b3_temporal_audit_response_schema_v1,
    build_b3_review_routing_report_v1,
    build_b3_temporal_review_overlay_v1,
    load_b3_temporal_review_packet_v1,
    render_b3_temporal_audit_request_v1,
)
from pipeline.literary.chapter_cycle_shared_runtime_v1 import (
    LiterarySharedRunnerBindingsV1,
    capability_binding_key,
)
from pipeline.literary.checkpoint import canonical_hash
from pipeline.literary.model_ref_v1 import MODEL_REF_MODE_CLASSIFIED_V1
from pipeline.literary.modelapi_b3_temporal_auditor_capability_probe_v1 import (
    RUNTIME_PROFILE_PATH,
    build_probe_plan_v1,
    execute_probe_once_v1,
    validator_ref_v1,
)
from pipeline.literary.shared_llm_adapter_v1 import LiterarySharedLlmAttemptAdapter
from pipeline.literary.shared_runtime_profile_v2 import (
    load_literary_shared_runtime_profile_v2,
)
from pipeline.scripts.run_literary_b1_enrich_v1 import (
    DEFAULT_SCHEDULER_ROOT,
    _clean_head,
    _credential,
    _fresh_roots,
    _now,
    _read,
    _usage,
    _write,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="B3 Stable/Temporal Auditor ModelAPI probe and one-case audit"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    probe = commands.add_parser("probe")
    probe.add_argument("--output-root", type=Path, required=True)
    probe.add_argument("--probe-run-id", required=True)
    _credential_args(probe)
    probe.add_argument("--scheduler-root", type=Path, default=DEFAULT_SCHEDULER_ROOT)

    audit = commands.add_parser("audit")
    audit.add_argument("--b3-root", type=Path, required=True)
    audit.add_argument("--pending-case-id")
    audit.add_argument("--output-root", type=Path, required=True)
    audit.add_argument("--capability-root", type=Path, required=True)
    audit.add_argument("--run-id", required=True)
    audit.add_argument("--attempt-run-id", required=True)
    audit.add_argument("--runtime-profile", type=Path)
    _credential_args(audit)
    audit.add_argument("--scheduler-root", type=Path, default=DEFAULT_SCHEDULER_ROOT)
    return parser


def _credential_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--credential-env", default="MODELAPI_API_KEY")
    parser.add_argument("--credential-file", type=Path)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    head = _clean_head()
    if args.command == "probe":
        secret = _credential(args.credential_env, args.credential_file)
        commitment = credential_commitment(secret)
        report = _run_probe(
            output_root=args.output_root,
            probe_run_id=args.probe_run_id,
            secret=secret,
            commitment=commitment,
            scheduler_root=args.scheduler_root,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["status"] == "qualified" else 2
    routing_report = build_b3_review_routing_report_v1(
        b3_root=args.b3_root,
        pending_case_id=args.pending_case_id,
    )
    if routing_report["status"] != "ready":
        report = _write_routing_only_report(
            output_root=args.output_root,
            routing_report=routing_report,
            current_head=head,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if routing_report["status"] == "no_matching_cases" else 3
    secret = _credential(args.credential_env, args.credential_file)
    commitment = credential_commitment(secret)
    report = _run_audit(
        b3_root=args.b3_root,
        pending_case_id=routing_report["selected_pending_case_id"],
        output_root=args.output_root,
        capability_root=args.capability_root,
        run_id=args.run_id,
        attempt_run_id=args.attempt_run_id,
        secret=secret,
        commitment=commitment,
        scheduler_root=args.scheduler_root,
        current_head=head,
        routing_report=routing_report,
        runtime_profile_path=args.runtime_profile,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def _write_routing_only_report(
    *, output_root: Path, routing_report: Mapping[str, Any], current_head: str
) -> dict[str, Any]:
    output, _shared = _fresh_roots(output_root)
    output.mkdir(parents=True, exist_ok=False)
    _write(output / "review_routing_report.json", routing_report)
    body = {
        "schema_version": "literary_b3_temporal_auditor_report_v1",
        "status": routing_report["status"],
        "reason": routing_report["reason"],
        "chapter_id": routing_report["chapter_id"],
        "git_head": current_head,
        "routing_report_hash": routing_report["routing_report_hash"],
        "requested_pending_case_id": routing_report["requested_pending_case_id"],
        "selected_pending_case_id": routing_report["selected_pending_case_id"],
        "selected_review_route": routing_report["selected_review_route"],
        "pending_case_ids_by_route": routing_report["pending_case_ids_by_route"],
        "route_destinations": routing_report["route_destinations"],
        "provider_called": False,
        "credential_read_performed": False,
        "capability_evidence_read_performed": False,
        "shared_output_created": False,
        "registry_mutation_performed": False,
        "production_publish_performed": False,
        "mandatory_stop_observed": True,
    }
    report = {**body, "report_hash": canonical_hash(body)}
    _write(output / "audit_report.json", report)
    return report


def _run_probe(
    *, output_root: Path, probe_run_id: str, secret: str, commitment: str,
    scheduler_root: Path,
) -> dict[str, Any]:
    output, shared = _fresh_roots(output_root)
    plan = build_probe_plan_v1(
        probe_run_id=probe_run_id,
        credential_commitment_sha256=commitment,
        issued_at_utc=_now(),
    )
    output.mkdir(parents=True, exist_ok=False)
    _write(output / "probe_seal.json", plan.seal)
    _write(output / "request.json", plan.request)
    _write(output / "transport_request.json", plan.request_body)
    shared.mkdir(parents=True, exist_ok=False)
    probe = SharedLlmCapabilityProbe(
        credential_provider=MappingCredentialProvider(
            {plan.source["credential_ref"]: secret}
        ),
        scheduler=PhysicalQuotaScheduler(scheduler_root),
        ledger=SharedLlmAttemptLedger(shared / "attempt_ledger.sqlite3"),
        artifact_store=ContentAddressedArtifactStore(shared / "artifacts"),
        sender=UrllibTransportSender(),
        implementation_binding=plan.implementation_binding,
    )
    result = execute_probe_once_v1(probe=probe, plan=plan)
    _write(output / "probe_receipt.json", result["receipt"])
    _write(output / "capability_evidence.json", result["capability_evidence"])
    body = {
        "schema_version": "literary_b3_temporal_auditor_probe_report_v1",
        "status": result["status"],
        "provider_called": result["provider_called"],
        "probe_seal_sha256": result["probe_seal_sha256"],
        "receipt_sha256": result["receipt"]["receipt_sha256"],
        "evidence_sha256": result["capability_evidence"]["evidence_sha256"],
        "usage": _usage(result["receipt"]),
        "failure": result["receipt"].get("failure"),
        "mandatory_stop_observed": True,
        "normal_output_created": False,
        "production_publish_performed": False,
        "source_id": plan.source["source_id"],
    }
    report = {**body, "report_hash": canonical_hash(body)}
    _write(output / "probe_report.json", report)
    return report


def _run_audit(
    *, b3_root: Path, pending_case_id: str | None, output_root: Path,
    capability_root: Path, run_id: str, attempt_run_id: str, secret: str,
    commitment: str, scheduler_root: Path, current_head: str,
    routing_report: Mapping[str, Any],
    runtime_profile_path: Path | None = None,
) -> dict[str, Any]:
    if (
        routing_report.get("status") != "ready"
        or routing_report.get("selected_pending_case_id") != pending_case_id
        or routing_report.get("provider_call_allowed") is not True
    ):
        raise SystemExit("B3 temporal auditor routing report is not call-ready")
    evidence = validate_capability_evidence(
        _read(Path(capability_root) / "capability_evidence.json")
    )
    if evidence["verdict"] != "qualified":
        raise SystemExit("B3 temporal auditor capability is not qualified")
    runtime = load_literary_shared_runtime_profile_v2(
        Path(runtime_profile_path or RUNTIME_PROFILE_PATH),
        expected_role_ids={ROLE_ID},
    )
    source_binding = dict(runtime.source_binding_for(ROLE_ID))
    source = {
        "schema_version": "api_source_v1",
        "source_id": source_binding["source_id"],
        "source_revision": source_binding["source_revision"],
        "source_class": source_binding["source_class"],
        "adapter_id": source_binding["adapter_id"],
        "protocol": source_binding["protocol"],
        "route_id": source_binding["route_id"],
        "endpoint_class": source_binding["endpoint_class"],
        "base_url": source_binding["base_url"],
        "credential_ref": source_binding["credential_ref"],
        "credential_commitment": commitment,
        "physical_quota_bucket_id": source_binding["physical_quota_bucket_id"],
        "enabled": True,
    }
    packet = load_b3_temporal_review_packet_v1(
        b3_root=b3_root, pending_case_id=pending_case_id
    )
    rendered = render_b3_temporal_audit_request_v1(packet)
    request = {
        "messages": [dict(row) for row in rendered.messages],
        "response_schema": rendered.response_schema,
        "request_fingerprint": rendered.request_fingerprint,
    }
    output, shared = _fresh_roots(output_root)
    output.mkdir(parents=True, exist_ok=False)
    _write(
        output / "run_seal.json",
        {
            "schema_version": "literary_b3_temporal_auditor_run_seal_v1",
            "git_head": current_head,
            "run_id": run_id,
            "attempt_run_id": attempt_run_id,
            "chapter_id": packet["chapter_id"],
            "pending_case_ids": [
                row["pending_case_id"] for row in packet["pending_cases"]
            ],
            "review_route": packet["pending_cases"][0]["review_route"],
            "routing_report_hash": routing_report["routing_report_hash"],
            "packet_hash": packet["packet_hash"],
            "source_b3_artifact_hash": packet["source_b3_artifact_hash"],
            "runtime_profile_sha256": runtime.profile_sha256,
            "capability_evidence_sha256": evidence["evidence_sha256"],
            "provider_fallback_allowed": False,
            "production_publish_allowed": False,
        },
    )
    _write(output / "review_routing_report.json", routing_report)
    _write(output / "review_packet.json", packet)
    _write(
        output / "request.json",
        {
            "request_fingerprint": rendered.request_fingerprint,
            "messages": request["messages"],
            "response_schema": request["response_schema"],
        },
    )
    shared.mkdir(parents=True, exist_ok=False)
    store = ContentAddressedArtifactStore(shared / "artifacts")
    backend = SharedLlmBackend(
        credential_provider=MappingCredentialProvider(
            {source["credential_ref"]: secret}
        ),
        scheduler=PhysicalQuotaScheduler(scheduler_root),
        ledger=SharedLlmAttemptLedger(shared / "attempt_ledger.sqlite3"),
        response_cache=ApplicationResponseCache(
            index_path=shared / "response_cache.sqlite3", artifact_store=store
        ),
        sender=UrllibTransportSender(),
    )
    bindings = LiterarySharedRunnerBindingsV1(
        adapter=LiterarySharedLlmAttemptAdapter(backend=backend),
        api_source=None,
        capabilities={
            capability_binding_key(
                ROLE_ID, b3_temporal_audit_response_schema_v1()
            ): evidence
        },
        run_id=run_id,
        attempt_run_id=attempt_run_id,
        structured_output=None,
        runtime_profile=runtime,
        api_sources_by_alias={source_binding["source_alias"]: source},
    )

    def validate(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return build_b3_temporal_review_overlay_v1(
            packet=packet, decision=payload
        )

    result = bindings.execute_accepted_request(
        role_id=ROLE_ID,
        stage_id="literary_b3_state_claim_auditor",
        logical_request_id="literary_b3_audit_" + packet["packet_hash"][:24],
        request=request,
        schema_name="literary_b3_temporal_review_response_v1",
        semantic_validator=validate,
        validator_ref=validator_ref_v1(),
        application_contract_id="literary.b3.state_claim_auditor.application",
        application_contract_revision="v1",
        output_dir=output,
        model_reference_mode=MODEL_REF_MODE_CLASSIFIED_V1,
        model_reference_fields=B3_TEMPORAL_AUDITOR_MODEL_REF_FIELDS_V1,
        additional_input_bindings=(
            {
                "name": "b3_review_routing_report",
                "sha256": routing_report["routing_report_hash"],
            },
            {"name": "b3_review_packet", "sha256": packet["packet_hash"]},
            {
                "name": "source_b3_artifact",
                "sha256": packet["source_b3_artifact_hash"],
            },
        ),
    )
    _write(output / "model_response.json", result.response_payload)
    _write(output / "temporal_review_overlay.json", result.semantic_payload)
    overlay = result.semantic_payload
    body = {
        "schema_version": "literary_b3_temporal_auditor_report_v1",
        "status": result.status,
        "chapter_id": packet["chapter_id"],
        "git_head": current_head,
        "packet_hash": packet["packet_hash"],
        "routing_report_hash": routing_report["routing_report_hash"],
        "review_route": packet["pending_cases"][0]["review_route"],
        "overlay_hash": overlay["overlay_hash"],
        "resolved_pending_case_ids": overlay["resolved_pending_case_ids"],
        "retained_pending_case_ids": overlay["retained_pending_case_ids"],
        "identity_referral_case_ids": overlay["identity_referral_case_ids"],
        "confirmed_state_count": len(overlay["confirmed_state_rows"]),
        "usage": dict(result.usage) if result.usage is not None else None,
        "provider_called": result.provider_called,
        "registry_mutation_performed": False,
        "production_publish_performed": False,
        "mandatory_stop_observed": True,
        "source_id": source["source_id"],
    }
    report = {**body, "report_hash": canonical_hash(body)}
    _write(output / "audit_report.json", report)
    return report


if __name__ == "__main__":
    raise SystemExit(main())
