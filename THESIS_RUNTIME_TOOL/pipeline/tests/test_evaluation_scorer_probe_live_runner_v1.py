from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

import pipeline.eval.scorer_probe_live_runner_v1 as probe_runner
from pipeline.eval.llm_profiles_v1 import (
    SF_BT_BACK_TRANSLATOR_ROLE_ID,
    SF_BT_SEMANTIC_JUDGE_ROLE_ID,
    build_evaluation_llm_profile_v1,
    evaluation_role_contract_v1,
)
from pipeline.eval.method_executors_v1 import SharedEvaluationRoleRunnerV1
from pipeline.eval.scorer_probe_fixtures_v1 import load_default_scorer_probe_fixture_set
from pipeline.eval.scorer_probe_live_runner_v1 import (
    run_evaluation_sf_bt_p2_probe_v1,
    validate_evaluation_sf_bt_p2_checkpoint_v1,
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


class _Clock:
    def __init__(self) -> None:
        self.current = datetime(2026, 7, 20, 10, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        value = self.current
        self.current += timedelta(milliseconds=100)
        return value


class _ProbeSender:
    def __init__(self, *, fail_on_call: int | None = None, fail_code: str = "http_503"):
        self.calls = 0
        self.successes = 0
        self.fail_on_call = fail_on_call
        self.fail_code = fail_code
        self.prompts: list[str] = []

    def send(self, request):
        self.calls += 1
        if self.fail_on_call == self.calls:
            status = int(self.fail_code.removeprefix("http_")) if self.fail_code.startswith("http_") else None
            response = (
                RawTransportResponse(
                    status_code=status,
                    headers={"retry-after": "60"},
                    body=b"",
                )
                if status == 429
                else None
            )
            raise TransportCallError(
                code=self.fail_code,
                status_code=status,
                safe_message=f"fixture {self.fail_code}",
                response=response,
            )
        body = json.loads(request.body.decode("utf-8"))
        prompt = body["prompt"]
        self.prompts.append(prompt)
        if "independent Vietnamese-to-English back-translator" in prompt:
            output = {"back_translation": "The active translation omits one source fact."}
        elif "You compare two English passages" in prompt:
            output = {
                "score": 75,
                "flags": ["coverage_mismatch"],
                "note": "one source fact is absent",
            }
        else:
            raise AssertionError("unexpected probe prompt")
        self.successes += 1
        response = canonical_json(
            {
                "model": "evaluation-probe-fixture-model",
                "finish_reason": "stop",
                "usage": {
                    "prompt_tokens": 50,
                    "cached_input_tokens": 0,
                    "completion_tokens": 10,
                    "reasoning_tokens": 0,
                    "total_tokens": 60,
                },
                "output_text": canonical_json(output),
            }
        ).encode("utf-8")
        return RawTransportResponse(
            status_code=200,
            headers={},
            body=response,
            request_id=f"probe-fixture-{self.calls}",
        )


def _source(*, row: str = "row1", route_id: str = "fixture_callback") -> dict:
    return {
        "schema_version": "api_source_v1",
        "source_id": f"evaluation_p2_probe_fixture_source_{row}",
        "source_revision": f"fixture_{row}",
        "source_class": "local_in_process",
        "adapter_id": "evaluation_p2_probe_fixture_adapter_v1",
        "protocol": "local_in_process",
        "route_id": route_id,
        "endpoint_class": "in_process",
        "base_url": None,
        "credential_ref": None,
        "credential_commitment": None,
        "physical_quota_bucket_id": f"evaluation-p2-probe-fixture-{row}",
        "enabled": True,
    }


def _capability(
    role_id: str,
    source: dict,
    *,
    model_id: str = "evaluation-probe-fixture-model",
    schema_sha256: str | None = None,
) -> dict:
    contract = evaluation_role_contract_v1(role_id)
    return {
        "schema_version": "capability_evidence_v1",
        "capability_id": role_id.replace(".", "_") + "_p2_fixture_v1",
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
        "probe_id": role_id.replace(".", "_") + "_p2_fixture_probe_v1",
        "evidence_sha256": "e" * 64,
        "observed_at_utc": NOW,
    }


def _runtime(
    root: Path,
    sender: _ProbeSender,
    *,
    row: str = "row1",
    model_id: str = "evaluation-probe-fixture-model",
    route_id: str = "fixture_callback",
    temperature: float = 0.0,
    schema_sha256: str | None = None,
):
    source = _source(row=row, route_id=route_id)
    roles = (SF_BT_BACK_TRANSLATOR_ROLE_ID, SF_BT_SEMANTIC_JUDGE_ROLE_ID)
    capabilities = [
        _capability(
            role_id,
            source,
            model_id=model_id,
            schema_sha256=schema_sha256,
        )
        for role_id in roles
    ]
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
        for role_id, capability in zip(roles, capabilities, strict=True)
    }
    profile = build_evaluation_llm_profile_v1(
        primary_targets=targets,
        profile_id=f"evaluation-p2-probe-fixture-profile-{row}",
        profile_revision=f"fixture-{row}",
        structured_output_mode="required",
    )
    if temperature != 0.0:
        profile = copy.deepcopy(profile)
        for role in profile["role_bindings"]:
            role["generation"]["temperature"] = temperature
    state = root / "_state"
    store = ContentAddressedArtifactStore(state / "objects")
    cache = ApplicationResponseCache(
        index_path=state / "response_cache.sqlite3", artifact_store=store
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
            capability_evidence=capabilities,
            run_id="evaluation-p2-probe-fixture-run",
            attempt_run_id=attempt_id,
            cache_mode="read_write",
        )

    return runner, ledger


def test_fake_transport_completes_exact_40_and_replays_without_calls(tmp_path: Path) -> None:
    root = tmp_path / "probe"
    sender = _ProbeSender()
    runner, ledger = _runtime(root, sender)
    fixture = load_default_scorer_probe_fixture_set()

    first = run_evaluation_sf_bt_p2_probe_v1(
        fixture,
        [runner("attempt-1")],
        root,
        created_at=NOW,
        producer_code_commit=COMMIT,
    )
    assert sender.calls == 40
    assert ledger.count("usage") == 40
    assert first.created_checkpoint_count == 40
    assert first.result is not None
    assert first.result["interpretation"] == "not_blind_to_planted_omission"
    assert all(row["omission_detection_rate"] == 1.0 for row in first.result["metrics"])

    replay = run_evaluation_sf_bt_p2_probe_v1(
        fixture,
        [runner("attempt-replay-unused")],
        root,
        created_at=NOW,
        producer_code_commit=COMMIT,
    )
    assert replay.result == first.result
    assert replay.reused_checkpoint_count == 40
    assert sender.calls == 40
    assert all("author_note" not in prompt and "planted_marker" not in prompt for prompt in sender.prompts)


def test_http_503_uses_one_new_sealed_attempt_without_duplicate_success(tmp_path: Path) -> None:
    root = tmp_path / "probe"
    sender = _ProbeSender(fail_on_call=7, fail_code="http_503")
    runner, ledger = _runtime(root, sender)
    result = run_evaluation_sf_bt_p2_probe_v1(
        load_default_scorer_probe_fixture_set(),
        [runner("attempt-primary"), runner("attempt-recovery")],
        root,
        created_at=NOW,
        producer_code_commit=COMMIT,
    )
    assert result.result is not None
    assert result.used_attempt_run_ids == ("attempt-primary", "attempt-recovery")
    assert sender.calls == 41
    assert sender.successes == 40
    assert ledger.count("usage") == 41
    assert ledger.count("error") == 1
    assert len(list((root / "checkpoints").glob("*.json"))) == 40


def test_http_429_halts_then_new_invocation_resumes_remaining_only(tmp_path: Path) -> None:
    root = tmp_path / "probe"
    sender = _ProbeSender(fail_on_call=6, fail_code="http_429")
    runner, ledger = _runtime(root, sender)
    fixture = load_default_scorer_probe_fixture_set()
    with pytest.raises(TransportCallError, match="http_429"):
        run_evaluation_sf_bt_p2_probe_v1(
            fixture,
            [runner("attempt-rate-limited")],
            root,
            created_at=NOW,
            producer_code_commit=COMMIT,
        )
    accepted_before = len(list((root / "checkpoints").glob("*.json")))
    assert accepted_before == 5
    assert not (root / "result.json").exists()
    halt = next((root / "attempts").glob("*/halt.json"))
    assert json.loads(halt.read_text(encoding="utf-8"))["error"]["retry_after"] == "60"

    sender.fail_on_call = None
    resumed = run_evaluation_sf_bt_p2_probe_v1(
        fixture,
        [runner("attempt-after-pause")],
        root,
        created_at=NOW,
        producer_code_commit=COMMIT,
    )
    assert resumed.result is not None
    assert resumed.reused_checkpoint_count == accepted_before
    assert resumed.created_checkpoint_count == 40 - accepted_before
    assert sender.calls == 41
    assert sender.successes == 40
    assert ledger.count("usage") == 41


def test_http_429_resume_may_change_only_physical_row_and_code_commit(
    tmp_path: Path,
) -> None:
    root = tmp_path / "probe"
    sender = _ProbeSender(fail_on_call=6, fail_code="http_429")
    row1_runner, _ = _runtime(root, sender, row="row1")
    fixture = load_default_scorer_probe_fixture_set()
    with pytest.raises(TransportCallError, match="http_429"):
        run_evaluation_sf_bt_p2_probe_v1(
            fixture,
            [row1_runner("attempt-row1")],
            root,
            created_at=NOW,
            producer_code_commit=COMMIT,
        )
    accepted_before = len(list((root / "checkpoints").glob("*.json")))
    assert accepted_before == 5

    sender.fail_on_call = None
    row2_runner, _ = _runtime(root, sender, row="row2")
    resumed = run_evaluation_sf_bt_p2_probe_v1(
        fixture,
        [row2_runner("attempt-row2")],
        root,
        created_at="2026-07-20T11:00:00Z",
        producer_code_commit="b" * 40,
    )
    assert resumed.result is not None
    assert resumed.reused_checkpoint_count == accepted_before
    assert resumed.created_checkpoint_count == 40 - accepted_before
    assert sender.calls == 41
    assert sender.successes == 40

    checkpoints = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (root / "checkpoints").glob("*.json")
    ]
    assert len({row["binding"]["profile_sha256"] for row in checkpoints}) == 2
    row2_binding_path = (
        probe_runner._attempt_directory(root, "attempt-row2")
        / "execution_binding.json"
    )
    row2_binding = json.loads(row2_binding_path.read_text(encoding="utf-8"))
    assert row2_binding["runtime_binding"]["api_sources"][0]["source_id"].endswith(
        "row2"
    )
    semantic_contract = json.loads(
        (root / "semantic_contract.json").read_text(encoding="utf-8")
    )
    assert (
        row2_binding["semantic_contract_sha256"]
        == semantic_contract["semantic_contract_sha256"]
    )


