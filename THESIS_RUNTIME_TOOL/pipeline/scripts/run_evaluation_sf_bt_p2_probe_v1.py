from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Sequence

from pipeline.eval.live_pilot_capability_run_v1 import (
    capabilities_by_role_from_probe_run_v1,
    validate_evaluation_capability_probe_run_summary_v1,
)
from pipeline.eval.llm_profiles_v1 import (
    SF_BT_BACK_TRANSLATOR_ROLE_ID,
    SF_BT_SEMANTIC_JUDGE_ROLE_ID,
    build_evaluation_llm_profile_v1,
)
from pipeline.eval.method_executors_v1 import SharedEvaluationRoleRunnerV1
from pipeline.eval.scorer_probe_fixtures_v1 import (
    DEFAULT_SCORER_PROBE_FIXTURE_PATH,
    validate_scorer_probe_fixture_set,
)
from pipeline.eval.scorer_probe_live_runner_v1 import (
    run_evaluation_sf_bt_p2_probe_v1,
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
)
from pipeline.scripts.run_evaluation_live_pilot_capability_probe_v1 import (
    load_selected_google_credential_v1,
)


_ROLE_IDS = (SF_BT_BACK_TRANSLATOR_ROLE_ID, SF_BT_SEMANTIC_JUDGE_ROLE_ID)


class _PacedRoleRunner:
    def __init__(self, delegate: SharedEvaluationRoleRunnerV1, pacer: "_CallPacer") -> None:
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
            raise ValueError("minimum call interval must be a finite non-negative number")
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
    fixture = validate_scorer_probe_fixture_set(_load_json_object(args.fixture))
    capability_summary = validate_evaluation_capability_probe_run_summary_v1(
        _load_json_object(args.capability_summary)
    )
    all_capabilities = capabilities_by_role_from_probe_run_v1(
        capability_summary,
        output_root=args.capability_summary.resolve().parent,
    )
    source = capability_summary["source"]
    capabilities = {role_id: all_capabilities[role_id] for role_id in _ROLE_IDS}
    profile = _build_profile(
        source,
        capabilities,
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
    pacer = _CallPacer(args.min_call_interval_seconds)
    _persist_runtime_binding(
        output_root,
        source,
        capabilities,
        profile,
        minimum_call_interval_seconds=args.min_call_interval_seconds,
    )
    attempt_ids = [args.attempt_run_id]
    if args.recovery_attempt_run_id is not None:
        attempt_ids.append(args.recovery_attempt_run_id)
    role_runners = [
        _PacedRoleRunner(
            SharedEvaluationRoleRunnerV1(
                backend=backend,
                profile=profile,
                api_sources=[source],
                capability_evidence=list(capabilities.values()),
                run_id=args.logical_run_id,
                attempt_run_id=attempt_id,
                cache_mode="read_write",
            ),
            pacer,
        )
        for attempt_id in attempt_ids
    ]
    result = run_evaluation_sf_bt_p2_probe_v1(
        fixture,
        role_runners,
        output_root,
        created_at=args.created_at,
        producer_code_commit=args.producer_code_commit,
    )
    usage = ledger.list_records("usage")
    errors = ledger.list_records("error")
    print(
        canonical_json(
            {
                "status": "complete" if result.result is not None else "halted",
                "output_root": str(result.output_root),
                "result_path": None if result.result_path is None else str(result.result_path),
                "result_sha256": (
                    None
                    if result.result is None
                    else result.result["integrity"]["result_sha256"]
                ),
                "reused_checkpoint_count": result.reused_checkpoint_count,
                "created_checkpoint_count": result.created_checkpoint_count,
                "used_attempt_run_ids": list(result.used_attempt_run_ids),
                "ledger_usage_row_count": len(usage),
                "ledger_error_row_count": len(errors),
                "known_prompt_tokens": sum(
                    row["prompt_tokens"] or 0 for row in usage
                ),
                "known_completion_tokens": sum(
                    row["completion_tokens"] or 0 for row in usage
                ),
            }
        )
    )
    return 0


def _build_profile(
    source: dict[str, Any],
    capabilities: dict[str, dict[str, Any]],
    *,
    profile_id: str,
    profile_revision: str,
) -> dict[str, Any]:
    targets = {
        role_id: {
            "source_id": source["source_id"],
            "source_revision": source["source_revision"],
            "source_record_sha256": canonical_sha256(source),
            "requested_model_id": capability["requested_model_id"],
            "capability_id": capability["capability_id"],
            "capability_revision": capability["capability_revision"],
            "capability_record_sha256": canonical_sha256(capability),
        }
        for role_id, capability in capabilities.items()
    }
    return build_evaluation_llm_profile_v1(
        primary_targets=targets,
        profile_id=profile_id,
        profile_revision=profile_revision,
        structured_output_mode="required",
    )


def _persist_runtime_binding(
    root: Path,
    source: dict[str, Any],
    capabilities: dict[str, dict[str, Any]],
    profile: dict[str, Any],
    *,
    minimum_call_interval_seconds: float,
) -> None:
    payload = {
        "schema_id": "EvaluationSfBtP2RuntimeBindingV1",
        "schema_version": "1.0.0",
        "api_source": source,
        "capabilities": [capabilities[role_id] for role_id in _ROLE_IDS],
        "profile": profile,
        "execution_policy": {
            "expected_accepted_call_count": 40,
            "max_failed_retryable_call_count": 1,
            "max_physical_call_count": 41,
            "minimum_call_interval_seconds": float(minimum_call_interval_seconds),
            "http_429_action": "pause",
            "semantic_retry": False,
            "provider_fallback": False,
        },
        "integrity": {
            "api_source_sha256": canonical_sha256(source),
            "capability_sha256s": [
                canonical_sha256(capabilities[role_id]) for role_id in _ROLE_IDS
            ],
            "profile_sha256": canonical_sha256(profile),
        },
    }
    path = root / "runtime_binding.json"
    rendered = (canonical_json(payload) + "\n").encode("utf-8")
    root.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = _load_json_object(path)
        if existing.get("execution_policy") != payload["execution_policy"]:
            raise ValueError(
                "resume changes the sealed P2 pacing or physical-call policy"
            )
    else:
        descriptor, temp_name = tempfile.mkstemp(
            prefix=".runtime_binding.", suffix=".tmp", dir=root
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(rendered)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)


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
    if not math.isfinite(float(value)):
        raise ValueError("non-finite JSON values are forbidden")
    raise ValueError("invalid JSON numeric constant")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the approved resumable 40-call SF-BT P2 omission probe."
    )
    parser.add_argument(
        "--fixture", type=Path, default=DEFAULT_SCORER_PROBE_FIXTURE_PATH
    )
    parser.add_argument("--capability-summary", type=Path, required=True)
    parser.add_argument("--keys-file", type=Path, required=True)
    parser.add_argument("--physical-row", type=int, required=True)
    parser.add_argument("--expected-row-count", type=int, default=5)
    parser.add_argument("--output-root-relative", required=True)
    parser.add_argument("--created-at", required=True)
    parser.add_argument("--producer-code-commit", required=True)
    parser.add_argument("--profile-id", required=True)
    parser.add_argument("--profile-revision", required=True)
    parser.add_argument("--logical-run-id", required=True)
    parser.add_argument("--attempt-run-id", required=True)
    parser.add_argument("--recovery-attempt-run-id")
    parser.add_argument(
        "--min-call-interval-seconds",
        type=float,
        default=4.2,
        help="Pipeline-owned pacing between semantic calls; default stays below 15 RPM.",
    )
    parser.add_argument("--execute-live", action="store_true")
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
