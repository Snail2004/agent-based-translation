from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
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
from pipeline.literary.b2_slim_speaker_recovery_v1 import (
    apply_b2_slim_speaker_recovery_decision_v1,
    build_b2_slim_speaker_recovery_index_v1,
    load_b2_slim_speaker_source_v1,
    request_payload_v1,
)
from pipeline.literary.b2_recovery_v1 import (
    B2RecoveryContractError,
    validate_registry_recovery_component_quarantines_v1,
    verify_registry_recovery_decision_v1,
)
from pipeline.literary.b2_recovery_batch_v1 import (
    MAX_BATCH_COMPONENTS_V1,
    render_registry_recovery_batch_request_v1,
    validate_registry_recovery_batch_response_v1,
)
from pipeline.literary.chapter_cycle_shared_runtime_v1 import (
    LiterarySharedRunnerBindingsV1,
    capability_binding_key,
)
from pipeline.literary.checkpoint import canonical_hash, file_sha256
from pipeline.literary.model_ref_v1 import MODEL_REF_MODE_CLASSIFIED_V1
from pipeline.literary.modelapi_b2_speaker_recovery_capability_probe_v1 import (
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
from pipeline.literary.structured_output_policy_v1 import validate_structured_payload
from pipeline.scripts.run_chapter_registry_v4_real import FROZEN_DB_SHA256


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_ROOT = RUNTIME_ROOT.parent
DEFAULT_CREDENTIAL_FILE = REPOSITORY_ROOT / "LOCAL-GPT-GATEWAY.txt"
DEFAULT_FROZEN_DB = RUNTIME_ROOT / "data" / "jobs" / "d2l_p1" / "memory.sqlite3"
DEFAULT_SCHEDULER_ROOT = Path(r"C:\work\shared-llm-physical-quota-v1")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ModelAPI B2 speaker recovery")
    parser.add_argument("--credential-file", type=Path, default=DEFAULT_CREDENTIAL_FILE)
    parser.add_argument("--scheduler-root", type=Path, default=DEFAULT_SCHEDULER_ROOT)
    commands = parser.add_subparsers(dest="command", required=True)
    probe = commands.add_parser("probe")
    probe.add_argument("--output-root", type=Path, required=True)
    probe.add_argument("--probe-run-id", required=True)
    canary = commands.add_parser("canary")
    canary.add_argument("--b2-root", type=Path, required=True)
    canary.add_argument("--output-root", type=Path, required=True)
    canary.add_argument("--capability-root", type=Path, required=True)
    canary.add_argument("--run-id", required=True)
    canary.add_argument("--attempt-run-id", required=True)
    canary.add_argument("--runtime-profile", type=Path)
    canary.add_argument("--frozen-db", type=Path, default=DEFAULT_FROZEN_DB)
    canary.add_argument(
        "--replay-semantic-rejection",
        type=Path,
        action="append",
        default=[],
        help=(
            "revalidate a complete stored semantic-rejection payload and skip "
            "that exact request when it is now accepted"
        ),
    )
    canary.add_argument(
        "--replay-batch-decisions-file",
        type=Path,
        action="append",
        default=[],
        help=(
            "reuse a stored list of already validated batch decisions and call "
            "only request batches not covered by that list"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    head = _clean_head()
    if args.command == "canary":
        _validate_frozen_db(args.frozen_db)
    secret = _credential(args.credential_file)
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
        b2_root=args.b2_root,
        output_root=args.output_root,
        capability_root=args.capability_root,
        run_id=args.run_id,
            attempt_run_id=args.attempt_run_id,
            frozen_db=args.frozen_db,
            replay_semantic_rejections=args.replay_semantic_rejection,
            replay_batch_decision_files=args.replay_batch_decisions_file,
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
    report_body = {
        "schema_version": "literary_b2_speaker_recovery_probe_report_v1",
        "status": result["status"],
        "provider_called": result["provider_called"],
        "probe_seal_sha256": result["probe_seal_sha256"],
        "receipt_sha256": result["receipt"]["receipt_sha256"],
        "evidence_sha256": result["capability_evidence"]["evidence_sha256"],
        "usage": deepcopy_mapping(result["receipt"].get("usage")),
        "failure": deepcopy_mapping(result["receipt"].get("failure")),
        "mandatory_stop_observed": True,
        "normal_output_created": False,
        "production_publish_performed": False,
    }
    report = {**report_body, "report_hash": canonical_hash(report_body)}
    _write(output / "probe_report.json", report)
    return report


def _run_canary(
    *,
    b2_root: Path,
    output_root: Path,
    capability_root: Path,
    run_id: str,
    attempt_run_id: str,
    frozen_db: Path,
    replay_semantic_rejections: Sequence[Path],
    replay_batch_decision_files: Sequence[Path],
    secret: str,
    commitment: str,
    scheduler_root: Path,
    current_head: str,
    runtime_profile_path: Path | None = None,
) -> dict[str, Any]:
    _validate_frozen_db(frozen_db)
    chapter, interaction_requests = load_b2_slim_speaker_source_v1(b2_root)
    index = build_b2_slim_speaker_recovery_index_v1(
        chapter_artifact=chapter, interaction_requests=interaction_requests
    )
    component_ids = [
        row["component_id"]
        for row in index["registry_components"]
        if not row["overflow"]
    ]
    if not component_ids:
        raise SystemExit("B2 source has no pending speaker-attribution ticket")
    if any(row.get("overflow") for row in index["registry_components"]):
        raise SystemExit("speaker recovery component exceeds its sealed cap")
    all_component_batches = [
        component_ids[start : start + MAX_BATCH_COMPONENTS_V1]
        for start in range(0, len(component_ids), MAX_BATCH_COMPONENTS_V1)
    ]
    all_requests = [
        render_registry_recovery_batch_request_v1(
            index=index,
            component_ids=batch,
        )
        for batch in all_component_batches
    ]
    replayed_decisions: list[dict[str, Any]] = []
    replayed_fingerprints: set[str] = set()
    replayed_diagnostic_hashes: list[str] = []
    for path in replay_semantic_rejections:
        decision, fingerprint, diagnostic_hash = (
            _replay_semantic_rejection_v1(
                path=path,
                index=index,
                component_batches=all_component_batches,
                requests=all_requests,
            )
        )
        if fingerprint in replayed_fingerprints:
            raise SystemExit("speaker recovery replay repeats a request")
        replayed_fingerprints.add(fingerprint)
        replayed_decisions.append(decision)
        replayed_diagnostic_hashes.append(diagnostic_hash)
    replayed_batch_file_hashes: list[str] = []
    for path in replay_batch_decision_files:
        file_decisions, fingerprints, file_hash = _replay_batch_decisions_file_v1(
            path=path,
            index=index,
            component_batches=all_component_batches,
            requests=all_requests,
        )
        if replayed_fingerprints.intersection(fingerprints):
            raise SystemExit("speaker recovery replay repeats a request")
        replayed_fingerprints.update(fingerprints)
        replayed_decisions.extend(file_decisions)
        replayed_batch_file_hashes.append(file_hash)
    live_pairs = [
        (batch, request)
        for batch, request in zip(
            all_component_batches,
            all_requests,
            strict=True,
        )
        if request.request_fingerprint not in replayed_fingerprints
    ]
    evidence = validate_capability_evidence(
        _read(Path(capability_root) / "capability_evidence.json")
    )
    if evidence["verdict"] != "qualified":
        raise SystemExit("speaker recovery capability is not qualified")
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
    output, shared = _fresh_roots(output_root)
    output.mkdir(parents=True, exist_ok=False)
    seal_body = {
        "schema_version": "literary_b2_speaker_recovery_run_seal_v1",
        "git_head": current_head,
        "run_id": run_id,
        "attempt_run_id": attempt_run_id,
        "chapter_id": chapter["chapter_id"],
        "source_b2_root": str(Path(b2_root).resolve()),
        "source_b2_artifact_hash": chapter["artifact_hash"],
        "recovery_index_hash": index["recovery_index_hash"],
        "request_fingerprints": [
            request.request_fingerprint for request in all_requests
        ],
        "component_batches": all_component_batches,
        "replayed_request_fingerprints": sorted(replayed_fingerprints),
        "replayed_diagnostic_hashes": replayed_diagnostic_hashes,
        "replayed_batch_decision_file_hashes": replayed_batch_file_hashes,
        "provider_call_cap": len(live_pairs),
        "runtime_profile_sha256": runtime.profile_sha256,
        "capability_evidence_sha256": evidence["evidence_sha256"],
        "frozen_db_sha256": FROZEN_DB_SHA256,
        "provider_fallback_allowed": False,
        "transport_retry_max": 0,
        "semantic_retry_max": 0,
        "accepted_turn_reinspection_allowed": False,
        "unticketed_turn_mutation_allowed": False,
        "production_publish_allowed": False,
        "sealed_at": _now(),
    }
    seal = {**seal_body, "seal_hash": canonical_hash(seal_body)}
    _write(output / "run_seal.json", seal)
    _write(output / "recovery_index.json", index)
    request_set_body = {
        "schema_version": "literary_b2_speaker_recovery_request_set_v1",
        "recovery_index_hash": index["recovery_index_hash"],
        "requests": [request_payload_v1(request) for request in all_requests],
    }
    _write(
        output / "request.json",
        {
            **request_set_body,
            "request_set_hash": canonical_hash(request_set_body),
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
            capability_binding_key(ROLE_ID, request.response_schema): evidence
            for request in all_requests
        },
        run_id=run_id,
        attempt_run_id=attempt_run_id,
        structured_output=None,
        runtime_profile=runtime,
        api_sources_by_alias={runtime.role_bindings[ROLE_ID].source_alias: source},
    )
    accepted_batches = []
    decisions = list(replayed_decisions)
    for batch_ordinal, (batch, request) in enumerate(
        live_pairs,
        start=1,
    ):
        def validate(
            payload: Mapping[str, Any],
            *,
            _batch: list[str] = batch,
            _request=request,
        ) -> Mapping[str, Any]:
            validate_structured_payload(
                payload,
                canonical_schema=_request.response_schema,
            )
            return validate_registry_recovery_batch_response_v1(
                payload,
                index=index,
                component_ids=_batch,
                request_fingerprint=_request.request_fingerprint,
            )

        accepted = bindings.execute_accepted_request(
            role_id=ROLE_ID,
            stage_id="literary_b2_speaker_recovery",
            logical_request_id=(
                "literary_b2_speaker_recovery_"
                + canonical_hash(
                    {
                        "run_id": run_id,
                        "source_b2_artifact_hash": chapter["artifact_hash"],
                        "recovery_index_hash": index["recovery_index_hash"],
                        "batch_ordinal": batch_ordinal,
                        "component_ids": batch,
                    }
                )[:24]
            ),
            request={
                "request_fingerprint": request.request_fingerprint,
                "messages": [dict(row) for row in request.messages],
                "response_schema": request.response_schema,
            },
            schema_name="literary_b2_registry_recovery_batch_v1",
            semantic_validator=validate,
            validator_ref=validator_ref_v1(),
            application_contract_id="literary.b2.speaker_recovery.application",
            application_contract_revision="v1",
            output_dir=output / "speaker_recovery" / f"batch_{batch_ordinal:02d}",
            model_reference_mode=MODEL_REF_MODE_CLASSIFIED_V1,
            additional_input_bindings=(
                {"name": "source_b2_artifact", "sha256": chapter["artifact_hash"]},
                {
                    "name": "speaker_recovery_index",
                    "sha256": index["recovery_index_hash"],
                },
            ),
        )
        accepted_batches.append(accepted)
        decisions.append(dict(accepted.semantic_payload))
    decision = _consolidate_batch_decisions_v1(
        chapter_id=chapter["chapter_id"],
        recovery_index_hash=index["recovery_index_hash"],
        expected_component_ids=component_ids,
        batch_decisions=decisions,
    )
    overlay = apply_b2_slim_speaker_recovery_decision_v1(
        chapter_artifact=chapter, index=index, batch_decision=decision
    )
    _write(
        output / "model_response.json",
        {
            "schema_version": "literary_b2_speaker_recovery_response_set_v1",
            "responses": [
                dict(accepted.response_payload) for accepted in accepted_batches
            ],
        },
    )
    _write(output / "batch_decisions.json", decisions)
    _write(output / "batch_decision.json", decision)
    _write(output / "speaker_recovery_artifact.json", overlay)
    usage_rows = [
        dict(accepted.usage)
        for accepted in accepted_batches
        if accepted.usage is not None
    ]
    usage = {
        field: sum(int(row.get(field) or 0) for row in usage_rows)
        for field in ("prompt_tokens", "completion_tokens", "total_tokens")
    }
    report_body = {
        "schema_version": "literary_b2_speaker_recovery_canary_report_v1",
        "status": "semantic_accepted",
        "chapter_id": chapter["chapter_id"],
        "git_head": current_head,
        "source_b2_artifact_hash": chapter["artifact_hash"],
        "recovery_index_hash": index["recovery_index_hash"],
        "speaker_recovery_artifact_hash": overlay["artifact_hash"],
        "provider_calls": len(accepted_batches),
        "batch_count": len(all_component_batches),
        "replayed_batch_count": len(replayed_decisions),
        "component_count": len(component_ids),
        "ticket_count": len(index["registry_gap_tickets"]),
        "speaker_overlay_count": len(overlay["speaker_overlays"]),
        "quarantined_ticket_action_count": len(
            overlay.get("quarantined_ticket_actions") or []
        ),
        "usage": usage,
        "usage_by_batch": usage_rows,
        "provider_called": any(
            accepted.provider_called for accepted in accepted_batches
        ),
        "accepted_turn_reinspection_performed": False,
        "unticketed_turn_mutation_performed": False,
        "source_artifact_mutated": False,
        "identity_or_claim_mutation_performed": False,
        "book_global_authority_granted": False,
        "production_publish_performed": False,
        "mandatory_stop_observed": True,
        "frozen_db_sha256_after": file_sha256(frozen_db).upper(),
    }
    report = {**report_body, "report_hash": canonical_hash(report_body)}
    _write(output / "canary_report.json", report)
    return report


def _replay_semantic_rejection_v1(
    *,
    path: Path,
    index: Mapping[str, Any],
    component_batches: Sequence[Sequence[str]],
    requests: Sequence[Any],
) -> tuple[dict[str, Any], str, str]:
    diagnostic = _read(Path(path).resolve())
    observed_hash = diagnostic.get("diagnostic_sha256")
    diagnostic_body = {
        key: value
        for key, value in diagnostic.items()
        if key != "diagnostic_sha256"
    }
    if (
        diagnostic.get("schema_version")
        != "literary_semantic_rejection_diagnostic_v1"
        or diagnostic.get("role_id") != ROLE_ID
        or diagnostic.get("stage_id") != "literary_b2_speaker_recovery"
        or diagnostic.get("semantic_authority_granted") is not False
        or not isinstance(observed_hash, str)
        or canonical_hash(diagnostic_body) != observed_hash
    ):
        raise SystemExit("speaker recovery replay diagnostic is malformed")
    payload_record = diagnostic.get("semantic_payload")
    if not isinstance(payload_record, Mapping):
        raise SystemExit("speaker recovery replay has no semantic payload")
    excerpt = payload_record.get("excerpt")
    if (
        payload_record.get("excerpt_truncated") is not False
        or not isinstance(excerpt, str)
        or int(payload_record.get("utf8_bytes") or -1)
        != len(excerpt.encode("utf-8"))
        or hashlib.sha256(excerpt.encode("utf-8")).hexdigest()
        != payload_record.get("sha256")
    ):
        raise SystemExit("speaker recovery replay payload is incomplete")
    fingerprint = diagnostic.get("request_fingerprint")
    matching = [
        (batch, request)
        for batch, request in zip(component_batches, requests, strict=True)
        if request.request_fingerprint == fingerprint
    ]
    if len(matching) != 1:
        raise SystemExit("speaker recovery replay request is not in this run")
    batch, request = matching[0]
    try:
        payload = json.loads(excerpt)
    except ValueError as exc:
        raise SystemExit("speaker recovery replay payload is not JSON") from exc
    if not isinstance(payload, Mapping):
        raise SystemExit("speaker recovery replay payload is not an object")
    validate_structured_payload(payload, canonical_schema=request.response_schema)
    decision = validate_registry_recovery_batch_response_v1(
        payload,
        index=index,
        component_ids=batch,
        request_fingerprint=request.request_fingerprint,
    )
    return dict(decision), str(fingerprint), observed_hash


def _replay_batch_decisions_file_v1(
    *,
    path: Path,
    index: Mapping[str, Any],
    component_batches: Sequence[Sequence[str]],
    requests: Sequence[Any],
) -> tuple[list[dict[str, Any]], set[str], str]:
    resolved = Path(path).resolve()
    try:
        raw_bytes = resolved.read_bytes()
        payload = json.loads(raw_bytes.decode("utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise SystemExit(
            f"cannot read speaker recovery batch decisions: {resolved}"
        ) from exc
    if not isinstance(payload, list) or not payload:
        raise SystemExit("speaker recovery batch decision replay must be a non-empty list")

    request_by_fingerprint = {
        request.request_fingerprint: (list(batch), request)
        for batch, request in zip(component_batches, requests, strict=True)
    }
    decisions: list[dict[str, Any]] = []
    fingerprints: set[str] = set()
    for raw_decision in payload:
        if not isinstance(raw_decision, Mapping):
            raise SystemExit("speaker recovery replay batch decision is malformed")
        decision = deepcopy_mapping(raw_decision)
        observed_hash = decision.get("batch_decision_hash")
        body = {
            key: value
            for key, value in decision.items()
            if key != "batch_decision_hash"
        }
        fingerprint = decision.get("request_fingerprint")
        matching = request_by_fingerprint.get(str(fingerprint))
        if (
            decision.get("schema_version")
            != "literary_b2_registry_recovery_batch_decision_v1"
            or decision.get("chapter_id") != index.get("chapter_id")
            or decision.get("recovery_index_hash")
            != index.get("recovery_index_hash")
            or not isinstance(observed_hash, str)
            or canonical_hash(body) != observed_hash
            or matching is None
            or fingerprint in fingerprints
        ):
            raise SystemExit("speaker recovery replay batch lineage differs")
        batch, _request = matching
        rows = decision.get("component_decisions")
        if not isinstance(rows, list):
            raise SystemExit("speaker recovery replay components are malformed")
        observed_components: list[str] = []
        try:
            for raw_component in rows:
                if not isinstance(raw_component, Mapping):
                    raise B2RecoveryContractError(
                        "speaker recovery component decision must be an object"
                    )
                verified_component = verify_registry_recovery_decision_v1(
                    raw_component,
                    index=index,
                )
                observed_components.append(verified_component["component_id"])
        except B2RecoveryContractError as exc:
            raise SystemExit(
                "speaker recovery replay component decision is invalid"
            ) from exc
        try:
            quarantined_components = (
                validate_registry_recovery_component_quarantines_v1(
                    index=index,
                    quarantines=decision.get("quarantined_components") or [],
                )
            )
        except B2RecoveryContractError as exc:
            raise SystemExit(
                "speaker recovery replay component quarantine is invalid"
            ) from exc
        quarantined_ids = [
            row["component_id"] for row in quarantined_components
        ]
        if (
            len(observed_components) != len(set(observed_components))
            or len(quarantined_ids) != len(set(quarantined_ids))
            or set(observed_components).intersection(quarantined_ids)
            or set(observed_components).union(quarantined_ids) != set(batch)
        ):
            raise SystemExit(
                "speaker recovery replay decisions and quarantines do not exact-cover its request"
            )
        fingerprints.add(str(fingerprint))
        decisions.append(decision)
    return decisions, fingerprints, hashlib.sha256(raw_bytes).hexdigest()


def _consolidate_batch_decisions_v1(
    *,
    chapter_id: str,
    recovery_index_hash: str,
    expected_component_ids: Sequence[str],
    batch_decisions: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    expected = [str(value) for value in expected_component_ids]
    if not expected or len(expected) != len(set(expected)):
        raise SystemExit("speaker recovery component index is malformed")
    component_decisions: list[dict[str, Any]] = []
    observed: list[str] = []
    quarantined_components: list[dict[str, Any]] = []
    quarantined_ids: list[str] = []
    source_hashes: list[str] = []
    normalizations: list[dict[str, Any]] = []
    for raw in batch_decisions:
        decision = deepcopy_mapping(raw)
        observed_hash = decision.get("batch_decision_hash")
        body = {
            key: value
            for key, value in decision.items()
            if key != "batch_decision_hash"
        }
        if not isinstance(observed_hash, str) or canonical_hash(body) != observed_hash:
            raise SystemExit("speaker recovery batch decision hash differs")
        if (
            decision.get("chapter_id") != chapter_id
            or decision.get("recovery_index_hash") != recovery_index_hash
        ):
            raise SystemExit("speaker recovery batch lineage differs")
        source_hashes.append(observed_hash)
        for row in decision.get("component_decisions") or []:
            if not isinstance(row, Mapping):
                raise SystemExit("speaker recovery component decision is malformed")
            component_id = row.get("component_id")
            if not isinstance(component_id, str) or not component_id:
                raise SystemExit("speaker recovery component decision lacks an id")
            observed.append(component_id)
            component_decisions.append(deepcopy_mapping(row))
        for row in decision.get("quarantined_components") or []:
            if not isinstance(row, Mapping):
                raise SystemExit(
                    "speaker recovery component quarantine is malformed"
                )
            component_id = row.get("component_id")
            if not isinstance(component_id, str) or not component_id:
                raise SystemExit(
                    "speaker recovery component quarantine lacks an id"
                )
            quarantined_ids.append(component_id)
            quarantined_components.append(deepcopy_mapping(row))
        for row in decision.get("contract_normalizations") or []:
            if isinstance(row, Mapping):
                normalizations.append(deepcopy_mapping(row))
    if (
        len(observed) != len(set(observed))
        or len(quarantined_ids) != len(set(quarantined_ids))
        or set(observed).intersection(quarantined_ids)
        or set(observed).union(quarantined_ids) != set(expected)
    ):
        raise SystemExit(
            "speaker recovery batch decisions and quarantines do not exact-cover components"
        )
    consolidated_body = {
        "schema_version": "literary_b2_registry_recovery_consolidated_decision_v1",
        "validator_version": "speaker_recovery_batch_fold_v1",
        "recovery_index_hash": recovery_index_hash,
        "chapter_id": chapter_id,
        "source_batch_decision_hashes": source_hashes,
        "component_decisions": sorted(
            component_decisions,
            key=lambda row: str(row["component_id"]),
        ),
        "quarantined_components": sorted(
            quarantined_components,
            key=lambda row: str(row["component_id"]),
        ),
        "contract_normalizations": normalizations,
    }
    return {
        **consolidated_body,
        "batch_decision_hash": canonical_hash(consolidated_body),
    }


def _validate_frozen_db(path: Path) -> None:
    if not path.is_file():
        raise SystemExit(f"frozen DB does not exist: {path}")
    if file_sha256(path).upper() != FROZEN_DB_SHA256:
        raise SystemExit("frozen DB differs from accepted baseline")


def _fresh_roots(output_root: Path) -> tuple[Path, Path]:
    output = output_root.resolve()
    shared = output.parent / f"{output.name}-shared"
    if output.exists() or shared.exists():
        raise SystemExit("output or shared root already exists")
    return output, shared


def _credential(path: Path) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SystemExit("cannot read external ModelAPI credential") from exc
    if not value or any(char.isspace() for char in value):
        raise SystemExit("external ModelAPI credential is malformed")
    return value


def _clean_head() -> str:
    import subprocess

    root = REPOSITORY_ROOT
    status = subprocess.run(
        ["git", "status", "--short", "--untracked-files=no"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if status:
        raise SystemExit("tracked worktree must be clean")
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"cannot read JSON artifact: {path}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"JSON artifact must be an object: {path}")
    return value


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def deepcopy_mapping(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): deepcopy_mapping(item) for key, item in value.items()}
    if isinstance(value, list):
        return [deepcopy_mapping(item) for item in value]
    return value


if __name__ == "__main__":
    raise SystemExit(main())