@pytest.mark.parametrize(
    ("runtime_kwargs", "expected_message"),
    [
        (
            {"row": "row2", "model_id": "another-model"},
            "semantic contract",
        ),
        (
            {"row": "row2", "temperature": 0.2},
            "semantic contract",
        ),
        (
            {"row": "row2", "route_id": "another_route"},
            "semantic contract",
        ),
        (
            {"row": "row2", "schema_sha256": "f" * 64},
            "semantic contract",
        ),
    ],
)
def test_resume_rejects_semantic_drift_before_transport(
    tmp_path: Path,
    runtime_kwargs: dict,
    expected_message: str,
) -> None:
    root = tmp_path / "probe"
    sender = _ProbeSender(fail_on_call=2, fail_code="http_429")
    row1_runner, _ = _runtime(root, sender, row="row1")
    fixture = load_default_scorer_probe_fixture_set()
    with pytest.raises(TransportCallError):
        run_evaluation_sf_bt_p2_probe_v1(
            fixture,
            [row1_runner("attempt-row1")],
            root,
            created_at=NOW,
            producer_code_commit=COMMIT,
        )
    calls_before = sender.calls
    sender.fail_on_call = None
    changed_runner, _ = _runtime(root, sender, **runtime_kwargs)
    with pytest.raises(Exception, match=expected_message):
        run_evaluation_sf_bt_p2_probe_v1(
            fixture,
            [changed_runner("attempt-drift")],
            root,
            created_at="2026-07-20T11:00:00Z",
            producer_code_commit="b" * 40,
        )
    assert sender.calls == calls_before
    assert not probe_runner._attempt_directory(root, "attempt-drift").exists()


