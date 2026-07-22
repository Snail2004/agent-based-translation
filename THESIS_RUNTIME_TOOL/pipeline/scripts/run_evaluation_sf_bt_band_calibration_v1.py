from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import time
from typing import Any, Sequence

from pipeline.eval.live_pilot_capability_run_v1 import (
    capabilities_by_role_from_probe_run_v1,
    validate_evaluation_capability_probe_run_summary_v1,
)
from pipeline.eval.llm_profiles_v1 import (
    SF_BT_SEMANTIC_JUDGE_ROLE_ID,
    build_evaluation_llm_profile_v1,
)
from pipeline.eval.method_executors_v1 import SharedEvaluationRoleRunnerV1
from pipeline.eval.sf_bt_band_calibration_live_runner_v1 import (
    run_evaluation_sf_bt_band_calibration_v1,
)
from pipeline.eval.sf_bt_band_calibration_v1 import (
    DEFAULT_SF_BT_BAND_CALIBRATION_FIXTURE_PATH,
    validate_sf_bt_band_calibration_fixture,
)
from pipeline.llm_backend import (
    ApplicationResponseCache,
    ContentAddressedArtifactStore,
    MappingCredentialProvider,
    PhysicalQuotaScheduler,
    SharedLlmAttemptLedger,
    SharedLlmBackend,
    UrllibTransportSender,
    canonical_json,
    canonical_sha256,
    validate_pipeline_profile,
)
from pipeline.scripts.run_evaluation_live_pilot_capability_probe_v1 import (
    load_selected_google_credential_v1,
)


_CALIBRATION_MAX_INPUT_TOKENS = 4_096
_THIRD_PARTY_CALIBRATION_MAX_INPUT_TOKENS = 8_192
_THIRD_PARTY_CALIBRATION_MAX_COMPLETION_CERTIFICATION_TOKENS = 2_048


class _PacedRoleRunner:
    def __init__(self, delegate: SharedEvaluationRoleRunnerV1, pacer: "_CallPacer"):
        self._delegate = delegate
        self._pacer = pacer

    @property
    def execution_binding(self):
        return self._delegate.execution_binding

    @property
    def semantic_contract(self):
        return self._delegate.semantic_contract

    @property
    def attempt_runtime_binding(self):
        return self._delegate.attempt_runtime_binding

    @property
    def cache_mode(self):
        return self._delegate.cache_mode

    def execute(self, **kwargs):
        self._pacer.wait()
        return self._delegate.execute(**kwargs)


class _CallPacer:
    def __init__(self, minimum_interval_seconds: float) -> None:
        if (
            isinstance(minimum_interval_seconds, bool)
            or not isinstance(minimum_interval_seconds, (int, float))
            or not math.isfinite(float(minimum_interval_seconds))
            or minimum_interval_seconds < 0
        ):
            raise ValueError("minimum call interval must be finite and non-negative")
        self.minimum_interval_seconds = float(minimum_interval_seconds)
        self._last_started: float | None = None

    def wait(self) -> None:
        now = time.monotonic()
        if self._last_started is not None:
            remaining = self.minimum_interval_seconds - (now - self._last_started)
            if remaining > 0:
                time.sleep(remaining)
        self._last_started = time.monotonic()


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.execute_live:
        parser.error("--execute-live is required; this runner has no implicit live mode")

    runtime_root = Path(__file__).resolve().parents[2]
    output_root = _contained_output_root(runtime_root, args.output_root_relative)
    fixture = validate_sf_bt_band_calibration_fixture(
        _load_json_object(args.fixture)
    )
    capability_summary = validate_evaluation_capability_probe_run_summary_v1(
        _load_json_object(args.capability_summary)
    )
    source = capability_summary["source"]
    _require_source_credential_binding(
        source,
        physical_row=args.physical_row,
        expected_credential_ref=args.expected_credential_ref,
    )
    capabilities = capabilities_by_role_from_probe_run_v1(
        capability_summary,
        output_root=args.capability_summary.resolve().parent,
    )
    capability = capabilities[SF_BT_SEMANTIC_JUDGE_ROLE_ID]
    profile = _build_profile(
        source,
        capability,
        profile_id=args.profile_id,
        profile_revision=args.profile_revision,
    )
    credential = load_selected_google_credential_v1(
        args.keys_file,
        physical_row=args.physical_row,
        expected_row_count=args.expected_row_count,
    )

    state_root = output_root / "_state"
    artifact_store = ContentAddressedArtifactStore(state_root / "raw_responses")
    response_cache = ApplicationResponseCache(
        index_path=state_root / "response_cache.sqlite3",
        artifact_store=artifact_store,
    )
    ledger = SharedLlmAttemptLedger(state_root / "attempt_ledger.sqlite3")
    backend = SharedLlmBackend(
        credential_provider=MappingCredentialProvider(
            {source["credential_ref"]: credential}
        ),
        scheduler=PhysicalQuotaScheduler(state_root / "quota_leases"),
        ledger=ledger,
        response_cache=response_cache,
        sender=UrllibTransportSender(),
    )
    delegate = SharedEvaluationRoleRunnerV1(
        backend=backend,
        profile=profile,
        api_sources=[source],
        capability_evidence=[capability],
        run_id=args.logical_run_id,
        attempt_run_id=args.attempt_run_id,
        cache_mode="bypass",
    )
    runner = _PacedRoleRunner(
        delegate,
        _CallPacer(args.min_call_interval_seconds),
    )
    result = run_evaluation_sf_bt_band_calibration_v1(
        fixture,
        runner,
        output_root,
        created_at=args.created_at,
        producer_code_commit=args.producer_code_commit,
        max_new_calls=args.max_new_calls,
    )
    usage = ledger.list_records("usage")
    errors = ledger.list_records("error")
    print(
        canonical_json(
            {
                "status": (
                    "complete"
                    if result.result is not None
                    else "paused_invocation_cap"
                ),
                "output_root": str(result.output_root),
                "result_path": (
                    None if result.result_path is None else str(result.result_path)
                ),
                "result_sha256": (
                    None
                    if result.result is None
                    else result.result["integrity"]["result_sha256"]
                ),
                "reused_checkpoint_count": result.reused_checkpoint_count,
                "created_checkpoint_count": result.created_checkpoint_count,
                "remaining_call_count": result.remaining_call_count,
                "attempt_run_id": result.attempt_run_id,
                "physical_quota_bucket_id": source["physical_quota_bucket_id"],
                "requested_model_id": capability["requested_model_id"],
                "profile_sha256": canonical_sha256(profile),
                "ledger_usage_row_count": len(usage),
                "ledger_error_row_count": len(errors),
                "known_prompt_tokens": sum(row["prompt_tokens"] or 0 for row in usage),
                "known_completion_tokens": sum(
                    row["completion_tokens"] or 0 for row in usage
                ),
            }
        )
    )
    return 0


