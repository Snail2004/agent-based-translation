from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

# Import thesis_runs the same way app/backend/tests does (sys.path + `services.`),
# NOT as `app.backend.services...`: that registers the namespace package `app`
# in sys.modules and breaks test_api_smoke's `from app import create_app`.
BACKEND_ROOT = Path(__file__).resolve().parents[2] / "app" / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.thesis_runs import RunRegistry, build_argv, cancel_run  # noqa: E402
from pipeline.scripts import run_one_button  # noqa: E402

# Point at the COMMITTED golden bundle, not the gitignored data/jobs run dir:
# these preflight fixtures must survive a clean checkout / CI.
GOLDEN_ONE_BUTTON_RUN = (
    Path(__file__).resolve().parents[2]
    / "app"
    / "prototype"
    / "fixtures"
    / "one_button_preface_golden"
)


def test_read_last_json_object_prefers_cost_object(tmp_path: Path) -> None:
    log = tmp_path / "estimate.log"
    log.write_text(
        '[OneButton estimate] argv=["python", {"nested": true}]\n'
        '{\n'
        '  "calls": 50,\n'
        '  "estimated_cost_usd_cap": 0.64972925,\n'
        '  "pricing": {"input": 0.25, "output": 2.0}\n'
        '}\n',
        encoding="utf-8",
    )

    payload = run_one_button._read_last_json_object(log)

    assert payload["calls"] == 50
    assert payload["estimated_cost_usd_cap"] == 0.64972925


