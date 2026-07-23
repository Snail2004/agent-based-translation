from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import pytest

import pipeline.eval.benchmark_runner_v1 as benchmark_runner_module
from pipeline.eval.benchmark_aggregate_v1 import validate_benchmark_run_report_v1
from pipeline.eval.benchmark_runner_v1 import (
    BenchmarkChapterRuntimeV1,
    run_benchmark_end_to_end_v1,
)
from pipeline.eval.benchmark_v1 import (
    BENCHMARK_ARM_IDS_V1,
    BENCHMARK_CHAPTER_IDS_V1,
    build_benchmark_manifest_v1,
    build_benchmark_preflight_v1,
    build_overlay_from_common_arm_v1,
)
from pipeline.eval.common_input_v1 import (
    CommonArmV1,
    CommonBlockV1,
    CommonEvaluationInputV1,
    CommonSourceSnapshotV1,
    CommonTranslationV1,
    LegacyD2LSourceBindingV1,
    source_binding_to_dict,
)
from pipeline.eval.contracts_v1 import ContractValidationError
from pipeline.eval.end_to_end_runner_v1 import (
    LocalSfQeRuntimeV1,
    run_evaluation_end_to_end_v1,
)
from pipeline.eval.execution_runner_v1 import seal_evaluation_execution_artifact
from pipeline.eval.local_sf_qe_v1 import SF_QE_MODEL_ID
from pipeline.llm_backend import SharedLlmAttemptLedger
from pipeline.eval.offline_orchestrator_v1 import seal_evaluation_run_config


NOW = "2026-07-21T16:00:00Z"
COMMIT = "d" * 40
SOURCE_DB = "1" * 64
_ROLES = {
    "S0": "pipeline_ablation",
    "S1": "thesis_system",
    "community": "human_community",
    "google_nmt": "conventional_nmt",
    "llm_lc": "long_context_diagnostic",
}


class _Predictor:
    def __init__(self, score: float, *, fail: bool = False) -> None:
        self.score = score
        self.fail = fail
        self.calls = 0

    def __call__(self, rows, batch_size):
        self.calls += 1
        if self.fail:
            raise AssertionError("completed chapter must not call its local scorer")
        assert batch_size == 8
        return [self.score for _ in rows]


class _FakeProviderRoleRunner:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0
        self.execution_binding = {
            "evaluation_logical_run_id": "evaluation_fixture_run",
            "evaluation_attempt_run_id": "evaluation_fixture_attempt",
            "evaluation_profile_id": "evaluation_fixture_profile",
            "evaluation_profile_sha256": "a" * 64,
        }
        self.cache_mode = "bypass"
        self.semantic_contract = {"contract_id": "fixture"}
        self.attempt_runtime_binding = {"binding_id": "fixture"}

    def execute(self, **kwargs):
        self.calls += 1
        if self.fail:
            raise RuntimeError("fixture provider failure")
        return {"status": "accepted"}


class _UsageSyncSpy:
    def __init__(self) -> None:
        self.calls = []

    def sync_usage_from_ledger(self, ledger, **kwargs):
        self.calls.append((ledger, kwargs))
        return ()


def _sources() -> list[CommonSourceSnapshotV1]:
    result = []
    order = 1
    for ordinal, chapter_id in enumerate(BENCHMARK_CHAPTER_IDS_V1):
        blocks = []
        for index in range(ordinal + 1):
            blocks.append(
                CommonBlockV1(
                    f"{chapter_id}_b{index + 1:03d}",
                    chapter_id,
                    order,
                    "paragraph",
                    f"English claim {chapter_id} {index + 1}.",
                    "translate",
                )
            )
            order += 1
        blocks.append(
            CommonBlockV1(
                f"{chapter_id}_preserve",
                chapter_id,
                order,
                "code",
                "x = 1",
                "preserve",
            )
        )
        order += 1
        result.append(
            CommonSourceSnapshotV1(
                "D2LEvaluationInputV1",
                "1.0.0",
                LegacyD2LSourceBindingV1(
                    "d2l",
                    "d2l",
                    SOURCE_DB,
                    hashlib.sha256(chapter_id.encode()).hexdigest(),
                ),
                tuple(blocks),
            )
        )
    return result


