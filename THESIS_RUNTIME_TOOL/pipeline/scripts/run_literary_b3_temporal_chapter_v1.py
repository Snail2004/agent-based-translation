from __future__ import annotations

import argparse
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
    UrllibTransportSender,
    credential_commitment,
    validate_capability_evidence,
)
from pipeline.literary.b3_temporal_chapter_runner_v1 import (
    bind_b3_runtime_call_budget_v1,
    execute_b3_temporal_chapter_run_v1,
    prepare_b3_temporal_chapter_run_v1,
)
from pipeline.literary.b3_temporal_context_v1 import load_b3_temporal_profile_v1
from pipeline.literary.b3_temporal_prompts_v7 import b3_temporal_response_schema_v7
from pipeline.literary.chapter_cycle_shared_runtime_v1 import (
    LiterarySharedRunnerBindingsV1,
    capability_binding_key,
)
from pipeline.literary.openai_b3_json_object_capability_probe_v1 import ROLE_ID
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
DEFAULT_RUNTIME_PROFILE = (
    RUNTIME_ROOT
    / "pipeline"
    / "configs"
    / "literary_shared_llm_runtime_openai_b3_temporal_chapter_v1.json"
)
DEFAULT_SCHEDULER_ROOT = Path(r"C:\work\shared-llm-physical-quota-v1")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--b2-root", type=Path, required=True)
    parser.add_argument("--speaker-recovery-root", type=Path)
    parser.add_argument("--identity-hearing-root", type=Path)
    parser.add_argument("--prior-b3-root", type=Path, action="append", default=[])
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--capability-root", type=Path, required=True)
    parser.add_argument("--context-profile", type=Path, default=DEFAULT_CONTEXT_PROFILE)
    parser.add_argument("--runtime-profile", type=Path, default=DEFAULT_RUNTIME_PROFILE)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--attempt-run-id", required=True)
    parser.add_argument("--credential-env", default="OPENAI_API_KEY")
    parser.add_argument("--scheduler-root", type=Path, default=DEFAULT_SCHEDULER_ROOT)
    parser.add_argument("--max-calls", type=int, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    head = _clean_head()
    context_profile = load_b3_temporal_profile_v1(args.context_profile)
    if (
        not isinstance(args.max_calls, int)
        or isinstance(args.max_calls, bool)
        or args.max_calls < 1
        or args.max_calls > context_profile.max_requests_per_chapter
    ):
        raise SystemExit(
            "B3 max-calls must be within the context profile ceiling: "
            f"requested={args.max_calls}, "
            f"ceiling={context_profile.max_requests_per_chapter}"
        )
    secret = _credential(args.credential_env)
    runtime = _build_runtime(
        output_root=args.output_root,
        capability_root=args.capability_root,
        runtime_profile=args.runtime_profile,
        run_id=args.run_id,
        attempt_run_id=args.attempt_run_id,
        secret=secret,
        scheduler_root=args.scheduler_root,
        max_calls=args.max_calls,
    )
    output = Path(args.output_root).resolve()
    if not output.exists():
        prepare_b3_temporal_chapter_run_v1(
            b2_run_root=args.b2_root,
            speaker_recovery_root=args.speaker_recovery_root,
            identity_hearing_root=args.identity_hearing_root,
            prior_b3_roots=args.prior_b3_root,
            output_root=output,
            profile=context_profile,
            shared_runtime=runtime,
            current_git_head=head,
            max_calls=args.max_calls,
        )
    report = execute_b3_temporal_chapter_run_v1(
        output_root=output,
        profile=context_profile,
        shared_runtime=runtime,
        current_git_head=head,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def _build_runtime(
    *,
    output_root: Path,
    capability_root: Path,
    runtime_profile: Path,
    run_id: str,
    attempt_run_id: str,
    secret: str,
    scheduler_root: Path,
    max_calls: int,
) -> LiterarySharedRunnerBindingsV1:
    evidence = validate_capability_evidence(
        _read(Path(capability_root) / "capability_evidence.json")
    )
    if evidence["verdict"] != "qualified":
        raise SystemExit("B3 capability evidence is not qualified")
    profile = load_literary_shared_runtime_profile_v2(
        runtime_profile,
        expected_role_ids={ROLE_ID},
    )
    profile = bind_b3_runtime_call_budget_v1(
        profile,
        max_calls=max_calls,
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
        "credential_commitment": credential_commitment(secret),
        "physical_quota_bucket_id": source_binding["physical_quota_bucket_id"],
        "enabled": True,
    }
    shared = Path(str(Path(output_root).resolve()) + "-shared")
    shared.mkdir(parents=True, exist_ok=True)
    store = ContentAddressedArtifactStore(shared / "artifacts")
    backend = SharedLlmBackend(
        credential_provider=MappingCredentialProvider(
            {source_binding["credential_ref"]: secret}
        ),
        scheduler=PhysicalQuotaScheduler(scheduler_root),
        ledger=SharedLlmAttemptLedger(shared / "attempt_ledger.sqlite3"),
        response_cache=ApplicationResponseCache(
            index_path=shared / "response_cache.sqlite3",
            artifact_store=store,
        ),
        sender=UrllibTransportSender(),
    )
    return LiterarySharedRunnerBindingsV1(
        adapter=LiterarySharedLlmAttemptAdapter(backend=backend),
        api_source=None,
        capabilities={
            capability_binding_key(ROLE_ID, b3_temporal_response_schema_v7()): evidence
        },
        run_id=run_id,
        attempt_run_id=attempt_run_id,
        structured_output=None,
        runtime_profile=profile,
        api_sources_by_alias={source_binding["source_alias"]: source},
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


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SystemExit(f"JSON artifact must be an object: {path}")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
