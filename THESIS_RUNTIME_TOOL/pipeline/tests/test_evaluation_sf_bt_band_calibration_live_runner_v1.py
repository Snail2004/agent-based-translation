from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

import pipeline.eval.sf_bt_band_calibration_live_runner_v1 as live_runner
from pipeline.eval.contracts_v1 import (
    ContractValidationError,
    seal_payload,
)
from pipeline.eval.llm_profiles_v1 import (
    SF_BT_SEMANTIC_JUDGE_ROLE_ID,
    build_evaluation_llm_profile_v1,
    evaluation_role_contract_v1,
)
from pipeline.eval.method_executors_v1 import SharedEvaluationRoleRunnerV1
from pipeline.eval.sf_bt_band_calibration_live_runner_v1 import (
    build_sf_bt_band_calibration_plan_v1,
    run_evaluation_sf_bt_band_calibration_v1,
    validate_sf_bt_band_calibration_plan_v1,
)
from pipeline.eval.sf_bt_band_calibration_v1 import (
    load_default_sf_bt_band_calibration_fixture,
)
from pipeline.llm_backend import (
    ApplicationResponseCache,
    ContentAddressedArtifactStore,
    MappingCredentialProvider,
    PhysicalQuotaScheduler,
    RawTransportResponse,
    SharedLlmAttemptLedger,
    SharedLlmBackend,
    canonical_json,
    canonical_sha256,
)
from pipeline.llm_backend.transport_v1 import TransportCallError


NOW = "2026-07-20T10:00:00Z"
COMMIT = "a" * 40
MODEL_ID = "evaluation-band-calibration-fixture-model"