def _common(
    source: CommonSourceSnapshotV1,
    arm_ids: tuple[str, ...] = BENCHMARK_ARM_IDS_V1,
) -> CommonEvaluationInputV1:
    arms = []
    translations = []
    for arm_id in arm_ids:
        arms.append(
            CommonArmV1(
                f"artifact-{arm_id}-{source.blocks[0].chapter_id}",
                hashlib.sha256(f"{arm_id}-{source.blocks[0].chapter_id}".encode()).hexdigest(),
                f"logical-{arm_id}",
                f"attempt-{arm_id}",
                arm_id,
                f"profile-{arm_id}",
                hashlib.sha256(f"profile-{arm_id}".encode()).hexdigest(),
                "en",
                "vi",
            )
        )
        for block in source.blocks:
            translations.append(
                CommonTranslationV1(
                    arm_id,
                    block.block_id,
                    "preserved" if block.admission == "preserve" else "translated",
                    block.source_text if block.admission == "preserve" else f"Ban dich {arm_id} {block.block_id}",
                    None,
                )
            )
    return CommonEvaluationInputV1(
        source.source_schema_id,
        source.source_schema_version,
        source.source_binding,
        source.blocks,
        tuple(arms),
        tuple(translations),
    )


def _manifest_and_preflight(
    sources,
    *,
    ready: bool = True,
    arm_ids: tuple[str, ...] = BENCHMARK_ARM_IDS_V1,
):
    chapter_ids = tuple(source.blocks[0].chapter_id for source in sources)
    evidence = [
        {
            "chapter_id": source.blocks[0].chapter_id,
            "source_artifact_id": f"source-{index}",
            "source_artifact_sha256": hashlib.sha256(f"source-{index}".encode()).hexdigest(),
            "source_evidence_kind": "d2l_evaluation_package",
        }
        for index, source in enumerate(sources)
    ]
    manifest = build_benchmark_manifest_v1(
        sources,
        evidence,
        benchmark_id="d2l-five-chapter-e2e-v1",
        created_at=NOW,
        producer_code_commit=COMMIT,
        selected_chapter_ids=chapter_ids,
        selected_arm_ids=arm_ids,
    )
    overlays = []
    for source in sources:
        common = _common(source, arm_ids)
        for arm_id in arm_ids:
            if not ready and source is sources[-1] and arm_id == "community":
                continue
            overlays.append(
                build_overlay_from_common_arm_v1(
                    common,
                    chapter_id=source.blocks[0].chapter_id,
                    arm_id=arm_id,
                    benchmark_role=_ROLES[arm_id],
                    created_at=NOW,
                    producer_code_commit=COMMIT,
                )
            )
    preflight = build_benchmark_preflight_v1(
        manifest,
        sources,
        overlays,
        created_at=NOW,
        producer_code_commit=COMMIT,
    )
    return manifest, preflight, overlays


