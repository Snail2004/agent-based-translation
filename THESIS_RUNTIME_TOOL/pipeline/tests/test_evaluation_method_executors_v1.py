from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from pipeline.eval.common_input_v1 import (
    CommonArmV1,
    CommonBlockV1,
    CommonEvaluationInputV1,
    CommonTranslationV1,
    LegacyD2LSourceBindingV1,
    source_binding_to_dict,
)
from pipeline.eval.contracts_v1 import ContractValidationError
from pipeline.eval.execution_runner_v1 import execute_evaluation_plan_v1
from pipeline.eval.llm_profiles_v1 import (
    EVALUATION_LLM_ROLE_IDS,
    PJ_JUDGE_ROLE_ID,
    SF_BT_BACK_TRANSLATOR_ROLE_ID,
    SF_BT_SEMANTIC_JUDGE_ROLE_ID,
    build_evaluation_llm_profile_v1,
    evaluation_role_contract_v1,
)
from pipeline.eval.method_executors_v1 import (
    EvaluationMethodExecutorV1,
    SharedEvaluationRoleRunnerV1,
)
from pipeline.eval.offline_orchestrator_v1 import (
    build_evaluation_plan,
    seal_evaluation_run_config,
)
from pipeline.eval.scorer_input_packets_v1 import build_scorer_input_packet
from pipeline.llm_backend import (
    ApplicationResponseCache,
    ContentAddressedArtifactStore,
    MappingCredentialProvider,
    PhysicalQuotaScheduler,
    RawTransportResponse,
    SharedLlmAttemptLedger,
    SharedLlmBackend,
    TransportCallError,
    canonical_json,
    canonical_sha256,
)


NOW = "2026-07-19T00:00:00Z"
COMMIT = "a" * 40