class _Clock:
    def __init__(self) -> None:
        self.current = datetime(2026, 7, 20, 10, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        value = self.current
        self.current += timedelta(milliseconds=100)
        return value


class _BandSender:
    def __init__(
        self,
        fixture: dict,
        *,
        fail_on_call: int | None = None,
        invalid_score_on_call: int | None = None,
    ) -> None:
        self.fixture = fixture
        self.fail_on_call = fail_on_call
        self.invalid_score_on_call = invalid_score_on_call
        self.calls = 0
        self.successes = 0
        self.prompts: list[str] = []

    def send(self, request):
        self.calls += 1
        if self.calls == self.fail_on_call:
            response = RawTransportResponse(
                status_code=429,
                headers={"retry-after": "60"},
                body=b"",
            )
            raise TransportCallError(
                code="http_429",
                status_code=429,
                safe_message="fixture rate limit",
                response=response,
            )

        body = json.loads(request.body.decode("utf-8"))
        prompt = body["prompt"]
        self.prompts.append(prompt)
        expected_score = self._expected_score(prompt)
        score = 58 if self.calls == self.invalid_score_on_call else expected_score
        flags = [] if score == 100 else ["semantic_mismatch"]
        output = {
            "score": score,
            "flags": flags,
            "note": "fixture semantic comparison",
        }
        self.successes += 1
        response = canonical_json(
            {
                "model": MODEL_ID,
                "finish_reason": "stop",
                "usage": {
                    "prompt_tokens": 80,
                    "cached_input_tokens": 0,
                    "completion_tokens": 12,
                    "reasoning_tokens": 0,
                    "total_tokens": 92,
                },
                "output_text": canonical_json(output),
            }
        ).encode("utf-8")
        return RawTransportResponse(
            status_code=200,
            headers={},
            body=response,
            request_id=f"band-fixture-{self.calls}",
        )

    def _expected_score(self, prompt: str) -> int:
        matches = [
            row
            for row in self.fixture["cases"]
            if row["reference_passage"] in prompt
            and row["candidate_passage"] in prompt
        ]
        if len(matches) != 1:
            raise AssertionError("fixture prompt does not resolve to exactly one case")
        return int(matches[0]["expected_score"])


def _source(*, row: str = "row1", route_id: str = "fixture_callback") -> dict:
    return {
        "schema_version": "api_source_v1",
        "source_id": f"evaluation_band_fixture_source_{row}",
        "source_revision": f"fixture_{row}",
        "source_class": "local_in_process",
        "adapter_id": "evaluation_band_fixture_adapter_v1",
        "protocol": "local_in_process",
        "route_id": route_id,
        "endpoint_class": "in_process",
        "base_url": None,
        "credential_ref": None,
        "credential_commitment": None,
        "physical_quota_bucket_id": f"evaluation-band-fixture-{row}",
        "enabled": True,
    }


def _capability(
    source: dict,
    *,
    model_id: str = MODEL_ID,
    schema_sha256: str | None = None,
) -> dict:
    contract = evaluation_role_contract_v1(SF_BT_SEMANTIC_JUDGE_ROLE_ID)
    return {
        "schema_version": "capability_evidence_v1",
        "capability_id": "evaluation_sf_bt_band_fixture_capability_v1",
        "capability_revision": "fixture_v1",
        "source_id": source["source_id"],
        "source_revision": source["source_revision"],
        "adapter_id": source["adapter_id"],
        "protocol": source["protocol"],
        "route_id": source["route_id"],
        "base_url": source["base_url"],
        "requested_model_id": model_id,
        "observed_model_id": model_id,
        "capability_kind": "native_structured_output",
        "schema_dialect": "json_schema_2020_12",
        "schema_sha256": schema_sha256 or contract["response_schema"]["sha256"],
        "local_validator_id": contract["validator"]["id"],
        "local_validator_sha256": contract["validator"]["sha256"],
        "verdict": "qualified",
        "probe_id": "evaluation_sf_bt_band_fixture_probe_v1",
        "evidence_sha256": "e" * 64,
        "observed_at_utc": NOW,
    }


def _runtime(
    root: Path,
    sender: _BandSender,
    *,
    row: str = "row1",
    model_id: str = MODEL_ID,
    route_id: str = "fixture_callback",
    temperature: float = 0.0,
    schema_sha256: str | None = None,
    cache_mode: str = "bypass",
):
    source = _source(row=row, route_id=route_id)
    capability = _capability(
        source,
        model_id=model_id,
        schema_sha256=schema_sha256,
    )
    target = {
        "source_id": source["source_id"],
        "source_revision": source["source_revision"],
        "source_record_sha256": canonical_sha256(source),
        "requested_model_id": capability["requested_model_id"],
        "capability_id": capability["capability_id"],
        "capability_revision": capability["capability_revision"],
        "capability_record_sha256": canonical_sha256(capability),
    }
    profile = build_evaluation_llm_profile_v1(
        primary_targets={SF_BT_SEMANTIC_JUDGE_ROLE_ID: target},
        profile_id=f"evaluation-band-fixture-profile-{row}",
        profile_revision=f"fixture-{row}",
        structured_output_mode="required",
    )
    if temperature != 0.0:
        profile = copy.deepcopy(profile)
        profile["role_bindings"][0]["generation"]["temperature"] = temperature

    state = root / "_state"
    store = ContentAddressedArtifactStore(state / "objects")
    cache = ApplicationResponseCache(
        index_path=state / "response_cache.sqlite3",
        artifact_store=store,
    )
    ledger = SharedLlmAttemptLedger(state / "attempt_ledger.sqlite3")
    backend = SharedLlmBackend(
        credential_provider=MappingCredentialProvider({}),
        scheduler=PhysicalQuotaScheduler(state / "quota_locks"),
        ledger=ledger,
        response_cache=cache,
        sender=sender,
        clock=_Clock(),
    )

    def runner(attempt_id: str) -> SharedEvaluationRoleRunnerV1:
        return SharedEvaluationRoleRunnerV1(
            backend=backend,
            profile=profile,
            api_sources=[source],
            capability_evidence=[capability],
            run_id="evaluation-sfbt-band-calibration-fixture-run",
            attempt_run_id=attempt_id,
            cache_mode=cache_mode,
        )

    return runner, ledger


def test_plan_is_fixed_35_calls_without_expected_labels() -> None:
    fixture = load_default_sf_bt_band_calibration_fixture()
    plan = build_sf_bt_band_calibration_plan_v1(
        fixture,
        logical_run_id="evaluation-sfbt-band-calibration-fixture-run",
        semantic_contract_sha256="f" * 64,
        created_at=NOW,
        producer_code_commit=COMMIT,
    )
    assert len(plan["calls"]) == 35
    assert [row["replicate_index"] for row in plan["calls"][:15]] == [1] * 15
    assert [row["replicate_index"] for row in plan["calls"][15:30]] == [2] * 15
    assert all(row["orientation"] == "reversed" for row in plan["calls"][30:])
    assert len(plan["binding"]["orientation_case_ids"]) == 5
    expected_by_case = {row["case_id"]: row["expected_score"] for row in fixture["cases"]}
    assert {
        expected_by_case[case_id]
        for case_id in plan["binding"]["orientation_case_ids"]
    } == {0, 25, 50, 75, 100}
    assert all("expected_score" not in row for row in plan["calls"])


def test_fake_transport_completes_35_and_replays_without_calls(tmp_path: Path) -> None:
    fixture = load_default_sf_bt_band_calibration_fixture()
    root = tmp_path / "band"
    sender = _BandSender(fixture)
    runner, ledger = _runtime(root, sender)

    first = run_evaluation_sf_bt_band_calibration_v1(
        fixture,
        runner("attempt-1"),
        root,
        created_at=NOW,
        producer_code_commit=COMMIT,
    )
    assert sender.calls == 35
    assert sender.successes == 35
    assert ledger.count("usage") == 35
    assert first.created_checkpoint_count == 35
    assert first.remaining_call_count == 0
    assert first.result is not None
    assert len(list((root / "checkpoints").glob("*.json"))) == 35
    assert all(
        row["analysis"]["summary"]["exact_band_accuracy"] == 1.0
        for row in first.result["round_analyses"]
    )
    assert first.result["repeatability"]["exact_agreement_rate"] == 1.0
    assert first.result["orientation_screen"]["exact_vs_repeat_1_rate"] == 1.0
    assert first.result["interpretation"] == "measurement_only_not_a_calibration_pass"

    forbidden = [
        value
        for row in fixture["cases"]
        for value in (
            row["case_id"],
            row["expected_primary_reason"],
            row["author_note"],
        )
    ] + ["calibration_reference_first", "calibration_candidate_first"]
    assert all(value not in prompt for prompt in sender.prompts for value in forbidden)

    replay = run_evaluation_sf_bt_band_calibration_v1(
        fixture,
        runner("attempt-unused"),
        root,
        created_at=NOW,
        producer_code_commit=COMMIT,
    )
    assert replay.result == first.result
    assert replay.reused_checkpoint_count == 35
    assert replay.created_checkpoint_count == 0
    assert replay.remaining_call_count == 0
    assert replay.attempt_run_id is None
    assert sender.calls == 35


def test_invocation_cap_pauses_cleanly_then_other_row_completes(
    tmp_path: Path,
) -> None:
    fixture = load_default_sf_bt_band_calibration_fixture()
    root = tmp_path / "band"
    sender = _BandSender(fixture)
    row1_runtime, _ = _runtime(root, sender, row="row1")

    paused = run_evaluation_sf_bt_band_calibration_v1(
        fixture,
        row1_runtime("attempt-row1"),
        root,
        created_at=NOW,
        producer_code_commit=COMMIT,
        max_new_calls=18,
    )
    assert paused.result is None
    assert paused.created_checkpoint_count == 18
    assert paused.remaining_call_count == 17
    assert sender.calls == 18
    attempt = json.loads(
        (root / "attempts" / "attempt-row1.json").read_text(encoding="utf-8")
    )
    assert attempt["invocation_policy"]["max_new_calls"] == 18
    assert attempt["invocation_policy"]["aggregate_max_total_tokens"] == (
        18
        * attempt["invocation_policy"]["per_call_max_total_tokens"]
    )

    row2_runtime, _ = _runtime(root, sender, row="row2")
    completed = run_evaluation_sf_bt_band_calibration_v1(
        fixture,
        row2_runtime("attempt-row2"),
        root,
        created_at="2026-07-20T11:00:00Z",
        producer_code_commit="b" * 40,
        max_new_calls=17,
    )
    assert completed.result is not None
    assert completed.reused_checkpoint_count == 18
    assert completed.created_checkpoint_count == 17
    assert completed.remaining_call_count == 0
    assert sender.calls == 35
    assert completed.result["binding"]["attempt_run_ids"] == [
        "attempt-row1",
        "attempt-row2",
    ]


def test_rate_limit_halts_then_other_physical_row_resumes_only_remaining(
    tmp_path: Path,
) -> None:
    fixture = load_default_sf_bt_band_calibration_fixture()
    root = tmp_path / "band"
    sender = _BandSender(fixture, fail_on_call=6)
    row1_runtime, _ = _runtime(root, sender, row="row1")

    with pytest.raises(TransportCallError) as exc_info:
        run_evaluation_sf_bt_band_calibration_v1(
            fixture,
            row1_runtime("attempt-row1"),
            root,
            created_at=NOW,
            producer_code_commit=COMMIT,
        )
    assert exc_info.value.code == "http_429"
    assert len(list((root / "checkpoints").glob("*.json"))) == 5
    assert not (root / "result.json").exists()

    sender.fail_on_call = None
    row2_runtime, _ = _runtime(root, sender, row="row2")
    resumed = run_evaluation_sf_bt_band_calibration_v1(
        fixture,
        row2_runtime("attempt-row2"),
        root,
        created_at="2026-07-20T11:00:00Z",
        producer_code_commit="b" * 40,
    )
    assert resumed.result is not None
    assert resumed.reused_checkpoint_count == 5
    assert resumed.created_checkpoint_count == 30
    assert resumed.remaining_call_count == 0
    assert sender.calls == 36
    assert sender.successes == 35
    assert resumed.result["binding"]["attempt_run_ids"] == [
        "attempt-row1",
        "attempt-row2",
    ]


@pytest.mark.parametrize(
    "runtime_kwargs",
    [
        {"row": "row2", "model_id": "another-model"},
        {"row": "row2", "temperature": 0.2},
        {"row": "row2", "route_id": "another-route"},
        {"row": "row2", "schema_sha256": "f" * 64},
    ],
)
def test_resume_rejects_semantic_drift_before_transport(
    tmp_path: Path,
    runtime_kwargs: dict,
) -> None:
    fixture = load_default_sf_bt_band_calibration_fixture()
    root = tmp_path / "band"
    sender = _BandSender(fixture, fail_on_call=2)
    row1_runtime, _ = _runtime(root, sender, row="row1")
    with pytest.raises(TransportCallError):
        run_evaluation_sf_bt_band_calibration_v1(
            fixture,
            row1_runtime("attempt-row1"),
            root,
            created_at=NOW,
            producer_code_commit=COMMIT,
        )
    calls_before = sender.calls
    sender.fail_on_call = None
    changed_runtime, _ = _runtime(root, sender, **runtime_kwargs)
    with pytest.raises(ContractValidationError, match="semantic_contract_drift"):
        run_evaluation_sf_bt_band_calibration_v1(
            fixture,
            changed_runtime("attempt-changed"),
            root,
            created_at=NOW,
            producer_code_commit=COMMIT,
        )
    assert sender.calls == calls_before


def test_cache_mode_must_be_bypass(tmp_path: Path) -> None:
    fixture = load_default_sf_bt_band_calibration_fixture()
    root = tmp_path / "band"
    sender = _BandSender(fixture)
    runtime, _ = _runtime(root, sender, cache_mode="read_write")
    with pytest.raises(ContractValidationError, match="cache bypass"):
        run_evaluation_sf_bt_band_calibration_v1(
            fixture,
            runtime("attempt-cache"),
            root,
            created_at=NOW,
            producer_code_commit=COMMIT,
        )
    assert sender.calls == 0


@pytest.mark.parametrize("cap", [0, 36, True])
def test_invocation_cap_is_closed_and_bounded(tmp_path: Path, cap: object) -> None:
    fixture = load_default_sf_bt_band_calibration_fixture()
    root = tmp_path / "band"
    sender = _BandSender(fixture)
    runtime, _ = _runtime(root, sender)
    with pytest.raises(ContractValidationError):
        run_evaluation_sf_bt_band_calibration_v1(
            fixture,
            runtime("attempt-cap"),
            root,
            created_at=NOW,
            producer_code_commit=COMMIT,
            max_new_calls=cap,  # type: ignore[arg-type]
        )
    assert sender.calls == 0


def test_invalid_pseudo_precision_score_halts_without_checkpoint(tmp_path: Path) -> None:
    fixture = load_default_sf_bt_band_calibration_fixture()
    root = tmp_path / "band"
    sender = _BandSender(fixture, invalid_score_on_call=3)
    runtime, _ = _runtime(root, sender)
    with pytest.raises(ContractValidationError, match="semantic judge failed"):
        run_evaluation_sf_bt_band_calibration_v1(
            fixture,
            runtime("attempt-invalid"),
            root,
            created_at=NOW,
            producer_code_commit=COMMIT,
        )
    assert sender.calls == 3
    assert len(list((root / "checkpoints").glob("*.json"))) == 2
    assert not (root / "result.json").exists()


def test_resealed_plan_and_checkpoint_tamper_fail_before_transport(
    tmp_path: Path,
) -> None:
    fixture = load_default_sf_bt_band_calibration_fixture()
    root = tmp_path / "band"
    sender = _BandSender(fixture)
    runtime, _ = _runtime(root, sender)
    run_evaluation_sf_bt_band_calibration_v1(
        fixture,
        runtime("attempt-1"),
        root,
        created_at=NOW,
        producer_code_commit=COMMIT,
    )
    calls_before = sender.calls

    plan_path = root / "plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    tampered_plan = copy.deepcopy(plan)
    tampered_plan["unexpected"] = "resealed"
    tampered_plan = seal_payload(
        tampered_plan,
        policy=live_runner._PLAN_POLICY,
        hash_path=("integrity", "plan_sha256"),
    )
    plan_path.write_text(canonical_json(tampered_plan), encoding="utf-8", newline="\n")
    with pytest.raises(ContractValidationError, match="unknown keys"):
        run_evaluation_sf_bt_band_calibration_v1(
            fixture,
            runtime("attempt-unused"),
            root,
            created_at=NOW,
            producer_code_commit=COMMIT,
        )
    assert sender.calls == calls_before

    plan_path.write_text(canonical_json(plan), encoding="utf-8", newline="\n")
    checkpoint_path = sorted((root / "checkpoints").glob("*.json"))[0]
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    checkpoint["binding"]["case_id"] = fixture["cases"][-1]["case_id"]
    checkpoint = seal_payload(
        checkpoint,
        policy=live_runner._CHECKPOINT_POLICY,
        hash_path=("integrity", "checkpoint_sha256"),
    )
    checkpoint_path.write_text(
        canonical_json(checkpoint), encoding="utf-8", newline="\n"
    )
    with pytest.raises(ContractValidationError, match="checkpoint differs"):
        run_evaluation_sf_bt_band_calibration_v1(
            fixture,
            runtime("attempt-unused"),
            root,
            created_at=NOW,
            producer_code_commit=COMMIT,
        )
    assert sender.calls == calls_before


def test_resealed_nested_result_tamper_is_recomputed_and_rejected(
    tmp_path: Path,
) -> None:
    fixture = load_default_sf_bt_band_calibration_fixture()
    root = tmp_path / "band"
    sender = _BandSender(fixture)
    runtime, _ = _runtime(root, sender)
    run_evaluation_sf_bt_band_calibration_v1(
        fixture,
        runtime("attempt-1"),
        root,
        created_at=NOW,
        producer_code_commit=COMMIT,
    )
    result_path = root / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["repeatability"]["exact_agreement_rate"] = 0.0
    result = seal_payload(
        result,
        policy=live_runner._RESULT_POLICY,
        hash_path=("integrity", "result_sha256"),
    )
    result_path.write_text(canonical_json(result), encoding="utf-8", newline="\n")
    calls_before = sender.calls
    with pytest.raises(ContractValidationError, match="persisted result differs"):
        run_evaluation_sf_bt_band_calibration_v1(
            fixture,
            runtime("attempt-unused"),
            root,
            created_at=NOW,
            producer_code_commit=COMMIT,
        )
    assert sender.calls == calls_before


def test_resealed_attempt_token_cap_tamper_is_rejected(tmp_path: Path) -> None:
    fixture = load_default_sf_bt_band_calibration_fixture()
    root = tmp_path / "band"
    sender = _BandSender(fixture)
    runtime, _ = _runtime(root, sender)
    paused = run_evaluation_sf_bt_band_calibration_v1(
        fixture,
        runtime("attempt-1"),
        root,
        created_at=NOW,
        producer_code_commit=COMMIT,
        max_new_calls=1,
    )
    assert paused.remaining_call_count == 34
    attempt_path = root / "attempts" / "attempt-1.json"
    attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
    attempt["invocation_policy"]["aggregate_max_total_tokens"] += 1
    attempt = live_runner._seal_opaque(attempt)
    attempt_path.write_text(canonical_json(attempt), encoding="utf-8", newline="\n")
    calls_before = sender.calls
    with pytest.raises(ContractValidationError, match="aggregate token caps"):
        run_evaluation_sf_bt_band_calibration_v1(
            fixture,
            runtime("attempt-unused"),
            root,
            created_at=NOW,
            producer_code_commit=COMMIT,
        )
    assert sender.calls == calls_before


def test_plan_validator_rejects_resealed_unknown_key() -> None:
    fixture = load_default_sf_bt_band_calibration_fixture()
    plan = build_sf_bt_band_calibration_plan_v1(
        fixture,
        logical_run_id="evaluation-sfbt-band-calibration-fixture-run",
        semantic_contract_sha256="f" * 64,
        created_at=NOW,
        producer_code_commit=COMMIT,
    )
    tampered = copy.deepcopy(plan)
    tampered["calls"][0]["expected_score"] = 100
    tampered = seal_payload(
        tampered,
        policy=live_runner._PLAN_POLICY,
        hash_path=("integrity", "plan_sha256"),
    )
    with pytest.raises(ContractValidationError, match="unknown keys"):
        validate_sf_bt_band_calibration_plan_v1(tampered)