def _config(common: CommonEvaluationInputV1) -> dict:
    return seal_evaluation_run_config(
        {
            "schema_id": "EvaluationRunConfigV1",
            "schema_version": "1.0.0",
            "config_id": "five-chapter-sf-qe-fixture",
            "created_at": NOW,
            "producer": {
                "workstream": "evaluation",
                "component": "benchmark_runner_test",
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
            "methods": [
                {
                    "method_id": "sf_qe",
                    "method_version": "sf-qe-fixture-v1",
                    "scorer_kind": "unary",
                    "profile_scope": "common",
                    "eligible_admissions": ["translate"],
                }
            ],
            "comparison_pairs": [],
            "unit_policy": {
                "unit_kind": "block",
                "context_before_blocks": 1,
                "context_after_blocks": 1,
            },
            "blinding": {"mode": "opaque_counterbalanced", "seed": "fixed-seed"},
            "retry_policy": {"max_transport_attempts": 1},
            "integrity": {"config_sha256": "0" * 64},
        }
    )


def _runtime_root(root: Path, ordinal: int, chapter_id: str) -> Path:
    return root / "chapters" / f"{ordinal:02d}_{chapter_id}"


def _runtimes(
    root: Path,
    sources,
    predictors,
    *,
    arm_ids: tuple[str, ...] = BENCHMARK_ARM_IDS_V1,
) -> dict[str, BenchmarkChapterRuntimeV1]:
    result = {}
    for ordinal, (source, predictor) in enumerate(zip(sources, predictors, strict=True)):
        chapter_id = source.blocks[0].chapter_id
        common = _common(source, arm_ids)
        child = _runtime_root(root, ordinal, chapter_id)
        input_path = child / "input" / "evaluation_input.json"
        input_path.parent.mkdir(parents=True, exist_ok=True)
        input_path.write_text('{"schema_id":"fixture"}\n', encoding="utf-8", newline="\n")
        arm_presentations = []
        for arm in common.arms:
            relative = f"translations/{arm.arm_id}.json"
            path = child / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"arm_id": arm.arm_id}) + "\n", encoding="utf-8", newline="\n")
            if arm.arm_id == "S0":
                role, kind = "baseline", "system"
            elif arm.arm_id == "S1":
                role, kind = "candidate", "system"
            elif arm.arm_id == "community":
                role, kind = "reference", "human_reference"
            else:
                role, kind = "external_baseline", "machine_baseline"
            arm_presentations.append(
                {
                    "arm_id": arm.arm_id,
                    "role": role,
                    "kind": kind,
                    "label": arm.arm_id,
                    "relative_path": relative,
                }
            )
        result[chapter_id] = BenchmarkChapterRuntimeV1(
            common_input=common,
            config_payload=_config(common),
            input_artifact={
                "artifact_id": f"input-{chapter_id}",
                "relative_path": "input/evaluation_input.json",
                "sha256": hashlib.sha256(chapter_id.encode()).hexdigest(),
            },
            arm_presentations=arm_presentations,
            method_presentations=[
                {
                    "display_name": "Semantic fidelity QE",
                    "method": {
                        "method_id": "sf_qe",
                        "method_version": "sf-qe-fixture-v1",
                        "implementation_commit": COMMIT,
                        "prompt_version": None,
                        "model_id": SF_QE_MODEL_ID,
                    },
                }
            ],
            local_sf_qe_runtime=LocalSfQeRuntimeV1(
                predictor=predictor,
                checkpoint_sha256="7" * 64,
                package_name="unbabel-comet",
                package_version="2.2.7",
                device="cpu",
                batch_size=8,
                clock=lambda: datetime(2026, 7, 21, 16, 0, tzinfo=timezone.utc),
                monotonic=lambda: 1.0,
            ),
            caveats=("Fixture-only local scorer.",),
        )
    return result


def _run(root, manifest, preflight, overlays, runtimes, **kwargs):
    return run_benchmark_end_to_end_v1(
        manifest,
        preflight,
        overlays,
        runtimes,
        root,
        generated_at=NOW,
        producer_code_commit=COMMIT,
        evaluation_logical_run_id="five-chapter-logical-run",
        evaluation_attempt_run_id="five-chapter-attempt-run",
        evaluation_profile_id="evaluation-five-chapter-fixture-v1",
        policy_profile_id=None,
        baseline_arm_id="S0",
        candidate_arm_id="S1",
        **kwargs,
    )


def test_five_chapter_runner_aggregates_denominators_instead_of_chapter_means(tmp_path: Path) -> None:
    sources = _sources()
    manifest, preflight, overlays = _manifest_and_preflight(sources)
    predictors = [_Predictor((ordinal + 1) / 10) for ordinal in range(5)]
    root = tmp_path / "benchmark"
    result = _run(root, manifest, preflight, overlays, _runtimes(root, sources, predictors))

    assert result.status["state"] == "completed"
    assert result.report is not None
    assert result.report["coverage"] == {
        "expected_chapter_count": 5,
        "completed_chapter_count": 5,
        "planned_job_count": 75,
        "blocked_job_count": 0,
        "succeeded_job_count": 75,
        "failed_job_count": 0,
    }
    aggregate = result.report["aggregates"][0]
    for arm in aggregate["arm_values"]:
        assert arm["denominator"] == 15
        assert arm["numerator"] == pytest.approx(550.0)
        assert arm["value"] == pytest.approx(550.0 / 15)
    assert [predictor.calls for predictor in predictors] == [1, 1, 1, 1, 1]
    validate_benchmark_run_report_v1(result.report)


