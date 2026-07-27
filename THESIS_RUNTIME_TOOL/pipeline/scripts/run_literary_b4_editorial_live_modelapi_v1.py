from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import subprocess
from typing import Any, Mapping, Sequence

from pipeline.llm_backend import (
    ContentAddressedArtifactStore,
    MappingCredentialProvider,
    PhysicalQuotaScheduler,
    SharedLlmAttemptLedger,
    SharedLlmCapabilityProbe,
    UrllibTransportSender,
    credential_commitment,
)
from pipeline.literary.b4_editorial_live_modelapi_v1 import (
    run_editorial_review_live_v1,
)
from pipeline.literary.modelapi_b4_editorial_capability_probe_v1 import (
    build_probe_plan_v1,
    execute_probe_once_v1,
)


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = RUNTIME_ROOT.parent
DEFAULT_SCHEDULER_ROOT = Path(r"C:\work\shared-llm-physical-quota-v1")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Probe or execute fail-closed B4 Editorial Review"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    probe = commands.add_parser("probe")
    probe.add_argument("--output-root", type=Path, required=True)
    probe.add_argument("--probe-run-id", required=True)
    _credential_args(probe)
    probe.add_argument(
        "--scheduler-root",
        type=Path,
        default=DEFAULT_SCHEDULER_ROOT,
    )

    review = commands.add_parser("review")
    review.add_argument("--prepared-batch-root", type=Path, required=True)
    review.add_argument("--capability-root", type=Path, required=True)
    review.add_argument("--output-root", type=Path, required=True)
    review.add_argument("--shared-root", type=Path, required=True)
    review.add_argument("--run-id", required=True)
    review.add_argument("--attempt-run-id", required=True)
    _credential_args(review)
    review.add_argument(
        "--scheduler-root",
        type=Path,
        default=DEFAULT_SCHEDULER_ROOT,
    )
    return parser


def _credential_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--credential-env", default="MODELAPI_API_KEY")
    parser.add_argument("--credential-file", type=Path)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    secret = _credential(args.credential_env, args.credential_file)
    commitment = credential_commitment(secret)
    try:
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
        head = _clean_head()
        root = Path(args.prepared_batch_root).resolve()
        report = run_editorial_review_live_v1(
            review_packet=_read(root / "editorial_review_packet.json"),
            style_profile=(root / "style_profile.txt").read_text(
                encoding="utf-8"
            ),
            capability_evidence=_read(
                args.capability_root / "capability_evidence.json"
            ),
            output_root=args.output_root,
            shared_root=args.shared_root,
            scheduler_root=args.scheduler_root,
            secret=secret,
            credential_commitment_sha256=commitment,
            run_id=args.run_id,
            attempt_run_id=args.attempt_run_id,
            current_git_head=head,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        _write_failure(args.output_root, exc)
        raise


def _run_probe(
    *,
    output_root: Path,
    probe_run_id: str,
    secret: str,
    commitment: str,
    scheduler_root: Path,
) -> dict[str, Any]:
    output = Path(output_root).resolve()
    if output.exists():
        raise SystemExit(f"output directory already exists: {output}")
    plan = build_probe_plan_v1(
        probe_run_id=probe_run_id,
        credential_commitment_sha256=commitment,
        issued_at_utc=_now(),
    )
    output.mkdir(parents=True)
    shared = output.parent / f".{output.name}_shared"
    if shared.exists():
        raise SystemExit(f"probe shared directory already exists: {shared}")
    shared.mkdir(parents=True)
    _write(output / "probe_seal.json", plan.seal)
    _write(output / "request.json", plan.request)
    _write(output / "transport_request.json", plan.request_body)
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
    _write(
        output / "capability_evidence.json",
        result["capability_evidence"],
    )
    receipt = result["receipt"]
    report_body = {
        "schema_version": "literary_b4_editorial_probe_report_v1",
        "status": result["status"],
        "role_id": "literary.b4.editorial_review",
        "provider_called": result["provider_called"],
        "source_id": plan.source["source_id"],
        "requested_model_id": receipt.get("requested_model_id"),
        "observed_model_id": receipt.get("observed_model_id"),
        "usage": {
            key: receipt.get(key)
            for key in (
                "prompt_tokens",
                "cached_input_tokens",
                "completion_tokens",
                "reasoning_tokens",
                "total_tokens",
            )
        },
        "failure": receipt.get("failure"),
        "production_publish_performed": False,
    }
    report = {
        **report_body,
        "report_hash": _hash(report_body),
    }
    _write(output / "probe_report.json", report)
    return report


def _credential(environment_name: str, credential_file: Path | None) -> str:
    value = os.environ.get(environment_name)
    if credential_file is not None:
        if value:
            raise SystemExit("select either credential environment or file")
        value = Path(credential_file).read_text(encoding="utf-8").strip()
    if not value:
        raise SystemExit("ModelAPI credential is absent")
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
        raise SystemExit(
            "B4 Editorial live call requires a clean tracked worktree"
        )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, TypeError, ValueError) as exc:
        raise SystemExit(f"cannot load JSON: {path}") from exc
    if not isinstance(value, Mapping):
        raise SystemExit(f"JSON object required: {path}")
    return dict(value)


def _write(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_failure(output_root: Path, exc: Exception) -> None:
    output = Path(output_root).resolve()
    output.mkdir(parents=True, exist_ok=True)
    target = output / "failure.json"
    if target.exists():
        return
    _write(
        target,
        {
            "schema_version": "literary_b4_editorial_live_failure_v1",
            "status": "halted_fail_closed",
            "error_type": type(exc).__name__,
            "message": str(exc)[:4000],
            "provider_retry_performed": False,
            "fallback_performed": False,
            "response_repaired": False,
            "continued_after_failure": False,
        },
    )


def _hash(value: Mapping[str, Any]) -> str:
    from pipeline.literary.checkpoint import canonical_hash

    return canonical_hash(value)


def _now() -> str:
    return (
        datetime.now(UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


if __name__ == "__main__":
    raise SystemExit(main())
