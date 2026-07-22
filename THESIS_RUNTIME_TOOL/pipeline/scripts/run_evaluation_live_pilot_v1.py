from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import re
import subprocess
from typing import Any, Sequence

from pipeline.eval.cometkiwi_subprocess_v1 import CometKiwiSubprocessPredictorV1
from pipeline.eval.d2l_package_adapter_v1 import project_d2l_evaluation_package
from pipeline.eval.live_pilot_capability_run_v1 import (
    capabilities_by_role_from_probe_run_v1,
    validate_evaluation_capability_probe_run_summary_v1,
)
from pipeline.eval.live_pilot_execution_run_v1 import (
    run_evaluation_live_pilot_execution_v1,
)
from pipeline.llm_backend import (
    MappingCredentialProvider,
    UrllibTransportSender,
    canonical_json,
)
from pipeline.scripts.run_evaluation_ckey_capability_probe_v1 import (
    load_selected_credential_row_v1,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.execute_live:
        parser.error("--execute-live is required; this runner has no implicit live mode")

    runtime_root = Path(__file__).resolve().parents[2]
    _require_current_checkout_commit(
        runtime_root.parent,
        expected_commit=args.producer_code_commit,
    )
    package = _load_json_object(args.d2l_package)
    common = project_d2l_evaluation_package(package)
    config = _load_json_object(args.run_config)
    preflight = _load_json_object(args.preflight)
    capability_summary = validate_evaluation_capability_probe_run_summary_v1(
        _load_json_object(args.capability_summary)
    )
    capabilities = capabilities_by_role_from_probe_run_v1(
        capability_summary,
        output_root=args.capability_summary.resolve().parent,
    )
    source = capability_summary["source"]
    _require_source_credential_binding(
        source,
        expected_credential_ref=args.expected_credential_ref,
    )
    credential = load_selected_credential_row_v1(
        args.credential_file,
        physical_row=args.physical_row,
        expected_row_count=args.expected_row_count,
    )
    predictor = CometKiwiSubprocessPredictorV1(
        python_executable=args.comet_python,
        checkpoint_path=args.comet_checkpoint,
        timeout_seconds=args.comet_timeout_seconds,
        max_rows_per_worker=args.comet_max_rows_per_worker,
    )
    result = run_evaluation_live_pilot_execution_v1(
        common,
        config,
        preflight,
        api_source=source,
        capabilities_by_role=capabilities,
        credential_provider=MappingCredentialProvider(
            {source["credential_ref"]: credential}
        ),
        sender=UrllibTransportSender(),
        sf_qe_predictor=predictor,
        output_base_root=runtime_root,
        output_root_relative=args.output_root_relative,
        created_at=args.created_at,
        producer_code_commit=args.producer_code_commit,
        profile_id=args.profile_id,
        profile_revision=args.profile_revision,
        evaluation_logical_run_id=args.logical_run_id,
        evaluation_attempt_run_id=args.attempt_run_id,
        sf_qe_batch_size=args.comet_batch_size,
        structured_output_mode=args.structured_output_mode,
    )
    print(
        canonical_json(
            {
                "status": "complete",
                "reused_complete_run": result.reused_complete_run,
                "output_root": str(result.output_root),
                "profile_path": str(result.profile_path),
                "local_sf_qe_path": str(result.local_sf_qe_path),
                "execution_path": str(result.execution_path),
                "execution_sha256": result.execution["integrity"][
                    "execution_sha256"
                ],
                "coverage": result.execution["coverage"],
                "claim": result.execution["claim"],
            }
        )
    )
    return 0


def _load_json_object(path: Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        value = json.load(
            handle,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite,
        )
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_non_finite(value: str) -> None:
    if not math.isfinite(float(value)):
        raise ValueError("non-finite JSON values are forbidden")
    raise ValueError("invalid JSON numeric constant")


def _require_source_credential_binding(
    source: dict[str, Any],
    *,
    expected_credential_ref: str,
) -> None:
    observed = source.get("credential_ref")
    if observed != expected_credential_ref:
        raise ValueError(
            "selected credential differs from the capability source binding"
        )


def _require_current_checkout_commit(
    git_root: Path,
    *,
    expected_commit: str,
) -> str:
    if (
        not isinstance(expected_commit, str)
        or re.fullmatch(r"[0-9a-f]{40}", expected_commit) is None
    ):
        raise ValueError("producer code commit must be a full lowercase Git SHA")
    completed = subprocess.run(
        ["git", "-C", str(Path(git_root).resolve()), "rev-parse", "HEAD"],
        text=True,
        encoding="utf-8",
        errors="strict",
        capture_output=True,
        timeout=10,
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError("current Git HEAD could not be resolved")
    observed = completed.stdout.strip()
    if re.fullmatch(r"[0-9a-f]{40}", observed) is None:
        raise ValueError("current Git HEAD is not a canonical full SHA")
    if expected_commit != observed:
        raise ValueError("producer code commit differs from current Git HEAD")
    return observed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run one sealed Evaluation live pilot on an exactly qualified "
            "source, model and output mode."
        )
    )
    parser.add_argument("--d2l-package", type=Path, required=True)
    parser.add_argument("--run-config", type=Path, required=True)
    parser.add_argument("--preflight", type=Path, required=True)
    parser.add_argument("--capability-summary", type=Path, required=True)
    parser.add_argument(
        "--credential-file",
        "--keys-file",
        dest="credential_file",
        type=Path,
        required=True,
    )
    parser.add_argument("--physical-row", type=int, required=True)
    parser.add_argument("--expected-row-count", type=int, required=True)
    parser.add_argument("--expected-credential-ref", required=True)
    parser.add_argument("--comet-python", type=Path, required=True)
    parser.add_argument("--comet-checkpoint", type=Path, required=True)
    parser.add_argument("--comet-timeout-seconds", type=int, default=1800)
    parser.add_argument("--comet-batch-size", type=int, default=1)
    parser.add_argument("--comet-max-rows-per-worker", type=int, default=1)
    parser.add_argument("--output-root-relative", required=True)
    parser.add_argument("--created-at", required=True)
    parser.add_argument("--producer-code-commit", required=True)
    parser.add_argument("--profile-id", required=True)
    parser.add_argument("--profile-revision", required=True)
    parser.add_argument("--logical-run-id", required=True)
    parser.add_argument("--attempt-run-id", required=True)
    parser.add_argument(
        "--structured-output-mode",
        choices=("prompt_validated", "required"),
        required=True,
        help=(
            "Use required only for qualified direct-provider native schema; "
            "use prompt_validated for qualified JSON-object syntax plus the "
            "unchanged local validator."
        ),
    )
    parser.add_argument("--execute-live", action="store_true")
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