def test_pause_requested_from_file_or_manifest(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    manifest = run_dir / "manifest.json"
    manifest.write_text(json.dumps({"pause_requested": False}), encoding="utf-8")
    assert run_one_button._pause_requested(manifest, run_dir) is False

    (run_dir / "PAUSE").write_text("", encoding="utf-8")
    assert run_one_button._pause_requested(manifest, run_dir) is True

    (run_dir / "PAUSE").unlink()
    manifest.write_text(json.dumps({"pause_requested": True}), encoding="utf-8")
    assert run_one_button._pause_requested(manifest, run_dir) is True


def test_runcontrol_builds_one_button_estimate_with_event_log() -> None:
    argv = build_argv(
        script="run_one_button",
        extra_args=[
            "--job-id",
            "demo",
            "--chapters",
            "d2l_preliminaries",
            "--workdb",
            "data/jobs/demo/memory.sqlite3",
            "--budget-cap-usd",
            "1",
        ],
        allow_api=False,
        event_log="data/jobs/run_events/run_demo.jsonl",
        run_id="run_demo",
    )

    assert "--estimate-only" in argv
    assert "--event-log" in argv
    assert "data/jobs/run_events/run_demo.jsonl" in argv
    assert "--run-id" in argv
    assert "run_demo" in argv


def test_translate_preflight_cost_cap_from_tokens() -> None:
    payload = {
        "phase": "run_translate_preflight",
        "run": {"llm_config": "pipeline/configs/llm_translate.yaml"},
        "preflight": {
            "configs": {
                "S1": {
                    "prompt_tokens_total_est": 1000,
                    "upper_total_with_max_output": 5000,
                }
            },
            "upper_total_all_configs": 5000,
        },
    }

    estimate = run_one_button._extract_cost_estimate(payload)

    assert estimate["translation_prompt_tokens_est"] == 1000
    assert estimate["translation_output_tokens_cap"] == 4000
    assert estimate["estimated_cost_cap_usd"] == 0.00825


def test_estimate_report_path_prefers_run_translate_report_arg(tmp_path: Path) -> None:
    report = tmp_path / "reports" / "translate_preflight.json"
    phantom = tmp_path / "artifacts" / "translator_not_real.json"
    stage = run_one_button.StageSpec(
        name="translator",
        script="run_translate",
        argv=[sys.executable, "-m", "pipeline.scripts.run_translate", "--report", str(report)],
        artifact_path=phantom,
    )

    assert run_one_button._estimate_report_path(stage, stage.argv) == report.resolve()


def test_estimate_report_path_uses_cascade_prefix_convention(tmp_path: Path) -> None:
    out_dir = tmp_path / "artifacts" / "cascade"
    stage = run_one_button.StageSpec(
        name="cascade",
        script="run_experiment_cascade",
        argv=[
            sys.executable,
            "-m",
            "pipeline.scripts.run_experiment_cascade",
            "--out-dir",
            str(out_dir),
            "--artifact-prefix",
            "d2l_preface_cascade",
        ],
        artifact_path=out_dir / "d2l_preface_cascade_summary.json",
    )

    assert (
        run_one_button._estimate_report_path(stage, stage.argv)
        == (out_dir / "d2l_preface_cascade_preflight.json").resolve()
    )


def test_golden_translate_preflight_cost_cap_from_real_report() -> None:
    payload = json.loads(
        (GOLDEN_ONE_BUTTON_RUN / "reports" / "translate_preflight.json").read_text(encoding="utf-8")
    )

    estimate = run_one_button._extract_cost_estimate(payload)

    assert 0.055 <= estimate["estimated_cost_cap_usd"] <= 0.065
    assert estimate["translation_prompt_tokens_est"] > 0
    assert estimate["translation_output_tokens_cap"] > 0


def test_golden_cascade_preflight_cost_cap_from_real_report() -> None:
    payload = json.loads(
        (
            GOLDEN_ONE_BUTTON_RUN
            / "reports"
            / "cascade_preflight.json"
        ).read_text(encoding="utf-8")
    )

    estimate = run_one_button._extract_cost_estimate(payload)

    assert 0.06 <= estimate["estimated_cost_cap_usd"] <= 0.07
    assert estimate["cascade_calls"] > 0
    assert estimate["cascade_prompt_tokens_estimate"] > 0
    assert estimate["cascade_output_tokens_cap"] > 0


def test_resume_skips_done_stage_without_reemitting_attempt1_events(tmp_path: Path, monkeypatch) -> None:
    suffix = uuid.uuid4().hex[:8]
    job_id = f"resume_demo_{suffix}"
    run_id = f"run_resume_{suffix}"
    workdb = tmp_path / "work.sqlite3"
    db = tmp_path / "frozen.sqlite3"
    db.write_bytes(b"not a real sqlite db; test bypasses hash guard")
    child_code = (
        "import argparse,json,pathlib,sys\n"
        "p=argparse.ArgumentParser();"
        "p.add_argument('--artifact');p.add_argument('--event-log');"
        "p.add_argument('--run-id');p.add_argument('--attempt-id');p.add_argument('--fail', action='store_true');"
        "a=p.parse_args()\n"
        "pathlib.Path(a.event_log).parent.mkdir(parents=True, exist_ok=True)\n"
        "pathlib.Path(a.event_log).write_text(json.dumps({'v':1,'seq':1,'event_id':a.run_id+':'+a.attempt_id+':child','stage':'fake','script':'fake','agent':'Fake','event':'child_event','payload':{'artifact':a.artifact,'attempt':a.attempt_id}})+'\\n', encoding='utf-8')\n"
        "if a.fail: sys.exit(5)\n"
        "pathlib.Path(a.artifact).parent.mkdir(parents=True, exist_ok=True); pathlib.Path(a.artifact).write_text('ok '+a.attempt_id, encoding='utf-8')\n"
    )

    def fake_stage(name: str, run_dir: Path, attempt_id: int, *, fail: bool = False) -> run_one_button.StageSpec:
        artifact = run_dir / "artifacts" / f"{name}.txt"
        event_log = run_dir / "stage_events" / f"{name}.a{attempt_id}.jsonl"
        argv = [
            sys.executable,
            "-c",
            child_code,
            "--artifact",
            str(artifact),
            "--event-log",
            str(event_log),
            "--run-id",
            run_id,
            "--attempt-id",
            str(attempt_id),
        ]
        if fail:
            argv.append("--fail")
        return run_one_button.StageSpec(
            name=name,
            script="fake",
            argv=argv,
            artifact_path=artifact,
            stage_event_log_path=event_log,
            stdout_log_path=run_dir / "stdout" / f"{name}.log",
        )

    def fake_plan(*, run_dir: Path, attempt_id: int, **_kwargs):
        return [
            fake_stage("stage1", run_dir, attempt_id),
            fake_stage("stage2", run_dir, attempt_id, fail=attempt_id == 1),
        ]

    def fake_preflight(_args, run_dir: Path, _run_id: str, attempt_id: int):
        return run_one_button.StageSpec(
            name="preflight_check",
            script="fake_preflight",
            argv=[sys.executable, "-c", "pass"],
            stdout_log_path=run_dir / "stdout" / "preflight_check.log",
            never_skip=True,
        )

    monkeypatch.setattr(run_one_button, "_assert_frozen_db_baseline", lambda _path: None)
    monkeypatch.setattr(run_one_button, "_build_stage_plan", fake_plan)
    monkeypatch.setattr(run_one_button, "_preflight_stage", fake_preflight)
    monkeypatch.setattr(run_one_button, "_pid_alive", lambda _pid: False)
    # Keep run_dir + merged event log inside tmp_path: without this the test
    # litters real data/jobs/ with a new uuid-suffixed dir on every suite run.
    monkeypatch.setattr(run_one_button, "TOOL_ROOT", tmp_path)

    monkeypatch.setattr(run_one_button.os, "getpid", lambda: 111)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_one_button",
            "--job-id",
            job_id,
            "--chapters",
            "demo_chapter",
            "--workdb",
            str(workdb),
            "--db",
            str(db),
            "--budget-cap-usd",
            "99",
            "--run-id",
            run_id,
        ],
    )
    try:
        run_one_button.main()
    except SystemExit:
        pass
    run_dir = run_one_button.TOOL_ROOT / "data" / "jobs" / job_id / "one_button" / run_id
    manifest_path = run_dir / "manifest.json"
    merged_log = run_one_button.TOOL_ROOT / "data" / "jobs" / "run_events" / f"{run_id}.jsonl"
    manifest1 = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest1["status"] == "failed"
    events1 = [json.loads(line) for line in merged_log.read_text(encoding="utf-8").splitlines()]
    assert any(row["event"] == "run_failed" for row in events1)

    monkeypatch.setattr(run_one_button.os, "getpid", lambda: 222)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_one_button",
            "--job-id",
            job_id,
            "--chapters",
            "demo_chapter",
            "--workdb",
            str(workdb),
            "--db",
            str(db),
            "--budget-cap-usd",
            "99",
            "--resume",
            run_id,
        ],
    )
    assert run_one_button.main() == 0
    manifest2 = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest2["status"] == "done"
    assert manifest2["attempt"] == 2
    assert manifest2["owner_pid"] == 222
    stage1 = next(row for row in manifest2["stages"] if row["name"] == "stage1")
    stage2 = next(row for row in manifest2["stages"] if row["name"] == "stage2")
    assert stage1["status"] == "done"
    assert stage1["skipped"] is True
    assert stage2["status"] == "done"
    events2 = [json.loads(line) for line in merged_log.read_text(encoding="utf-8").splitlines()]
    stage1_child_events = [
        row for row in events2
        if row["event"] == "child_event" and row.get("payload", {}).get("artifact", "").endswith("stage1.txt")
    ]
    assert len(stage1_child_events) == 1


def test_cancel_run_taskkills_registered_pid(tmp_path: Path, monkeypatch) -> None:
    calls: list[list[str]] = []
    registry = RunRegistry(runs_root=tmp_path)
    entry = registry.create_run(script="snapshot_runs", argv=[sys.executable, "-c", "pass"])
    registry.update_run(entry["run_id"], status="running", pid=12345)
    monkeypatch.setattr(run_one_button.os, "name", "nt")
    monkeypatch.setattr(
        "services.thesis_runs.subprocess.run",
        lambda argv, **_kwargs: calls.append([str(item) for item in argv]),
    )

    updated = cancel_run(registry, entry["run_id"])

    assert updated["status"] == "cancelled"
    assert calls == [["taskkill", "/T", "/F", "/PID", "12345"]]