def test_resume_rejects_tampered_historical_runtime_binding_before_transport(
    tmp_path: Path,
) -> None:
    root = tmp_path / "probe"
    sender = _ProbeSender()
    runner_factory, _ = _runtime(root, sender, row="row1")
    runner = runner_factory("attempt-row1")
    attempt_binding = runner.attempt_runtime_binding
    source = attempt_binding["api_sources"][0]
    capabilities = attempt_binding["capabilities"]
    profile = attempt_binding["profile"]
    runtime_binding = {
        "schema_id": "EvaluationSfBtP2RuntimeBindingV1",
        "schema_version": "1.0.0",
        "api_source": source,
        "capabilities": capabilities,
        "profile": profile,
        "execution_policy": {
            "expected_accepted_call_count": 40,
            "max_failed_retryable_call_count": 1,
            "max_physical_call_count": 41,
            "minimum_call_interval_seconds": 4.2,
            "http_429_action": "pause",
            "semantic_retry": False,
            "provider_fallback": False,
        },
        "integrity": {
            "api_source_sha256": "f" * 64,
            "capability_sha256s": [canonical_sha256(row) for row in capabilities],
            "profile_sha256": canonical_sha256(profile),
        },
    }
    root.mkdir(parents=True, exist_ok=True)
    (root / "runtime_binding.json").write_text(
        canonical_json(runtime_binding) + "\n", encoding="utf-8", newline="\n"
    )
    with pytest.raises(Exception, match="runtime binding hashes"):
        run_evaluation_sf_bt_p2_probe_v1(
            load_default_scorer_probe_fixture_set(),
            [runner],
            root,
            created_at=NOW,
            producer_code_commit=COMMIT,
        )
    assert sender.calls == 0


