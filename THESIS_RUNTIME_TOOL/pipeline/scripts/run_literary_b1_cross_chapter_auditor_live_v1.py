"""Probe and run the current B1 cross-chapter hearing consumer.

This runner consumes only the prepared requests emitted by
``run_literary_b1_cross_chapter_audits_v1``.  It does not use the retired M4f
identity index, does not mutate registries, and never calls a waiting component.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import shutil
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
from pipeline.literary.b1_chapter_registry_writer_v1 import (
    verify_b1_chapter_registry_v1,
)
from pipeline.literary.b1_cross_chapter_auditor_live_v1 import (
    B1CrossChapterAuditorLiveError,
    CROSS_CHAPTER_MODEL_REF_FIELDS_V1,
    IDENTITY_ROLE_ID,
    IDENTITY_ROUTE,
    ROLE_ID_BY_ROUTE,
    STABLE_CLAIM_ROLE_ID,
    STABLE_CLAIM_ROUTE,
    build_live_hearing_plan_v1,
    collect_source_blocks_v1,
    load_prepared_requests_v1,
    make_hearing_semantic_validator_v1,
    response_schema_for_route_v1,
)
from pipeline.literary.chapter_cycle_shared_runtime_v1 import (
    LiterarySharedRunnerBindingsV1,
    capability_binding_key,
)
from pipeline.literary.checkpoint import canonical_hash
from pipeline.literary.model_ref_v1 import MODEL_REF_MODE_CLASSIFIED_V1
from pipeline.literary.modelapi_b1_cross_chapter_auditor_capability_probe_v1 import (
    DESIGN_DOC,
    RUNTIME_PROFILE_PATH,
    build_probe_plan_v1,
    execute_probe_once_v1,
)
from pipeline.literary.shared_llm_adapter_v1 import (
    LiterarySharedLlmAdapterError,
    LiterarySharedLlmAttemptAdapter,
)
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


ROUTE_BY_PROBE_NAME = {
    "identity": IDENTITY_ROUTE,
    "stable_claim": STABLE_CLAIM_ROUTE,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    probe = commands.add_parser("probe")
    probe.add_argument("--route", choices=sorted(ROUTE_BY_PROBE_NAME), required=True)
    probe.add_argument("--output-root", type=Path, required=True)
    probe.add_argument("--probe-run-id", required=True)
    _credential_args(probe)
    probe.add_argument("--scheduler-root", type=Path, default=DEFAULT_SCHEDULER_ROOT)

    run = commands.add_parser("run")
    run.add_argument("--output-root", type=Path, required=True)
    run.add_argument("--prepared-dir", type=Path, required=True)
    run.add_argument("--queue", type=Path, required=True)
    run.add_argument("--registry", type=Path, required=True)
    run.add_argument(
        "--chapter",
        action="append",
        required=True,
        type=Path,
        help="chapter source JSON with blocks[]; repeat for prior chapters",
    )
    run.add_argument("--design-doc", type=Path, default=DESIGN_DOC)
    run.add_argument("--identity-capability-root", type=Path)
    run.add_argument("--stable-claim-capability-root", type=Path)
    run.add_argument("--run-id", required=True)
    run.add_argument("--attempt-run-id", required=True)
    run.add_argument(
        "--recovery-root",
        type=Path,
        help=(
            "archived incomplete output from the same sealed hearing plan; "
            "validated components are reused and only missing components are called"
        ),
    )
    run.add_argument("--runtime-profile", type=Path)
    run.add_argument(
        "--batch-index",
        type=int,
        help=(
            "one-based deterministic batch to execute when ready hearings exceed "
            "a sealed role call cap"
        ),
    )
    _credential_args(run)
    run.add_argument("--scheduler-root", type=Path, default=DEFAULT_SCHEDULER_ROOT)
    return parser


def _credential_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--credential-env", default="OPENAI_API_KEY")
    parser.add_argument("--credential-file", type=Path)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    current_head = _clean_head()
    if args.command == "probe":
        secret = _credential(args.credential_env, args.credential_file)
        report = _run_probe(
            route=ROUTE_BY_PROBE_NAME[args.route],
            output_root=args.output_root,
            probe_run_id=args.probe_run_id,
            secret=secret,
            commitment=credential_commitment(secret),
            scheduler_root=args.scheduler_root,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["status"] == "qualified" else 2
    report = _run_hearings(
        output_root=args.output_root,
        prepared_dir=args.prepared_dir,
        queue_path=args.queue,
        registry_path=args.registry,
        chapter_paths=args.chapter,
        design_doc=args.design_doc,
        capability_roots={
            IDENTITY_ROUTE: args.identity_capability_root,
            STABLE_CLAIM_ROUTE: args.stable_claim_capability_root,
        },
        run_id=args.run_id,
        attempt_run_id=args.attempt_run_id,
        recovery_root=args.recovery_root,
        batch_index=args.batch_index,
        credential_env=args.credential_env,
        credential_file=args.credential_file,
        scheduler_root=args.scheduler_root,
        current_head=current_head,
        runtime_profile_path=args.runtime_profile,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["selected_batch_complete"] else 2


def _run_probe(
    *,
    route: str,
    output_root: Path,
    probe_run_id: str,
    secret: str,
    commitment: str,
    scheduler_root: Path,
    sender: Any | None = None,
) -> dict[str, Any]:
    output, shared = _fresh_roots(output_root)
    plan = build_probe_plan_v1(
        route=route,
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
        sender=sender or UrllibTransportSender(),
        implementation_binding=plan.implementation_binding,
    )
    result = execute_probe_once_v1(probe=probe, plan=plan)
    _write(output / "probe_receipt.json", result["receipt"])
    _write(output / "capability_evidence.json", result["capability_evidence"])
    body = {
        "schema_version": "literary_b1_cross_chapter_auditor_probe_report_v1",
        "status": result["status"],
        "review_route": route,
        "role_id": plan.role_id,
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


def _run_hearings(
    *,
    output_root: Path,
    prepared_dir: Path,
    queue_path: Path,
    registry_path: Path,
    chapter_paths: Sequence[Path],
    design_doc: Path,
    capability_roots: Mapping[str, Path | None],
    run_id: str,
    attempt_run_id: str,
    recovery_root: Path | None = None,
    batch_index: int | None = None,
    credential_env: str,
    credential_file: Path | None,
    scheduler_root: Path,
    current_head: str,
    sender: Any | None = None,
    runtime_profile_path: Path | None = None,
) -> dict[str, Any]:
    runtime = load_literary_shared_runtime_profile_v2(
        Path(runtime_profile_path or RUNTIME_PROFILE_PATH),
        expected_role_ids={IDENTITY_ROLE_ID, STABLE_CLAIM_ROLE_ID},
    )
    queue = _read(queue_path)
    registry = _read(registry_path)
    verify_b1_chapter_registry_v1(registry)
    prepared = load_prepared_requests_v1(prepared_dir)
    source_blocks = collect_source_blocks_v1(chapter_paths)
    plan = build_live_hearing_plan_v1(
        queue=queue,
        registry=registry,
        prepared_requests=prepared,
        source_blocks=source_blocks,
        design_doc=design_doc,
        runtime=runtime,
        batch_index=batch_index,
    )
    output, shared = _fresh_roots(output_root)
    output.mkdir(parents=True, exist_ok=False)
    _write(output / "preflight_plan.json", plan.to_payload())

    routes_present = {row.route for row in plan.hearings}
    evidence_by_route: dict[str, Mapping[str, Any]] = {}
    for route in sorted(routes_present):
        root = capability_roots.get(route)
        if root is None:
            failure = _preflight_failure(
                current_head=current_head,
                plan=plan,
                message=f"qualified capability root is required for {route}",
            )
            _write(output / "preflight_failure.json", failure)
            return failure
        evidence = validate_capability_evidence(
            _read(Path(root) / "capability_evidence.json")
        )
        if evidence["verdict"] != "qualified":
            failure = _preflight_failure(
                current_head=current_head,
                plan=plan,
                message=f"capability evidence is not qualified for {route}",
            )
            _write(output / "preflight_failure.json", failure)
            return failure
        evidence_by_route[route] = evidence

    secret = _credential(credential_env, credential_file)
    commitment = credential_commitment(secret)
    source = _source_record(runtime=runtime, commitment=commitment)
    run_seal = {
        "schema_version": "literary_b1_cross_chapter_auditor_run_seal_v1",
        "git_head": current_head,
        "run_id": run_id,
        "attempt_run_id": attempt_run_id,
        "chapter_id": queue.get("chapter_id"),
        "queue_hash": plan.queue_hash,
        "registry_hash": plan.registry_hash,
        "prepared_request_hashes": [
            canonical_hash(row.stored_request) for row in plan.hearings
        ],
        "live_request_fingerprints": [
            row.live_request["request_fingerprint"] for row in plan.hearings
        ],
        "plan_hash": plan.plan_hash,
        "batch_index": plan.batch_index,
        "batch_count": plan.batch_count,
        "deferred_ready_component_ids": list(
            plan.deferred_ready_component_ids
        ),
        "runtime_profile_sha256": runtime.profile_sha256,
        "capability_evidence_sha256_by_route": {
            route: evidence["evidence_sha256"]
            for route, evidence in evidence_by_route.items()
        },
        "source_id": source["source_id"],
        "source_revision": source["source_revision"],
        "physical_quota_bucket_id": source["physical_quota_bucket_id"],
        "provider_fallback_allowed": False,
        "semantic_retry_max": 0,
        "transport_retry_max": 0,
        "application_response_cache_enabled": False,
        "production_publish_allowed": False,
    }
    _write(output / "run_seal.json", run_seal)

    recovered = _load_recovered_components_v1(
        recovery_root=recovery_root,
        plan=plan,
    )
    recovered_ids = sorted(recovered)

    if not plan.hearings:
        report = _final_report(
            current_head=current_head,
            plan=plan,
            accepted=[],
            quarantined=[],
            usage_rows=[],
            source=source,
            recovered_component_ids=[],
        )
        _write_json_value(output / "validated_decisions.json", [])
        _write(output / "run_report.json", report)
        return report

    shared.mkdir(parents=True, exist_ok=False)
    store = ContentAddressedArtifactStore(shared / "artifacts")
    ledger = SharedLlmAttemptLedger(shared / "attempt_ledger.sqlite3")
    backend = SharedLlmBackend(
        credential_provider=MappingCredentialProvider(
            {source["credential_ref"]: secret}
        ),
        scheduler=PhysicalQuotaScheduler(scheduler_root),
        ledger=ledger,
        response_cache=ApplicationResponseCache(
            index_path=shared / "response_cache.sqlite3",
            artifact_store=store,
        ),
        sender=sender or UrllibTransportSender(),
    )
    capabilities = {
        capability_binding_key(
            ROLE_ID_BY_ROUTE[route], response_schema_for_route_v1(route)
        ): evidence
        for route, evidence in evidence_by_route.items()
    }
    bindings = LiterarySharedRunnerBindingsV1(
        adapter=LiterarySharedLlmAttemptAdapter(backend=backend),
        api_source=None,
        capabilities=capabilities,
        run_id=run_id,
        attempt_run_id=attempt_run_id,
        structured_output=None,
        runtime_profile=runtime,
        api_sources_by_alias={
            runtime.role_bindings[role_id].source_alias: source
            for role_id in {row.role_id for row in plan.hearings}
        },
    )

    accepted: list[dict[str, Any]] = [
        deepcopy(recovered[component_id]["decision"])
        for component_id in recovered_ids
    ]
    quarantined: list[dict[str, Any]] = []
    try:
        for index, hearing in enumerate(plan.hearings, start=1):
            component_dir = output / "components" / f"{index:03d}_{hearing.component_id}"
            recovered_row = recovered.get(hearing.component_id)
            if recovered_row is not None:
                shutil.copytree(recovered_row["component_dir"], component_dir)
                continue
            component_dir.mkdir(parents=True, exist_ok=False)
            _write(component_dir / "request.json", hearing.live_request)
            _write(
                component_dir / "token_preflight.json",
                hearing.token_preflight.to_payload(),
            )
            logical_request_id = _logical_request_id(
                run_id=run_id,
                plan_hash=plan.plan_hash,
                component_id=hearing.component_id,
                request_fingerprint=hearing.live_request["request_fingerprint"],
            )
            _write(
                component_dir / "execution_intent.json",
                {
                    "schema_version": "literary_b1_cross_chapter_execution_intent_v1",
                    "component_id": hearing.component_id,
                    "review_route": hearing.route,
                    "logical_request_id": logical_request_id,
                    "request_fingerprint": hearing.live_request[
                        "request_fingerprint"
                    ],
                    "plan_hash": plan.plan_hash,
                },
            )
            try:
                result = bindings.execute_accepted_request(
                    role_id=hearing.role_id,
                    stage_id="literary_b1_cross_chapter_auditor",
                    logical_request_id=logical_request_id,
                    request=hearing.live_request,
                    schema_name=hearing.schema_name,
                    semantic_validator=make_hearing_semantic_validator_v1(
                        component=hearing.component,
                        rendered_request=hearing.live_request,
                    ),
                    validator_ref=hearing.validator_ref,
                    application_contract_id=(
                        "literary.b1.cross_chapter_hearing.application"
                    ),
                    application_contract_revision="v1",
                    output_dir=component_dir,
                    model_reference_mode=MODEL_REF_MODE_CLASSIFIED_V1,
                    model_reference_fields=CROSS_CHAPTER_MODEL_REF_FIELDS_V1,
                    additional_input_bindings=(
                        {"name": "hearing_queue", "sha256": plan.queue_hash},
                        {"name": "chapter_registry", "sha256": plan.registry_hash},
                        {"name": "hearing_plan", "sha256": plan.plan_hash},
                        {
                            "name": "prepared_hearing",
                            "sha256": canonical_hash(hearing.stored_request),
                        },
                    ),
                )
            except LiterarySharedLlmAdapterError as exc:
                if not _is_quarantinable_semantic_failure(exc):
                    raise
                issue = {
                    "schema_version": "literary_b1_cross_chapter_quarantine_v1",
                    "component_id": hearing.component_id,
                    "review_route": hearing.route,
                    "state": "unreviewed",
                    "reason": "provider response failed model-reference or local semantic validation",
                    "failure_type": type(exc).__name__,
                    "provider_attempt_persisted": _logical_request_was_called(
                        ledger, logical_request_id
                    ),
                    "identity_authority_granted": False,
                    "claim_authority_granted": False,
                }
                _write(component_dir / "quarantine.json", issue)
                quarantined.append(issue)
                continue
            decision = dict(result.semantic_payload)
            _write(component_dir / "model_response.json", result.response_payload)
            _write(component_dir / "validated_decision.json", decision)
            report = {
                "schema_version": "literary_b1_cross_chapter_component_report_v1",
                "component_id": hearing.component_id,
                "review_route": hearing.route,
                "status": result.status,
                "provider_called": result.provider_called,
                "usage": dict(result.usage) if result.usage is not None else None,
                "decision_sha256": canonical_hash(decision),
                "identity_authority_granted": False,
                "claim_authority_granted": False,
            }
            _write(component_dir / "component_report.json", report)
            accepted.append(decision)
    except Exception as exc:
        failure = {
            "schema_version": "literary_b1_cross_chapter_auditor_failure_v1",
            "status": "halted_fail_closed",
            "plan_hash": plan.plan_hash,
            "accepted_component_ids": [row["component_id"] for row in accepted],
            "quarantined_component_ids": [
                row["component_id"] for row in quarantined
            ],
            "failure_type": type(exc).__name__,
            "failure_message": str(exc),
            "production_publish_performed": False,
        }
        _write(output / "run_failure.json", failure)
        raise

    _write_json_value(output / "validated_decisions.json", accepted)
    _write(
        output / "review_issues.json",
        {
            "schema_version": "literary_b1_cross_chapter_review_issues_v1",
            "issues": quarantined,
        },
    )
    usage_rows = ledger.list_records("usage")
    report = _final_report(
        current_head=current_head,
        plan=plan,
        accepted=accepted,
        quarantined=quarantined,
        usage_rows=usage_rows,
        source=source,
        recovered_component_ids=recovered_ids,
    )
    _write(output / "run_report.json", report)
    return report


def _source_record(*, runtime: Any, commitment: str) -> dict[str, Any]:
    binding = dict(runtime.source_binding_for(IDENTITY_ROLE_ID))
    stable = dict(runtime.source_binding_for(STABLE_CLAIM_ROLE_ID))
    if binding != stable:
        raise B1CrossChapterAuditorLiveError(
            "cross-chapter Auditor roles resolve to different physical sources"
        )
    return {
        "schema_version": "api_source_v1",
        "source_id": binding["source_id"],
        "source_revision": binding["source_revision"],
        "source_class": binding["source_class"],
        "adapter_id": binding["adapter_id"],
        "protocol": binding["protocol"],
        "route_id": binding["route_id"],
        "endpoint_class": binding["endpoint_class"],
        "base_url": binding["base_url"],
        "credential_ref": binding["credential_ref"],
        "credential_commitment": commitment,
        "physical_quota_bucket_id": binding["physical_quota_bucket_id"],
        "enabled": True,
    }


def _load_recovered_components_v1(
    *,
    recovery_root: Path | None,
    plan: Any,
) -> dict[str, dict[str, Any]]:
    if recovery_root is None:
        return {}
    root = Path(recovery_root).resolve()
    if not root.is_dir() or root.is_symlink():
        raise B1CrossChapterAuditorLiveError(
            "cross-chapter recovery root must be a real directory"
        )
    report = _read(root / "run_report.json")
    if (
        report.get("schema_version")
        != "literary_b1_cross_chapter_auditor_report_v1"
        or report.get("plan_hash") != plan.plan_hash
        or report.get("queue_hash") != plan.queue_hash
        or report.get("registry_hash") != plan.registry_hash
        or report.get("batch_index") != plan.batch_index
        or report.get("batch_count") != plan.batch_count
    ):
        raise B1CrossChapterAuditorLiveError(
            "cross-chapter recovery root belongs to another sealed plan"
        )
    accepted_ids = report.get("accepted_component_ids")
    if not isinstance(accepted_ids, list) or any(
        not isinstance(value, str) or not value for value in accepted_ids
    ):
        raise B1CrossChapterAuditorLiveError(
            "cross-chapter recovery accepted ids are malformed"
        )
    if len(set(accepted_ids)) != len(accepted_ids):
        raise B1CrossChapterAuditorLiveError(
            "cross-chapter recovery repeats an accepted component"
        )
    ready_by_id = {row.component_id: row for row in plan.hearings}
    if not set(accepted_ids).issubset(ready_by_id):
        raise B1CrossChapterAuditorLiveError(
            "cross-chapter recovery contains a foreign accepted component"
        )
    decisions = json.loads(
        (root / "validated_decisions.json").read_text(encoding="utf-8")
    )
    if not isinstance(decisions, list):
        raise B1CrossChapterAuditorLiveError(
            "cross-chapter recovery decisions must be a list"
        )
    decision_by_id: dict[str, dict[str, Any]] = {}
    for decision in decisions:
        if not isinstance(decision, dict):
            raise B1CrossChapterAuditorLiveError(
                "cross-chapter recovery decision must be an object"
            )
        component_id = decision.get("component_id")
        if (
            not isinstance(component_id, str)
            or component_id in decision_by_id
        ):
            raise B1CrossChapterAuditorLiveError(
                "cross-chapter recovery decision ids are malformed"
            )
        decision_by_id[component_id] = decision
    if set(decision_by_id) != set(accepted_ids):
        raise B1CrossChapterAuditorLiveError(
            "cross-chapter recovery decisions do not exactly cover accepted ids"
        )

    components_root = root / "components"
    recovered: dict[str, dict[str, Any]] = {}
    for component_id in accepted_ids:
        matches = [
            path
            for path in components_root.iterdir()
            if path.is_dir() and path.name.endswith(f"_{component_id}")
        ]
        if len(matches) != 1:
            raise B1CrossChapterAuditorLiveError(
                "cross-chapter recovery component directory is not unique"
            )
        component_dir = matches[0]
        stored_decision = _read(component_dir / "validated_decision.json")
        component_report = _read(component_dir / "component_report.json")
        hearing = ready_by_id[component_id]
        if (
            stored_decision != decision_by_id[component_id]
            or component_report.get("component_id") != component_id
            or component_report.get("review_route") != hearing.route
            or component_report.get("decision_sha256")
            != canonical_hash(stored_decision)
        ):
            raise B1CrossChapterAuditorLiveError(
                "cross-chapter recovery component binding differs"
            )
        recovered[component_id] = {
            "decision": stored_decision,
            "component_dir": component_dir,
        }
    return recovered


def _preflight_failure(
    *, current_head: str, plan: Any, message: str
) -> dict[str, Any]:
    body = {
        "schema_version": "literary_b1_cross_chapter_preflight_failure_v1",
        "status": "preflight_rejected",
        "git_head": current_head,
        "plan_hash": plan.plan_hash,
        "provider_called": False,
        "failure_message": message,
        "selected_batch_complete": False,
        "chapter_loop_complete": False,
        "production_publish_performed": False,
    }
    return {**body, "report_hash": canonical_hash(body)}


def _final_report(
    *,
    current_head: str,
    plan: Any,
    accepted: Sequence[Mapping[str, Any]],
    quarantined: Sequence[Mapping[str, Any]],
    usage_rows: Sequence[Mapping[str, Any]],
    source: Mapping[str, Any],
    recovered_component_ids: Sequence[str] = (),
) -> dict[str, Any]:
    accepted_ids = {str(row["component_id"]) for row in accepted}
    ready_ids = {row.component_id for row in plan.hearings}
    unconsumed_count = sum(len(rows) for rows in plan.unconsumed_ready.values())
    batch_complete = accepted_ids == ready_ids and not quarantined
    complete = (
        batch_complete
        and not unconsumed_count
        and not plan.deferred_ready_component_ids
    )
    if batch_complete:
        status = "semantic_accepted"
    elif accepted:
        status = "semantic_accepted_with_quarantine"
    else:
        status = "semantic_rejected_all" if plan.hearings else "nothing_ready"
    usage = _aggregate_usage(usage_rows)
    body = {
        "schema_version": "literary_b1_cross_chapter_auditor_report_v1",
        "status": status,
        "git_head": current_head,
        "plan_hash": plan.plan_hash,
        "queue_hash": plan.queue_hash,
        "registry_hash": plan.registry_hash,
        "ready_component_count": len(ready_ids),
        "batch_index": plan.batch_index,
        "batch_count": plan.batch_count,
        "deferred_ready_component_ids": list(
            plan.deferred_ready_component_ids
        ),
        "accepted_component_ids": sorted(accepted_ids),
        "quarantined_component_ids": sorted(
            str(row["component_id"]) for row in quarantined
        ),
        "waiting_components": [
            {
                "component_id": row.get("component_id"),
                "review_route": row.get("review_route"),
                "lifecycle_state": row.get("lifecycle_state"),
            }
            for row in plan.waiting_components
        ],
        "unconsumed_ready": {
            key: list(value) for key, value in plan.unconsumed_ready.items()
        },
        "ready_hearings_complete": complete,
        "selected_batch_complete": batch_complete,
        "chapter_loop_complete": complete,
        "provider_call_count": len(usage_rows),
        "recovered_component_ids": sorted(recovered_component_ids),
        "recovered_component_count": len(recovered_component_ids),
        "usage": usage,
        "source_id": source["source_id"],
        "physical_quota_bucket_id": source["physical_quota_bucket_id"],
        "identity_authority_granted": False,
        "claim_authority_granted": False,
        "registry_mutation_performed": False,
        "production_publish_performed": False,
        "mandatory_stop_observed": True,
    }
    return {**body, "report_hash": canonical_hash(body)}


def _aggregate_usage(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    if not rows:
        return None
    names = ("prompt_tokens", "completion_tokens", "total_tokens")
    if not all(all(isinstance(row.get(name), int) for name in names) for row in rows):
        return None
    cached = [row.get("cached_input_tokens") for row in rows]
    reasoning = [row.get("reasoning_tokens") for row in rows]
    costs = [row.get("cost_usd") for row in rows]
    return {
        **{name: sum(int(row[name]) for row in rows) for name in names},
        "cached_input_tokens": (
            sum(int(value) for value in cached)
            if all(isinstance(value, int) for value in cached)
            else None
        ),
        "reasoning_tokens": (
            sum(int(value) for value in reasoning)
            if all(isinstance(value, int) for value in reasoning)
            else None
        ),
        "cost_usd": (
            sum(float(value) for value in costs)
            if all(isinstance(value, (int, float)) for value in costs)
            else None
        ),
        "cost_status": (
            "known"
            if all(isinstance(value, (int, float)) for value in costs)
            else "unknown"
        ),
        "physical_call_count": len(rows),
    }


def _logical_request_id(
    *, run_id: str, plan_hash: str, component_id: str, request_fingerprint: str
) -> str:
    return "literary_b1_cross_chapter_" + canonical_hash(
        {
            "run_id": run_id,
            "plan_hash": plan_hash,
            "component_id": component_id,
            "request_fingerprint": request_fingerprint,
        }
    )[:24]


def _logical_request_was_called(
    ledger: SharedLlmAttemptLedger, logical_request_id: str
) -> bool:
    return any(
        row.get("logical_request_id") == logical_request_id
        for row in ledger.list_records("usage")
    )


def _is_quarantinable_semantic_failure(exc: Exception) -> bool:
    text = str(exc)
    return any(
        marker in text
        for marker in (
            "provider response is not",
            "provider message content is empty",
            "provider content is not a JSON object",
            "provider semantic response is not an object",
            "model-reference transport resolution rejected",
            "local semantic validation rejected",
        )
    )


def _write_json_value(path: Path, payload: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise SystemExit(f"refusing to overwrite artifact: {target}")
    target.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