def test_runner_executes_only_selected_chapters_and_arms(tmp_path: Path) -> None:
    sources = _sources()[2:4]
    chapter_ids = tuple(source.blocks[0].chapter_id for source in sources)
    arm_ids = ("S0", "S1", "google_nmt")
    manifest, preflight, overlays = _manifest_and_preflight(
        sources, arm_ids=arm_ids
    )
    predictors = [_Predictor(0.5) for _ in sources]
    root = tmp_path / "bounded"
    result = _run(
        root,
        manifest,
        preflight,
        overlays,
        _runtimes(root, sources, predictors, arm_ids=arm_ids),
    )

    assert result.status["state"] == "completed"
    assert result.status["selected_chapter_ids"] == list(chapter_ids)
    assert list(result.status["chapter_states"]) == list(chapter_ids)
    assert result.report is not None
    assert result.report["identity"]["selected_chapter_ids"] == list(chapter_ids)
    assert result.report["identity"]["selected_arm_ids"] == list(arm_ids)
    assert result.report["identity"]["selected_scorer_ids"] == ["sf_qe"]
    assert result.report["identity"]["workflow_settings_sha256"] is None
    assert result.report["coverage"]["expected_chapter_count"] == 2
    assert [row["chapter_id"] for row in result.report["chapter_runs"]] == list(
        chapter_ids
    )
    assert [predictor.calls for predictor in predictors] == [1, 1]


def test_resume_rejects_changed_arm_selection(tmp_path: Path) -> None:
    sources = _sources()[2:3]
    root = tmp_path / "selection-locked"
    first_arms = ("S0", "S1")
    manifest, preflight, overlays = _manifest_and_preflight(
        sources, arm_ids=first_arms
    )
    _run(
        root,
        manifest,
        preflight,
        overlays,
        _runtimes(root, sources, [_Predictor(0.5)], arm_ids=first_arms),
    )

    changed_arms = ("S0", "S1", "google_nmt")
    changed_manifest, changed_preflight, changed_overlays = _manifest_and_preflight(
        sources, arm_ids=changed_arms
    )
    with pytest.raises(ContractValidationError, match="resume_binding"):
        _run(
            root,
            changed_manifest,
            changed_preflight,
            changed_overlays,
            _runtimes(
                root,
                sources,
                [_Predictor(0.5)],
                arm_ids=changed_arms,
            ),
        )