def _build_profile(
    source: dict[str, Any],
    capability: dict[str, Any],
    *,
    profile_id: str,
    profile_revision: str,
) -> dict[str, Any]:
    target = {
        "source_id": source["source_id"],
        "source_revision": source["source_revision"],
        "source_record_sha256": canonical_sha256(source),
        "requested_model_id": capability["requested_model_id"],
        "capability_id": capability["capability_id"],
        "capability_revision": capability["capability_revision"],
        "capability_record_sha256": canonical_sha256(capability),
    }
    capability_kind = capability["capability_kind"]
    if capability_kind == "native_structured_output":
        structured_output_mode = "required"
    elif capability_kind == "json_object":
        structured_output_mode = "prompt_validated"
    else:
        raise ValueError(
            "calibration requires native_structured_output or json_object capability"
        )
    profile = build_evaluation_llm_profile_v1(
        primary_targets={SF_BT_SEMANTIC_JUDGE_ROLE_ID: target},
        profile_id=profile_id,
        profile_revision=profile_revision,
        structured_output_mode=structured_output_mode,
    )
    role = profile["role_bindings"][0]
    max_input_tokens = (
        _THIRD_PARTY_CALIBRATION_MAX_INPUT_TOKENS
        if capability_kind == "json_object"
        else _CALIBRATION_MAX_INPUT_TOKENS
    )
    role["generation"]["max_input_tokens"] = max_input_tokens
    role["limits"]["max_prompt_tokens"] = max_input_tokens
    max_completion_tokens = role["limits"]["max_completion_tokens"]
    if capability_kind == "json_object":
        max_completion_tokens = (
            _THIRD_PARTY_CALIBRATION_MAX_COMPLETION_CERTIFICATION_TOKENS
        )
        role["limits"]["max_completion_tokens"] = max_completion_tokens
    role["limits"]["max_total_tokens"] = (
        max_input_tokens
        + max_completion_tokens
    )
    return validate_pipeline_profile(profile)


def _require_source_credential_binding(
    source: dict[str, Any],
    *,
    physical_row: int,
    expected_credential_ref: str | None,
) -> None:
    expected_ref = expected_credential_ref
    if expected_ref is None:
        if source.get("protocol") != "google_genai_generate_content":
            raise ValueError(
                "non-Google capability sources require --expected-credential-ref"
            )
        expected_ref = f"shared.google.gemini_free.row{physical_row}"
    if source.get("credential_ref") != expected_ref:
        raise ValueError(
            "selected credential row differs from the capability source binding"
        )


def _require_source_row_binding(source: dict[str, Any], physical_row: int) -> None:
    """Compatibility wrapper for the existing official-Google CLI contract."""

    _require_source_credential_binding(
        source,
        physical_row=physical_row,
        expected_credential_ref=None,
    )


def _contained_output_root(runtime_root: Path, relative: str) -> Path:
    if not relative or "\\" in relative or ":" in relative:
        raise ValueError("output root must be a relative POSIX path")
    path = (runtime_root / relative).resolve()
    try:
        path.relative_to(runtime_root.resolve())
    except ValueError as exc:
        raise ValueError("output root escapes THESIS_RUNTIME_TOOL") from exc
    return path


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
    raise ValueError(f"non-finite JSON values are forbidden: {value}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run or resume the approved 35-call SF-BT band calibration."
    )
    parser.add_argument(
        "--fixture",
        type=Path,
        default=DEFAULT_SF_BT_BAND_CALIBRATION_FIXTURE_PATH,
    )
    parser.add_argument("--capability-summary", type=Path, required=True)
    parser.add_argument("--keys-file", type=Path, required=True)
    parser.add_argument("--physical-row", type=int, required=True)
    parser.add_argument("--expected-row-count", type=int, default=5)
    parser.add_argument("--expected-credential-ref")
    parser.add_argument("--output-root-relative", required=True)
    parser.add_argument("--created-at", required=True)
    parser.add_argument("--producer-code-commit", required=True)
    parser.add_argument("--profile-id", required=True)
    parser.add_argument("--profile-revision", required=True)
    parser.add_argument("--logical-run-id", required=True)
    parser.add_argument("--attempt-run-id", required=True)
    parser.add_argument("--max-new-calls", type=int, required=True)
    parser.add_argument(
        "--min-call-interval-seconds",
        type=float,
        default=4.2,
        help="Pipeline-owned pacing between calls; no automatic quota retry.",
    )
    parser.add_argument("--execute-live", action="store_true")
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
