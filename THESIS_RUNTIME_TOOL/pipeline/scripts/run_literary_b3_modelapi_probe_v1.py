from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
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
    canonical_json,
    credential_commitment,
)
from pipeline.literary.checkpoint import canonical_hash
from pipeline.literary.modelapi_b3_json_object_capability_probe_v1 import (
    build_probe_plan_v1,
    execute_probe_once_v1,
)


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_ROOT = RUNTIME_ROOT.parent
DEFAULT_CREDENTIAL_FILE = REPOSITORY_ROOT / "LOCAL-GPT-GATEWAY.txt"
DEFAULT_SCHEDULER_ROOT = Path(r"C:\work\shared-llm-physical-quota-v1")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ModelAPI B3 capability probe")
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--probe-run-id", required=True)
    parser.add_argument("--credential-file", type=Path, default=DEFAULT_CREDENTIAL_FILE)
    parser.add_argument("--scheduler-root", type=Path, default=DEFAULT_SCHEDULER_ROOT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _clean_head()
    secret = _credential(args.credential_file)
    output, shared = _fresh_roots(args.output_root)
    plan = build_probe_plan_v1(
        probe_run_id=args.probe_run_id,
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
        scheduler=PhysicalQuotaScheduler(args.scheduler_root),
        ledger=SharedLlmAttemptLedger(shared / "attempt_ledger.sqlite3"),
        artifact_store=ContentAddressedArtifactStore(shared / "artifacts"),
        sender=UrllibTransportSender(),
        implementation_binding=plan.implementation_binding,
    )
    result = execute_probe_once_v1(probe=probe, plan=plan)
    _write(output / "probe_receipt.json", result["receipt"])
    _write(output / "capability_evidence.json", result["capability_evidence"])
    report_body = {
        "schema_version": "literary_modelapi_b3_probe_report_v1",
        "status": result["status"],
        "provider_called": result["provider_called"],
        "probe_seal_sha256": result["probe_seal_sha256"],
        "receipt_sha256": result["receipt"]["receipt_sha256"],
        "evidence_sha256": result["capability_evidence"]["evidence_sha256"],
        "usage": _mapping_or_none(result["receipt"].get("usage")),
        "failure": _mapping_or_none(result["receipt"].get("failure")),
        "mandatory_stop_observed": True,
        "normal_output_created": False,
        "production_publish_performed": False,
    }
    report = {**report_body, "report_hash": canonical_hash(report_body)}
    _write(output / "probe_report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "qualified" else 2


def _credential(path: Path) -> str:
    value = Path(path).read_text(encoding="utf-8").strip()
    if not value or any(char.isspace() for char in value):
        raise SystemExit("ModelAPI credential file is absent or malformed")
    return value


def _fresh_roots(output_root: Path) -> tuple[Path, Path]:
    output = Path(output_root).resolve()
    shared = Path(str(output) + "-shared")
    if output.exists() or shared.exists():
        raise SystemExit("probe output roots must not exist")
    return output, shared


def _clean_head() -> str:
    status = subprocess.run(
        ["git", "status", "--short", "--untracked-files=no"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if status:
        raise SystemExit("live B3 probe requires a clean tracked worktree")
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _mapping_or_none(value: Any) -> dict[str, Any] | None:
    return dict(value) if isinstance(value, Mapping) else None


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise SystemExit(f"refusing to overwrite artifact: {path}")
    path.write_text(canonical_json(payload) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