def test_attempt_binding_rejects_resealed_unknown_runtime_schema_version(
    tmp_path: Path,
) -> None:
    root = tmp_path / "probe"
    sender = _ProbeSender(fail_on_call=2, fail_code="http_429")
    runner_factory, _ = _runtime(root, sender, row="row1")
    runner = runner_factory("attempt-row1")
    with pytest.raises(TransportCallError):
        run_evaluation_sf_bt_p2_probe_v1(
            load_default_scorer_probe_fixture_set(),
            [runner],
            root,
            created_at=NOW,
            producer_code_commit=COMMIT,
        )
    path = probe_runner._attempt_directory(root, "attempt-row1") / "execution_binding.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["runtime_binding"]["schema_version"] = "9.9.9"
    payload = probe_runner._seal_opaque_artifact(payload)
    with pytest.raises(Exception, match="schema_version"):
        probe_runner._validate_attempt_binding_artifact(payload)


def test_tampered_checkpoint_fails_before_new_transport(tmp_path: Path) -> None:
    root = tmp_path / "probe"
    sender = _ProbeSender(fail_on_call=4, fail_code="http_429")
    runner, _ = _runtime(root, sender)
    fixture = load_default_scorer_probe_fixture_set()
    with pytest.raises(TransportCallError):
        run_evaluation_sf_bt_p2_probe_v1(
            fixture,
            [runner("attempt-1")],
            root,
            created_at=NOW,
            producer_code_commit=COMMIT,
        )
    checkpoint_path = next((root / "checkpoints").glob("*.json"))
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    checkpoint["semantic_output"] = {"back_translation": "tampered"}
    checkpoint_path.write_text(canonical_json(checkpoint) + "\n", encoding="utf-8")
    calls_before = sender.calls
    sender.fail_on_call = None
    with pytest.raises(Exception, match="self-hash|semantic output differs"):
        run_evaluation_sf_bt_p2_probe_v1(
            fixture,
            [runner("attempt-2")],
            root,
            created_at=NOW,
            producer_code_commit=COMMIT,
        )
    assert sender.calls == calls_before


