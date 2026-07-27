from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.literary.chapter_cycle_live_executor_v1 import (
    ChapterCycleLiveExecutorV1,
)
from pipeline.literary.chapter_cycle_orchestrator_v1 import (
    ChapterCycleStage,
    initialize_chapter_cycle_run_v1,
    load_chapter_cycle_plan_v1,
    load_chapter_cycle_state_v1,
)
from pipeline.literary.literary_pipeline_profile_v1 import (
    load_literary_pipeline_profile,
)
from pipeline.scripts.run_literary_chapter_cycle_v1 import main


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
CONFIG_ROOT = RUNTIME_ROOT / "pipeline" / "configs"
PIPELINE_PROFILE = CONFIG_ROOT / "literary_pipeline_profile_v2.json"


def _write_document(tmp_path: Path, chapter_count: int = 4) -> Path:
    payload = {
        "document_id": "book-neutral-runner-fixture",
        "chapters": [
            {
                "chapter_id": f"fixture_ch{index:02d}",
                "blocks": [
                    {
                        "block_id": f"fixture_ch{index:02d}_b001",
                        "order_index": 1,
                        "block_type": "paragraph",
                        "clean_text": f"A named visitor enters chapter {index}.",
                    }
                ],
            }
            for index in range(1, chapter_count + 1)
        ],
    }
    path = tmp_path / "document.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _write_frozen(tmp_path: Path) -> Path:
    path = tmp_path / "frozen.sqlite3"
    path.write_bytes(b"book-neutral-offline-frozen-fixture")
    return path


def _fixture_for_plan(plan: dict[str, object]) -> dict[str, object]:
    rows: dict[str, object] = {}
    for raw in plan["stage_plan"]:  # type: ignore[index]
        stage = dict(raw)
        requires_api = bool(stage["requires_api"])
        stage_name = str(stage["stage_name"])
        called = requires_api and stage_name not in {
            "stable_claim_components",
            "identity_components",
        }
        rows[str(stage["stage_id"])] = {
            "status": "accepted",
            "payload": {
                "public_stage_name": (
                    "b1" if stage_name in {"b0", "b0_prior"} else stage_name
                ),
                "implementation_stage_name": stage_name,
                "production_publish_performed": False,
            },
            "call_disposition": (
                "called" if called else "not_required" if requires_api else "code_only"
            ),
            "logical_call_ids": ["primary"] if called else [],
        }
    return rows


def test_plan_exposes_public_b1_and_no_secret_paths(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    document = _write_document(tmp_path, 2)

    assert (
        main(
            [
                "plan",
                "--document",
                str(document),
                "--pipeline-profile",
                str(PIPELINE_PROFILE),
                "--all-chapters",
            ]
        )
        == 0
    )
    rendered = capsys.readouterr().out
    payload = json.loads(rendered)
    assert payload["stage_plan"][0]["public_stage_name"] == "b1"
    assert payload["stage_plan"][0]["implementation_stage_name"] == "b0"
    assert payload["public_stage_contract"]["b2"]["enabled"] is False
    assert payload["structured_output"]["policy_id"] == (
        "literary_structured_output_recommended_v1"
    )
    assert "OPENAI-KEY" not in rendered
    assert "CKEY.txt" not in rendered
    assert "sk-" not in rendered.casefold()


def test_runner_rejects_non_prefix_chapter_selection(tmp_path: Path) -> None:
    document = _write_document(tmp_path, 3)

    with pytest.raises(SystemExit, match="must start at the first"):
        main(
            [
                "plan",
                "--document",
                str(document),
                "--chapter-id",
                "fixture_ch02",
            ]
        )


def test_four_chapter_offline_cycle_runs_through_one_facade(
    tmp_path: Path,
) -> None:
    document = _write_document(tmp_path, 4)
    frozen = _write_frozen(tmp_path)
    run_root = tmp_path / "run"
    fixture_path = tmp_path / "fixture.json"

    assert (
        main(
            [
                "init",
                "--run-root",
                str(run_root),
                "--document",
                str(document),
                "--pipeline-profile",
                str(PIPELINE_PROFILE),
                "--frozen-db",
                str(frozen),
                "--all-chapters",
                "--stop-after-chapter-count",
                "4",
            ]
        )
        == 0
    )
    plan = load_chapter_cycle_plan_v1(run_root)
    assert plan["structured_output_policy_id"] == (
        "literary_structured_output_recommended_v1"
    )
    fixture_path.write_text(
        json.dumps(_fixture_for_plan(plan)), encoding="utf-8"
    )

    assert (
        main(
            [
                "run-offline-fixture",
                "--run-root",
                str(run_root),
                "--fixture",
                str(fixture_path),
            ]
        )
        == 0
    )
    state = load_chapter_cycle_state_v1(run_root)
    assert state["status"] == "complete"
    assert state["completed_chapter_ids"] == [
        "fixture_ch01",
        "fixture_ch02",
        "fixture_ch03",
        "fixture_ch04",
    ]
    assert state["production_publish_performed"] is False
    summary = json.loads((run_root / "run_summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "complete"
    assert summary["b2"] == {"enabled": False, "ready": False}
    assert summary["production_publish_performed"] is False


def test_component_reconcile_reads_component_stage_directory(
    tmp_path: Path,
) -> None:
    document = _write_document(tmp_path, 2)
    frozen = _write_frozen(tmp_path)
    run_root = tmp_path / "run"
    profile = load_literary_pipeline_profile(PIPELINE_PROFILE)
    initialize_chapter_cycle_run_v1(
        run_root=run_root,
        document_path=document,
        profile_path=profile.chapter_cycle_profile_path,
        frozen_db_path=frozen,
        ordered_chapter_ids=["fixture_ch01", "fixture_ch02"],
        stop_after_chapter_count=2,
        pipeline_profile_path=PIPELINE_PROFILE,
    )
    executor = ChapterCycleLiveExecutorV1(
        run_root=run_root,
        plan=load_chapter_cycle_plan_v1(run_root),
        credential_root=None,
    )
    stage = ChapterCycleStage(
        stage_id="ch002_stable_claim_reconcile",
        chapter_id="fixture_ch02",
        chapter_ordinal=2,
        stage_name="stable_claim_reconcile",
        stage_role="code",
        requires_api=False,
        is_chapter_checkpoint=False,
        stage_descriptor_hash="0" * 64,
    )
    paths = executor._paths(stage)

    assert paths["claim_component_live"].as_posix().endswith(
        "stages/ch002_stable_claim_components/live"
    )
    assert paths["identity_component_live"].as_posix().endswith(
        "stages/ch002_identity_components/live"
    )
