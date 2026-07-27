from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
from typing import Any

import pytest

import pipeline.literary.chapter_cycle_orchestrator_v1 as orchestrator
from pipeline.literary.chapter_cycle_orchestrator_v1 import (
    ApiCallPermit,
    ChapterCycleIntegrityError,
    ChapterCycleOrchestratorError,
    ChapterCycleStage,
    ChapterCycleStagePause,
    StageExecutionResult,
    advance_chapter_cycle_stage_v1,
    build_dynamic_stage_plan_v1,
    initialize_chapter_cycle_run_v1,
    load_chapter_cycle_plan_v1,
    load_chapter_cycle_state_v1,
    resume_chapter_cycle_run_v1,
    run_chapter_cycle_until_boundary_v1,
)
from pipeline.literary.chapter_cycle_resilience_v1 import IntegrityOrLineageFailure
from pipeline.literary.checkpoint import canonical_hash


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
CONFIG_ROOT = RUNTIME_ROOT / "pipeline" / "configs"
DEFAULT_PROFILE = CONFIG_ROOT / "literary_chapter_cycle_profile_v1.json"
PIPELINE_PROFILE = CONFIG_ROOT / "literary_pipeline_profile_v1.json"


def _document(chapter_count: int = 4) -> dict[str, Any]:
    return {
        "document_id": "book-neutral-fixture",
        "chapters": [
            {
                "chapter_id": f"fixture_ch{index:02d}",
                "blocks": [
                    {
                        "block_id": f"fixture_ch{index:02d}_b001",
                        "order_index": 1,
                        "block_type": "paragraph",
                        "clean_text": f"Source chapter {index}.",
                    }
                ],
            }
            for index in range(1, chapter_count + 1)
        ],
    }