class _Clock:
    def __init__(self) -> None:
        self.current = datetime(2026, 7, 19, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        value = self.current
        self.current += timedelta(milliseconds=100)
        return value


class _SemanticSender:
    def __init__(self, *, pj_mode: str = "consistent", bad_sf_bt: bool = False):
        self.pj_mode = pj_mode
        self.bad_sf_bt = bad_sf_bt
        self.calls = 0
        self.pj_calls = 0
        self.prompts: list[str] = []

    def send(self, request):
        self.calls += 1
        body = json.loads(request.body.decode("utf-8"))
        prompt = body["prompt"]
        self.prompts.append(prompt)
        if "independent Vietnamese-to-English back-translator" in prompt:
            output = {"back_translation": "English active claim."}
        elif "You compare two English passages" in prompt:
            output = (
                {"score": 30, "flags": [], "note": "invalid band"}
                if self.bad_sf_bt
                else {"score": 75, "flags": ["coverage_mismatch"], "note": "minor drift"}
            )
        elif "strict, impartial evaluator" in prompt:
            self.pj_calls += 1
            verdict = (
                "candidate_1"
                if self.pj_mode == "conflict" or self.pj_calls % 2 == 1
                else "candidate_2"
            )
            output = {
                "overall_verdict": verdict,
                "style_verdict": verdict,
                "tags": ["meaning"],
                "note": "one candidate preserves the active claim",
            }
        else:
            raise AssertionError("unexpected prompt")
        response = canonical_json(
            {
                "model": "evaluation-fixture-model",
                "finish_reason": "stop",
                "usage": {
                    "prompt_tokens": 40,
                    "cached_input_tokens": 0,
                    "completion_tokens": 12,
                    "reasoning_tokens": 0,
                    "total_tokens": 52,
                },
                "output_text": canonical_json(output),
            }
        ).encode("utf-8")
        return RawTransportResponse(
            status_code=200,
            headers={},
            body=response,
            request_id=f"fixture-request-{self.calls}",
        )


class _FailingSender:
    def __init__(self) -> None:
        self.calls = 0

    def send(self, request):
        self.calls += 1
        raise TransportCallError(
            code="http_500",
            status_code=500,
            safe_message="provider returned HTTP 500",
        )


def _common(*, equal_candidates: bool = False) -> CommonEvaluationInputV1:
    blocks = (
        CommonBlockV1("b001", "ch1", 1, "paragraph", "English before.", "translate"),
        CommonBlockV1("b002", "ch1", 2, "paragraph", "English active secret.", "translate"),
        CommonBlockV1("b003", "ch1", 3, "paragraph", "English after.", "translate"),
    )
    arms = (
        CommonArmV1(
            artifact_id="artifact-alpha",
            artifact_sha256="1" * 64,
            logical_run_id="logical-run",
            attempt_run_id="attempt-run-alpha",
            arm_id="S0",
            profile_id="profile-alpha",
            profile_config_sha256="3" * 64,
            source_language="en",
            target_language="vi",
        ),
        CommonArmV1(
            artifact_id="artifact-beta",
            artifact_sha256="2" * 64,
            logical_run_id="logical-run",
            attempt_run_id="attempt-run-beta",
            arm_id="S1",
            profile_id="profile-beta",
            profile_config_sha256="4" * 64,
            source_language="en",
            target_language="vi",
        ),
    )
    translations: list[CommonTranslationV1] = []
    for arm in arms:
        for block in blocks:
            if equal_candidates:
                text = f"Bản dịch chung {block.block_id}."
            elif arm.arm_id == "S0":
                text = f"Bản dịch thứ nhất {block.block_id}."
            else:
                text = f"Bản dịch thứ hai {block.block_id}."
            translations.append(
                CommonTranslationV1(
                    arm_id=arm.arm_id,
                    block_id=block.block_id,
                    status="translated",
                    target_text=text,
                    error_code=None,
                )
            )
    return CommonEvaluationInputV1(
        source_schema_id="D2LEvaluationInputV1",
        source_schema_version="1.0.0",
        source_binding=LegacyD2LSourceBindingV1(
            project_id="project",
            document_id="document",
            source_db_sha256="5" * 64,
            runtime_manifest_sha256="6" * 64,
        ),
        blocks=blocks,
        arms=arms,
        translations=tuple(translations),
    )


def _config(common: CommonEvaluationInputV1, *, methods=("sf_qe", "sf_bt", "pj")) -> dict:
    rows = []
    for method_id in methods:
        rows.append(
            {
                "method_id": method_id,
                "method_version": "executor-v1",
                "scorer_kind": "pairwise" if method_id == "pj" else "unary",
                "profile_scope": "common",
                "eligible_admissions": ["translate"],
            }
        )
    pairs = (
        [{"pair_id": "s0-vs-s1", "arm_1_id": "S0", "arm_2_id": "S1"}]
        if "pj" in methods
        else []
    )
    return seal_evaluation_run_config(
        {
            "schema_id": "EvaluationRunConfigV1",
            "schema_version": "1.0.0",
            "config_id": "method-executor-test",
            "created_at": NOW,
            "producer": {
                "workstream": "evaluation",
                "component": "method_executor_test",
                "component_version": "1.0.0",
                "code_commit": COMMIT,
            },
            "input_binding": {
                "source_schema_id": common.source_schema_id,
                "source_schema_version": common.source_schema_version,
                "source_binding": source_binding_to_dict(common.source_binding),
                "arm_artifacts": [
                    {
                        "arm_id": arm.arm_id,
                        "translation_artifact_id": arm.artifact_id,
                        "translation_artifact_sha256": arm.artifact_sha256,
                        "logical_run_id": arm.logical_run_id,
                        "attempt_run_id": arm.attempt_run_id,
                        "profile_id": arm.profile_id,
                        "profile_config_sha256": arm.profile_config_sha256,
                    }
                    for arm in common.arms
                ],
            },
            "methods": rows,
            "comparison_pairs": pairs,
            "unit_policy": {
                "unit_kind": "block",
                "context_before_blocks": 1,
                "context_after_blocks": 1,
            },
            "blinding": {"mode": "opaque_counterbalanced", "seed": "seed"},
            "retry_policy": {"max_transport_attempts": 1},
            "integrity": {"config_sha256": "0" * 64},
        }
    )


def _source() -> dict:
    return {
        "schema_version": "api_source_v1",
        "source_id": "evaluation_fixture_source_v1",
        "source_revision": "fixture_v1",
        "source_class": "local_in_process",
        "adapter_id": "evaluation_fixture_adapter_v1",
        "protocol": "local_in_process",
        "route_id": "fixture_callback",
        "endpoint_class": "in_process",
        "base_url": None,
        "credential_ref": None,
        "credential_commitment": None,
        "physical_quota_bucket_id": "evaluation-fixture-local-v1",
        "enabled": True,
    }


def _capability(role_id: str, source: dict) -> dict:
    contract = evaluation_role_contract_v1(role_id)
    return {
        "schema_version": "capability_evidence_v1",
        "capability_id": role_id.replace(".", "_") + "_fixture_capability_v1",
        "capability_revision": "fixture_v1",
        "source_id": source["source_id"],
        "source_revision": source["source_revision"],
        "adapter_id": source["adapter_id"],
        "protocol": source["protocol"],
        "route_id": source["route_id"],
        "base_url": source["base_url"],
        "requested_model_id": "evaluation-fixture-model",
        "observed_model_id": "evaluation-fixture-model",
        "capability_kind": "native_structured_output",
        "schema_dialect": "json_schema_2020_12",
        "schema_sha256": contract["response_schema"]["sha256"],
        "local_validator_id": contract["validator"]["id"],
        "local_validator_sha256": contract["validator"]["sha256"],
        "verdict": "qualified",
        "probe_id": role_id.replace(".", "_") + "_fixture_probe_v1",
        "evidence_sha256": "e" * 64,
        "observed_at_utc": NOW,
    }


def _runtime(
    tmp_path: Path,
    sender: _SemanticSender,
    common: CommonEvaluationInputV1,
    config: dict,
    *,
    cache_mode: str = "bypass",
    sf_qe_scorer=None,
    distribution_suffix: str | None = None,
):
    source = _source()
    if distribution_suffix is not None:
        source["source_id"] = f"evaluation_fixture_source_{distribution_suffix}"
        source["source_revision"] = f"fixture_{distribution_suffix}"
        source["physical_quota_bucket_id"] = (
            f"evaluation-fixture-local-{distribution_suffix}"
        )
    by_role = {
        role_id: _capability(role_id, source) for role_id in EVALUATION_LLM_ROLE_IDS
    }
    if distribution_suffix is not None:
        for capability in by_role.values():
            capability["capability_id"] = (
                f"{capability['capability_id']}_{distribution_suffix}"
            )
            capability["capability_revision"] = f"fixture_{distribution_suffix}"
            capability["probe_id"] = f"{capability['probe_id']}_{distribution_suffix}"
    capabilities = list(by_role.values())
    targets = {}
    for role_id, capability in by_role.items():
        targets[role_id] = {
            "source_id": source["source_id"],
            "source_revision": source["source_revision"],
            "source_record_sha256": canonical_sha256(source),
            "requested_model_id": capability["requested_model_id"],
            "capability_id": capability["capability_id"],
            "capability_revision": capability["capability_revision"],
            "capability_record_sha256": canonical_sha256(capability),
        }
    profile = build_evaluation_llm_profile_v1(primary_targets=targets)
    store = ContentAddressedArtifactStore(tmp_path / "objects")
    cache = ApplicationResponseCache(
        index_path=tmp_path / "response_cache.sqlite3", artifact_store=store
    )
    ledger = SharedLlmAttemptLedger(tmp_path / "attempt_ledger.sqlite3")
    backend = SharedLlmBackend(
        credential_provider=MappingCredentialProvider({}),
        scheduler=PhysicalQuotaScheduler(tmp_path / "quota_locks"),
        ledger=ledger,
        response_cache=cache,
        sender=sender,
        clock=_Clock(),
    )
    roles = SharedEvaluationRoleRunnerV1(
        backend=backend,
        profile=profile,
        api_sources=[source],
        capability_evidence=capabilities,
        run_id="evaluation_fixture_run",
        attempt_run_id="evaluation_fixture_attempt",
        cache_mode=cache_mode,
    )
    executor = EvaluationMethodExecutorV1(
        common_input=common,
        config_payload=config,
        sf_qe_scorer=(
            sf_qe_scorer
            if sf_qe_scorer is not None
            else lambda source_text, target_text: 0.9 if "thứ hai" in target_text else 0.8
        ),
        llm_roles=roles,
        created_at=NOW,
        producer_code_commit=COMMIT,
    )
    return executor, ledger


def test_end_to_end_executor_runs_local_qe_and_shared_fake_llm(tmp_path: Path) -> None:
    common = _common()
    config = _config(common)
    sender = _SemanticSender()
    executor, ledger = _runtime(tmp_path, sender, common, config)
    artifact = execute_evaluation_plan_v1(
        common,
        config,
        executor,
        created_at=NOW,
        runner_code_commit=COMMIT,
        baseline_arm_id="S0",
        candidate_arm_id="S1",
    )
    assert artifact["coverage"] == {
        "planned_job_count": 15,
        "blocked_job_count": 0,
        "succeeded_job_count": 15,
        "failed_job_count": 0,
    }
    assert sender.calls == 18
    assert ledger.count("usage") == 18
    assert all("S0" not in prompt and "S1" not in prompt for prompt in sender.prompts)
    sf_qe = next(row for row in artifact["aggregates"] if row["method_id"] == "sf_qe")
    assert sf_qe["comparison"]["wins"] == 3


def test_sf_bt_source_is_hidden_from_reverse_and_visible_to_semantic_judge(tmp_path: Path) -> None:
    common = _common()
    config = _config(common, methods=("sf_bt",))
    sender = _SemanticSender()
    executor, _ = _runtime(tmp_path, sender, common, config)
    execute_evaluation_plan_v1(
        common, config, executor, created_at=NOW, runner_code_commit=COMMIT
    )
    reverse = [prompt for prompt in sender.prompts if "back-translator" in prompt]
    semantic = [prompt for prompt in sender.prompts if "compare two English" in prompt]
    assert reverse and semantic
    assert all("English active secret." not in prompt for prompt in reverse)
    assert any("English active secret." in prompt for prompt in semantic)


def test_pj_two_order_consistency_maps_back_to_original_slots(tmp_path: Path) -> None:
    common = _common()
    config = _config(common, methods=("pj",))
    sender = _SemanticSender(pj_mode="consistent")
    executor, _ = _runtime(tmp_path, sender, common, config)
    artifact = execute_evaluation_plan_v1(
        common, config, executor, created_at=NOW, runner_code_commit=COMMIT
    )
    assert sender.calls == 6
    assert all(
        row["semantic_output"]["overall_verdict"] == "candidate_1"
        for row in artifact["jobs"]
    )


def test_pj_order_conflict_resolves_conservatively_to_tie(tmp_path: Path) -> None:
    common = _common()
    config = _config(common, methods=("pj",))
    sender = _SemanticSender(pj_mode="conflict")
    executor, _ = _runtime(tmp_path, sender, common, config)
    artifact = execute_evaluation_plan_v1(
        common, config, executor, created_at=NOW, runner_code_commit=COMMIT
    )
    assert all(
        row["semantic_output"]["overall_verdict"] == "tie"
        and "conservatively resolved" in row["semantic_output"]["note"]
        for row in artifact["jobs"]
    )


def test_pj_mechanical_equal_uses_zero_provider_calls(tmp_path: Path) -> None:
    common = _common(equal_candidates=True)
    config = _config(common, methods=("pj",))
    sender = _SemanticSender()
    executor, ledger = _runtime(tmp_path, sender, common, config)
    artifact = execute_evaluation_plan_v1(
        common, config, executor, created_at=NOW, runner_code_commit=COMMIT
    )
    assert sender.calls == 0
    assert ledger.count("usage") == 0
    assert all(row["semantic_output"]["overall_verdict"] == "tie" for row in artifact["jobs"])


def test_sf_bt_semantic_contract_rejection_becomes_failed_denominator(tmp_path: Path) -> None:
    common = _common()
    config = _config(common, methods=("sf_bt",))
    sender = _SemanticSender(bad_sf_bt=True)
    executor, _ = _runtime(tmp_path, sender, common, config)
    artifact = execute_evaluation_plan_v1(
        common, config, executor, created_at=NOW, runner_code_commit=COMMIT
    )
    assert artifact["coverage"]["failed_job_count"] == 6
    assert all(row["error_code"] == "sf_bt_semantic_score_band" for row in artifact["jobs"])


@pytest.mark.parametrize("score", [float("nan"), -0.1, 100.1])
def test_sf_qe_nonfinite_or_out_of_range_score_fails_job(tmp_path: Path, score: float) -> None:
    common = _common()
    config = _config(common, methods=("sf_qe",))
    sender = _SemanticSender()
    executor, _ = _runtime(
        tmp_path,
        sender,
        common,
        config,
        sf_qe_scorer=lambda _source, _target: score,
    )
    artifact = execute_evaluation_plan_v1(
        common, config, executor, created_at=NOW, runner_code_commit=COMMIT
    )
    assert artifact["coverage"]["failed_job_count"] == 6


def test_exact_cache_hit_reuses_stage_outputs_with_bound_provenance(tmp_path: Path) -> None:
    common = _common()
    config = _config(common, methods=("sf_bt",))
    sender = _SemanticSender()
    executor, ledger = _runtime(tmp_path, sender, common, config, cache_mode="read_write")
    plan = build_evaluation_plan(common, config)
    job = next(row for row in plan.jobs if row.status == "ready")
    packet = build_scorer_input_packet(
        common, plan, job.job_id, created_at=NOW, producer_code_commit=COMMIT
    )
    first = executor(packet)
    second = executor(copy.deepcopy(packet))
    assert first == second
    assert sender.calls == 2
    assert ledger.count("usage") == 2
    assert ledger.count("cache") == 4


def test_foreign_packet_is_rejected_before_any_model_call(tmp_path: Path) -> None:
    common = _common()
    config = _config(common, methods=("sf_qe",))
    sender = _SemanticSender()
    executor, _ = _runtime(tmp_path, sender, common, config)
    plan = build_evaluation_plan(common, config)
    job = next(row for row in plan.jobs if row.status == "ready")
    packet = build_scorer_input_packet(
        common, plan, job.job_id, created_at=NOW, producer_code_commit=COMMIT
    )
    packet["binding"]["plan_sha256"] = "f" * 64
    with pytest.raises(ContractValidationError):
        executor(packet)
    assert sender.calls == 0


def test_transport_failure_halts_without_hidden_retry_or_fallback(tmp_path: Path) -> None:
    common = _common()
    config = _config(common, methods=("sf_bt",))
    sender = _FailingSender()
    executor, ledger = _runtime(tmp_path, sender, common, config)
    with pytest.raises(TransportCallError, match="HTTP 500"):
        execute_evaluation_plan_v1(
            common, config, executor, created_at=NOW, runner_code_commit=COMMIT
        )
    assert sender.calls == 1
    assert ledger.count("usage") == 1
    assert ledger.count("error") == 1