def test_resealed_checkpoint_with_foreign_prompt_hash_still_fails(tmp_path: Path) -> None:
    root = tmp_path / "probe"
    sender = _ProbeSender(fail_on_call=4, fail_code="http_429")
    runner, _ = _runtime(root, sender)
    fixture = load_default_scorer_probe_fixture_set()
    with pytest.raises(TransportCallError):
        run_evaluation_sf_bt_p2_probe_v1(
            fixture,
            [runner("attempt-1")],
            root,
            created_at=NOW,
            producer_code_commit=COMMIT,
        )
    checkpoint_path = next((root / "checkpoints").glob("*.json"))
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    checkpoint["binding"]["stage_input_sha256"] = "4" * 64
    checkpoint["integrity"]["checkpoint_sha256"] = "0" * 64
    checkpoint = probe_runner._seal(checkpoint, "EvaluationSfBtP2ProbeCheckpointV1")
    checkpoint_path.write_text(canonical_json(checkpoint) + "\n", encoding="utf-8")
    calls_before = sender.calls
    sender.fail_on_call = None
    with pytest.raises(Exception, match="reconstructed prompt"):
        run_evaluation_sf_bt_p2_probe_v1(
            fixture,
            [runner("attempt-2")],
            root,
            created_at=NOW,
            producer_code_commit=COMMIT,
        )
    assert sender.calls == calls_before


def test_attempt_id_cannot_be_reused_after_halt(tmp_path: Path) -> None:
    root = tmp_path / "probe"
    sender = _ProbeSender(fail_on_call=2, fail_code="http_429")
    runner, _ = _runtime(root, sender)
    fixture = load_default_scorer_probe_fixture_set()
    with pytest.raises(TransportCallError):
        run_evaluation_sf_bt_p2_probe_v1(
            fixture,
            [runner("attempt-reused")],
            root,
            created_at=NOW,
            producer_code_commit=COMMIT,
        )
    calls_before = sender.calls
    sender.fail_on_call = None
    with pytest.raises(Exception, match="attempt ID already has persisted evidence"):
        run_evaluation_sf_bt_p2_probe_v1(
            fixture,
            [runner("attempt-reused")],
            root,
            created_at=NOW,
            producer_code_commit=COMMIT,
        )
    assert sender.calls == calls_before


def test_changed_fixture_cannot_resume_existing_probe(tmp_path: Path) -> None:
    root = tmp_path / "probe"
    sender = _ProbeSender(fail_on_call=2, fail_code="http_429")
    runner, _ = _runtime(root, sender)
    fixture = load_default_scorer_probe_fixture_set()
    with pytest.raises(TransportCallError):
        run_evaluation_sf_bt_p2_probe_v1(
            fixture,
            [runner("attempt-1")],
            root,
            created_at=NOW,
            producer_code_commit=COMMIT,
        )
    changed = json.loads(json.dumps(fixture))
    p2 = next(row for row in changed["sf_bt_context_ablation"] if row["stratum"] == "P2_omission_control")
    p2["target_active_vi"] += "!"
    calls_before = sender.calls
    sender.fail_on_call = None
    with pytest.raises(Exception, match="approved fixture"):
        run_evaluation_sf_bt_p2_probe_v1(
            changed,
            [runner("attempt-2")],
            root,
            created_at=NOW,
            producer_code_commit=COMMIT,
        )
    assert sender.calls == calls_before


def test_checkpoint_validator_rejects_unknown_key() -> None:
    # Load-bearing closed-schema smoke test uses a valid checkpoint from a temp run
    # in the other tests; this direct malformed object must fail before hashing.
    with pytest.raises(Exception, match="unknown_keys|missing_keys"):
        validate_evaluation_sf_bt_p2_checkpoint_v1({"unexpected": True})