def _write_document(tmp_path: Path, chapter_count: int = 4) -> Path:
    path = tmp_path / "document.json"
    path.write_text(
        json.dumps(_document(chapter_count), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def _write_frozen_db(tmp_path: Path) -> Path:
    path = tmp_path / "frozen.sqlite3"
    path.write_bytes(b"offline-frozen-fixture")
    return path


def _write_profile(
    tmp_path: Path,
    *,
    max_api_calls_per_chapter: int | None = None,
    max_api_calls_per_run: int | None = None,
) -> Path:
    profile = json.loads(DEFAULT_PROFILE.read_text(encoding="utf-8"))
    provider_source = CONFIG_ROOT / profile["provider_profile"]
    provider_target = tmp_path / provider_source.name
    provider_target.write_bytes(provider_source.read_bytes())
    if max_api_calls_per_chapter is not None:
        profile["orchestration"]["max_api_calls_per_chapter"] = (
            max_api_calls_per_chapter
        )
    if max_api_calls_per_run is not None:
        profile["orchestration"]["max_api_calls_per_run"] = max_api_calls_per_run
    path = tmp_path / "literary_chapter_cycle_profile_v1.json"
    path.write_text(
        json.dumps(profile, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def _initialize(
    tmp_path: Path,
    *,
    chapter_count: int = 4,
    stop_after: int | None = None,
    profile_path: Path = DEFAULT_PROFILE,
) -> Path:
    run_root = tmp_path / "run"
    document_path = _write_document(tmp_path, chapter_count)
    initialize_chapter_cycle_run_v1(
        run_root=run_root,
        document_path=document_path,
        profile_path=profile_path,
        frozen_db_path=_write_frozen_db(tmp_path),
        ordered_chapter_ids=[
            f"fixture_ch{index:02d}" for index in range(1, chapter_count + 1)
        ],
        stop_after_chapter_count=stop_after,
    )
    return run_root


class SyntheticExecutor:
    def __init__(
        self,
        *,
        pending_stages: set[str] | None = None,
        fail_once_stage: str | None = None,
        integrity_stage: str | None = None,
    ) -> None:
        self.pending_stages = pending_stages or set()
        self.fail_once_stage = fail_once_stage
        self.integrity_stage = integrity_stage
        self.invocations: Counter[str] = Counter()
        self.transports: list[str] = []

    def __call__(
        self, stage: ChapterCycleStage, permit: ApiCallPermit
    ) -> StageExecutionResult:
        self.invocations[stage.stage_id] += 1
        if (
            self.fail_once_stage == stage.stage_id
            and self.invocations[stage.stage_id] == 1
        ):
            raise ChapterCycleStagePause("transport", "synthetic_transport_pause")
        if self.integrity_stage == stage.stage_id:
            raise IntegrityOrLineageFailure("synthetic_lineage_failure")

        calls = 0
        call_disposition = "code_only"
        request_fingerprint = None
        model_actual = None
        resilience_report_hash = None
        if stage.requires_api:
            if stage.stage_name in {"stable_claim_components", "identity_components"}:
                call_disposition = "not_required"
            else:
                permit.reserve("main")
                self.transports.append(stage.stage_id)
                calls = 1
                call_disposition = "called"
                request_fingerprint = canonical_hash(
                    {"stage_id": stage.stage_id, "request": "synthetic"}
                )
                model_actual = "synthetic-model"
                resilience_report_hash = canonical_hash(
                    {"stage_id": stage.stage_id, "report": "accepted"}
                )

        status = (
            "semantic_pending"
            if stage.stage_id in self.pending_stages
            else "accepted"
        )
        cumulative_updates: dict[str, str] = {}
        cumulative_key = {
            "local_auditor": "review_ledger_hash",
            "prefix": "prefix_hash",
            "prefix_extend": "prefix_hash",
            "stable_claim_reconcile": "claim_ledger_hash",
            "semantic_leads": "semantic_lead_index_hash",
            "identity_reconcile": "identity_ledger_hash",
        }.get(stage.stage_name)
        if cumulative_key is not None:
            cumulative_updates[cumulative_key] = canonical_hash(
                {"stage_id": stage.stage_id, "ledger": cumulative_key}
            )
        return StageExecutionResult(
            status=status,
            payload={"stage_id": stage.stage_id, "synthetic": True},
            call_disposition=call_disposition,
            request_fingerprint=request_fingerprint,
            model_actual=model_actual,
            resilience_report_hash=resilience_report_hash,
            attempt_count=calls,
            semantic_pending_count=1 if status == "semantic_pending" else 0,
            cumulative_hash_updates=cumulative_updates,
        )


def test_dynamic_four_chapter_plan_is_contiguous_and_chapter_indexed() -> None:
    document = _document(4)
    rows = build_dynamic_stage_plan_v1(
        document=document,
        ordered_chapter_ids=[f"fixture_ch{index:02d}" for index in range(1, 5)],
    )

    assert rows[0]["stage_id"] == "ch001_b0"
    assert rows[4]["stage_id"] == "ch001_checkpoint"
    assert rows[5]["stage_id"] == "ch002_b0_prior"
    assert rows[-1]["stage_id"] == "ch004_checkpoint"
    assert len(rows) == 5 + 3 * 11
    assert len({row["stage_id"] for row in rows}) == len(rows)


def test_pipeline_profile_is_sealed_without_renaming_internal_stages(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run"
    document_path = _write_document(tmp_path, 2)
    initialize_chapter_cycle_run_v1(
        run_root=run_root,
        document_path=document_path,
        profile_path=CONFIG_ROOT / "literary_chapter_cycle_profile_v2.json",
        frozen_db_path=_write_frozen_db(tmp_path),
        ordered_chapter_ids=["fixture_ch01", "fixture_ch02"],
        stop_after_chapter_count=2,
        pipeline_profile_path=PIPELINE_PROFILE,
    )

    plan = load_chapter_cycle_plan_v1(run_root)
    assert plan["pipeline_profile_id"] == "literary_pipeline_console_ready_v1"
    assert plan["public_stage_aliases"]["b0"] == "b1"
    assert plan["public_stage_aliases"]["b0_prior"] == "b1"
    assert plan["future_b2_enabled"] is False
    assert plan["stage_plan"][0]["stage_name"] == "b0"


@pytest.mark.parametrize(
    "chapter_ids",
    [
        ["fixture_ch01", "fixture_ch03"],
        ["fixture_ch01", "fixture_ch01"],
        ["fixture_ch02", "foreign_chapter"],
    ],
)
def test_noncontiguous_repeated_or_foreign_plan_is_rejected(
    chapter_ids: list[str],
) -> None:
    with pytest.raises(ChapterCycleOrchestratorError):
        build_dynamic_stage_plan_v1(
            document=_document(4),
            ordered_chapter_ids=chapter_ids,
        )


def test_stop_after_chapter_two_writes_resumable_checkpoint(tmp_path: Path) -> None:
    run_root = _initialize(tmp_path, stop_after=2)
    executor = SyntheticExecutor()

    state = run_chapter_cycle_until_boundary_v1(
        run_root=run_root,
        executor=executor,
    )

    assert state["status"] == "stopped"
    assert state["completed_chapter_ids"] == ["fixture_ch01", "fixture_ch02"]
    assert state["current_stage"] == "ch003_b0_prior"
    pointer = json.loads(
        (run_root / "chapter_checkpoint.json").read_text(encoding="utf-8")
    )
    assert pointer["completed_chapter_id"] == "fixture_ch02"
    assert pointer["completed_chapter_count"] == 2


def test_resume_starts_chapter_three_without_repeating_successful_calls(
    tmp_path: Path,
) -> None:
    run_root = _initialize(tmp_path, stop_after=2)
    executor = SyntheticExecutor()
    first = run_chapter_cycle_until_boundary_v1(
        run_root=run_root,
        executor=executor,
    )
    assert first["status"] == "stopped"
    before = executor.invocations.copy()

    resumed = resume_chapter_cycle_run_v1(
        run_root=run_root,
        stop_after_chapter_count=4,
    )
    assert resumed["current_stage"] == "ch003_b0_prior"
    final = run_chapter_cycle_until_boundary_v1(
        run_root=run_root,
        executor=executor,
    )

    assert final["status"] == "complete"
    assert final["completed_chapter_ids"] == [
        "fixture_ch01",
        "fixture_ch02",
        "fixture_ch03",
        "fixture_ch04",
    ]
    for stage_id, count in before.items():
        assert executor.invocations[stage_id] == count


def test_crash_after_stage_artifact_does_not_repeat_model_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_root = _initialize(tmp_path, chapter_count=1, stop_after=1)
    executor = SyntheticExecutor()
    original = orchestrator._complete_stage_unlocked
    crashed = False

    def crash_once(**kwargs: Any) -> dict[str, Any]:
        nonlocal crashed
        if not crashed:
            crashed = True
            raise RuntimeError("synthetic crash after stage artifact")
        return original(**kwargs)

    monkeypatch.setattr(orchestrator, "_complete_stage_unlocked", crash_once)
    with pytest.raises(RuntimeError, match="synthetic crash"):
        advance_chapter_cycle_stage_v1(run_root=run_root, executor=executor)
    assert executor.invocations["ch001_b0"] == 1
    assert (run_root / "stages" / "ch001_b0" / "stage_result.json").is_file()

    state = advance_chapter_cycle_stage_v1(run_root=run_root, executor=executor)

    assert state["current_stage"] == "ch001_local_auditor"
    assert executor.invocations["ch001_b0"] == 1


def test_failed_chapter_three_stage_resumes_at_exact_stage(tmp_path: Path) -> None:
    run_root = _initialize(tmp_path, stop_after=4)
    executor = SyntheticExecutor(fail_once_stage="ch003_b0_prior")

    paused = run_chapter_cycle_until_boundary_v1(
        run_root=run_root,
        executor=executor,
    )

    assert paused["status"] == "paused"
    assert paused["current_stage"] == "ch003_b0_prior"
    assert paused["completed_chapter_ids"] == ["fixture_ch01", "fixture_ch02"]
    assert paused["halt_failure_class"] == "transport"
    before = executor.invocations.copy()

    resume_chapter_cycle_run_v1(
        run_root=run_root,
        stop_after_chapter_count=4,
    )
    final = run_chapter_cycle_until_boundary_v1(
        run_root=run_root,
        executor=executor,
    )

    assert final["status"] == "complete"
    assert executor.invocations["ch003_b0_prior"] == 2
    for stage_id, count in before.items():
        if stage_id != "ch003_b0_prior":
            assert executor.invocations[stage_id] == count


def test_tampered_chapter_indexed_receipt_is_fatal(tmp_path: Path) -> None:
    run_root = _initialize(tmp_path, chapter_count=1, stop_after=1)
    run_chapter_cycle_until_boundary_v1(
        run_root=run_root,
        executor=SyntheticExecutor(),
    )
    receipt_path = run_root / "receipts" / "ch001_b0.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["chapter_id"] = "tampered_chapter"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(ChapterCycleIntegrityError):
        load_chapter_cycle_state_v1(run_root)


def test_call_cap_is_checked_before_transport(tmp_path: Path) -> None:
    profile_path = _write_profile(
        tmp_path,
        max_api_calls_per_chapter=1,
        max_api_calls_per_run=4,
    )
    run_root = _initialize(
        tmp_path,
        chapter_count=1,
        stop_after=1,
        profile_path=profile_path,
    )
    executor = SyntheticExecutor()

    state = run_chapter_cycle_until_boundary_v1(
        run_root=run_root,
        executor=executor,
    )

    assert state["status"] == "paused"
    assert state["current_stage"] == "ch001_local_auditor"
    assert state["halt_failure_class"] == "api_call_cap"
    assert state["run_api_call_count"] == 1
    assert executor.transports == ["ch001_b0"]


def test_semantic_pending_advances_through_checkpoint(tmp_path: Path) -> None:
    run_root = _initialize(tmp_path, chapter_count=1, stop_after=1)
    executor = SyntheticExecutor(pending_stages={"ch001_semantic_leads"})

    state = run_chapter_cycle_until_boundary_v1(
        run_root=run_root,
        executor=executor,
    )

    assert state["status"] == "complete"
    assert state["semantic_pending_count"] == 1
    semantic_receipt = next(
        row for row in state["stage_receipts"] if row["stage_id"] == "ch001_semantic_leads"
    )
    assert semantic_receipt["status"] == "semantic_pending"
    assert semantic_receipt["production_publish_performed"] is False


def test_integrity_failure_pauses_without_moving_chapter_checkpoint(
    tmp_path: Path,
) -> None:
    run_root = _initialize(tmp_path, chapter_count=2, stop_after=1)
    executor = SyntheticExecutor()
    stopped = run_chapter_cycle_until_boundary_v1(
        run_root=run_root,
        executor=executor,
    )
    assert stopped["status"] == "stopped"
    pointer_before = (run_root / "chapter_checkpoint.json").read_bytes()
    resume_chapter_cycle_run_v1(
        run_root=run_root,
        stop_after_chapter_count=2,
    )

    failing = SyntheticExecutor(integrity_stage="ch002_b0_prior")
    paused = run_chapter_cycle_until_boundary_v1(
        run_root=run_root,
        executor=failing,
    )

    assert paused["status"] == "paused"
    assert paused["halt_failure_class"] == "integrity_or_lineage"
    assert paused["current_stage"] == "ch002_b0_prior"
    assert (run_root / "chapter_checkpoint.json").read_bytes() == pointer_before
    assert (run_root / "integrity_pause.json").is_file()
    assert failing.invocations["ch002_b0_prior"] == 1


def test_missing_boundary_pointer_is_repaired_from_immutable_state(
    tmp_path: Path,
) -> None:
    run_root = _initialize(tmp_path, chapter_count=1, stop_after=1)
    state = run_chapter_cycle_until_boundary_v1(
        run_root=run_root,
        executor=SyntheticExecutor(),
    )
    pointer_path = run_root / "chapter_checkpoint.json"
    pointer_path.unlink()

    recovered = load_chapter_cycle_state_v1(run_root)

    assert recovered["state_hash"] == state["state_hash"]
    assert pointer_path.is_file()
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    assert pointer["completed_chapter_id"] == "fixture_ch01"


def test_production_writer_spy_is_never_called(tmp_path: Path) -> None:
    run_root = _initialize(tmp_path, chapter_count=1, stop_after=1)
    calls = 0

    def forbidden_writer(*_args: Any, **_kwargs: Any) -> None:
        nonlocal calls
        calls += 1

    state = run_chapter_cycle_until_boundary_v1(
        run_root=run_root,
        executor=SyntheticExecutor(),
        production_writer=forbidden_writer,
    )

    assert state["status"] == "complete"
    assert calls == 0
    plan = load_chapter_cycle_plan_v1(run_root)
    assert plan["production_publish_enabled"] is False
    assert state["production_publish_performed"] is False
