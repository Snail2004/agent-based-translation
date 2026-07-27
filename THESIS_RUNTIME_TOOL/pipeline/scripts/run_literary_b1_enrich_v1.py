from __future__ import annotations

import argparse
from datetime import datetime, timezone
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
    canonical_json,
    credential_commitment,
    validate_capability_evidence,
)
from pipeline.literary.b1_enrich_v1 import (
    b1_enrich_response_schema_v1,
    build_b1_enrich_continuity_context_v1,
    make_b1_enrich_semantic_validator_v1,
    render_b1_enrich_request_v1,
    shared_b1_enrich_request_v1,
)
from pipeline.literary.chapter_cycle_shared_runtime_v1 import (
    LiterarySharedRunnerBindingsV1,
    capability_binding_key,
)
from pipeline.literary.checkpoint import canonical_hash
from pipeline.literary.model_ref_v1 import MODEL_REF_MODE_CLASSIFIED_V1
from pipeline.literary.request_token_preflight_v1 import (
    measure_literary_request_token_preflight_v1,
)
from pipeline.literary.modelapi_b1_enrich_capability_probe_v1 import (
    DESIGN_DOC,
    ROLE_ID,
    RUNTIME_PROFILE_PATH,
    build_probe_plan_v1,
    execute_probe_once_v1,
    validator_ref_v1,
)
from pipeline.literary.shared_llm_adapter_v1 import LiterarySharedLlmAttemptAdapter
from pipeline.literary.shared_runtime_profile_v2 import (
    load_literary_shared_runtime_profile_v2,
)
from pipeline.scripts.run_literary_builder_pilot import DEFAULT_EPUB, _load_document
from pipeline.literary.chapter_source_document_v1 import (
    load_literary_source_document_v1,
)
from pipeline.scripts.run_literary_b1_scan_v1 import (
    _load_prior_cards_context,
    _load_summary_context,
)


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = RUNTIME_ROOT.parent
DEFAULT_SCHEDULER_ROOT = Path(r"C:\work\shared-llm-physical-quota-v1")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Literary B1-Enrich probe and canary")
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
    canary.add_argument("--epub", type=Path, default=DEFAULT_EPUB)
    canary.add_argument(
        "--document",
        type=Path,
        help="sealed project document.json; when supplied, EPUB parsing is bypassed",
    )
    canary.add_argument("--scan-artifact", type=Path, required=True)
    prior = canary.add_mutually_exclusive_group()
    prior.add_argument("--injected-prior-cards", type=Path)
    prior.add_argument("--prior-registry-root", type=Path)
    canary.add_argument("--previous-summary-root", type=Path)
    canary.add_argument("--run-id", required=True)
    canary.add_argument("--attempt-run-id", required=True)
    canary.add_argument(
        "--runtime-profile",
        type=Path,
        help="versioned runtime profile; defaults to the qualified role profile",
    )
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
        injected_prior_cards_path=args.injected_prior_cards,
        prior_registry_root=args.prior_registry_root,
        previous_summary_root=args.previous_summary_root,
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
        "schema_version": "literary_b1_enrich_probe_report_v1",
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
    injected_prior_cards_path: Path | None,
    prior_registry_root: Path | None,
    previous_summary_root: Path | None,
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
        raise SystemExit("B1-Enrich capability evidence is not qualified")
    runtime_profile = load_literary_shared_runtime_profile_v2(
        Path(runtime_profile_path or RUNTIME_PROFILE_PATH),
        expected_role_ids={ROLE_ID},
    )
    source_binding = dict(runtime_profile.source_binding_for(ROLE_ID))
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
        chapter_order, chapter = next(
            (index, row)
            for index, row in enumerate(document["chapters"], start=1)
            if row["chapter_id"] == chapter_id
        )
    except StopIteration as exc:
        raise SystemExit(f"chapter is absent: {chapter_id}") from exc
    scan_artifact = _read(scan_artifact_path)
    if scan_artifact.get("chapter_id") != chapter_id:
        raise SystemExit("B1-Scan artifact chapter differs from requested chapter")
    prior_cards, prior_lineage = _load_prior_cards_context(
        prior_cards_path=injected_prior_cards_path,
        prior_registry_root=prior_registry_root,
    )
    continuity_context = build_b1_enrich_continuity_context_v1(
        scan_artifact=scan_artifact,
        prior_cards=prior_cards,
    )
    injected_prior_cards = continuity_context["selected_prior_cards"]
    previous_summary, global_summary, summary_lineage = _load_summary_context(
        previous_summary_root=previous_summary_root,
        current_chapter_order=chapter_order,
    )
    rendered = render_b1_enrich_request_v1(
        chapter=chapter,
        scan_artifact=scan_artifact,
        design_doc=DESIGN_DOC,
        continuity_context=continuity_context,
        previous_chapter_summary=previous_summary,
        global_summary=global_summary,
    )
    request = shared_b1_enrich_request_v1(rendered)
    output, shared = _fresh_roots(output_root)
    output.mkdir(parents=True, exist_ok=False)
    preset = runtime_profile.role_presets[ROLE_ID]
    token_preflight = measure_literary_request_token_preflight_v1(
        request,
        prompt_token_cap=int(preset.generation["max_input_tokens"]),
        output_token_cap=int(preset.generation["max_output_tokens"]),
    )
    context_body = {
        "schema_version": "literary_b1_enrich_cross_chapter_context_v2",
        "chapter_id": chapter_id,
        "scan_artifact_sha256": canonical_hash(scan_artifact),
        "prior_input_kind": prior_lineage["prior_input_kind"],
        "prior_registry_hash": prior_lineage["prior_registry_hash"],
        "selected_prior_cards": injected_prior_cards,
        "continuity_context": continuity_context,
        "previous_chapter_summary": previous_summary,
        "global_summary": global_summary,
        "previous_summary_artifact_hash": summary_lineage[
            "previous_summary_artifact_hash"
        ],
        "capsule_log_hash": summary_lineage["capsule_log_hash"],
        "identity_authority_granted": False,
    }
    cross_chapter_context = {
        **context_body,
        "context_hash": canonical_hash(context_body),
    }
    _write(output / "cross_chapter_context.json", cross_chapter_context)
    _write(output / "run_seal.json", {
        "schema_version": "literary_b1_enrich_run_seal_v3",
        "git_head": current_head,
        "run_id": run_id,
        "attempt_run_id": attempt_run_id,
        "chapter_id": chapter_id,
        "request_fingerprint": rendered.request_fingerprint,
        "scan_artifact_sha256": canonical_hash(scan_artifact),
        "injected_prior_cards_sha256": canonical_hash(injected_prior_cards),
        "continuity_context_sha256": continuity_context["context_hash"],
        "cross_chapter_context_sha256": cross_chapter_context["context_hash"],
        "prior_registry_hash": prior_lineage["prior_registry_hash"],
        "previous_summary_artifact_hash": summary_lineage[
            "previous_summary_artifact_hash"
        ],
        "capsule_log_hash": summary_lineage["capsule_log_hash"],
        "token_preflight": token_preflight.to_payload(),
        "runtime_profile_sha256": runtime_profile.profile_sha256,
        "runtime_profile_path": str(
            Path(runtime_profile_path or RUNTIME_PROFILE_PATH).as_posix()
        ),
        "capability_evidence_sha256": evidence["evidence_sha256"],
        "provider_fallback_allowed": False,
        "production_publish_allowed": False,
    })
    rendered_payload = rendered.to_dict()
    rendered_payload["messages"] = [dict(row) for row in rendered.messages]
    _write(
        output / "request.json",
        {**rendered_payload, "response_schema": request["response_schema"]},
    )
    _write(output / "token_preflight.json", token_preflight.to_payload())
    if not token_preflight.fits_prompt_cap:
        failure_body = {
            "schema_version": "literary_b1_enrich_preflight_failure_v1",
            "status": "preflight_rejected",
            "chapter_id": chapter_id,
            "git_head": current_head,
            "provider_called": False,
            "failure_message": (
                f"{ROLE_ID} prompt reserve "
                f"{token_preflight.prompt_token_reserve} exceeds input cap "
                f"{token_preflight.prompt_token_cap}"
            ),
            "token_preflight": token_preflight.to_payload(),
            "production_publish_performed": False,
        }
        failure = {**failure_body, "report_hash": canonical_hash(failure_body)}
        _write(output / "preflight_failure.json", failure)
        return failure
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
            capability_binding_key(ROLE_ID, b1_enrich_response_schema_v1()): evidence
        },
        run_id=run_id,
        attempt_run_id=attempt_run_id,
        structured_output=None,
        runtime_profile=runtime_profile,
        api_sources_by_alias={
            runtime_profile.role_bindings[ROLE_ID].source_alias: source
        },
    )
    validator_ref = validator_ref_v1()
    result = bindings.execute_accepted_request(
        role_id=ROLE_ID,
        stage_id="literary_b1_enrich",
        logical_request_id=(
            "literary_b1_enrich_"
            + canonical_hash(
                {
                    "run_id": run_id,
                    "chapter_id": chapter_id,
                    "scan_artifact_hash": scan_artifact.get("artifact_hash"),
                }
            )[:24]
        ),
        request=request,
        schema_name="literary_b1_enrich_response_v1",
        semantic_validator=make_b1_enrich_semantic_validator_v1(
            chapter=chapter,
            scan_artifact=scan_artifact,
            rendered=rendered,
            continuity_context=continuity_context,
        ),
        validator_ref=validator_ref,
        application_contract_id="literary.b1.enrich.application",
        application_contract_revision="v1",
        output_dir=output,
        model_reference_mode=MODEL_REF_MODE_CLASSIFIED_V1,
        additional_input_bindings=(
            {"name": "source_chapter", "sha256": canonical_hash(chapter)},
            {"name": "b1_scan_artifact", "sha256": canonical_hash(scan_artifact)},
            {
                "name": "injected_prior_cards",
                "sha256": canonical_hash(injected_prior_cards),
            },
            {
                "name": "b1_enrich_continuity_context",
                "sha256": continuity_context["context_hash"],
            },
            {
                "name": "cross_chapter_context",
                "sha256": cross_chapter_context["context_hash"],
            },
        ),
    )
    _write(output / "model_response.json", result.response_payload)
    _write(output / "b1_enrich_artifact.json", result.semantic_payload)
    report_body = {
        "schema_version": "literary_b1_enrich_canary_report_v1",
        "status": result.status,
        "chapter_id": chapter_id,
        "git_head": current_head,
        "request_fingerprint": rendered.request_fingerprint,
        "artifact_hash": result.semantic_payload["artifact_hash"],
        "metrics": result.semantic_payload["metrics"],
        "scan_entity_task_count": len(
            scan_artifact.get("entity_observations", [])
        ),
        "scan_glossary_task_count": len(
            scan_artifact.get("glossary_observations", [])
        ),
        "injected_prior_card_count": len(injected_prior_cards),
        "continuity_case_count": len(continuity_context["continuity_cases"]),
        "identity_hearing_required_count": sum(
            1
            for row in continuity_context["continuity_cases"]
            if row["hearing_required"]
        ),
        "previous_summary_supplied": previous_summary is not None,
        "global_summary_supplied": global_summary is not None,
        "token_preflight": token_preflight.to_payload(),
        "usage": dict(result.usage) if result.usage is not None else None,
        "provider_called": result.provider_called,
        "identity_authority_granted": False,
        "registry_mutation_performed": False,
        "production_publish_performed": False,
        "mandatory_stop_observed": True,
        "source_id": source["source_id"],
    }
    report = {**report_body, "report_hash": canonical_hash(report_body)}
    _write(output / "canary_report.json", report)
    return report


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
        raise SystemExit("live B1-Enrich command requires a clean tracked worktree")
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
    value = json.loads(Path(path).read_text(encoding="utf-8"))
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
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


if __name__ == "__main__":
    raise SystemExit(main())
