from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
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
from pipeline.literary.b1_scan_v1 import (
    b1_scan_response_schema_v1,
    make_b1_scan_semantic_validator_v1,
    render_b1_scan_request_v1,
    shared_b1_scan_request_v1,
    validate_b1_scan_response_v1,
)
from pipeline.literary.b0_chapter_summary_v1 import (
    CAPSULE_LOG_SCHEMA_VERSION,
    verify_b0_summary_artifact_v1,
)
from pipeline.literary.b1_chapter_registry_writer_v1 import (
    build_b1_prior_context_cards_v1,
    verify_b1_chapter_registry_v1,
)
from pipeline.literary.b1_cross_chapter_decision_ledger_v1 import (
    build_projected_prior_cards_v1,
    verify_decision_ledger_v1,
)
from pipeline.literary.chapter_cycle_shared_runtime_v1 import (
    LiterarySharedRunnerBindingsV1,
    build_literary_code_ref_v1,
    capability_binding_key,
)
from pipeline.literary.chapter_source_document_v1 import (
    load_literary_source_document_v1,
)
from pipeline.literary.checkpoint import canonical_hash
from pipeline.literary.model_ref_v1 import MODEL_REF_MODE_CLASSIFIED_V1
from pipeline.literary.request_token_preflight_v1 import (
    measure_literary_request_token_preflight_v1,
)
from pipeline.literary.openai_b1_scan_capability_probe_v1 import (
    DESIGN_DOC,
    MODELAPI_PROFILE_PATH,
    MODELAPI_RUNTIME_PROFILE_PATH,
    ROLE_ID,
    RUNTIME_PROFILE_PATH,
    build_probe_plan_v1,
    execute_probe_once_v1,
)
from pipeline.literary.shared_llm_adapter_v1 import LiterarySharedLlmAttemptAdapter
from pipeline.literary.shared_runtime_profile_v2 import (
    load_literary_shared_runtime_profile_v2,
)
from pipeline.scripts.run_literary_builder_pilot import DEFAULT_EPUB, _load_document


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = RUNTIME_ROOT.parent
DEFAULT_SCHEDULER_ROOT = Path(r"C:\work\shared-llm-physical-quota-v1")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Literary B1-Scan probe and canary")
    commands = parser.add_subparsers(dest="command", required=True)

    probe = commands.add_parser("probe")
    probe.add_argument("--output-root", type=Path, required=True)
    probe.add_argument("--probe-run-id", required=True)
    probe.add_argument("--source", choices=("openai-row2", "modelapi"), default="modelapi")
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
    prior = canary.add_mutually_exclusive_group()
    prior.add_argument("--prior-cards", type=Path)
    prior.add_argument("--prior-registry-root", type=Path)
    prior.add_argument(
        "--prior-projection-root",
        type=Path,
        help=(
            "root written by run_literary_b1_apply_cross_chapter_decisions_v1; "
            "use this once any cross-chapter hearing has been answered so the "
            "chapter reads reconciled cards instead of raw conflicting ones"
        ),
    )
    canary.add_argument("--previous-summary-root", type=Path)
    canary.add_argument("--run-id", required=True)
    canary.add_argument("--attempt-run-id", required=True)
    canary.add_argument(
        "--runtime-profile",
        type=Path,
        help=(
            "sealed runtime profile to bind; defaults to the source profile. "
            "Use a distinct profile file per generation setting so every run "
            "records which one produced it."
        ),
    )
    canary.add_argument(
        "--without-roster",
        action="store_true",
        help="ablation arm: withhold REGISTRY_ROSTER from the rendered request",
    )
    canary.add_argument("--source", choices=("openai-row2", "modelapi"), default="modelapi")
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
            source_name=args.source,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["status"] == "qualified" else 2
    report = _run_canary(
        output_root=args.output_root,
        capability_root=args.capability_root,
        chapter_id=args.chapter,
        epub_path=args.epub,
        document_path=args.document,
        prior_cards_path=args.prior_cards,
        prior_registry_root=args.prior_registry_root,
        prior_projection_root=args.prior_projection_root,
        previous_summary_root=args.previous_summary_root,
        run_id=args.run_id,
        attempt_run_id=args.attempt_run_id,
        secret=secret,
        commitment=commitment,
        scheduler_root=args.scheduler_root,
        current_head=head,
        source_name=args.source,
        runtime_profile_path=args.runtime_profile,
        include_roster=not args.without_roster,
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
    source_name: str,
) -> dict[str, Any]:
    output, shared = _fresh_roots(output_root)
    plan = build_probe_plan_v1(
        probe_run_id=probe_run_id,
        credential_commitment_sha256=commitment,
        issued_at_utc=_now(),
        profile_path=_source_paths(source_name)[0],
        runtime_profile_path=_source_paths(source_name)[1],
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
        "schema_version": "literary_b1_scan_probe_report_v1",
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
    prior_cards_path: Path | None,
    prior_registry_root: Path | None,
    previous_summary_root: Path | None,
    run_id: str,
    attempt_run_id: str,
    secret: str,
    commitment: str,
    scheduler_root: Path,
    current_head: str,
    source_name: str,
    runtime_profile_path: Path | None = None,
    include_roster: bool = True,
    prior_projection_root: Path | None = None,
) -> dict[str, Any]:
    evidence = validate_capability_evidence(
        _read(Path(capability_root) / "capability_evidence.json")
    )
    if evidence["verdict"] != "qualified":
        raise SystemExit("B1-Scan capability evidence is not qualified")
    runtime_profile = load_literary_shared_runtime_profile_v2(
        Path(runtime_profile_path or _source_paths(source_name)[1]),
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
        chapter = next(
            row for row in document["chapters"] if row["chapter_id"] == chapter_id
        )
    except StopIteration as exc:
        raise SystemExit(f"chapter is absent: {chapter_id}") from exc
    prior_cards, prior_lineage = _load_prior_cards_context(
        prior_cards_path=prior_cards_path,
        prior_registry_root=prior_registry_root,
        prior_projection_root=prior_projection_root,
    )
    chapter_order = next(
        index
        for index, row in enumerate(document["chapters"], start=1)
        if row["chapter_id"] == chapter_id
    )
    previous_summary, global_summary, summary_lineage = _load_summary_context(
        previous_summary_root=previous_summary_root,
        current_chapter_order=chapter_order,
    )
    preset = runtime_profile.role_presets[ROLE_ID]
    memory_token_budget = preset.generation.get("memory_token_budget")
    memory_dormancy_chapters = preset.generation.get(
        "memory_dormancy_chapters", 3
    )
    rendered = render_b1_scan_request_v1(
        chapter=chapter,
        design_doc=DESIGN_DOC,
        prior_cards=prior_cards,
        previous_chapter_summary=previous_summary,
        global_summary=global_summary,
        include_registry_roster=include_roster,
        memory_token_budget=memory_token_budget,
        memory_dormancy_chapters=memory_dormancy_chapters,
        chapter_order_by_id={
            row["chapter_id"]: index
            for index, row in enumerate(document["chapters"], start=1)
        },
    )
    request = shared_b1_scan_request_v1(rendered)
    output, shared = _fresh_roots(output_root)
    output.mkdir(parents=True, exist_ok=False)
    token_preflight = measure_literary_request_token_preflight_v1(
        request,
        prompt_token_cap=int(preset.generation["max_input_tokens"]),
        output_token_cap=int(preset.generation["max_output_tokens"]),
    )
    _write(output / "run_seal.json", {
        "schema_version": "literary_b1_scan_run_seal_v1",
        "git_head": current_head,
        "run_id": run_id,
        "attempt_run_id": attempt_run_id,
        "chapter_id": chapter_id,
        "request_fingerprint": rendered.request_fingerprint,
        "runtime_profile_sha256": runtime_profile.profile_sha256,
        "runtime_profile_path": str(
            Path(runtime_profile_path or _source_paths(source_name)[1]).as_posix()
        ),
        "registry_roster_included": bool(include_roster),
        "token_preflight": token_preflight.to_payload(),
        "capability_evidence_sha256": evidence["evidence_sha256"],
        "cross_chapter_lineage": {
            **prior_lineage,
            **summary_lineage,
        },
        "provider_fallback_allowed": False,
        "production_publish_allowed": False,
    })
    rendered_payload = rendered.to_dict()
    rendered_payload["messages"] = [dict(row) for row in rendered.messages]
    _write(
        output / "request.json",
        {**rendered_payload, "response_schema": request["response_schema"]},
    )
    _write(
        output / "cross_chapter_context.json",
        {
            "chapter_id": chapter_id,
            "chapter_order": chapter_order,
            "prior_card_count": len(prior_cards),
            "prior_packet_count": len(
                rendered.sections["prior_candidate_packets"]
            ),
            "memory_budget_report": rendered.sections.get(
                "memory_budget_report"
            ),
            "summary_context": rendered.sections["summary_context"],
            "lineage": {**prior_lineage, **summary_lineage},
        },
    )
    _write(output / "token_preflight.json", token_preflight.to_payload())
    if not token_preflight.fits_prompt_cap:
        failure_body = {
            "schema_version": "literary_b1_scan_preflight_failure_v1",
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
            capability_binding_key(ROLE_ID, b1_scan_response_schema_v1()): evidence
        },
        run_id=run_id,
        attempt_run_id=attempt_run_id,
        structured_output=None,
        runtime_profile=runtime_profile,
        api_sources_by_alias={
            runtime_profile.role_bindings[ROLE_ID].source_alias: source
        },
    )
    validator_ref = build_literary_code_ref_v1(
        identifier="literary.b1.scan.validator",
        revision="v1",
        callables=(
            b1_scan_response_schema_v1,
            validate_b1_scan_response_v1,
            make_b1_scan_semantic_validator_v1,
        ),
    )
    result = bindings.execute_accepted_request(
        role_id=ROLE_ID,
        stage_id="literary_b1_scan",
        logical_request_id=(
            f"literary_b1_scan_{canonical_hash({'run_id': run_id, 'chapter_id': chapter_id})[:24]}"
        ),
        request=request,
        schema_name="literary_b1_scan_response_v1",
        semantic_validator=make_b1_scan_semantic_validator_v1(
            chapter=chapter, rendered=rendered
        ),
        validator_ref=validator_ref,
        application_contract_id="literary.b1.scan.application",
        application_contract_revision="v1",
        output_dir=output,
        model_reference_mode=MODEL_REF_MODE_CLASSIFIED_V1,
        additional_input_bindings=(
            {"name": "source_chapter", "sha256": canonical_hash(chapter)},
            {"name": "prior_cards", "sha256": canonical_hash(prior_cards)},
            {
                "name": "previous_chapter_summary",
                "sha256": canonical_hash(previous_summary),
            },
            {"name": "global_summary", "sha256": canonical_hash(global_summary)},
        ),
    )
    _write(output / "model_response.json", result.response_payload)
    _write(output / "b1_scan_artifact.json", result.semantic_payload)
    report_body = {
        "schema_version": "literary_b1_scan_canary_report_v1",
        "status": result.status,
        "chapter_id": chapter_id,
        "git_head": current_head,
        "request_fingerprint": rendered.request_fingerprint,
        "artifact_hash": result.semantic_payload["artifact_hash"],
        "metrics": result.semantic_payload["metrics"],
        "cross_chapter_context": {
            "prior_card_count": len(prior_cards),
            "prior_packet_count": len(
                rendered.sections["prior_candidate_packets"]
            ),
            "memory_budget_report": rendered.sections.get(
                "memory_budget_report"
            ),
            "previous_summary_supplied": previous_summary is not None,
            "global_summary_supplied": global_summary is not None,
        },
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


def _source_paths(source_name: str) -> tuple[Path, Path]:
    if source_name == "openai-row2":
        return (
            Path(__file__).resolve().parents[1]
            / "configs"
            / "literary_openai_b1_scan_json_object_probe_v1.json",
            RUNTIME_PROFILE_PATH,
        )
    if source_name == "modelapi":
        return MODELAPI_PROFILE_PATH, MODELAPI_RUNTIME_PROFILE_PATH
    raise SystemExit(f"unsupported B1-Scan source: {source_name}")


def _clean_head() -> str:
    status = subprocess.run(
        ["git", "status", "--short", "--untracked-files=no"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if status:
        raise SystemExit("live B1-Scan command requires a clean tracked worktree")
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


def _read_list(path: Path) -> list[dict[str, Any]]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(value, dict):
        if value.get("schema_version") != "literary_b1_prior_cards_v1":
            raise SystemExit("prior cards envelope has an unknown schema")
        if set(value) != {"cards", "chapter_id", "schema_version"}:
            raise SystemExit("prior cards envelope has an invalid shape")
        if not isinstance(value.get("chapter_id"), str) or not value["chapter_id"]:
            raise SystemExit("prior cards envelope has an invalid chapter_id")
        value = value.get("cards")
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise SystemExit("prior cards JSON must be an array of objects")
    return value


def _load_prior_cards_context(
    *,
    prior_cards_path: Path | None,
    prior_registry_root: Path | None,
    prior_projection_root: Path | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if prior_projection_root is not None:
        # Preferred path once any cross-chapter hearing has been answered: the
        # chapter reads the reconciled view, so a merged referent arrives as one
        # card under all its names and a question already settled is not asked
        # again. Every sealed registry that fed the projection must be supplied.
        root = Path(prior_projection_root)
        projection = _read(root / "reconciled_projection.json")
        ledger = _read(root / "decision_ledger.json")
        verify_decision_ledger_v1(ledger)
        if projection.get("ledger_hash") != ledger.get("ledger_hash"):
            raise SystemExit("reconciled projection does not match its decision ledger")
        registries = []
        for registry_root in sorted(root.glob("source_registry_*.json")):
            registries.append(_read(registry_root))
        if not registries and prior_registry_root is not None:
            registries = [_read(Path(prior_registry_root) / "chapter_registry.json")]
        for registry in registries:
            verify_b1_chapter_registry_v1(registry)
        observed = [r["registry_hash"] for r in registries]
        if sorted(observed) != sorted(projection.get("source_registry_hashes") or []):
            raise SystemExit(
                "supplied registries do not match the ones the projection was built from"
            )
        cards = build_projected_prior_cards_v1(
            registries=registries, projection=projection
        )
        return cards, {
            "prior_input_kind": "reconciled_cross_chapter_projection",
            "prior_registry_hash": None,
            "projection_hash": projection["projection_hash"],
            "decision_ledger_hash": ledger["ledger_hash"],
            "settled_distinct_case_count": len(
                projection.get("resolved_distinct_cases") or []
            ),
            "pending_case_count": len(projection.get("pending_cases") or []),
        }
    if prior_registry_root is None:
        cards = _read_list(prior_cards_path) if prior_cards_path else []
        return cards, {
            "prior_input_kind": "standalone_cards" if prior_cards_path else "none",
            "prior_registry_hash": None,
        }
    registry = _read(Path(prior_registry_root) / "chapter_registry.json")
    verify_b1_chapter_registry_v1(registry)
    cards = build_b1_prior_context_cards_v1(registry)
    return cards, {
        "prior_input_kind": "verified_chapter_registry_identity_projection",
        "prior_registry_hash": registry["registry_hash"],
    }


def _load_summary_context(
    *, previous_summary_root: Path | None, current_chapter_order: int
) -> tuple[str | None, str | None, dict[str, Any]]:
    if previous_summary_root is None:
        return None, None, {
            "previous_summary_artifact_hash": None,
            "capsule_log_hash": None,
        }
    root = Path(previous_summary_root)
    artifact = verify_b0_summary_artifact_v1(
        _read(root / "chapter_summary_artifact.json")
    )
    if artifact.get("chapter_order") != current_chapter_order - 1:
        raise SystemExit("previous summary is not from the adjacent chapter")
    summary = artifact.get("summary")
    if not isinstance(summary, Mapping) or not isinstance(
        summary.get("chapter_summary"), str
    ):
        raise SystemExit("previous chapter summary is malformed")

    capsule_log = _read(root / "capsule_log.json")
    observed_hash = capsule_log.get("capsule_log_hash")
    capsule_body = dict(capsule_log)
    capsule_body.pop("capsule_log_hash", None)
    if (
        capsule_log.get("schema_version") != CAPSULE_LOG_SCHEMA_VERSION
        or observed_hash != canonical_hash(capsule_body)
        or capsule_log.get("authority") != "orientation_only"
        or capsule_log.get("append_only") is not True
    ):
        raise SystemExit("capsule log is invalid")
    capsules = capsule_log.get("capsules")
    if not isinstance(capsules, list) or not capsules:
        raise SystemExit("capsule log is empty or malformed")
    if any(
        not isinstance(row, Mapping)
        or not isinstance(row.get("text"), str)
        or not row["text"].strip()
        for row in capsules
    ):
        raise SystemExit("capsule log contains a malformed capsule")
    orders = [row.get("chapter_order") for row in capsules]
    if (
        not all(isinstance(order, int) and order > 0 for order in orders)
        or orders != sorted(orders)
        or len(orders) != len(set(orders))
        or orders != list(range(1, current_chapter_order))
    ):
        raise SystemExit("capsule log chapter order is invalid")
    latest = capsules[-1]
    if (
        latest.get("summary_artifact_hash") != artifact.get("artifact_hash")
        or latest.get("chapter_id") != artifact.get("chapter_id")
    ):
        raise SystemExit("latest capsule does not bind the previous summary")
    global_summary = "\n".join(
        f"[{row['chapter_id']}] {row['text'].strip()}" for row in capsules
    )
    return summary["chapter_summary"].strip(), global_summary, {
        "previous_summary_artifact_hash": artifact["artifact_hash"],
        "capsule_log_hash": observed_hash,
    }


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
