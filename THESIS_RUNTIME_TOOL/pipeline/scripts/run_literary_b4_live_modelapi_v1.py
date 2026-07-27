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
from pipeline.literary.b4_address_anchor_v1 import load_style_profile_v1
from pipeline.literary.b4_live_modelapi_v1 import (
    run_address_anchor_live_v1,
    run_translation_window_live_v1,
)
from pipeline.literary.chapter_source_document_v1 import (
    chapter_from_document_v1,
    load_literary_source_document_v1,
)
from pipeline.literary.modelapi_b4_capability_probe_v1 import (
    ADDRESS_ROLE_ID,
    TRANSLATOR_ROLE_ID,
    build_probe_plan_v1,
    execute_probe_once_v1,
)


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = RUNTIME_ROOT.parent
DEFAULT_SCHEDULER_ROOT = Path(r"C:\work\shared-llm-physical-quota-v1")
ROLE_ALIASES = {
    "address": ADDRESS_ROLE_ID,
    "translator": TRANSLATOR_ROLE_ID,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Probe and execute fail-closed B4 ModelAPI calls"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    probe = commands.add_parser("probe")
    probe.add_argument("--role", choices=sorted(ROLE_ALIASES), required=True)
    probe.add_argument("--output-root", type=Path, required=True)
    probe.add_argument("--probe-run-id", required=True)
    _credential_args(probe)
    probe.add_argument("--scheduler-root", type=Path, default=DEFAULT_SCHEDULER_ROOT)

    address = commands.add_parser("address")
    address.add_argument("--anchor-input", type=Path, required=True)
    address.add_argument("--style-design", type=Path, required=True)
    address.add_argument("--style-profile-version", required=True)
    address.add_argument("--capability-root", type=Path, required=True)
    address.add_argument("--output-root", type=Path, required=True)
    _live_args(address)

    translate = commands.add_parser("translate-window")
    translate.add_argument("--translator-pack", type=Path, required=True)
    translate.add_argument("--address-anchor", type=Path, required=True)
    translate.add_argument("--window-slice", type=Path, required=True)
    translate.add_argument("--document", type=Path, required=True)
    translate.add_argument("--accepted-tail", type=Path)
    translate.add_argument("--style-design", type=Path, required=True)
    translate.add_argument("--style-profile-version", required=True)
    translate.add_argument("--capability-root", type=Path, required=True)
    translate.add_argument("--output-root", type=Path, required=True)
    _live_args(translate)
    return parser


def _credential_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--credential-env", default="MODELAPI_API_KEY")
    parser.add_argument("--credential-file", type=Path)


def _live_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--shared-root", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--attempt-run-id", required=True)
    _credential_args(parser)
    parser.add_argument("--scheduler-root", type=Path, default=DEFAULT_SCHEDULER_ROOT)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    secret = _credential(args.credential_env, args.credential_file)
    commitment = credential_commitment(secret)
    try:
        if args.command == "probe":
            report = _run_probe(
                role_id=ROLE_ALIASES[args.role],
                output_root=args.output_root,
                probe_run_id=args.probe_run_id,
                secret=secret,
                commitment=commitment,
                scheduler_root=args.scheduler_root,
            )
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report["status"] == "qualified" else 2
        head = _clean_head()
        style_profile = load_style_profile_v1(
            design_doc=args.style_design,
            style_profile_version=args.style_profile_version,
        )
        if args.command == "address":
            report = run_address_anchor_live_v1(
                anchor_input=_read(args.anchor_input),
                style_profile=style_profile,
                style_profile_version=args.style_profile_version,
                measured_arm=False,
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
        else:
            window = _read(args.window_slice)
            document = load_literary_source_document_v1(args.document)
            chapter = chapter_from_document_v1(document, str(window["chapter_id"]))
            report = run_translation_window_live_v1(
                translator_pack_bytes=args.translator_pack.read_bytes(),
                address_anchor_bytes=args.address_anchor.read_bytes(),
                window_slice_bytes=args.window_slice.read_bytes(),
                chapter=chapter,
                accepted_tail_translations={
                    str(key): str(value)
                    for key, value in _read_optional(args.accepted_tail).items()
                },
                style_profile=style_profile,
                style_profile_version=args.style_profile_version,
                measured_arm=False,
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
    role_id: str,
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
        role_id=role_id,
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
    _write(output / "capability_evidence.json", result["capability_evidence"])
    receipt = result["receipt"]
    report_body = {
        "schema_version": "literary_b4_capability_probe_report_v1",
        "status": result["status"],
        "role_id": role_id,
        "provider_called": result["provider_called"],
        "source_id": plan.source["source_id"],
        "requested_model_id": receipt.get("requested_model_id"),
        "observed_model_id": receipt.get("observed_model_id"),
        "usage": _usage(receipt),
        "failure": receipt.get("failure"),
        "production_publish_performed": False,
    }
    report = {**report_body, "report_hash": _hash(report_body)}
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
        raise SystemExit("B4 live call requires a clean tracked worktree")
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


def _read_optional(path: Path | None) -> dict[str, Any]:
    return {} if path is None else _read(path)


def _write(path: Path, value: Mapping[str, Any]) -> None:
    Path(path).write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_failure(output_root: Path, exc: Exception) -> None:
    output = Path(output_root).resolve()
    output.mkdir(parents=True, exist_ok=True)
    failure = {
        "schema_version": "literary_b4_live_failure_v1",
        "status": "halted_fail_closed",
        "error_type": type(exc).__name__,
        "message": str(exc)[:4000],
        "provider_retry_performed": False,
        "fallback_performed": False,
        "response_repaired": False,
        "continued_after_failure": False,
    }
    target = output / "failure.json"
    if not target.exists():
        _write(target, failure)


def _usage(receipt: Mapping[str, Any]) -> dict[str, int | None]:
    return {
        key: receipt.get(key)
        for key in (
            "prompt_tokens",
            "cached_input_tokens",
            "completion_tokens",
            "reasoning_tokens",
            "total_tokens",
        )
    }


def _hash(value: Mapping[str, Any]) -> str:
    from pipeline.literary.checkpoint import canonical_hash

    return canonical_hash(value)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
