from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
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
from pipeline.literary.b2_live_canary_v1 import (
    execute_b2_frame_live_v1,
    execute_b2_interactions_live_v1,
    prepare_b2_ch1_canary_v1,
)
from pipeline.literary.b2_prompts_v3 import (
    b2_frame_response_schema_v2,
    b2_interaction_response_schema_v3,
)
from pipeline.literary.chapter_cycle_shared_runtime_v1 import (
    BACKEND_MODE_SHARED_V1,
    LiterarySharedRunnerBindingsV1,
    capability_binding_key,
)
from pipeline.literary.checkpoint import canonical_hash, file_sha256
from pipeline.literary.modelapi_b2_json_object_capability_probe_v1 import (
    RUNTIME_PROFILE_PATH,
    build_modelapi_b2_probe_plan_v1,
    execute_modelapi_b2_probe_once_v1,
)
from pipeline.literary.shared_llm_adapter_v1 import LiterarySharedLlmAttemptAdapter
from pipeline.literary.shared_runtime_profile_v2 import (
    load_literary_shared_runtime_profile_v2,
)
from pipeline.scripts.run_chapter_registry_v4_real import FROZEN_DB_SHA256


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_ROOT = RUNTIME_ROOT.parent
DEFAULT_CANARY_PROFILE = (
    RUNTIME_ROOT
    / "pipeline"
    / "configs"
    / "literary_b2_ch1_openai_shared_slim_canary_v1.json"
)
DEFAULT_FROZEN_DB = RUNTIME_ROOT / "data" / "jobs" / "d2l_p1" / "memory.sqlite3"
DEFAULT_CREDENTIAL_FILE = REPOSITORY_ROOT / "LOCAL-GPT-GATEWAY.txt"
DEFAULT_SCHEDULER_ROOT = Path(r"C:\work\shared-llm-physical-quota-v1")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--credential-file", type=Path, default=DEFAULT_CREDENTIAL_FILE)
    parser.add_argument("--scheduler-root", type=Path, default=DEFAULT_SCHEDULER_ROOT)
    subparsers = parser.add_subparsers(dest="command", required=True)

    probe = subparsers.add_parser("probe")
    probe.add_argument("--probe-name", choices=("frame", "interaction"), required=True)
    probe.add_argument("--probe-run-id", required=True)
    probe.add_argument("--output-root", type=Path, required=True)

    run = subparsers.add_parser("run")
    run.add_argument("--source-run-root", type=Path, required=True)
    run.add_argument("--output-root", type=Path, required=True)
    run.add_argument("--frame-capability-root", type=Path, required=True)
    run.add_argument("--interaction-capability-root", type=Path, required=True)
    run.add_argument("--run-id", required=True)
    run.add_argument("--attempt-run-id", required=True)
    run.add_argument("--canary-profile", type=Path, default=DEFAULT_CANARY_PROFILE)
    run.add_argument(
        "--runtime-profile",
        type=Path,
        default=RUNTIME_PROFILE_PATH,
        help="versioned Shared LLM runtime profile for this B2 run",
    )
    run.add_argument(
        "--prior-b2-root",
        type=Path,
        default=None,
        help="preceding chapter B2 output root when the selected profile seals frame carry",
    )
    run.add_argument("--frozen-db", type=Path, default=DEFAULT_FROZEN_DB)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    head = _clean_head()
    if args.command == "run":
        _validate_frozen_db(args.frozen_db)
    secret = _credential(args.credential_file)
    if args.command == "probe":
        report = _run_probe(
            probe_name=args.probe_name,
            probe_run_id=args.probe_run_id,
            output_root=args.output_root,
            secret=secret,
            scheduler_root=args.scheduler_root,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["status"] == "qualified" else 2
    report = _run_canary(
        source_run_root=args.source_run_root,
        output_root=args.output_root,
        frame_capability_root=args.frame_capability_root,
        interaction_capability_root=args.interaction_capability_root,
        run_id=args.run_id,
        attempt_run_id=args.attempt_run_id,
        canary_profile=args.canary_profile,
        runtime_profile=args.runtime_profile,
        prior_b2_root=args.prior_b2_root,
        frozen_db=args.frozen_db,
        secret=secret,
        scheduler_root=args.scheduler_root,
        current_head=head,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def _run_probe(
    *,
    probe_name: str,
    probe_run_id: str,
    output_root: Path,
    secret: str,
    scheduler_root: Path,
) -> dict[str, Any]:
    output, shared = _fresh_roots(output_root)
    plan = build_modelapi_b2_probe_plan_v1(
        probe_name=probe_name,
        probe_run_id=probe_run_id,
        credential_commitment_sha256=credential_commitment(secret),
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
    result = execute_modelapi_b2_probe_once_v1(probe=probe, plan=plan)
    _write(output / "probe_receipt.json", result["receipt"])
    _write(output / "capability_evidence.json", result["capability_evidence"])
    report_body = {
        "schema_version": "literary_modelapi_b2_probe_report_v1",
        "status": result["status"],
        "probe_name": probe_name,
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
    source_run_root: Path,
    output_root: Path,
    frame_capability_root: Path,
    interaction_capability_root: Path,
    run_id: str,
    attempt_run_id: str,
    canary_profile: Path,
    runtime_profile: Path,
    prior_b2_root: Path | None,
    frozen_db: Path,
    secret: str,
    scheduler_root: Path,
    current_head: str,
) -> dict[str, Any]:
    _validate_frozen_db(frozen_db)
    runtime = _build_runtime(
        output_root=output_root,
        frame_capability_root=frame_capability_root,
        interaction_capability_root=interaction_capability_root,
        run_id=run_id,
        attempt_run_id=attempt_run_id,
        secret=secret,
        scheduler_root=scheduler_root,
        runtime_profile=runtime_profile,
    )
    prepare_b2_ch1_canary_v1(
        source_run_root=source_run_root,
        output_root=output_root,
        canary_profile_path=canary_profile,
        credential_root=None,
        frozen_db=frozen_db,
        current_git_head=current_head,
        prior_b2_root=prior_b2_root,
        backend_mode=BACKEND_MODE_SHARED_V1,
        shared_runtime=runtime,
    )
    frame = execute_b2_frame_live_v1(
        output_root=output_root,
        credential_root=None,
        frozen_db=frozen_db,
        current_git_head=current_head,
        backend_mode=BACKEND_MODE_SHARED_V1,
        shared_runtime=runtime,
    )
    chapter = execute_b2_interactions_live_v1(
        output_root=output_root,
        credential_root=None,
        frozen_db=frozen_db,
        current_git_head=current_head,
        backend_mode=BACKEND_MODE_SHARED_V1,
        shared_runtime=runtime,
    )
    return _canary_summary(frame=frame, chapter=chapter)


def _canary_summary(
    *, frame: Mapping[str, Any], chapter: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "status": "complete",
        "chapter_id": chapter["chapter_id"],
        "frame_artifact_hash": frame["artifact_hash"],
        "chapter_artifact_hash": chapter["artifact_hash"],
        "frame_segments": len(frame["frame_segments"]),
        "speaker_turns": len(chapter["speaker_turns"]),
        "salient_events": len(chapter["salient_events"]),
        "review_requests": len(chapter["review_requests"]),
        "production_publish_performed": False,
    }


def _validate_frozen_db(path: Path) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"frozen DB does not exist: {path}")
    actual = file_sha256(path).upper()
    if actual != FROZEN_DB_SHA256:
        raise ValueError(
            "frozen DB hash mismatch: "
            f"expected {FROZEN_DB_SHA256}, observed {actual}"
        )


def _build_runtime(
    *,
    output_root: Path,
    frame_capability_root: Path,
    interaction_capability_root: Path,
    run_id: str,
    attempt_run_id: str,
    secret: str,
    scheduler_root: Path,
    runtime_profile: Path,
) -> LiterarySharedRunnerBindingsV1:
    role_schemas = {
        "literary.b2.frame": b2_frame_response_schema_v2(),
        "literary.b2.interaction": b2_interaction_response_schema_v3(),
    }
    evidence_by_role = {
        "literary.b2.frame": validate_capability_evidence(
            _read(Path(frame_capability_root) / "capability_evidence.json")
        ),
        "literary.b2.interaction": validate_capability_evidence(
            _read(Path(interaction_capability_root) / "capability_evidence.json")
        ),
    }
    if any(row["verdict"] != "qualified" for row in evidence_by_role.values()):
        raise SystemExit("B2 capability evidence is not qualified")
    profile = load_literary_shared_runtime_profile_v2(
        runtime_profile, expected_role_ids=set(role_schemas)
    )
    source_binding = dict(profile.source_binding_for("literary.b2.frame"))
    if dict(profile.source_binding_for("literary.b2.interaction")) != source_binding:
        raise SystemExit("B2 roles do not share the sealed ModelAPI source")
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
        "credential_commitment": credential_commitment(secret),
        "physical_quota_bucket_id": source_binding["physical_quota_bucket_id"],
        "enabled": True,
    }
    shared = Path(str(Path(output_root).resolve()) + "-shared")
    shared.mkdir(parents=True, exist_ok=False)
    store = ContentAddressedArtifactStore(shared / "artifacts")
    backend = SharedLlmBackend(
        credential_provider=MappingCredentialProvider(
            {source_binding["credential_ref"]: secret}
        ),
        scheduler=PhysicalQuotaScheduler(scheduler_root),
        ledger=SharedLlmAttemptLedger(shared / "attempt_ledger.sqlite3"),
        response_cache=ApplicationResponseCache(
            index_path=shared / "response_cache.sqlite3", artifact_store=store
        ),
        sender=UrllibTransportSender(),
    )
    capabilities = {
        capability_binding_key(role_id, schema): evidence_by_role[role_id]
        for role_id, schema in role_schemas.items()
    }
    return LiterarySharedRunnerBindingsV1(
        adapter=LiterarySharedLlmAttemptAdapter(backend=backend),
        api_source=None,
        capabilities=capabilities,
        run_id=run_id,
        attempt_run_id=attempt_run_id,
        structured_output=None,
        runtime_profile=profile,
        api_sources_by_alias={source_binding["source_alias"]: source},
    )


def _credential(path: Path) -> str:
    try:
        value = Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SystemExit("ModelAPI credential file is unavailable") from exc
    if not value or any(char.isspace() for char in value):
        raise SystemExit("ModelAPI credential is malformed")
    return value


def _clean_head() -> str:
    dirty = subprocess.run(
        ["git", "status", "--short", "--untracked-files=no"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if dirty:
        raise SystemExit("live B2 command requires a clean tracked worktree")
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _fresh_roots(output_root: Path) -> tuple[Path, Path]:
    output = Path(output_root).resolve()
    shared = Path(str(output) + "-shared")
    if output.exists() or shared.exists():
        raise SystemExit("output or shared root already exists")
    return output, shared


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as exc:
        raise SystemExit(f"cannot read JSON artifact: {path}") from exc
    if not isinstance(value, Mapping):
        raise SystemExit(f"JSON artifact must be an object: {path}")
    return dict(value)


def _write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def deepcopy_mapping(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): deepcopy_mapping(item) for key, item in value.items()}
    if isinstance(value, list):
        return [deepcopy_mapping(item) for item in value]
    return value


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
