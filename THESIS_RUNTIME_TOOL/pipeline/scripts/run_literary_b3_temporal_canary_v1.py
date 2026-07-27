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
from pipeline.literary.b3_temporal_context_v1 import load_b3_temporal_profile_v1
from pipeline.literary.b3_temporal_live_v1 import (
    execute_b3_temporal_canary_v1,
    prepare_b3_temporal_canary_v1,
)
from pipeline.literary.b3_temporal_prompts_v2 import (
    b3_temporal_response_schema_v2,
)
from pipeline.literary.chapter_cycle_shared_runtime_v1 import (
    LiterarySharedRunnerBindingsV1,
    capability_binding_key,
)
from pipeline.literary.openai_b3_json_object_capability_probe_v1 import (
    ROLE_ID,
    RUNTIME_PROFILE_PATH,
    build_literary_openai_b3_probe_plan_v1,
    execute_literary_openai_b3_probe_once_v1,
)
from pipeline.literary.shared_llm_adapter_v1 import LiterarySharedLlmAttemptAdapter
from pipeline.literary.shared_runtime_profile_v2 import (
    load_literary_shared_runtime_profile_v2,
)


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = RUNTIME_ROOT.parent
DEFAULT_CONTEXT_PROFILE = (
    RUNTIME_ROOT
    / "pipeline"
    / "configs"
    / "literary_b3_temporal_phase_a_v1.json"
)
DEFAULT_SCHEDULER_ROOT = Path(r"C:\work\shared-llm-physical-quota-v1")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)

    probe = commands.add_parser("probe")
    probe.add_argument("--output-root", type=Path, required=True)
    probe.add_argument("--probe-run-id", required=True)
    probe.add_argument("--credential-env", default="OPENAI_API_KEY")
    probe.add_argument("--scheduler-root", type=Path, default=DEFAULT_SCHEDULER_ROOT)

    canary = commands.add_parser("canary")
    canary.add_argument("--b2-root", type=Path, required=True)
    canary.add_argument("--output-root", type=Path, required=True)
    canary.add_argument("--capability-root", type=Path, required=True)
    canary.add_argument("--context-profile", type=Path, default=DEFAULT_CONTEXT_PROFILE)
    canary.add_argument("--run-id", required=True)
    canary.add_argument("--attempt-run-id", required=True)
    canary.add_argument("--credential-env", default="OPENAI_API_KEY")
    canary.add_argument("--scheduler-root", type=Path, default=DEFAULT_SCHEDULER_ROOT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    head = _clean_head()
    secret = _credential(args.credential_env)
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
        context_profile=args.context_profile,
        run_id=args.run_id,
        attempt_run_id=args.attempt_run_id,
        secret=secret,
        commitment=commitment,
        scheduler_root=args.scheduler_root,
        current_head=head,
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
    output = Path(output_root).resolve()
    shared = Path(str(output) + "-shared")
    if output.exists() or shared.exists():
        raise SystemExit("probe output roots must not exist")
    plan = build_literary_openai_b3_probe_plan_v1(
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
            {"credential.openai_row2": secret}
        ),
        scheduler=PhysicalQuotaScheduler(scheduler_root),
        ledger=SharedLlmAttemptLedger(shared / "attempt_ledger.sqlite3"),
        artifact_store=ContentAddressedArtifactStore(shared / "artifacts"),
        sender=UrllibTransportSender(),
        implementation_binding=plan.implementation_binding,
    )
    result = execute_literary_openai_b3_probe_once_v1(probe=probe, plan=plan)
    _write(output / "probe_receipt.json", result["receipt"])
    _write(output / "capability_evidence.json", result["capability_evidence"])
    report_body = {
        "schema_version": "literary_openai_b3_json_object_probe_report_v1",
        "status": result["status"],
        "probe_seal_sha256": result["probe_seal_sha256"],
        "receipt_sha256": result["receipt"]["receipt_sha256"],
        "evidence_sha256": result["capability_evidence"]["evidence_sha256"],
        "provider_called": result["provider_called"],
        "usage": _probe_usage(result["receipt"]),
        "failure": result["receipt"].get("failure"),
        "normal_output_created": False,
        "production_publish_performed": False,
        "mandatory_stop_observed": True,
    }
    report = {**report_body, "report_hash": hashlib.sha256(
        canonical_json(report_body).encode("utf-8")
    ).hexdigest()}
    _write(output / "probe_report.json", report)
    return report


def _probe_usage(receipt: Mapping[str, Any]) -> dict[str, int] | None:
    fields = (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "cached_input_tokens",
        "reasoning_tokens",
    )
    values = {field: receipt.get(field) for field in fields}
    if not all(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0
        for value in values.values()
    ):
        return None
    return values


def _run_canary(
    *,
    b2_root: Path,
    output_root: Path,
    capability_root: Path,
    context_profile: Path,
    run_id: str,
    attempt_run_id: str,
    secret: str,
    commitment: str,
    scheduler_root: Path,
    current_head: str,
) -> dict[str, Any]:
    evidence = validate_capability_evidence(
        _read(Path(capability_root) / "capability_evidence.json")
    )
    if evidence["verdict"] != "qualified":
        raise SystemExit("B3 capability evidence is not qualified")
    profile = load_literary_shared_runtime_profile_v2(
        RUNTIME_PROFILE_PATH,
        expected_role_ids={ROLE_ID},
    )
    source_binding = dict(profile.source_binding_for(ROLE_ID))
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
    output = Path(output_root).resolve()
    shared = Path(str(output) + "-shared")
    if output.exists() or shared.exists():
        raise SystemExit("canary output roots must not exist")
    shared.mkdir(parents=True, exist_ok=False)
    store = ContentAddressedArtifactStore(shared / "artifacts")
    backend = SharedLlmBackend(
        credential_provider=MappingCredentialProvider(
            {"credential.openai_row2": secret}
        ),
        scheduler=PhysicalQuotaScheduler(scheduler_root),
        ledger=SharedLlmAttemptLedger(shared / "attempt_ledger.sqlite3"),
        response_cache=ApplicationResponseCache(
            index_path=shared / "response_cache.sqlite3",
            artifact_store=store,
        ),
        sender=UrllibTransportSender(),
    )
    runtime = LiterarySharedRunnerBindingsV1(
        adapter=LiterarySharedLlmAttemptAdapter(backend=backend),
        api_source=None,
        capabilities={
            capability_binding_key(ROLE_ID, b3_temporal_response_schema_v2()): evidence
        },
        run_id=run_id,
        attempt_run_id=attempt_run_id,
        structured_output=None,
        runtime_profile=profile,
        api_sources_by_alias={"openai_official_row2": source},
    )
    prepare_b3_temporal_canary_v1(
        b2_run_root=b2_root,
        output_root=output,
        profile=load_b3_temporal_profile_v1(context_profile),
        shared_runtime=runtime,
        current_git_head=current_head,
    )
    return execute_b3_temporal_canary_v1(
        output_root=output,
        shared_runtime=runtime,
        current_git_head=current_head,
    )


def _credential(environment_name: str) -> str:
    value = os.environ.get(environment_name)
    if not isinstance(value, str) or not value or value != value.strip():
        raise SystemExit(f"credential environment is absent or malformed: {environment_name}")
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
        raise SystemExit("live B3 command requires a clean tracked worktree")
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


if __name__ == "__main__":
    raise SystemExit(main())