def test_blocked_preflight_makes_zero_chapter_runner_calls(tmp_path: Path) -> None:
    sources = _sources()
    manifest, preflight, overlays = _manifest_and_preflight(sources, ready=False)
    predictors = [_Predictor(0.5) for _ in sources]
    root = tmp_path / "benchmark"
    calls = 0

    def forbidden(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("blocked preflight must not invoke a scorer runner")

    result = _run(
        root,
        manifest,
        preflight,
        overlays,
        _runtimes(root, sources, predictors),
        chapter_runner=forbidden,
    )
    assert result.status["state"] == "blocked"
    assert result.report is None
    assert calls == 0
    assert [predictor.calls for predictor in predictors] == [0, 0, 0, 0, 0]


def test_resume_reuses_completed_chapters_after_third_chapter_failure(tmp_path: Path) -> None:
    sources = _sources()
    manifest, preflight, overlays = _manifest_and_preflight(sources)
    predictors = [_Predictor(0.5) for _ in sources]
    root = tmp_path / "benchmark"
    runtimes = _runtimes(root, sources, predictors)
    failed_once = False

    def fail_third(common, config, child_root, **kwargs):
        nonlocal failed_once
        chapter_id = common.blocks[0].chapter_id
        if chapter_id == BENCHMARK_CHAPTER_IDS_V1[2] and not failed_once:
            failed_once = True
            raise RuntimeError("fixture interruption")
        return run_evaluation_end_to_end_v1(common, config, child_root, **kwargs)

    with pytest.raises(RuntimeError, match="fixture interruption"):
        _run(root, manifest, preflight, overlays, runtimes, chapter_runner=fail_third)
    assert [predictor.calls for predictor in predictors] == [1, 1, 0, 0, 0]

    result = _run(root, manifest, preflight, overlays, runtimes)
    assert result.status["state"] == "completed"
    assert [predictor.calls for predictor in predictors] == [1, 1, 1, 1, 1]
    assert result.chapter_results[0].reused_complete_run is True
    assert result.chapter_results[1].reused_complete_run is True


def test_scoring_contract_drift_fails_before_any_chapter_call(tmp_path: Path) -> None:
    sources = _sources()
    manifest, preflight, overlays = _manifest_and_preflight(sources)
    predictors = [_Predictor(0.5) for _ in sources]
    root = tmp_path / "benchmark"
    runtimes = _runtimes(root, sources, predictors)
    chapter_id = BENCHMARK_CHAPTER_IDS_V1[-1]
    changed = copy.deepcopy(runtimes[chapter_id].config_payload)
    changed["unit_policy"]["context_before_blocks"] = 0
    changed["integrity"]["config_sha256"] = "0" * 64
    changed = seal_evaluation_run_config(changed)
    runtimes[chapter_id] = BenchmarkChapterRuntimeV1(
        **{
            **{field: getattr(runtimes[chapter_id], field) for field in runtimes[chapter_id].__dataclass_fields__},
            "config_payload": changed,
        }
    )

    with pytest.raises(ContractValidationError, match="scoring policy or model contract differs"):
        _run(root, manifest, preflight, overlays, runtimes)
    assert [predictor.calls for predictor in predictors] == [0, 0, 0, 0, 0]


def test_tampered_benchmark_report_fails_closed_on_reuse(tmp_path: Path) -> None:
    sources = _sources()
    manifest, preflight, overlays = _manifest_and_preflight(sources)
    predictors = [_Predictor(0.5) for _ in sources]
    root = tmp_path / "benchmark"
    runtimes = _runtimes(root, sources, predictors)
    result = _run(root, manifest, preflight, overlays, runtimes)
    assert result.report_path is not None
    row = json.loads(result.report_path.read_text(encoding="utf-8"))
    row["coverage"]["succeeded_job_count"] -= 1
    result.report_path.write_text(json.dumps(row), encoding="utf-8", newline="\n")

    with pytest.raises(ContractValidationError):
        _run(root, manifest, preflight, overlays, runtimes)
    assert [predictor.calls for predictor in predictors] == [1, 1, 1, 1, 1]


def test_preflight_approved_overlay_must_equal_scoring_runtime(tmp_path: Path) -> None:
    sources = _sources()
    manifest, _, overlays = _manifest_and_preflight(sources)
    chapter_id = BENCHMARK_CHAPTER_IDS_V1[0]
    original = _common(sources[0])
    changed_translations = list(original.translations)
    changed_index = next(
        index
        for index, row in enumerate(changed_translations)
        if row.arm_id == "S0" and row.status == "translated"
    )
    changed_row = changed_translations[changed_index]
    changed_translations[changed_index] = CommonTranslationV1(
        changed_row.arm_id,
        changed_row.block_id,
        changed_row.status,
        "Ban dich da duoc preflight phe duyet nhung khac runtime.",
        changed_row.error_code,
    )
    changed_common = CommonEvaluationInputV1(
        original.source_schema_id,
        original.source_schema_version,
        original.source_binding,
        original.blocks,
        original.arms,
        tuple(changed_translations),
    )
    replacement = build_overlay_from_common_arm_v1(
        changed_common,
        chapter_id=chapter_id,
        arm_id="S0",
        benchmark_role=_ROLES["S0"],
        created_at=NOW,
        producer_code_commit=COMMIT,
    )
    changed_overlays = [
        replacement
        if row["arm"]["arm_id"] == "S0" and row["source"]["chapter_id"] == chapter_id
        else row
        for row in overlays
    ]
    preflight = build_benchmark_preflight_v1(
        manifest,
        sources,
        changed_overlays,
        created_at=NOW,
        producer_code_commit=COMMIT,
    )
    predictors = [_Predictor(0.5) for _ in sources]
    root = tmp_path / "benchmark"

    with pytest.raises(ContractValidationError, match="runtime translation differs"):
        _run(
            root,
            manifest,
            preflight,
            changed_overlays,
            _runtimes(root, sources, predictors),
        )
    assert [predictor.calls for predictor in predictors] == [0, 0, 0, 0, 0]


def test_valid_resealed_child_execution_tamper_fails_on_parent_reuse(tmp_path: Path) -> None:
    sources = _sources()
    manifest, preflight, overlays = _manifest_and_preflight(sources)
    predictors = [_Predictor(0.5) for _ in sources]
    root = tmp_path / "benchmark"
    runtimes = _runtimes(root, sources, predictors)
    result = _run(root, manifest, preflight, overlays, runtimes)
    child_execution_path = result.chapter_results[0].execution_path
    execution = json.loads(child_execution_path.read_text(encoding="utf-8"))
    execution["created_at"] = "2026-07-21T16:00:01Z"
    execution["integrity"]["artifact_sha256"] = "0" * 64
    resealed = seal_evaluation_execution_artifact(execution)
    child_execution_path.write_text(
        json.dumps(resealed, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    with pytest.raises(ContractValidationError, match="benchmark report reference"):
        _run(root, manifest, preflight, overlays, runtimes)
    assert [predictor.calls for predictor in predictors] == [1, 1, 1, 1, 1]


def test_checkpoint_ordinal_outside_benchmark_is_controlled_contract_error(tmp_path: Path) -> None:
    sources = _sources()
    manifest, preflight, overlays = _manifest_and_preflight(sources)
    predictors = [_Predictor(0.5) for _ in sources]
    root = tmp_path / "benchmark"
    _run(root, manifest, preflight, overlays, _runtimes(root, sources, predictors))
    checkpoint_path = (
        root
        / "benchmark_state"
        / "chapters"
        / f"00_{BENCHMARK_CHAPTER_IDS_V1[0]}.json"
    )
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    checkpoint["ordinal"] = len(BENCHMARK_CHAPTER_IDS_V1)
    checkpoint_path.write_text(
        json.dumps(checkpoint, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    with pytest.raises(
        ContractValidationError, match="outside the registered chapter universe"
    ):
        _run(root, manifest, preflight, overlays, _runtimes(root, sources, predictors))
    assert [predictor.calls for predictor in predictors] == [1, 1, 1, 1, 1]


def test_usage_recording_role_runner_syncs_after_success_and_failure(
    tmp_path: Path,
) -> None:
    ledger = SharedLlmAttemptLedger(tmp_path / "attempt_ledger.sqlite3")
    workflow = _UsageSyncSpy()
    success = _FakeProviderRoleRunner()
    wrapped = benchmark_runner_module._UsageRecordingRoleRunnerV1(
        success,
        workflow=workflow,
        ledger=ledger,
        stage_id="chapter_d2l_preliminaries",
    )
    assert wrapped.execute(logical_request_id="request_success") == {
        "status": "accepted"
    }
    assert workflow.calls[-1][1]["current_work_id"] == "request_success"

    failure = _FakeProviderRoleRunner(fail=True)
    wrapped_failure = benchmark_runner_module._UsageRecordingRoleRunnerV1(
        failure,
        workflow=workflow,
        ledger=ledger,
        stage_id="chapter_d2l_preliminaries",
    )
    with pytest.raises(RuntimeError, match="provider failure"):
        wrapped_failure.execute(logical_request_id="request_failure")
    assert workflow.calls[-1][1]["current_work_id"] == "request_failure"


def test_provider_runtime_requires_exact_ledger_pair_before_chapter_execution(
    tmp_path: Path,
) -> None:
    sources = _sources()
    manifest, preflight, overlays = _manifest_and_preflight(sources)
    predictors = [_Predictor(0.5) for _ in sources]
    root = tmp_path / "benchmark"
    runtimes = _runtimes(root, sources, predictors)
    chapter_id = BENCHMARK_CHAPTER_IDS_V1[0]
    current = runtimes[chapter_id]
    runtimes[chapter_id] = BenchmarkChapterRuntimeV1(
        **{
            **{
                field: getattr(current, field)
                for field in current.__dataclass_fields__
            },
            "llm_roles": _FakeProviderRoleRunner(),
        }
    )
    with pytest.raises(ContractValidationError, match="attempt ledger"):
        _run(root, manifest, preflight, overlays, runtimes)
    assert [predictor.calls for predictor in predictors] == [0, 0, 0, 0, 0]
