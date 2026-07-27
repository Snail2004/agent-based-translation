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
from pipeline.literary.b1_enrich_local_auditor_v1 import (
    ROLE_ID,
    b1_enrich_local_audit_response_schema_v1,
    make_b1_enrich_local_audit_semantic_validator_v1,
    merge_b1_enrich_local_audit_batch_artifacts_v1,
    plan_b1_enrich_local_audit_batches_v1,
)
from pipeline.literary.chapter_cycle_shared_runtime_v1 import (
    LiterarySharedRunnerBindingsV1,
    capability_binding_key,
)
from pipeline.literary.checkpoint import canonical_hash
from pipeline.literary.model_ref_v1 import MODEL_REF_MODE_CLASSIFIED_V1
from pipeline.literary.modelapi_b1_enrich_local_auditor_capability_probe_v1 import (
    DESIGN_DOC,
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
from pipeline.scripts.run_literary_builder_pilot import DEFAULT_EPUB, _load_document
from pipeline.literary.chapter_source_document_v1 import (
    load_literary_source_document_v1,
)


LIVE_RUNTIME_PROFILE_PATH = (
    Path(__file__).resolve().parents[1]
    / "configs"
    / "literary_shared_llm_runtime_modelapi_b1_enrich_local_auditor_v4.json"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="B1-Enrich Local Auditor ModelAPI probe and canary"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    probe = commands.add_parser("probe")
    probe.add_argument("--output-root", type=Path, required=True)
    probe.add_argument("--probe-run-id", required=True)
    _credential_args(probe)
    probe.add_argument("--scheduler-root", type=Path, default=DEFAULT_SCHEDULER_ROOT)

    canary = commands.add_parser("canary")
    canary.add_argument("--output-root", type=Path, required=True)
    canary.add_argument("--capability-root", type=Path, required=True)
    canary.add_argument("--scan-artifact", type=Path, required=True)
    canary.add_argument("--enrich-artifact", type=Path, required=True)
    canary.add_argument("--chapter", default="wh_ch01")
    canary.add_argument("--epub", type=Path, default=DEFAULT_EPUB)
    canary.add_argument(
        "--document",
        type=Path,
        help="sealed project document.json; when supplied, EPUB parsing is bypassed",
    )
    canary.add_argument("--runtime-profile", type=Path)
    canary.add_argument("--run-id", required=True)
    canary.add_argument("--attempt-run-id", required=True)
    _credential_args(canary)
    canary.add_argument("--scheduler-root", type=Path, default=DEFAULT_SCHEDULER_ROOT)
    return parser


def _credential_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--credential-env", default="OPENAI_API_KEY")
    parser.add_argument("--credential-file", type=Path)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    head = _clean_head()
    secret = _credential(args.credential_env, args.credential_file)
    commitment = credential_commitment(secret)
    if args.command == "probe":
        report = _run_probe(
            output_root=args.output_root,
            probe_run_id=args.probe_run_id,
            secret=secret,
            commitment=commitment,
            scheduler_root=args.scheduler_root,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["status"] == "qualified" else 2
    report = _run_canary(
        output_root=args.output_root,
        capability_root=args.capability_root,
        chapter_id=args.chapter,
        epub_path=args.epub,
        document_path=args.document,
        scan_artifact_path=args.scan_artifact,
        enrich_artifact_path=args.enrich_artifact,
        run_id=args.run_id,
        attempt_run_id=args.attempt_run_id,
        secret=secret,
        commitment=commitment,
        scheduler_root=args.scheduler_root,
        current_head=head,
        runtime_profile_path=args.runtime_profile,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "semantic_accepted" else 2


def _run_probe(
    *,
    output_root: Path,
    probe_run_id: str,
    secret: str,
    commitment: str,
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
    report_body = {
        "schema_version": "literary_b1_enrich_local_auditor_probe_report_v1",
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
    report = {**report_body, "report_hash": canonical_hash(report_body)}
    _write(output / "probe_report.json", report)
    return report


def _run_canary(
    *,
    output_root: Path,
    capability_root: Path,
    chapter_id: str,
    epub_path: Path,
    document_path: Path | None,
    scan_artifact_path: Path,
    enrich_artifact_path: Path,
    run_id: str,
    attempt_run_id: str,
    secret: str,
    commitment: str,
    scheduler_root: Path,
    current_head: str,
    runtime_profile_path: Path | None = None,
) -> dict[str, Any]:
    evidence = validate_capability_evidence(
        _read(Path(capability_root) / "capability_evidence.json")
    )
    if evidence["verdict"] != "qualified":
        raise SystemExit("Local Auditor capability evidence is not qualified")
    runtime = load_literary_shared_runtime_profile_v2(
        Path(runtime_profile_path or LIVE_RUNTIME_PROFILE_PATH),
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
    document = (
        load_literary_source_document_v1(document_path)
        if document_path is not None
        else _load_document("wuthering_heights", Path(epub_path))[0]
    )
    try:
        chapter = next(
            row for row in document["chapters"] if row["chapter_id"] == chapter_id
        )
    except StopIteration as exc:
        raise SystemExit(f"chapter is absent: {chapter_id}") from exc
    scan = _read(scan_artifact_path)
    enrich = _read(enrich_artifact_path)
    output, shared = _fresh_roots(output_root)
    output.mkdir(parents=True, exist_ok=False)
    preset = runtime.role_presets[ROLE_ID]
    try:
        batch_plan, batches = plan_b1_enrich_local_audit_batches_v1(
            chapter=chapter,
            scan_artifact=scan,
            enrich_artifact=enrich,
            design_doc=DESIGN_DOC,
            prompt_token_cap=int(preset.generation["max_input_tokens"]),
            output_token_cap=int(preset.generation["max_output_tokens"]),
            model_id=preset.requested_model_id,
            reasoning_effort=str(preset.generation["reasoning_effort"]),
            temperature=float(preset.generation["temperature"]),
            seed=int(preset.generation["seed"]),
        )
    except Exception as exc:
        failure_body = {
            "schema_version": "literary_b1_enrich_local_auditor_preflight_failure_v1",
            "status": "preflight_rejected",
            "chapter_id": chapter_id,
            "git_head": current_head,
            "provider_called": False,
            "failure_type": type(exc).__name__,
            "failure_message": str(exc),
            "prompt_token_cap": int(preset.generation["max_input_tokens"]),
            "output_token_cap": int(preset.generation["max_output_tokens"]),
            "production_publish_performed": False,
        }
        failure = {**failure_body, "report_hash": canonical_hash(failure_body)}
        _write(output / "preflight_failure.json", failure)
        return failure
    _write(output / "batch_plan.json", batch_plan)
    _write(
        output / "run_seal.json",
        {
            "schema_version": "literary_b1_enrich_local_auditor_run_seal_v2",
            "git_head": current_head,
            "run_id": run_id,
            "attempt_run_id": attempt_run_id,
            "chapter_id": chapter_id,
            "request_fingerprint": batch_plan["batch_plan_hash"],
            "batch_plan_hash": batch_plan["batch_plan_hash"],
            "batch_count": len(batches),
            "batch_request_fingerprints": [
                batch.rendered.request_fingerprint for batch in batches
            ],
            "scan_artifact_sha256": canonical_hash(scan),
            "enrich_artifact_sha256": canonical_hash(enrich),
            "runtime_profile_sha256": runtime.profile_sha256,
            "capability_evidence_sha256": evidence["evidence_sha256"],
            "provider_fallback_allowed": False,
            "production_publish_allowed": False,
        },
    )

    if not batches:
        artifact = merge_b1_enrich_local_audit_batch_artifacts_v1(
            chapter=chapter,
            scan_artifact=scan,
            enrich_artifact=enrich,
            batch_plan=batch_plan,
            batch_artifacts=(),
        )
        _write(output / "local_audit_artifact.json", artifact)
        report_body = {
            "schema_version": "literary_b1_enrich_local_auditor_canary_report_v2",
            "status": "semantic_accepted",
            "chapter_id": chapter_id,
            "git_head": current_head,
            "request_fingerprint": batch_plan["batch_plan_hash"],
            "artifact_hash": artifact["artifact_hash"],
            "metrics": artifact["metrics"],
            "batch_count": 0,
            "batch_reports": [],
            "usage": None,
            "provider_called": False,
            "identity_authority_granted": False,
            "book_authority_granted": False,
            "registry_mutation_performed": False,
            "production_publish_performed": False,
            "mandatory_stop_observed": True,
            "source_id": source["source_id"],
        }
        report = {**report_body, "report_hash": canonical_hash(report_body)}
        _write(output / "canary_report.json", report)
        return report

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
                ROLE_ID, b1_enrich_local_audit_response_schema_v1()
            ): evidence
        },
        run_id=run_id,
        attempt_run_id=attempt_run_id,
        structured_output=None,
        runtime_profile=runtime,
        api_sources_by_alias={
            runtime.role_bindings[ROLE_ID].source_alias: source
        },
    )
    batch_artifacts: list[Mapping[str, Any]] = []
    batch_reports: list[dict[str, Any]] = []
    provider_called = False
    try:
        for index, batch in enumerate(batches, start=1):
            batch_dir = output / "batches" / f"{index:03d}_{batch.batch_id}"
            batch_dir.mkdir(parents=True, exist_ok=False)
            request_payload = batch.rendered.to_dict()
            request_payload["messages"] = [
                dict(row) for row in batch.rendered.messages
            ]
            request_payload["response_schema"] = batch.request["response_schema"]
            _write(batch_dir / "manifest.json", batch.manifest)
            _write(batch_dir / "request.json", request_payload)
            _write(
                batch_dir / "token_preflight.json",
                batch.token_preflight.to_payload(),
            )
            result = bindings.execute_accepted_request(
                role_id=ROLE_ID,
                stage_id="literary_b1_enrich_local_auditor",
                logical_request_id=(
                    "literary_b1_enrich_local_auditor_"
                    + canonical_hash(
                        {
                            "run_id": run_id,
                            "chapter_id": chapter_id,
                            "batch_id": batch.batch_id,
                            "enrich_artifact_hash": enrich.get("artifact_hash"),
                        }
                    )[:24]
                ),
                request=batch.request,
                schema_name="literary_b1_enrich_local_audit_response_v1",
                semantic_validator=make_b1_enrich_local_audit_semantic_validator_v1(
                    chapter=chapter,
                    scan_artifact=scan,
                    enrich_artifact=enrich,
                    rendered=batch.rendered,
                    component_ids=batch.component_ids,
                ),
                validator_ref=validator_ref_v1(),
                application_contract_id=(
                    "literary.b1.enrich_local_auditor.application"
                ),
                application_contract_revision="v1",
                output_dir=batch_dir,
                model_reference_mode=MODEL_REF_MODE_CLASSIFIED_V1,
                additional_input_bindings=(
                    {"name": "source_chapter", "sha256": canonical_hash(chapter)},
                    {"name": "b1_scan_artifact", "sha256": canonical_hash(scan)},
                    {"name": "b1_enrich_artifact", "sha256": canonical_hash(enrich)},
                    {
                        "name": "local_audit_batch_plan",
                        "sha256": batch_plan["batch_plan_hash"],
                    },
                    {
                        "name": "local_audit_batch_manifest",
                        "sha256": batch.manifest["manifest_hash"],
                    },
                ),
            )
            provider_called = provider_called or result.provider_called
            _write(batch_dir / "model_response.json", result.response_payload)
            _write(batch_dir / "local_audit_batch_artifact.json", result.semantic_payload)
            batch_artifacts.append(result.semantic_payload)
            batch_reports.append(
                {
                    "batch_id": batch.batch_id,
                    "batch_index": index,
                    "component_count": len(batch.component_ids),
                    "source_block_count": len(batch.manifest["source_blocks"]),
                    "request_fingerprint": batch.rendered.request_fingerprint,
                    "token_preflight": batch.token_preflight.to_payload(),
                    "status": result.status,
                    "provider_called": result.provider_called,
                    "usage": dict(result.usage) if result.usage is not None else None,
                    "artifact_hash": result.semantic_payload["artifact_hash"],
                }
            )
    except Exception as exc:
        failure_body = {
            "schema_version": "literary_b1_enrich_local_auditor_run_failure_v1",
            "status": "failed",
            "chapter_id": chapter_id,
            "git_head": current_head,
            "batch_plan_hash": batch_plan["batch_plan_hash"],
            "completed_batch_count": len(batch_artifacts),
            "provider_called": provider_called,
            "failure_type": type(exc).__name__,
            "failure_message": str(exc),
            "production_publish_performed": False,
        }
        _write(
            output / "run_failure.json",
            {**failure_body, "report_hash": canonical_hash(failure_body)},
        )
        raise

    artifact = merge_b1_enrich_local_audit_batch_artifacts_v1(
        chapter=chapter,
        scan_artifact=scan,
        enrich_artifact=enrich,
        batch_plan=batch_plan,
        batch_artifacts=batch_artifacts,
    )
    _write(output / "local_audit_artifact.json", artifact)
    usage_rows = [
        row["usage"] for row in batch_reports if isinstance(row.get("usage"), Mapping)
    ]
    aggregate_usage = None
    if len(usage_rows) == len(batch_reports):
        aggregate_usage = {
            key: sum(int(row[key]) for row in usage_rows)
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
            if all(isinstance(row.get(key), int) for row in usage_rows)
        }
        if len(aggregate_usage) != 3:
            aggregate_usage = None
        else:
            cached_values = [row.get("cached_input_tokens") for row in usage_rows]
            reasoning_values = [row.get("reasoning_tokens") for row in usage_rows]
            cost_values = [row.get("cost_usd") for row in usage_rows]
            aggregate_usage.update(
                {
                    "cached_input_tokens": (
                        sum(int(value) for value in cached_values)
                        if all(isinstance(value, int) for value in cached_values)
                        else None
                    ),
                    "reasoning_tokens": (
                        sum(int(value) for value in reasoning_values)
                        if all(isinstance(value, int) for value in reasoning_values)
                        else None
                    ),
                    "cost_usd": (
                        sum(float(value) for value in cost_values)
                        if all(isinstance(value, (int, float)) for value in cost_values)
                        else None
                    ),
                    "cost_status": (
                        "known"
                        if all(isinstance(value, (int, float)) for value in cost_values)
                        else "unknown"
                    ),
                    "physical_call_count": len(usage_rows),
                }
            )
    report_body = {
        "schema_version": "literary_b1_enrich_local_auditor_canary_report_v2",
        "status": "semantic_accepted",
        "chapter_id": chapter_id,
        "git_head": current_head,
        "request_fingerprint": batch_plan["batch_plan_hash"],
        "batch_plan_hash": batch_plan["batch_plan_hash"],
        "artifact_hash": artifact["artifact_hash"],
        "metrics": artifact["metrics"],
        "batch_count": len(batches),
        "batch_reports": batch_reports,
        "usage": aggregate_usage,
        "provider_called": provider_called,
        "identity_authority_granted": False,
        "book_authority_granted": False,
        "registry_mutation_performed": False,
        "production_publish_performed": False,
        "mandatory_stop_observed": True,
        "source_id": source["source_id"],
    }
    report = {**report_body, "report_hash": canonical_hash(report_body)}
    _write(output / "canary_report.json", report)
    return report


if __name__ == "__main__":
    raise SystemExit(main())
