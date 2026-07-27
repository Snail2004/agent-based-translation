from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import subprocess
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
from pipeline.literary.b0_chapter_summary_v1 import (
    ROLE_ID,
    append_capsule_log_v1,
    b0_summary_response_schema_v1,
    b2_speaker_recovery_candidate_scope_v1,
    build_b0_summary_context_v1,
    make_b0_summary_semantic_validator_v1,
    render_b0_summary_request_v1,
    shared_b0_summary_request_v1,
    verify_b0_summary_artifact_v1,
    verify_capsule_log_v1,
)
from pipeline.literary.b3_temporal_prefix_v1 import (
    load_b3_temporal_chapter_artifact_v1,
)
from pipeline.literary.b2_slim_speaker_recovery_v1 import (
    build_b2_effective_review_projection_v1,
)
from pipeline.literary.chapter_cycle_shared_runtime_v1 import (
    LiterarySharedRunnerBindingsV1,
    capability_binding_key,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json, file_sha256
from pipeline.literary.model_ref_v1 import MODEL_REF_MODE_CLASSIFIED_V1
from pipeline.literary.modelapi_b0_chapter_summary_capability_probe_v1 import (
    RUNTIME_PROFILE_PATH,
    build_probe_plan_v1,
    execute_probe_once_v1,
    validator_ref_v1,
)
from pipeline.literary.shared_llm_adapter_v1 import LiterarySharedLlmAttemptAdapter
from pipeline.literary.shared_runtime_profile_v2 import (
    load_literary_shared_runtime_profile_v2,
)
from pipeline.literary.chapter_source_document_v1 import (
    load_literary_source_document_v1,
)
from pipeline.scripts.run_literary_builder_pilot import DEFAULT_EPUB, _load_document


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = RUNTIME_ROOT.parent
DEFAULT_SCHEDULER_ROOT = Path(r"C:\work\shared-llm-physical-quota-v1")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="B0 chapter-summary ModelAPI probe and one-chapter canary"
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
    canary.add_argument("--chapter", default="wh_ch01")
    canary.add_argument("--chapter-order", type=int)
    canary.add_argument("--epub", type=Path, default=DEFAULT_EPUB)
    canary.add_argument(
        "--document",
        type=Path,
        help="sealed project document.json; when supplied, EPUB parsing is bypassed",
    )
    canary.add_argument("--runtime-profile", type=Path)
    canary.add_argument("--b1-root", type=Path, required=True)
    canary.add_argument("--b2-root", type=Path, required=True)
    canary.add_argument("--b2-speaker-recovery-root", type=Path)
    canary.add_argument("--b3-root", type=Path)
    canary.add_argument("--b3-review-root", type=Path)
    canary.add_argument("--prior-summary-root", type=Path)
    canary.add_argument("--run-id", required=True)
    canary.add_argument("--attempt-run-id", required=True)
    _credential_args(canary)
    canary.add_argument("--scheduler-root", type=Path, default=DEFAULT_SCHEDULER_ROOT)
    return parser


def _credential_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--credential-env", default="MODELAPI_API_KEY")
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
        chapter_order=args.chapter_order,
        epub_path=args.epub,
        document_path=args.document,
        b1_root=args.b1_root,
        b2_root=args.b2_root,
        b2_speaker_recovery_root=args.b2_speaker_recovery_root,
        b3_root=args.b3_root,
        b3_review_root=args.b3_review_root,
        prior_summary_root=args.prior_summary_root,
        run_id=args.run_id,
        attempt_run_id=args.attempt_run_id,
        secret=secret,
        commitment=commitment,
        scheduler_root=args.scheduler_root,
        current_head=head,
        runtime_profile_path=args.runtime_profile,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


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
    body = {
        "schema_version": "literary_b0_chapter_summary_probe_report_v1",
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


def _run_canary(
    *,
    output_root: Path,
    capability_root: Path,
    chapter_id: str,
    chapter_order: int | None,
    epub_path: Path,
    document_path: Path | None,
    b1_root: Path,
    b2_root: Path,
    b2_speaker_recovery_root: Path | None,
    b3_root: Path | None,
    b3_review_root: Path | None,
    prior_summary_root: Path | None,
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
        raise SystemExit("B0 chapter-summary capability is not qualified")
    runtime = load_literary_shared_runtime_profile_v2(
        Path(runtime_profile_path or RUNTIME_PROFILE_PATH),
        expected_role_ids={ROLE_ID},
    )
    source_binding = dict(runtime.source_binding_for(ROLE_ID))
    source = _source_record(source_binding, commitment)

    document = (
        load_literary_source_document_v1(document_path)
        if document_path is not None
        else _load_document("wuthering_heights", Path(epub_path))[0]
    )
    chapters = list(document["chapters"])
    try:
        chapter_index, chapter = next(
            (index, row)
            for index, row in enumerate(chapters)
            if row["chapter_id"] == chapter_id
        )
    except StopIteration as exc:
        raise SystemExit(f"chapter is absent: {chapter_id}") from exc
    resolved_order = chapter_order if chapter_order is not None else chapter_index + 1
    if prior_summary_root is None:
        if resolved_order != 1:
            raise SystemExit("B0 chapter after chapter 1 requires --prior-summary-root")
        prior_capsule_log = None
    else:
        if resolved_order == 1:
            raise SystemExit("B0 chapter 1 must not receive --prior-summary-root")
        prior_capsule_log = verify_capsule_log_v1(
            _read(Path(prior_summary_root) / "capsule_log.json")
        )
        prior_orders = [row["chapter_order"] for row in prior_capsule_log["capsules"]]
        if prior_orders != list(range(1, resolved_order)):
            raise SystemExit("prior B0 capsule log does not immediately precede chapter")

    b1 = _read(Path(b1_root) / "chapter_registry.json")
    b2 = _read(Path(b2_root) / "chapter_b2_artifact.json")
    if b2_speaker_recovery_root is not None:
        speaker_recovery_root = Path(b2_speaker_recovery_root)
        b2_speaker_recovery = _read(
            speaker_recovery_root / "speaker_recovery_artifact.json"
        )
        b2_speaker_recovery_index = _read(
            speaker_recovery_root / "recovery_index.json"
        )
        allowed_speaker_recovery_candidate_ids = (
            b2_speaker_recovery_candidate_scope_v1(
                b2_artifact=b2,
                recovery_artifact=b2_speaker_recovery,
                recovery_index=b2_speaker_recovery_index,
            )
        )
    else:
        b2_speaker_recovery = None
        b2_speaker_recovery_index = None
        allowed_speaker_recovery_candidate_ids = set()
    has_speaker_reviews = any(
        isinstance(row, Mapping) and row.get("review_kind") == "speaker_attribution"
        for row in b2.get("review_requests") or []
    )
    b2_review_projection = (
        build_b2_effective_review_projection_v1(
            chapter_artifact=b2,
            recovery_artifact=b2_speaker_recovery,
            allowed_candidate_card_ids=allowed_speaker_recovery_candidate_ids,
            recovery_index=b2_speaker_recovery_index,
        )
        if b2_speaker_recovery is not None or has_speaker_reviews
        else None
    )
    b3 = (
        load_b3_temporal_chapter_artifact_v1(Path(b3_root))[0]
        if b3_root is not None
        else None
    )
    b3_review = (
        _read(Path(b3_review_root) / "temporal_review_overlay.json")
        if b3_review_root is not None
        else None
    )
    packet = build_b0_summary_context_v1(
        chapter=chapter,
        chapter_order=resolved_order,
        b1_registry=b1,
        b2_artifact=b2,
        b2_effective_review_projection=b2_review_projection,
        b3_artifact=b3,
        b3_review_overlay=b3_review,
    )
    rendered = render_b0_summary_request_v1(packet)
    request = shared_b0_summary_request_v1(rendered)
    lineage = {
        "source_epub_sha256": file_sha256(Path(epub_path)),
        "source_chapter_sha256": canonical_hash(chapter),
        "b1_registry_hash": b1["registry_hash"],
        "b2_artifact_hash": b2["artifact_hash"],
    }
    if b2_speaker_recovery is not None:
        lineage["b2_speaker_recovery_artifact_hash"] = b2_speaker_recovery[
            "artifact_hash"
        ]
        lineage["b2_speaker_recovery_index_hash"] = b2_speaker_recovery_index[
            "recovery_index_hash"
        ]
    if b2_review_projection is not None:
        lineage["b2_effective_review_projection_hash"] = b2_review_projection[
            "projection_hash"
        ]
    if b3 is not None:
        lineage["b3_artifact_hash"] = b3["artifact_hash"]
    if b3_review is not None:
        lineage["b3_review_overlay_hash"] = b3_review["overlay_hash"]

    output, shared = _fresh_roots(output_root)
    output.mkdir(parents=True, exist_ok=False)
    seal = {
        "schema_version": "literary_b0_chapter_summary_run_seal_v1_1",
        "git_head": current_head,
        "run_id": run_id,
        "attempt_run_id": attempt_run_id,
        "chapter_id": chapter_id,
        "chapter_order": resolved_order,
        "request_fingerprint": rendered.request_fingerprint,
        "packet_hash": packet["packet_hash"],
        "lineage": lineage,
        "runtime_profile_sha256": runtime.profile_sha256,
        "capability_evidence_sha256": evidence["evidence_sha256"],
        "provider_fallback_allowed": False,
        "production_publish_allowed": False,
    }
    _write(output / "run_seal.json", {**seal, "seal_hash": canonical_hash(seal)})
    _write(output / "context_packet.json", packet)
    if b2_review_projection is not None:
        _write(
            output / "b2_effective_review_projection.json",
            b2_review_projection,
        )
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
            capability_binding_key(ROLE_ID, b0_summary_response_schema_v1()): evidence
        },
        run_id=run_id,
        attempt_run_id=attempt_run_id,
        structured_output=None,
        runtime_profile=runtime,
        api_sources_by_alias={source_binding["source_alias"]: source},
    )
    result = bindings.execute_accepted_request(
        role_id=ROLE_ID,
        stage_id="literary_b0_chapter_summary",
        logical_request_id=(
            f"literary_b0_summary_{canonical_hash({'run_id': run_id, 'chapter_id': chapter_id, 'packet_hash': packet['packet_hash']})[:24]}"
        ),
        request=request,
        schema_name="literary_b0_summary_response_v1",
        semantic_validator=make_b0_summary_semantic_validator_v1(
            packet=packet,
            rendered=rendered,
            lineage=lineage,
        ),
        validator_ref=validator_ref_v1(),
        application_contract_id="literary.b0.chapter_summary.application",
        application_contract_revision="v1",
        output_dir=output,
        model_reference_mode=MODEL_REF_MODE_CLASSIFIED_V1,
        additional_input_bindings=(
            {"name": "source_chapter", "sha256": canonical_hash(chapter)},
            {"name": "b1_registry", "sha256": b1["registry_hash"]},
            {"name": "b2_artifact", "sha256": b2["artifact_hash"]},
            {
                "name": "b2_speaker_recovery_artifact",
                "sha256": lineage.get(
                    "b2_speaker_recovery_artifact_hash", canonical_hash([])
                ),
            },
            {
                "name": "b2_effective_review_projection",
                "sha256": lineage.get(
                    "b2_effective_review_projection_hash", canonical_hash([])
                ),
            },
            {"name": "b3_artifact", "sha256": lineage.get("b3_artifact_hash", canonical_hash([]))},
            {
                "name": "b3_review_overlay",
                "sha256": lineage.get("b3_review_overlay_hash", canonical_hash([])),
            },
        ),
    )
    artifact = verify_b0_summary_artifact_v1(result.semantic_payload)
    capsule_log = append_capsule_log_v1(
        artifact=artifact,
        prior_log=prior_capsule_log,
    )
    _write(output / "model_response.json", result.response_payload)
    _write(output / "chapter_summary_artifact.json", artifact)
    _write(output / "capsule_log.json", capsule_log)
    summary = artifact["summary"]
    report_body = {
        "schema_version": "literary_b0_chapter_summary_canary_report_v1",
        "status": result.status,
        "chapter_id": chapter_id,
        "git_head": current_head,
        "request_fingerprint": rendered.request_fingerprint,
        "artifact_hash": artifact["artifact_hash"],
        "capsule_log_hash": capsule_log["capsule_log_hash"],
        "prior_capsule_log_hash": (
            prior_capsule_log["capsule_log_hash"]
            if prior_capsule_log is not None
            else None
        ),
        "metrics": {
            "chapter_summary_words": len(summary["chapter_summary"].split()),
            "entity_refs": len(summary["narrative_handoff"]["entities_mentioned"]),
            "event_refs": len(summary["salient_event_refs"]),
            "state_refs": len(summary["effective_state_refs"]),
            "unresolved_case_refs": len(summary["unresolved_case_refs"]),
            "b2_review_projection_status": packet[
                "b2_review_projection_status"
            ],
            "b2_resolved_review_count": (
                len(b2_review_projection["resolved_review_ids"])
                if b2_review_projection is not None
                else 0
            ),
            "quarantined_refs": len(summary["quarantined_refs"]),
            "review_issues": list(summary["review_issues"]),
            "prompt_utf8_bytes": sum(
                len(row["content"].encode("utf-8")) for row in rendered.messages
            ),
        },
        "usage": dict(result.usage) if result.usage is not None else None,
        "provider_called": result.provider_called,
        "semantic_authority_granted": False,
        "registry_mutation_performed": False,
        "production_publish_performed": False,
        "mandatory_stop_observed": True,
        "source_id": source["source_id"],
    }
    report = {**report_body, "report_hash": canonical_hash(report_body)}
    _write(output / "canary_report.json", report)
    return report


def _source_record(binding: Mapping[str, Any], commitment: str) -> dict[str, Any]:
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


def _credential(environment_name: str, credential_file: Path | None) -> str:
    value = os.environ.get(environment_name)
    if credential_file is not None:
        if value:
            raise SystemExit("select either credential environment or file, not both")
        try:
            value = Path(credential_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise SystemExit("cannot read credential file") from exc
    if not isinstance(value, str) or not value or value != value.strip():
        raise SystemExit("credential is absent or malformed")
    return value


def _clean_head() -> str:
    status = subprocess.run(
        ["git", "status", "--short", "--untracked-files=no"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if status:
        raise SystemExit("live B0 summary command requires a clean tracked worktree")
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _fresh_roots(output_root: Path) -> tuple[Path, Path]:
    output = Path(output_root).resolve()
    shared = Path(str(output) + "-shared")
    if output.exists() or shared.exists():
        raise SystemExit("output roots must not already exist")
    return output, shared


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"cannot read JSON artifact: {path}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"JSON artifact must be an object: {path}")
    return value


def _write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise SystemExit(f"refusing to overwrite artifact: {path}")
    path.write_text(canonical_json(payload) + "\n", encoding="utf-8")


def _usage(receipt: Mapping[str, Any]) -> dict[str, int] | None:
    names = (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "cached_input_tokens",
        "reasoning_tokens",
    )
    values = {name: receipt.get(name) for name in names}
    if not all(isinstance(value, int) and value >= 0 for value in values.values()):
        return None
    return values


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


if __name__ == "__main__":
    raise SystemExit(main())
