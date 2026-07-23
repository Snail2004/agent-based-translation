"""Tests for APP-C01 RunControl.

These tests stay 0-API.  They do launch one real frozen pipeline script
(`snapshot_runs`) to prove module invocation and cwd are wired correctly.
"""
from __future__ import annotations

import importlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
TOOL_ROOT = BACKEND_ROOT.parents[1]
EVALUATION_FIXTURE_ROOT = (
    TOOL_ROOT / "pipeline" / "tests" / "fixtures" / "evaluation_v1"
)
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _make_doc_db(tmp_path: Path) -> Path:
    from pipeline.ingest.document_loader import load_document
    from pipeline.memory.store_init import migrate_db

    doc = {
        "doc_id": "ti",
        "metadata": {"source_language": "en", "target_language": "vi"},
        "chapters": [
            {
                "chapter_id": "ti_ch02",
                "blocks": [
                    {
                        "block_id": "ch02_b001",
                        "order_index": 0,
                        "block_type": "paragraph",
                        "clean_text": "Hello, Jim.",
                        "source_text": "Hello, Jim.",
                        "annotations": {},
                    },
                    {
                        "block_id": "ch02_b002",
                        "order_index": 1,
                        "block_type": "paragraph",
                        "clean_text": "Good day, captain.",
                        "source_text": "Good day, captain.",
                        "annotations": {},
                    },
                ],
            }
        ],
    }
    doc_path = tmp_path / "document.json"
    db_path = tmp_path / "memory.sqlite3"
    _write_json(doc_path, doc)
    load_document(db_path, doc_path)
    conn = migrate_db(db_path)
    conn.close()
    return db_path


def _wait_for_status(registry, run_id: str, done_statuses=("done", "failed", "error")):
    for _ in range(80):
        time.sleep(0.1)
        entry = registry.get_run(run_id)
        if entry and entry["status"] in done_statuses:
            return entry
    return registry.get_run(run_id)


def _reset_app_modules() -> None:
    for name in list(sys.modules):
        if (
            name == "app"
            or name == "config"
            or name == "routes"
            or name.startswith("routes.")
            or name.startswith("services.thesis_")
            or name == "services.workflow_replay"
        ):
            sys.modules.pop(name, None)


def _create_resumable_one_button_run(
    tmp_path: Path,
    registry,
    *,
    run_id: str,
    pid: int | None = None,
    job_id: str = "jobA",
    resumed_from: str | None = None,
):
    run_dir = tmp_path / job_id / "one_button" / run_id
    run_dir.mkdir(parents=True)
    manifest_path = run_dir / "manifest.json"
    manifest_path.write_text(json.dumps({"attempt": 1, "status": "paused"}), encoding="utf-8")
    event_dir = tmp_path / "run_events"
    event_dir.mkdir(exist_ok=True)
    event_log = event_dir / f"{run_id}.jsonl"
    entry = registry.create_run(
        script="run_one_button",
        argv=[
            sys.executable,
            "-m",
            "pipeline.scripts.run_one_button",
            "--job-id",
            job_id,
            "--chapters",
            "d2l_mlp",
            "--workdb",
            str(run_dir / "workdb.sqlite3"),
            "--budget-cap-usd",
            "1.0",
            "--event-log",
            str(event_log),
            "--run-id",
            run_id,
            "--estimate-only",
        ],
        run_id=run_id,
        job_id=job_id,
        event_log_path=str(event_log),
        run_dir=str(run_dir),
        manifest_path=str(manifest_path),
        resumed_from=resumed_from,
    )
    registry.update_run(run_id, status="paused", exit_code=0, pid=pid)
    return entry, run_dir


def _full_run_report(
    *,
    fixture_name: str = "full_run_one_arm.json",
    project_id: str = "jobA",
    logical_run_id: str = "run_report",
    attempt_run_ids: list[str] | None = None,
) -> dict:
    from pipeline.eval.full_run_report_v1 import seal_full_run_report

    attempt_run_ids = attempt_run_ids or [logical_run_id]
    report = json.loads(
        (EVALUATION_FIXTURE_ROOT / fixture_name).read_text(encoding="utf-8")
    )
    report["identity"].update(
        {
            "project_id": project_id,
            "logical_run_id": logical_run_id,
            "attempt_run_ids": attempt_run_ids,
        }
    )
    for stage in report["stages"]:
        if stage["attempt_run_id"] is not None:
            stage["attempt_run_id"] = attempt_run_ids[-1]
    return seal_full_run_report(report)


def _reseal_full_run_report(report: dict) -> dict:
    from pipeline.eval.contracts_v1 import canonical_sha256
    from pipeline.eval.full_run_report_v1 import (
        FULL_RUN_CANONICAL_POLICY,
        seal_full_run_report,
    )

    report["integrity"]["artifact_set_sha256"] = canonical_sha256(
        {"artifacts": report["artifacts"]},
        policy=FULL_RUN_CANONICAL_POLICY,
    )
    return seal_full_run_report(report)


def _prepare_full_report_route(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("THESIS_JOBS_ROOT", str(tmp_path))
    monkeypatch.setenv("THESIS_TOOL_ROOT", str(TOOL_ROOT))
    monkeypatch.setenv("THESIS_TOOL_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("THESIS_APP_MODE", "cockpit")
    _reset_app_modules()
    app_module = importlib.import_module("app")
    routes = importlib.import_module("routes.thesis_runs")
    service = importlib.import_module("services.thesis_runs")
    registry = service.RunRegistry(runs_root=tmp_path)
    routes.set_registry(registry)
    return app_module.create_app().test_client(), routes, registry


def _register_full_report_run(
    registry,
    tmp_path: Path,
    *,
    run_id: str = "run_report",
    job_id: str = "jobA",
    run_dir: Path | None = None,
) -> Path:
    run_dir = run_dir or (tmp_path / job_id / "one_button" / "run_report")
    run_dir.mkdir(parents=True, exist_ok=True)
    registry.create_run(
        script="run_one_button",
        argv=[sys.executable, "-c", "pass"],
        run_id=run_id,
        job_id=job_id,
        run_dir=str(run_dir),
        manifest_path=str(run_dir / "manifest.json"),
    )
    return run_dir


def _write_full_run_report(run_dir: Path, report: dict) -> Path:
    report_path = run_dir / "reports" / "full_run_report_v1.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(report_path, report)
    return report_path


def _fake_d2l_preview(chapters: list[str], *, token_cap: int = 6_000_000) -> dict:
    return {
        "project_id": "d2l_run-5-chapter",
        "source_binding": {"schema": "canonical_source_binding_v1"},
        "source_binding_sha256": "A" * 64,
        "source_db_sha256": "1" * 64,
        "selected_chapter_ids": list(chapters),
        "selected_block_count": 2355,
        "selected_universe_sha256": "2" * 64,
        "chapter_counts": [
            {"chapter_id": chapter_id, "block_count": 1, "channel_counts": {"semantic_text": 1}}
            for chapter_id in chapters
        ],
        "channel_counts": {"semantic_text": len(chapters)},
        "window_counts": {"b1": 2, "translator_per_arm": 2},
        "forecast_total_tokens": 120_000,
        "forecast_token_range": {"low": 90_000, "high": 180_000},
        "forecast_status": "empirical_range",
        "hard_total_token_cap": token_cap,
        "theoretical_role_reserve_tokens": 500_000,
        "hard_physical_attempt_cap": 100,
        "campaign_config_sha256": "B" * 64,
        "reserved_cost_cap_usd": None,
        "semantic_roles": [],
        "transport_sources": [],
        "cost_usd": None,
        "cost_basis": {"status": "unknown"},
        "profile_id": "technical_d2l_v1",
        "pipeline_version": "d2l_project_campaign_v2",
        "code_revision": "c" * 40,
    }


def test_build_argv_uses_real_module_invocation_and_no_job_arg(tmp_path):
    from services.thesis_runs import build_argv

    db = tmp_path / "memory.sqlite3"
    argv = build_argv(
        script="run_translate",
        python_exe=sys.executable,
        db=str(db),
        chapters=["ch02"],
        configs=["S0", "S1"],
        profile="literary_v1",
        experiment="ti_test",
        cache=str(tmp_path / "cache.sqlite3"),
        allow_api=True,
    )

    assert argv[:3] == [sys.executable, "-m", "pipeline.scripts.run_translate"]
    assert "--job" not in argv
    assert "--db" in argv
    assert "--preflight-only" not in argv


def test_build_argv_d2l_campaign_uses_server_owned_app_run_boundary(tmp_path):
    from services.thesis_runs import build_argv

    argv = build_argv(
        script="run_d2l_project_campaign",
        python_exe=sys.executable,
        job_id="jobA",
        job_root=str(tmp_path / "jobA"),
        campaign_root=str(tmp_path / "_work" / "d2l_campaign" / "jobA" / "run_d2l"),
        workflow_run_id="wf_run_d2l",
        component_run_id="tr_run_d2l",
        chapters=["d2l_preliminaries", "d2l_linear_networks"],
        hard_total_token_cap=6_000_000,
        allow_api=False,
        runtime_root=str(tmp_path / "_runtime" / "d2l" / "jobA" / "run_d2l"),
    )

    assert argv[:4] == [
        sys.executable,
        "-m",
        "pipeline.scripts.run_d2l_project_campaign",
        "app-run",
    ]
    assert argv.count("--chapter-id") == 2
    assert "--dry-run" in argv
    assert "--live" not in argv
    assert "--db" not in argv


def test_d2l_semantic_role_preview_reads_nested_campaign_contract():
    from services.thesis_runs import _d2l_semantic_role_preview

    role = {
        "role_id": "d2l.candidate_discovery",
        "stage_id": "b1_candidate_discovery",
        "model_id": "gemini-3.5-flash",
        "source_id": "shopaikey_gemini_proxy_v2",
        "prompt": {"id": "prompt_v2", "sha256": "A" * 64},
        "response_schema_sha256": "B" * 64,
        "validator_id": "validator_v2",
        "validator_sha256": "C" * 64,
        "generation": {
            "max_input_tokens": 6000,
            "max_output_tokens": 6144,
            "temperature": 1.0,
            "reasoning_effort": "none",
            "verbosity": "low",
        },
        "output_contract": {
            "structured_output_mode": "disabled",
            "envelope": "prompt_generated_json",
        },
        "semantic_retry_cap": 1,
        "semantic_role_sha256": "D" * 64,
    }

    preview = _d2l_semantic_role_preview(role)

    assert preview["prompt_id"] == "prompt_v2"
    assert preview["max_input_tokens"] == 6000
    assert preview["max_output_tokens"] == 6144
    assert preview["structured_output_mode"] == "disabled"
    assert preview["output_envelope"] == "prompt_generated_json"
    assert preview["semantic_role_sha256"] == "D" * 64


def test_build_resume_argv_d2l_removes_complete_two_value_chapter_range(tmp_path):
    from services.thesis_runs import build_resume_argv_from_entry

    argv = [
        sys.executable,
        "-m",
        "pipeline.scripts.run_d2l_project_campaign",
        "app-run",
        "--job-root",
        str(tmp_path / "jobA"),
        "--campaign-root",
        str(tmp_path / "campaign"),
        "--workflow-run-id",
        "wf_run_range",
        "--component-run-id",
        "tr_run_range",
        "--chapter-range",
        "d2l_preliminaries",
        "d2l_linear_networks",
        "--dry-run",
    ]

    resumed = build_resume_argv_from_entry(
        {
            "script": "run_d2l_project_campaign",
            "run_id": "run_range",
            "argv": argv,
        }
    )

    assert "--chapter-range" not in resumed
    assert "d2l_preliminaries" not in resumed
    assert "d2l_linear_networks" not in resumed
    assert "--workflow-run-id" not in resumed
    assert "--component-run-id" not in resumed
    assert resumed[-1] == "--resume"


def test_d2l_confirmation_token_binds_source_and_config_identity():
    from services.thesis_runs import (
        RunControlError,
        issue_estimate_token_for_argv,
        validate_api_gate,
    )

    argv = [
        sys.executable,
        "-m",
        "pipeline.scripts.run_d2l_project_campaign",
        "app-run",
    ]
    token = issue_estimate_token_for_argv(
        job_id="jobA",
        script="run_d2l_project_campaign",
        argv=argv,
        run_identity_digest="A" * 64,
    )
    try:
        validate_api_gate(
            allow_api=True,
            script="run_d2l_project_campaign",
            confirm_token=token,
            job_id="jobA",
            argv=argv,
            run_identity_digest="B" * 64,
        )
    except RunControlError as exc:
        assert exc.code == "confirm_token_identity_mismatch"
    else:
        raise AssertionError("D2L confirmation token accepted a different source/config identity")


def test_run_translate_event_flags_are_part_of_argv(tmp_path):
    from services.thesis_runs import build_argv

    event_log = tmp_path / "run_events" / "run_abc.jsonl"
    argv = build_argv(
        script="run_translate",
        python_exe=sys.executable,
        db=str(tmp_path / "memory.sqlite3"),
        chapters=["ch02"],
        configs=["S1"],
        allow_api=True,
        event_log=str(event_log),
        run_id="run_abc",
    )

    assert "--event-log" in argv
    assert str(event_log) in argv
    assert argv[argv.index("--run-id") + 1] == "run_abc"


def test_run_translate_dry_run_forces_preflight_only(tmp_path):
    from services.thesis_runs import build_argv

    argv = build_argv(
        script="run_translate",
        python_exe=sys.executable,
        db=str(tmp_path / "memory.sqlite3"),
        chapters=["ch02"],
        configs=["S0"],
        allow_api=False,
    )
    assert argv[:3] == [sys.executable, "-m", "pipeline.scripts.run_translate"]
    assert "--preflight-only" in argv


def test_api_capable_script_without_safe_dry_run_is_rejected(tmp_path):
    from services.thesis_runs import RunControlError, build_argv

    try:
        build_argv(
            script="run_judge",
            python_exe=sys.executable,
            db=str(tmp_path / "memory.sqlite3"),
            experiment="x",
            compare="S0:S1",
            chapters=["ch02"],
            out=str(tmp_path / "judge.json"),
            allow_api=False,
        )
        assert False, "expected RunControlError"
    except RunControlError as exc:
        assert exc.code == "dry_run_not_supported"


def test_real_pipeline_module_help_smoke():
    result = subprocess.run(
        [sys.executable, "-m", "pipeline.scripts.snapshot_runs", "--help"],
        cwd=TOOL_ROOT,
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0
    assert "--db" in result.stdout
    assert "--out" in result.stdout


def test_run_lifecycle_create_running_done_log_tail(tmp_path):
    from services.thesis_runs import RunRegistry, read_log, spawn_run

    registry = RunRegistry(runs_root=tmp_path)
    entry = registry.create_run(
        script="run_translate",
        argv=[sys.executable, "-c", "print('line1'); print('line2')"],
        config="S1",
        seed=42,
        model="gpt-test",
        prompt_version="s1_v1",
        cache_path=str(tmp_path / "cache.sqlite3"),
        job_id="test_job",
        experiment="exp_test",
        allow_api=False,
    )

    spawn_run(registry, entry["run_id"])
    final = _wait_for_status(registry, entry["run_id"])
    assert final["status"] == "done"
    assert final["exit_code"] == 0
    assert final["pid"] is not None

    log_result = read_log(registry, entry["run_id"], offset=0)
    assert "line1" in log_result["log"]
    assert "line2" in log_result["log"]
    assert log_result["running"] is False
    assert log_result["exit_code"] == 0

    log_result2 = read_log(registry, entry["run_id"], offset=log_result["offset"])
    assert log_result2["log"] == ""


def test_run_nonzero_exit_code_marked_failed(tmp_path):
    from services.thesis_runs import RunRegistry, spawn_run

    registry = RunRegistry(runs_root=tmp_path)
    entry = registry.create_run(
        script="run_translate",
        argv=[sys.executable, "-c", "import sys; sys.exit(1)"],
    )
    spawn_run(registry, entry["run_id"])
    final = _wait_for_status(registry, entry["run_id"])
    assert final["status"] == "failed"
    assert final["exit_code"] == 1


def test_run_registry_persists_to_jsonl(tmp_path):
    from services.thesis_runs import RunRegistry

    registry1 = RunRegistry(runs_root=tmp_path)
    entry = registry1.create_run(script="score_run", argv=[sys.executable, "-c", "pass"])
    registry1.update_run(entry["run_id"], status="done", exit_code=0)

    registry2 = RunRegistry(runs_root=tmp_path)
    reloaded = registry2.get_run(entry["run_id"])
    assert reloaded["status"] == "done"
    assert reloaded["exit_code"] == 0


def test_script_outside_allowlist_returns_400():
    from services.thesis_runs import RunControlError, validate_script

    try:
        validate_script("rm_rf_everything")
        assert False, "Expected RunControlError"
    except RunControlError as exc:
        assert exc.status == 400
        assert exc.code == "script_not_allowed"


def test_arg_with_shell_meta_returns_400_but_windows_path_allowed():
    from services.thesis_runs import RunControlError, validate_args

    validate_args([r"C:\tmp\memory.sqlite3", "data/jobs/x.sqlite3"])
    for bad_arg in ["foo;bar", "a|b", "a&&b", "$(evil)", "a`cmd`b", "a>out"]:
        try:
            validate_args(["safe", bad_arg])
            assert False, f"Expected RunControlError for {bad_arg!r}"
        except RunControlError as exc:
            assert exc.status == 400
            assert exc.code == "invalid_arg"


def test_allow_api_true_requires_job_id_and_preview_token(tmp_path):
    from services.thesis_runs import RunControlError, build_argv, validate_api_gate

    argv = build_argv(
        script="run_translate",
        db=str(tmp_path / "memory.sqlite3"),
        chapters=["ch02"],
        configs=["S1"],
        allow_api=True,
    )
    try:
        validate_api_gate(
            allow_api=True,
            script="run_translate",
            confirm_token="anything",
            job_id=None,
            argv=argv,
        )
        assert False, "Expected job_id_required"
    except RunControlError as exc:
        assert exc.code == "job_id_required"


def test_prompt_preview_renders_real_translate_prompt_and_token_is_one_time(tmp_path):
    from services.thesis_runs import generate_prompt_preview, validate_api_gate

    db_path = _make_doc_db(tmp_path)
    cache_path = tmp_path / "translate_cache.sqlite3"
    preview = generate_prompt_preview(
        job_id="preview_job",
        script="run_translate",
        db=str(db_path),
        chapters=["ch02"],
        configs=["S1"],
        profile="literary_v1",
        cache=str(cache_path),
        tool_root=TOOL_ROOT,
        jobs_root=tmp_path,
    )

    assert preview["preview_kind"] == "real_translate_prompt"
    assert preview["confirm_token"]
    assert preview["planned_run_id"].startswith("run_")
    assert "--event-log" in preview["argv_preview"]
    assert "--run-id" in preview["argv_preview"]
    assert preview["event_log_path"].endswith(f"{preview['planned_run_id']}.jsonl")
    assert preview["representative_prompt"]["messages"]
    assert preview["representative_prompt"]["prompt_tokens_est"] > 0
    assert preview["token_estimate"]["configs"]["S1"]["windows"] >= 1
    assert "N/A" not in json.dumps(preview["token_estimate"], ensure_ascii=False)

    argv = preview["argv_preview"]
    token = preview["confirm_token"]
    validate_api_gate(
        allow_api=True,
        script="run_translate",
        confirm_token=token,
        job_id="preview_job",
        argv=argv,
    )
    try:
        validate_api_gate(
            allow_api=True,
            script="run_translate",
            confirm_token=token,
            job_id="preview_job",
            argv=argv,
        )
        assert False, "Expected one-time token rejection"
    except Exception as exc:
        assert getattr(exc, "code", "") == "confirm_token_invalid"


def test_confirm_token_must_match_exact_argv(tmp_path):
    from services.thesis_runs import RunControlError, build_argv, generate_prompt_preview, validate_api_gate

    db_path = _make_doc_db(tmp_path)
    preview = generate_prompt_preview(
        job_id="preview_job",
        script="run_translate",
        db=str(db_path),
        chapters=["ch02"],
        configs=["S1"],
        profile="literary_v1",
        tool_root=TOOL_ROOT,
        jobs_root=tmp_path,
    )
    mismatched = build_argv(
        script="run_translate",
        db=str(db_path),
        chapters=["ch02"],
        configs=["S0"],
        profile="literary_v1",
        allow_api=True,
    )
    try:
        validate_api_gate(
            allow_api=True,
            script="run_translate",
            confirm_token=preview["confirm_token"],
            job_id="preview_job",
            argv=mismatched,
        )
        assert False, "Expected mismatch"
    except RunControlError as exc:
        assert exc.code == "confirm_token_mismatch"


def test_estimate_preview_supports_one_button_script_and_binds_token(tmp_path):
    from services.thesis_runs import (
        build_argv,
        generate_estimate_preview,
        validate_api_gate,
        validate_script,
    )

    validate_script("run_experiment_cascade")
    preview = generate_estimate_preview(
        job_id="one_button_job",
        script="run_experiment_cascade",
        extra_args=[
            "--db",
            str(tmp_path / "memory.sqlite3"),
            "--configs",
            "S1",
            "--out-dir",
            str(tmp_path / "reports"),
        ],
        tool_root=TOOL_ROOT,
        jobs_root=tmp_path,
    )

    assert preview["preview_kind"] == "estimate_only"
    assert preview["confirm_token"]
    assert "--preflight-only" in preview["estimate_argv_preview"]
    assert "--preflight-only" not in preview["argv_preview"]
    assert preview["estimate_by_stage"][0]["script"] == "run_experiment_cascade"
    token = preview["confirm_token"]
    validate_api_gate(
        allow_api=True,
        script="run_experiment_cascade",
        confirm_token=token,
        job_id="one_button_job",
        argv=preview["argv_preview"],
    )

    preview2 = generate_estimate_preview(
        job_id="one_button_job",
        script="run_experiment_cascade",
        extra_args=["--db", str(tmp_path / "memory.sqlite3"), "--configs", "S1"],
        tool_root=TOOL_ROOT,
        jobs_root=tmp_path,
    )
    mismatched = build_argv(
        script="run_experiment_cascade",
        extra_args=["--db", str(tmp_path / "memory.sqlite3"), "--configs", "S0"],
        allow_api=True,
    )
    try:
        validate_api_gate(
            allow_api=True,
            script="run_experiment_cascade",
            confirm_token=preview2["confirm_token"],
            job_id="one_button_job",
            argv=mismatched,
        )
        assert False, "Expected mismatch"
    except Exception as exc:
        assert getattr(exc, "code", "") == "confirm_token_mismatch"


def test_route_real_snapshot_script_zero_api(tmp_path, monkeypatch):
    db_path = _make_doc_db(tmp_path)
    out_path = tmp_path / "snapshot.json"
    monkeypatch.setenv("THESIS_JOBS_ROOT", str(tmp_path))
    monkeypatch.setenv("THESIS_TOOL_ROOT", str(TOOL_ROOT))
    monkeypatch.setenv("THESIS_TOOL_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("THESIS_APP_MODE", "cockpit")

    _reset_app_modules()
    app_module = importlib.import_module("app")
    client = app_module.create_app().test_client()
    resp = client.post(
        "/api/thesis/runs",
        json={"script": "snapshot_runs", "db": str(db_path), "out": str(out_path)},
    )
    assert resp.status_code == 201
    run_id = resp.get_json()["data"]["run_id"]

    for _ in range(80):
        time.sleep(0.1)
        detail = client.get(f"/api/thesis/runs/{run_id}").get_json()["data"]
        if detail["status"] in {"done", "failed", "error"}:
            break
    assert detail["status"] == "done"
    assert detail["exit_code"] == 0
    assert out_path.exists()

    log = client.get(f"/api/thesis/runs/{run_id}/log?offset=0").get_json()["data"]
    assert "Snapshot written" in log["log"]


def test_managed_run_freezes_before_registry_and_spawn_and_reuses_exact_run(
    tmp_path,
    monkeypatch,
):
    db_path = _make_doc_db(tmp_path)
    monkeypatch.setenv("THESIS_JOBS_ROOT", str(tmp_path))
    monkeypatch.setenv("THESIS_TOOL_ROOT", str(TOOL_ROOT))
    monkeypatch.setenv("THESIS_TOOL_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("THESIS_APP_MODE", "cockpit")
    _reset_app_modules()
    app_module = importlib.import_module("app")
    routes = importlib.import_module("routes.thesis_runs")
    from services.thesis_runs import RunRegistry

    registry = RunRegistry(runs_root=tmp_path)
    routes.set_registry(registry)
    events: list[str] = []
    real_create = registry.create_run

    def tracked_create(**kwargs):
        events.append("registry")
        return real_create(**kwargs)

    def tracked_freeze(job_id, run_id, *, jobs_root):
        events.append("freeze")
        assert job_id == "managed_job"
        assert run_id == "run_managed"
        assert jobs_root == tmp_path
        return {"lifecycle": "run_started_frozen"}

    monkeypatch.setattr(registry, "create_run", tracked_create)
    monkeypatch.setattr(routes, "freeze_managed_runtime_for_run", tracked_freeze)
    monkeypatch.setattr(
        routes,
        "spawn_run",
        lambda _registry, run_id: events.append(f"spawn:{run_id}"),
    )
    client = app_module.create_app().test_client()
    request_payload = {
        "script": "snapshot_runs",
        "job_id": "managed_job",
        "planned_run_id": "run_managed",
        "db": str(db_path),
        "out": str(tmp_path / "snapshot.json"),
    }

    created = client.post("/api/thesis/runs", json=request_payload)
    assert created.status_code == 201
    assert events == ["freeze", "registry", "spawn:run_managed"]

    reused = client.post("/api/thesis/runs", json=request_payload)
    assert reused.status_code == 200
    assert reused.get_json()["data"]["reused"] is True
    assert events == [
        "freeze",
        "registry",
        "spawn:run_managed",
        "freeze",
    ]


def test_planned_run_id_collision_rejects_foreign_identity_before_freeze(
    tmp_path,
    monkeypatch,
):
    db_path = _make_doc_db(tmp_path)
    monkeypatch.setenv("THESIS_JOBS_ROOT", str(tmp_path))
    monkeypatch.setenv("THESIS_TOOL_ROOT", str(TOOL_ROOT))
    monkeypatch.setenv("THESIS_TOOL_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("THESIS_APP_MODE", "cockpit")
    _reset_app_modules()
    app_module = importlib.import_module("app")
    routes = importlib.import_module("routes.thesis_runs")
    from services.thesis_runs import RunRegistry

    registry = RunRegistry(runs_root=tmp_path)
    registry.create_run(
        script="snapshot_runs",
        argv=[sys.executable, "-m", "pipeline.scripts.snapshot_runs"],
        cwd=str(TOOL_ROOT),
        job_id="foreign_job",
        run_id="run_collision",
    )
    routes.set_registry(registry)
    freezes: list[str] = []
    spawned: list[str] = []
    monkeypatch.setattr(
        routes,
        "freeze_managed_runtime_for_run",
        lambda _job_id, run_id, **_kwargs: freezes.append(run_id),
    )
    monkeypatch.setattr(
        routes,
        "spawn_run",
        lambda _registry, run_id: spawned.append(run_id),
    )
    client = app_module.create_app().test_client()

    response = client.post(
        "/api/thesis/runs",
        json={
            "script": "snapshot_runs",
            "job_id": "managed_job",
            "planned_run_id": "run_collision",
            "db": str(db_path),
            "out": str(tmp_path / "snapshot.json"),
        },
    )

    assert response.status_code == 409
    assert response.get_json()["errors"][0]["code"] == "planned_run_id_collision"
    assert registry.get_run("run_collision")["job_id"] == "foreign_job"
    assert len(registry.list_runs()) == 1
    assert freezes == []
    assert spawned == []


def test_managed_freeze_failure_prevents_registry_and_spawn(tmp_path, monkeypatch):
    db_path = _make_doc_db(tmp_path)
    monkeypatch.setenv("THESIS_JOBS_ROOT", str(tmp_path))
    monkeypatch.setenv("THESIS_TOOL_ROOT", str(TOOL_ROOT))
    monkeypatch.setenv("THESIS_TOOL_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("THESIS_APP_MODE", "cockpit")
    _reset_app_modules()
    app_module = importlib.import_module("app")
    routes = importlib.import_module("routes.thesis_runs")
    from services.project_runtime import ProjectRuntimeError
    from services.thesis_runs import RunRegistry

    registry = RunRegistry(runs_root=tmp_path)
    routes.set_registry(registry)
    spawned: list[str] = []
    monkeypatch.setattr(
        routes,
        "freeze_managed_runtime_for_run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ProjectRuntimeError(
                "source_package_already_frozen",
                "Source package belongs to another run.",
                409,
            )
        ),
    )
    monkeypatch.setattr(
        routes,
        "spawn_run",
        lambda _registry, run_id: spawned.append(run_id),
    )
    client = app_module.create_app().test_client()

    response = client.post(
        "/api/thesis/runs",
        json={
            "script": "snapshot_runs",
            "job_id": "managed_job",
            "planned_run_id": "run_rejected",
            "db": str(db_path),
            "out": str(tmp_path / "snapshot.json"),
        },
    )

    assert response.status_code == 409
    assert response.get_json()["errors"][0]["code"] == "source_package_already_frozen"
    assert registry.list_runs() == []
    assert spawned == []


def test_route_allow_api_without_token_rejected(tmp_path, monkeypatch):
    db_path = _make_doc_db(tmp_path)
    monkeypatch.setenv("THESIS_JOBS_ROOT", str(tmp_path))
    monkeypatch.setenv("THESIS_TOOL_ROOT", str(TOOL_ROOT))
    monkeypatch.setenv("THESIS_TOOL_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("THESIS_APP_MODE", "cockpit")

    _reset_app_modules()
    app_module = importlib.import_module("app")
    client = app_module.create_app().test_client()
    resp = client.post(
        "/api/thesis/runs",
        json={
            "script": "run_translate",
            "job_id": "preview_job",
            "db": str(db_path),
            "chapters": ["ch02"],
            "configs": ["S1"],
            "allow_api": True,
            "planned_run_id": "run_missing_token",
        },
    )
    assert resp.status_code == 403
    assert resp.get_json()["errors"][0]["code"] == "confirm_token_required"


def test_route_cancel_run_taskkills_registered_process(tmp_path, monkeypatch):
    monkeypatch.setenv("THESIS_JOBS_ROOT", str(tmp_path))
    monkeypatch.setenv("THESIS_TOOL_ROOT", str(TOOL_ROOT))
    monkeypatch.setenv("THESIS_TOOL_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("THESIS_APP_MODE", "cockpit")

    _reset_app_modules()
    app_module = importlib.import_module("app")
    routes = importlib.import_module("routes.thesis_runs")
    services = importlib.import_module("services.thesis_runs")
    from services.thesis_runs import RunRegistry

    registry = RunRegistry(runs_root=tmp_path)
    entry = registry.create_run(script="snapshot_runs", argv=[sys.executable, "-c", "pass"], run_id="run_cancel")
    registry.update_run(entry["run_id"], status="running", pid=12345)
    routes.set_registry(registry)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        services.subprocess,
        "run",
        lambda argv, **_kwargs: calls.append([str(item) for item in argv]),
    )
    client = app_module.create_app().test_client()

    resp = client.post("/api/thesis/runs/run_cancel/cancel")

    assert resp.status_code == 200
    assert resp.get_json()["data"]["status"] == "cancelled"
    if services.os.name == "nt":
        assert calls == [["taskkill", "/T", "/F", "/PID", "12345"]]


def test_route_run_events_tails_registered_sidecar_only(tmp_path, monkeypatch):
    monkeypatch.setenv("THESIS_JOBS_ROOT", str(tmp_path))
    monkeypatch.setenv("THESIS_TOOL_ROOT", str(TOOL_ROOT))
    monkeypatch.setenv("THESIS_TOOL_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("THESIS_APP_MODE", "cockpit")

    _reset_app_modules()
    app_module = importlib.import_module("app")
    routes = importlib.import_module("routes.thesis_runs")
    from services.thesis_runs import RunRegistry

    registry = RunRegistry(runs_root=tmp_path)
    event_dir = tmp_path / "run_events"
    event_dir.mkdir()
    event_path = event_dir / "run_evt.jsonl"
    event_path.write_text(
        "\n".join([
            json.dumps({"event": "window_started", "seq": 1}),
            json.dumps({"event": "run_committed", "seq": 2}),
        ])
        + "\n",
        encoding="utf-8",
    )
    entry = registry.create_run(
        script="run_translate",
        argv=[sys.executable, "-c", "pass"],
        run_id="run_evt",
        event_log_path=str(event_path),
    )
    registry.update_run(entry["run_id"], status="done", exit_code=0)
    routes.set_registry(registry)
    client = app_module.create_app().test_client()

    resp = client.get("/api/thesis/runs/run_evt/events?offset=0")
    assert resp.status_code == 200
    data = resp.get_json()["data"]
    assert [event["event"] for event in data["events"]] == ["window_started", "run_committed"]
    assert data["running"] is False
    assert data["offset"] > 0

    outside = tmp_path / "outside.jsonl"
    outside.write_text("{}\n", encoding="utf-8")
    bad = registry.create_run(
        script="run_translate",
        argv=[sys.executable, "-c", "pass"],
        run_id="run_bad",
        event_log_path=str(outside),
    )
    registry.update_run(bad["run_id"], status="done", exit_code=0)
    resp_bad = client.get("/api/thesis/runs/run_bad/events?offset=0")
    assert resp_bad.status_code == 500
    assert resp_bad.get_json()["errors"][0]["code"] == "invalid_event_log_path"


def test_route_run_events_preserves_partial_line_for_next_poll(tmp_path, monkeypatch):
    monkeypatch.setenv("THESIS_JOBS_ROOT", str(tmp_path))
    monkeypatch.setenv("THESIS_TOOL_ROOT", str(TOOL_ROOT))
    monkeypatch.setenv("THESIS_TOOL_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("THESIS_APP_MODE", "cockpit")

    _reset_app_modules()
    app_module = importlib.import_module("app")
    routes = importlib.import_module("routes.thesis_runs")
    from services.thesis_runs import RunRegistry

    registry = RunRegistry(runs_root=tmp_path)
    event_dir = tmp_path / "run_events"
    event_dir.mkdir()
    event_path = event_dir / "run_partial.jsonl"
    first = json.dumps({"event": "stage_start", "seq": 1})
    partial = json.dumps({"event": "stage_done", "seq": 2})
    event_path.write_text(first + "\n" + partial[:12], encoding="utf-8")
    entry = registry.create_run(
        script="run_translate",
        argv=[sys.executable, "-c", "pass"],
        run_id="run_partial",
        event_log_path=str(event_path),
    )
    registry.update_run(entry["run_id"], status="running", exit_code=None)
    routes.set_registry(registry)
    client = app_module.create_app().test_client()

    resp = client.get("/api/thesis/runs/run_partial/events?offset=0")
    assert resp.status_code == 200
    data = resp.get_json()["data"]
    assert [event["event"] for event in data["events"]] == ["stage_start"]
    assert data["partial_line"] is True
    expected_offset = len(event_path.read_bytes().splitlines(keepends=True)[0])
    assert data["offset"] == expected_offset

    with event_path.open("a", encoding="utf-8") as fh:
        fh.write(partial[12:] + "\n")
    resp2 = client.get(f"/api/thesis/runs/run_partial/events?offset={data['offset']}")
    data2 = resp2.get_json()["data"]
    assert [event["event"] for event in data2["events"]] == ["stage_done"]
    assert data2["partial_line"] is False


def test_route_run_events_max_bytes_truncates_on_line_boundary(tmp_path, monkeypatch):
    monkeypatch.setenv("THESIS_JOBS_ROOT", str(tmp_path))
    monkeypatch.setenv("THESIS_TOOL_ROOT", str(TOOL_ROOT))
    monkeypatch.setenv("THESIS_TOOL_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("THESIS_APP_MODE", "cockpit")

    _reset_app_modules()
    app_module = importlib.import_module("app")
    routes = importlib.import_module("routes.thesis_runs")
    from services.thesis_runs import RunRegistry

    registry = RunRegistry(runs_root=tmp_path)
    event_dir = tmp_path / "run_events"
    event_dir.mkdir()
    event_path = event_dir / "run_trunc.jsonl"
    rows = [json.dumps({"event": "item", "seq": idx, "payload": "x" * 120}) for idx in range(8)]
    event_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    entry = registry.create_run(
        script="run_translate",
        argv=[sys.executable, "-c", "pass"],
        run_id="run_trunc",
        event_log_path=str(event_path),
    )
    registry.update_run(entry["run_id"], status="running", exit_code=None)
    routes.set_registry(registry)
    client = app_module.create_app().test_client()

    resp = client.get("/api/thesis/runs/run_trunc/events?offset=0&max_bytes=250")
    assert resp.status_code == 200
    data = resp.get_json()["data"]
    assert data["truncated"] is True
    assert data["events"]
    assert data["offset"] <= 250
    assert event_path.read_bytes()[data["offset"] - 1:data["offset"]] == b"\n"


def test_route_one_button_estimate_confirm_stores_manifest_and_run_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("THESIS_JOBS_ROOT", str(tmp_path))
    monkeypatch.setenv("THESIS_TOOL_ROOT", str(TOOL_ROOT))
    monkeypatch.setenv("THESIS_TOOL_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("THESIS_APP_MODE", "cockpit")

    _reset_app_modules()
    app_module = importlib.import_module("app")
    routes = importlib.import_module("routes.thesis_runs")
    spawned: list[str] = []
    monkeypatch.setattr(routes, "spawn_run", lambda _registry, run_id: spawned.append(run_id))
    client = app_module.create_app().test_client()

    preview = client.get(
        "/api/thesis/runs/estimate-preview"
        "?script=run_one_button&job_id=jobA&chapters=d2l_mlp"
        "&budget_cap_usd=0.75&planned_run_id=run_onebtn&with_s0=true"
    )
    assert preview.status_code == 200
    preview_data = preview.get_json()["data"]
    assert preview_data["planned_run_id"] == "run_onebtn"
    assert preview_data["run_dir"].endswith("jobA\\one_button\\run_onebtn") or preview_data["run_dir"].endswith("jobA/one_button/run_onebtn")
    assert "--estimate-only" in preview_data["estimate_argv_preview"]
    assert "--estimate-only" not in preview_data["argv_preview"]
    assert "--workdb" in preview_data["argv_preview"]
    assert str(tmp_path / "_work" / "one_button" / "jobA" / "run_onebtn" / "workdb.sqlite3") in preview_data["argv_preview"]

    resp = client.post(
        "/api/thesis/runs",
        json={
            "script": "run_one_button",
            "job_id": "jobA",
            "chapters": ["d2l_mlp"],
            "budget_cap_usd": 0.75,
            "with_s0": True,
            "allow_api": True,
            "planned_run_id": "run_onebtn",
            "confirm_token": preview_data["confirm_token"],
        },
    )
    assert resp.status_code == 201
    data = resp.get_json()["data"]
    assert data["run_id"] == "run_onebtn"
    assert data["manifest_path"].endswith("manifest.json")
    assert spawned == ["run_onebtn"]
    detail = client.get("/api/thesis/runs/run_onebtn").get_json()["data"]
    assert detail["run_dir"] == data["run_dir"]
    assert detail["manifest_path"] == data["manifest_path"]
    assert "--estimate-only" not in detail["argv"]


def test_route_one_button_pause_manifest_and_unpause(tmp_path, monkeypatch):
    monkeypatch.setenv("THESIS_JOBS_ROOT", str(tmp_path))
    monkeypatch.setenv("THESIS_TOOL_ROOT", str(TOOL_ROOT))
    monkeypatch.setenv("THESIS_TOOL_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("THESIS_APP_MODE", "cockpit")

    _reset_app_modules()
    app_module = importlib.import_module("app")
    routes = importlib.import_module("routes.thesis_runs")
    from services.thesis_runs import RunRegistry

    registry = RunRegistry(runs_root=tmp_path)
    run_dir = tmp_path / "jobA" / "one_button" / "run_pause"
    run_dir.mkdir(parents=True)
    manifest_path = run_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "status": "paused",
                "attempt": 2,
                "stages": [{"name": "builder_c2", "status": "done"}],
                "estimate_by_stage": [{"stage": "builder_c2", "cost_usd_estimate": 0.01}],
                "paused_at_stage_boundary_before": "translator",
                "error": None,
            }
        ),
        encoding="utf-8",
    )
    registry.create_run(
        script="run_one_button",
        argv=[sys.executable, "-c", "pass"],
        run_id="run_pause",
        job_id="jobA",
        run_dir=str(run_dir),
        manifest_path=str(manifest_path),
    )
    routes.set_registry(registry)
    client = app_module.create_app().test_client()

    pause = client.post("/api/thesis/runs/run_pause/pause")
    assert pause.status_code == 200
    assert (run_dir / "PAUSE").exists()
    manifest = client.get("/api/thesis/runs/run_pause/manifest")
    assert manifest.status_code == 200
    data = manifest.get_json()["data"]
    assert data["status"] == "paused"
    assert data["attempt"] == 2
    assert data["paused_at_stage_boundary_before"] == "translator"
    assert data["stages"][0]["name"] == "builder_c2"
    unpause = client.delete("/api/thesis/runs/run_pause/pause")
    assert unpause.status_code == 200
    assert not (run_dir / "PAUSE").exists()
    assert client.delete("/api/thesis/runs/run_pause/pause").status_code == 200

    no_run_dir = registry.create_run(script="run_one_button", argv=[sys.executable, "-c", "pass"], run_id="run_nodir")
    assert no_run_dir["run_id"] == "run_nodir"
    missing = client.post("/api/thesis/runs/run_nodir/pause")
    assert missing.status_code == 404
    assert missing.get_json()["errors"][0]["code"] == "run_pause_unavailable"


def test_route_one_button_resume_creates_new_registry_run_and_clears_pause(tmp_path, monkeypatch):
    monkeypatch.setenv("THESIS_JOBS_ROOT", str(tmp_path))
    monkeypatch.setenv("THESIS_TOOL_ROOT", str(TOOL_ROOT))
    monkeypatch.setenv("THESIS_TOOL_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("THESIS_APP_MODE", "cockpit")

    _reset_app_modules()
    app_module = importlib.import_module("app")
    routes = importlib.import_module("routes.thesis_runs")
    from services.thesis_runs import (
        RunRegistry,
        build_resume_argv_from_entry,
        issue_estimate_token_for_argv,
    )

    registry = RunRegistry(runs_root=tmp_path)
    run_dir = tmp_path / "jobA" / "one_button" / "run_resume"
    run_dir.mkdir(parents=True)
    (run_dir / "PAUSE").write_text("paused_by_user\n", encoding="utf-8")
    manifest_path = run_dir / "manifest.json"
    manifest_path.write_text(json.dumps({"attempt": 1, "status": "paused"}), encoding="utf-8")
    event_dir = tmp_path / "run_events"
    event_dir.mkdir()
    event_log = event_dir / "run_resume.jsonl"
    old = registry.create_run(
        script="run_one_button",
        argv=[
            sys.executable,
            "-m",
            "pipeline.scripts.run_one_button",
            "--job-id",
            "jobA",
            "--chapters",
            "d2l_mlp",
            "--workdb",
            str(run_dir / "workdb.sqlite3"),
            "--budget-cap-usd",
            "1.0",
            "--event-log",
            str(event_log),
            "--run-id",
            "run_resume",
            "--estimate-only",
        ],
        run_id="run_resume",
        job_id="jobA",
        event_log_path=str(event_log),
        run_dir=str(run_dir),
        manifest_path=str(manifest_path),
    )
    registry.update_run("run_resume", status="paused", exit_code=0)
    token = issue_estimate_token_for_argv(
        job_id="jobA",
        script="run_one_button",
        argv=build_resume_argv_from_entry(old),
        preview_kind="resume_estimate_only",
    )
    routes.set_registry(registry)
    spawned: list[str] = []
    frozen_run_ids: list[str] = []
    monkeypatch.setattr(
        routes,
        "freeze_managed_runtime_for_run",
        lambda _job_id, frozen_run_id, **_kwargs: frozen_run_ids.append(frozen_run_id),
    )
    monkeypatch.setattr(routes, "spawn_run", lambda _registry, run_id: spawned.append(run_id))
    client = app_module.create_app().test_client()

    preview = client.get("/api/thesis/runs/estimate-preview?resume_run_id=run_resume")
    assert preview.status_code == 200
    assert "--resume" in preview.get_json()["data"]["argv_preview"]

    resp = client.post("/api/thesis/runs/run_resume/resume", json={"confirm_token": token})
    assert resp.status_code == 201
    data = resp.get_json()["data"]
    assert data["resumed_from"] == "run_resume"
    assert data["attempt_index"] == 2
    assert frozen_run_ids == ["run_resume"]
    assert spawned == [data["run_id"]]
    assert not (run_dir / "PAUSE").exists()
    new_entry = client.get(f"/api/thesis/runs/{data['run_id']}").get_json()["data"]
    assert new_entry["resumed_from"] == "run_resume"
    assert new_entry["attempt_index"] == 2
    assert new_entry["attempt_log_path"].endswith(f"{data['run_id']}.log")
    assert "--estimate-only" not in new_entry["argv"]
    assert "--run-id" not in new_entry["argv"]
    assert new_entry["argv"][-2:] == ["--resume", "run_resume"]


def test_route_repeated_resume_validates_original_frozen_run(tmp_path, monkeypatch):
    monkeypatch.setenv("THESIS_JOBS_ROOT", str(tmp_path))
    monkeypatch.setenv("THESIS_TOOL_ROOT", str(TOOL_ROOT))
    monkeypatch.setenv("THESIS_TOOL_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("THESIS_APP_MODE", "cockpit")
    _reset_app_modules()
    app_module = importlib.import_module("app")
    routes = importlib.import_module("routes.thesis_runs")
    from services.thesis_runs import (
        RunRegistry,
        build_resume_argv_from_entry,
        issue_estimate_token_for_argv,
    )

    registry = RunRegistry(runs_root=tmp_path)
    root, run_dir = _create_resumable_one_button_run(
        tmp_path,
        registry,
        run_id="run_resume_root",
    )
    attempt = registry.create_run(
        script="run_one_button",
        argv=build_resume_argv_from_entry(root),
        cwd=str(TOOL_ROOT),
        job_id="jobA",
        event_log_path=root["event_log_path"],
        run_dir=root["run_dir"],
        manifest_path=root["manifest_path"],
        run_id="run_resume_attempt_1",
        attempt_index=2,
        resumed_from="run_resume_root",
    )
    registry.update_run(attempt["run_id"], status="paused", exit_code=0)
    (run_dir / "PAUSE").write_text("paused_by_user\n", encoding="utf-8")
    token = issue_estimate_token_for_argv(
        job_id="jobA",
        script="run_one_button",
        argv=build_resume_argv_from_entry(attempt),
        preview_kind="resume_estimate_only",
    )
    routes.set_registry(registry)
    frozen_run_ids: list[str] = []
    spawned: list[str] = []
    monkeypatch.setattr(
        routes,
        "freeze_managed_runtime_for_run",
        lambda _job_id, frozen_run_id, **_kwargs: frozen_run_ids.append(frozen_run_id),
    )
    monkeypatch.setattr(routes, "spawn_run", lambda _registry, run_id: spawned.append(run_id))
    client = app_module.create_app().test_client()

    response = client.post(
        "/api/thesis/runs/run_resume_attempt_1/resume",
        json={"confirm_token": token},
    )

    assert response.status_code == 201
    data = response.get_json()["data"]
    assert data["resumed_from"] == "run_resume_attempt_1"
    assert data["attempt_index"] == 3
    assert frozen_run_ids == ["run_resume_root"]
    assert spawned == [data["run_id"]]
    assert not (run_dir / "PAUSE").exists()


def test_route_resume_rejects_invalid_ancestry_without_mutation(tmp_path, monkeypatch):
    monkeypatch.setenv("THESIS_JOBS_ROOT", str(tmp_path))
    monkeypatch.setenv("THESIS_TOOL_ROOT", str(TOOL_ROOT))
    monkeypatch.setenv("THESIS_TOOL_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("THESIS_APP_MODE", "cockpit")
    _reset_app_modules()
    app_module = importlib.import_module("app")
    routes = importlib.import_module("routes.thesis_runs")
    from services.thesis_runs import RunRegistry

    registry = RunRegistry(runs_root=tmp_path)
    stale, stale_dir = _create_resumable_one_button_run(
        tmp_path,
        registry,
        run_id="run_stale_child",
        resumed_from="run_missing_parent",
    )
    foreign_parent, _foreign_dir = _create_resumable_one_button_run(
        tmp_path,
        registry,
        run_id="run_foreign_parent",
        job_id="jobB",
    )
    cross_job, cross_dir = _create_resumable_one_button_run(
        tmp_path,
        registry,
        run_id="run_cross_job_child",
        resumed_from=foreign_parent["run_id"],
    )
    cycle_a, cycle_a_dir = _create_resumable_one_button_run(
        tmp_path,
        registry,
        run_id="run_cycle_a",
        resumed_from="run_cycle_b",
    )
    _cycle_b, _cycle_b_dir = _create_resumable_one_button_run(
        tmp_path,
        registry,
        run_id="run_cycle_b",
        resumed_from=cycle_a["run_id"],
    )
    for run_dir in (stale_dir, cross_dir, cycle_a_dir):
        (run_dir / "PAUSE").write_text("paused_by_user\n", encoding="utf-8")
    routes.set_registry(registry)
    freezes: list[str] = []
    spawned: list[str] = []
    monkeypatch.setattr(
        routes,
        "freeze_managed_runtime_for_run",
        lambda _job_id, run_id, **_kwargs: freezes.append(run_id),
    )
    monkeypatch.setattr(routes, "spawn_run", lambda _registry, run_id: spawned.append(run_id))
    client = app_module.create_app().test_client()
    before_run_ids = {row["run_id"] for row in registry.list_runs()}

    for run_id, run_dir in (
        (stale["run_id"], stale_dir),
        (cross_job["run_id"], cross_dir),
        (cycle_a["run_id"], cycle_a_dir),
    ):
        response = client.post(
            f"/api/thesis/runs/{run_id}/resume",
            json={"confirm_token": "unused"},
        )
        assert response.status_code == 409
        assert response.get_json()["errors"][0]["code"] == "resume_ancestry_invalid"
        assert (run_dir / "PAUSE").is_file()

    assert {row["run_id"] for row in registry.list_runs()} == before_run_ids
    assert freezes == []
    assert spawned == []


def test_route_resume_freeze_failure_preserves_pause(tmp_path, monkeypatch):
    monkeypatch.setenv("THESIS_JOBS_ROOT", str(tmp_path))
    monkeypatch.setenv("THESIS_TOOL_ROOT", str(TOOL_ROOT))
    monkeypatch.setenv("THESIS_TOOL_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("THESIS_APP_MODE", "cockpit")
    _reset_app_modules()
    app_module = importlib.import_module("app")
    routes = importlib.import_module("routes.thesis_runs")
    from services.project_runtime import ProjectRuntimeError
    from services.thesis_runs import (
        RunRegistry,
        build_resume_argv_from_entry,
        issue_estimate_token_for_argv,
    )

    registry = RunRegistry(runs_root=tmp_path)
    entry, run_dir = _create_resumable_one_button_run(
        tmp_path,
        registry,
        run_id="run_resume_freeze_failure",
    )
    (run_dir / "PAUSE").write_text("paused_by_user\n", encoding="utf-8")
    token = issue_estimate_token_for_argv(
        job_id="jobA",
        script="run_one_button",
        argv=build_resume_argv_from_entry(entry),
        preview_kind="resume_estimate_only",
    )
    routes.set_registry(registry)
    spawned: list[str] = []
    monkeypatch.setattr(
        routes,
        "freeze_managed_runtime_for_run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ProjectRuntimeError(
                "source_package_already_frozen",
                "Source package belongs to another run.",
                409,
            )
        ),
    )
    monkeypatch.setattr(routes, "spawn_run", lambda _registry, run_id: spawned.append(run_id))
    client = app_module.create_app().test_client()
    before_run_ids = {row["run_id"] for row in registry.list_runs()}

    response = client.post(
        "/api/thesis/runs/run_resume_freeze_failure/resume",
        json={"confirm_token": token},
    )

    assert response.status_code == 409
    assert response.get_json()["errors"][0]["code"] == "source_package_already_frozen"
    assert (run_dir / "PAUSE").is_file()
    assert {row["run_id"] for row in registry.list_runs()} == before_run_ids
    assert spawned == []


def test_route_one_button_resume_rejects_live_pid_without_spawning(tmp_path, monkeypatch):
    monkeypatch.setenv("THESIS_JOBS_ROOT", str(tmp_path))
    monkeypatch.setenv("THESIS_TOOL_ROOT", str(TOOL_ROOT))
    monkeypatch.setenv("THESIS_TOOL_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("THESIS_APP_MODE", "cockpit")

    _reset_app_modules()
    app_module = importlib.import_module("app")
    routes = importlib.import_module("routes.thesis_runs")
    from services.thesis_runs import (
        RunRegistry,
        build_resume_argv_from_entry,
        issue_estimate_token_for_argv,
    )

    registry = RunRegistry(runs_root=tmp_path)
    old, _run_dir = _create_resumable_one_button_run(
        tmp_path,
        registry,
        run_id="run_live_resume",
        pid=os.getpid(),
    )
    token = issue_estimate_token_for_argv(
        job_id="jobA",
        script="run_one_button",
        argv=build_resume_argv_from_entry(old),
        preview_kind="resume_estimate_only",
    )
    routes.set_registry(registry)
    spawned: list[str] = []
    monkeypatch.setattr(routes, "spawn_run", lambda _registry, run_id: spawned.append(run_id))
    client = app_module.create_app().test_client()

    resp = client.post("/api/thesis/runs/run_live_resume/resume", json={"confirm_token": token})

    assert resp.status_code == 409
    assert resp.get_json()["errors"][0]["code"] == "run_still_active"
    assert spawned == []


def test_route_one_button_resume_allows_dead_pid(tmp_path, monkeypatch):
    monkeypatch.setenv("THESIS_JOBS_ROOT", str(tmp_path))
    monkeypatch.setenv("THESIS_TOOL_ROOT", str(TOOL_ROOT))
    monkeypatch.setenv("THESIS_TOOL_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("THESIS_APP_MODE", "cockpit")

    _reset_app_modules()
    app_module = importlib.import_module("app")
    routes = importlib.import_module("routes.thesis_runs")
    from services.thesis_runs import (
        RunRegistry,
        build_resume_argv_from_entry,
        issue_estimate_token_for_argv,
    )

    registry = RunRegistry(runs_root=tmp_path)
    old, _run_dir = _create_resumable_one_button_run(
        tmp_path,
        registry,
        run_id="run_dead_resume",
        pid=999999999,
    )
    token = issue_estimate_token_for_argv(
        job_id="jobA",
        script="run_one_button",
        argv=build_resume_argv_from_entry(old),
        preview_kind="resume_estimate_only",
    )
    routes.set_registry(registry)
    spawned: list[str] = []
    monkeypatch.setattr(routes, "spawn_run", lambda _registry, run_id: spawned.append(run_id))
    client = app_module.create_app().test_client()

    resp = client.post("/api/thesis/runs/run_dead_resume/resume", json={"confirm_token": token})

    assert resp.status_code == 201
    data = resp.get_json()["data"]
    assert data["resumed_from"] == "run_dead_resume"
    assert spawned == [data["run_id"]]


def test_route_one_button_block_preview_reads_translation_runs_workdb_ro(tmp_path, monkeypatch):
    monkeypatch.setenv("THESIS_JOBS_ROOT", str(tmp_path))
    monkeypatch.setenv("THESIS_TOOL_ROOT", str(TOOL_ROOT))
    monkeypatch.setenv("THESIS_TOOL_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("THESIS_APP_MODE", "cockpit")

    _reset_app_modules()
    app_module = importlib.import_module("app")
    routes = importlib.import_module("routes.thesis_runs")
    from services.thesis_runs import RunRegistry

    registry = RunRegistry(runs_root=tmp_path)
    run_dir = tmp_path / "jobA" / "one_button" / "run_preview"
    run_dir.mkdir(parents=True)
    workdb = run_dir / "workdb.sqlite3"
    con = sqlite3.connect(workdb)
    con.executescript(
        """
        CREATE TABLE blocks (
            block_id TEXT PRIMARY KEY,
            text TEXT,
            order_index INTEGER
        );
        CREATE TABLE translation_runs (
            run_id TEXT,
            block_id TEXT,
            config TEXT,
            output_text TEXT,
            model TEXT,
            window_id TEXT,
            created_at TEXT
        );
        """
    )
    con.executemany(
        "INSERT INTO blocks(block_id, text, order_index) VALUES (?, ?, ?)",
        [
            ("b001", "source one", 1),
            ("b002", "source two", 2),
        ],
    )
    con.executemany(
        """
        INSERT INTO translation_runs(run_id, block_id, config, output_text, model, window_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("old_b001", "b001", "S1", "old target", "old-model", "w001", "2026-01-01T00:00:00"),
            ("new_b001", "b001", "S1", "new target", "gpt-5.4-mini", "w001", "2026-01-02T00:00:00"),
            ("b002_s1", "b002", "S1", "target two", "gpt-5.4-mini", "w002", "2026-01-02T00:00:00"),
        ],
    )
    con.commit()
    con.close()
    manifest_path = run_dir / "manifest.json"
    manifest_path.write_text(json.dumps({"workdb_path": str(workdb)}), encoding="utf-8")
    registry.create_run(
        script="run_one_button",
        argv=[sys.executable, "-c", "pass"],
        run_id="run_preview",
        job_id="jobA",
        run_dir=str(run_dir),
        manifest_path=str(manifest_path),
    )
    routes.set_registry(registry)
    client = app_module.create_app().test_client()

    resp = client.get("/api/thesis/runs/run_preview/block-preview?limit=2")

    assert resp.status_code == 200
    data = resp.get_json()["data"]
    assert data["source"] == "translation_runs"
    assert [row["block_id"] for row in data["blocks"]] == ["b001", "b002"]
    assert data["blocks"][0]["source_text"] == "source one"
    assert data["blocks"][0]["target_text"] == "new target"
    assert data["blocks"][0]["model"] == "gpt-5.4-mini"
    assert data["blocks"][0]["window_id"] == "w001"


def test_route_one_button_block_preview_missing_workdb_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("THESIS_JOBS_ROOT", str(tmp_path))
    monkeypatch.setenv("THESIS_TOOL_ROOT", str(TOOL_ROOT))
    monkeypatch.setenv("THESIS_TOOL_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("THESIS_APP_MODE", "cockpit")

    _reset_app_modules()
    app_module = importlib.import_module("app")
    routes = importlib.import_module("routes.thesis_runs")
    from services.thesis_runs import RunRegistry

    registry = RunRegistry(runs_root=tmp_path)
    run_dir = tmp_path / "jobA" / "one_button" / "run_empty"
    run_dir.mkdir(parents=True)
    registry.create_run(
        script="run_one_button",
        argv=[sys.executable, "-c", "pass"],
        run_id="run_empty",
        job_id="jobA",
        run_dir=str(run_dir),
        manifest_path=str(run_dir / "manifest.json"),
    )
    routes.set_registry(registry)
    client = app_module.create_app().test_client()

    resp = client.get("/api/thesis/runs/run_empty/block-preview")

    assert resp.status_code == 200
    assert resp.get_json()["data"] == {"blocks": [], "source": "none"}


def test_route_one_button_watchlist_reads_run_artifact_and_missing_is_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("THESIS_JOBS_ROOT", str(tmp_path))
    monkeypatch.setenv("THESIS_TOOL_ROOT", str(TOOL_ROOT))
    monkeypatch.setenv("THESIS_TOOL_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("THESIS_APP_MODE", "cockpit")

    _reset_app_modules()
    app_module = importlib.import_module("app")
    routes = importlib.import_module("routes.thesis_runs")
    from services.thesis_runs import RunRegistry

    registry = RunRegistry(runs_root=tmp_path)
    run_dir = tmp_path / "jobA" / "one_button" / "run_watch"
    watch_dir = run_dir / "artifacts" / "reelection"
    watch_dir.mkdir(parents=True)
    watchlist = [
        {
            "source_term": "regularization",
            "canonical_target_vi": "điều chuẩn",
            "audit_label": "keep_as_translate_term",
            "injection_action": "translate",
        }
    ]
    (watch_dir / "watchlist.json").write_text(json.dumps(watchlist, ensure_ascii=False), encoding="utf-8")
    registry.create_run(
        script="run_one_button",
        argv=[sys.executable, "-c", "pass"],
        run_id="run_watch",
        job_id="jobA",
        run_dir=str(run_dir),
        manifest_path=str(run_dir / "manifest.json"),
    )
    missing_dir = tmp_path / "jobA" / "one_button" / "run_no_watch"
    missing_dir.mkdir(parents=True)
    registry.create_run(
        script="run_one_button",
        argv=[sys.executable, "-c", "pass"],
        run_id="run_no_watch",
        job_id="jobA",
        run_dir=str(missing_dir),
        manifest_path=str(missing_dir / "manifest.json"),
    )
    routes.set_registry(registry)
    client = app_module.create_app().test_client()

    resp = client.get("/api/thesis/runs/run_watch/watchlist")
    missing = client.get("/api/thesis/runs/run_no_watch/watchlist")

    assert resp.status_code == 200
    assert resp.get_json()["data"]["watchlist"] == watchlist
    assert missing.status_code == 200
    assert missing.get_json()["data"] == {"watchlist": []}


def test_route_one_button_report_summary_reads_score_reports(tmp_path, monkeypatch):
    monkeypatch.setenv("THESIS_JOBS_ROOT", str(tmp_path))
    monkeypatch.setenv("THESIS_TOOL_ROOT", str(TOOL_ROOT))
    monkeypatch.setenv("THESIS_TOOL_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("THESIS_APP_MODE", "cockpit")

    _reset_app_modules()
    app_module = importlib.import_module("app")
    routes = importlib.import_module("routes.thesis_runs")
    from services.thesis_runs import RunRegistry

    registry = RunRegistry(runs_root=tmp_path)
    run_dir = tmp_path / "jobA" / "one_button" / "run_scores"
    reports_dir = run_dir / "reports"
    reports_dir.mkdir(parents=True)
    report = {
        "configs": ["S1"],
        "D_registry_consistency": {"S1": {"overall": 0.9467}},
        "B_gold_occurrence_adherence": {"S1": {"flat": {"adherence_lower": 0.875}}},
        "A_registry_occurrence_adherence": {"S1": {"adherence_lower": 0.91}},
        "stage_gate": {
            "no_passthrough_translated": {"S1": True},
            "scope_equals_translation_runs": {"S1": True},
            "preserve_terms_excluded_from_injection": True,
        },
    }
    (reports_dir / "score_run_phase_1.json").write_text(json.dumps(report), encoding="utf-8")
    (reports_dir / "score_run_final.json").write_text(json.dumps(report), encoding="utf-8")
    registry.create_run(
        script="run_one_button",
        argv=[sys.executable, "-c", "pass"],
        run_id="run_scores",
        job_id="jobA",
        run_dir=str(run_dir),
        manifest_path=str(run_dir / "manifest.json"),
    )
    routes.set_registry(registry)
    client = app_module.create_app().test_client()

    resp = client.get("/api/thesis/runs/run_scores/report-summary")

    assert resp.status_code == 200
    data = resp.get_json()["data"]
    assert data["phase_1"]["present"] is True
    assert data["final"]["present"] is True
    assert data["final"]["verdict"] == {"pass": True, "reasons": []}
    assert data["final"]["stage_gate"] == {
        "present": True,
        "passed": 3,
        "total": 3,
        "all_ok": True,
        "failed": [],
    }
    assert data["final"]["report_path"] == "reports/score_run_final.json"
    assert [(row["key"], row["value"], row["status"]) for row in data["final"]["metrics"]] == [
        ("TC", 0.9467, None),
        ("TA", 0.875, None),
        ("TA_REGISTRY", 0.91, None),
    ]
    assert data["compare"] == {"present": False, "gap": None}


def test_route_one_button_report_summary_colors_compare_metrics_relatively(tmp_path, monkeypatch):
    monkeypatch.setenv("THESIS_JOBS_ROOT", str(tmp_path))
    monkeypatch.setenv("THESIS_TOOL_ROOT", str(TOOL_ROOT))
    monkeypatch.setenv("THESIS_TOOL_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("THESIS_APP_MODE", "cockpit")

    _reset_app_modules()
    app_module = importlib.import_module("app")
    routes = importlib.import_module("routes.thesis_runs")
    from services.thesis_runs import RunRegistry

    registry = RunRegistry(runs_root=tmp_path)
    run_dir = tmp_path / "jobA" / "one_button" / "run_compare_scores"
    reports_dir = run_dir / "reports"
    reports_dir.mkdir(parents=True)
    report = {
        "configs": ["S0", "S1"],
        "D_registry_consistency": {"S0": {"overall": 0.777778}, "S1": {"overall": 1.0}},
        "B_gold_occurrence_adherence": {
            "S0": {"flat": {"adherence_lower": 0.747368}},
            "S1": {"flat": {"adherence_lower": 0.705263}},
        },
        "stage_gate": {
            "no_passthrough_translated": {"S0": True, "S1": True},
            "scope_equals_translation_runs": {"S0": True, "S1": True},
            "manual_passthrough_audit_required": True,
        },
    }
    (reports_dir / "score_run_final.json").write_text(json.dumps(report), encoding="utf-8")
    registry.create_run(
        script="run_one_button",
        argv=[sys.executable, "-c", "pass"],
        run_id="run_compare_scores",
        job_id="jobA",
        run_dir=str(run_dir),
        manifest_path=str(run_dir / "manifest.json"),
    )
    routes.set_registry(registry)
    client = app_module.create_app().test_client()

    resp = client.get("/api/thesis/runs/run_compare_scores/report-summary")

    assert resp.status_code == 200
    final = resp.get_json()["data"]["final"]
    by_key = {row["key"]: row for row in final["metrics"]}
    assert by_key["TC_S0"]["status"] is None
    assert by_key["TC_S1"]["status"] == "good"
    assert by_key["TA_S0"]["status"] is None
    assert by_key["TA_S1"]["status"] == "warn"
    assert final["stage_gate"] == {
        "present": True,
        "passed": 5,
        "total": 5,
        "all_ok": True,
        "failed": [],
    }


def test_route_one_button_report_summary_projects_consistency_bridge(tmp_path, monkeypatch):
    monkeypatch.setenv("THESIS_JOBS_ROOT", str(tmp_path))
    monkeypatch.setenv("THESIS_TOOL_ROOT", str(TOOL_ROOT))
    monkeypatch.setenv("THESIS_TOOL_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("THESIS_APP_MODE", "cockpit")

    _reset_app_modules()
    app_module = importlib.import_module("app")
    routes = importlib.import_module("routes.thesis_runs")
    from services.thesis_runs import RunRegistry

    registry = RunRegistry(runs_root=tmp_path)
    run_dir = tmp_path / "jobA" / "one_button" / "run_consistency"
    reports_dir = run_dir / "reports"
    reports_dir.mkdir(parents=True)
    report = {
        "configs": ["S0", "S1"],
        "D_registry_consistency": {
            "S0": {
                "overall": 0.5,
                "by_tier": {
                    "hard": {"terms": 2, "consistent_terms": 1, "drift_terms": 1, "undetected_terms": 0},
                    "soft": {"terms": 1, "consistent_terms": 0, "drift_terms": 1, "undetected_terms": 0},
                    "ignore_for_consistency": {"terms": 1, "consistent_terms": 0, "drift_terms": 1, "undetected_terms": 0},
                },
                "terms_all": [
                    {
                        "source_term": "optimization algorithms",
                        "target_term": "thuật toán tối ưu hóa",
                        "constraint_strength": "hard",
                        "status": "drift",
                        "forms_used": {"các thuật toán tối ưu": 1, "thuật toán tối ưu hóa": 1},
                    },
                    {
                        "source_term": "models",
                        "target_term": "mô hình",
                        "constraint_strength": "soft",
                        "status": "drift",
                        "forms_used": {"các mô hình": 2, "mô hình": 1},
                    },
                    {
                        "source_term": "example",
                        "target_term": "mẫu",
                        "constraint_strength": "ignore_for_consistency",
                        "status": "drift",
                        "forms_used": {"mẫu": 1, "ví dụ": 4},
                    },
                ],
            },
            "S1": {
                "overall": 1.0,
                "by_tier": {
                    "hard": {"terms": 2, "consistent_terms": 2, "drift_terms": 0, "undetected_terms": 0},
                    "soft": {"terms": 1, "consistent_terms": 0, "drift_terms": 1, "undetected_terms": 0},
                },
                "terms_all": [
                    {
                        "source_term": "optimization algorithms",
                        "target_term": "thuật toán tối ưu hóa",
                        "constraint_strength": "hard",
                        "status": "consistent",
                        "forms_used": {"thuật toán tối ưu hóa": 2},
                    },
                    {
                        "source_term": "models",
                        "target_term": "mô hình",
                        "constraint_strength": "soft",
                        "status": "drift",
                        "forms_used": {"các mô hình": 3, "mô hình": 1},
                    },
                    {
                        "source_term": "example",
                        "target_term": "mẫu",
                        "constraint_strength": "ignore_for_consistency",
                        "status": "drift",
                        "forms_used": {"mẫu": 1, "ví dụ": 4},
                    },
                ],
            },
        },
    }
    (reports_dir / "score_run_final.json").write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    registry.create_run(
        script="run_one_button",
        argv=[sys.executable, "-c", "pass"],
        run_id="run_consistency",
        job_id="jobA",
        run_dir=str(run_dir),
        manifest_path=str(run_dir / "manifest.json"),
    )
    routes.set_registry(registry)
    client = app_module.create_app().test_client()

    resp = client.get("/api/thesis/runs/run_consistency/report-summary")

    assert resp.status_code == 200
    consistency = resp.get_json()["data"]["consistency"]
    assert consistency["present"] is True
    assert consistency["overall"] == {"S0": 0.5, "S1": 1.0}
    assert consistency["by_tier"]["S0"]["hard"]["terms"] == 2
    terms = {item["source_term"]: item for item in consistency["notable_terms"]}
    assert "example" not in terms
    assert terms["optimization algorithms"]["fixed_by_injection"] is True
    assert terms["optimization algorithms"]["by_config"]["S0"]["status"] == "drift"
    assert terms["optimization algorithms"]["by_config"]["S1"]["status"] == "consistent"
    assert terms["models"]["fixed_by_injection"] is False
    assert terms["models"]["tier"] == "soft"


def test_route_one_button_report_summary_consistency_single_arm_has_no_fixed_entries(tmp_path, monkeypatch):
    monkeypatch.setenv("THESIS_JOBS_ROOT", str(tmp_path))
    monkeypatch.setenv("THESIS_TOOL_ROOT", str(TOOL_ROOT))
    monkeypatch.setenv("THESIS_TOOL_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("THESIS_APP_MODE", "cockpit")

    _reset_app_modules()
    app_module = importlib.import_module("app")
    routes = importlib.import_module("routes.thesis_runs")
    from services.thesis_runs import RunRegistry

    registry = RunRegistry(runs_root=tmp_path)
    run_dir = tmp_path / "jobA" / "one_button" / "run_consistency_s1"
    reports_dir = run_dir / "reports"
    reports_dir.mkdir(parents=True)
    report = {
        "configs": ["S1"],
        "D_registry_consistency": {
            "S1": {
                "overall": 0.5,
                "by_tier": {"hard": {"terms": 1, "consistent_terms": 0, "drift_terms": 1}},
                "terms_all": [
                    {
                        "source_term": "framework",
                        "target_term": "khung phần mềm",
                        "constraint_strength": "hard",
                        "status": "drift",
                        "forms_used": {"khung phần mềm": 1, "framework": 1},
                    }
                ],
            }
        },
    }
    (reports_dir / "score_run_final.json").write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    registry.create_run(
        script="run_one_button",
        argv=[sys.executable, "-c", "pass"],
        run_id="run_consistency_s1",
        job_id="jobA",
        run_dir=str(run_dir),
        manifest_path=str(run_dir / "manifest.json"),
    )
    routes.set_registry(registry)
    client = app_module.create_app().test_client()

    resp = client.get("/api/thesis/runs/run_consistency_s1/report-summary")

    assert resp.status_code == 200
    consistency = resp.get_json()["data"]["consistency"]
    assert consistency["present"] is True
    assert consistency["configs"] == ["S1"]
    assert consistency["notable_terms"][0]["source_term"] == "framework"
    assert consistency["notable_terms"][0]["fixed_by_injection"] is False


def test_route_one_button_report_summary_missing_reports_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("THESIS_JOBS_ROOT", str(tmp_path))
    monkeypatch.setenv("THESIS_TOOL_ROOT", str(TOOL_ROOT))
    monkeypatch.setenv("THESIS_TOOL_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("THESIS_APP_MODE", "cockpit")

    _reset_app_modules()
    app_module = importlib.import_module("app")
    routes = importlib.import_module("routes.thesis_runs")
    from services.thesis_runs import RunRegistry

    registry = RunRegistry(runs_root=tmp_path)
    run_dir = tmp_path / "jobA" / "one_button" / "run_no_scores"
    run_dir.mkdir(parents=True)
    registry.create_run(
        script="run_one_button",
        argv=[sys.executable, "-c", "pass"],
        run_id="run_no_scores",
        job_id="jobA",
        run_dir=str(run_dir),
        manifest_path=str(run_dir / "manifest.json"),
    )
    routes.set_registry(registry)
    client = app_module.create_app().test_client()

    resp = client.get("/api/thesis/runs/run_no_scores/report-summary")

    assert resp.status_code == 200
    data = resp.get_json()["data"]
    assert data["phase_1"]["present"] is False
    assert data["final"]["present"] is False
    assert data["compare"] == {"present": False, "gap": None}


def test_report_full_relays_persisted_projection_unchanged_and_read_only(tmp_path, monkeypatch):
    client, routes, registry = _prepare_full_report_route(tmp_path, monkeypatch)
    run_dir = _register_full_report_run(registry, tmp_path)
    report = _full_run_report(fixture_name="full_run_s0_s1.json")
    report_path = _write_full_run_report(run_dir, report)
    misleading = run_dir / "reports" / "score_run_final.json"
    _write_json(
        misleading,
        {
            "verdict": "NOT_BETTER",
            "delta": -999,
            "cost_usd": 123456,
        },
    )
    raw_cache = run_dir / "cache.sqlite3"
    raw_cache.write_bytes(b"not a real sqlite database")
    before = {
        path.relative_to(run_dir).as_posix(): path.read_bytes()
        for path in run_dir.rglob("*")
        if path.is_file()
    }
    monkeypatch.setattr(
        routes.sqlite3,
        "connect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("report-full must not open SQLite")
        ),
    )

    response = client.get("/api/thesis/runs/run_report/report-full")

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data == {
        "availability": "available",
        "schema_id": "FullRunReportV1",
        "schema_version": "1.0.0",
        "report": report,
    }
    assert [arm["arm_id"] for arm in data["report"]["arms"]] == ["s1", "s0"]
    assert data["report"]["metrics"][0]["comparison"]["delta"] == 12.0
    assert data["report"]["claim"]["verdict"] == "BETTER"
    assert report_path.read_bytes() == before["reports/full_run_report_v1.json"]
    after = {
        path.relative_to(run_dir).as_posix(): path.read_bytes()
        for path in run_dir.rglob("*")
        if path.is_file()
    }
    assert after == before
    route_source = Path(routes.__file__).read_text(encoding="utf-8")
    assert "from pipeline.eval.full_run_report_v1 import" in route_source
    assert "def _validate_full_run_report(" not in route_source


def test_report_full_missing_projection_is_not_generated_without_fallback(tmp_path, monkeypatch):
    client, routes, registry = _prepare_full_report_route(tmp_path, monkeypatch)
    run_dir = _register_full_report_run(registry, tmp_path)
    _write_json(run_dir / "score_run_final.json", {"verdict": "BETTER"})
    (run_dir / "reports").mkdir(parents=True, exist_ok=True)
    _write_json(run_dir / "reports" / "legacy.json", {"schema_id": "legacy"})
    (run_dir / "workdb.sqlite3").write_bytes(b"raw sqlite sentinel")
    monkeypatch.setattr(
        routes.sqlite3,
        "connect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("missing report must not scan SQLite")
        ),
    )

    response = client.get("/api/thesis/runs/run_report/report-full")

    assert response.status_code == 200
    assert response.get_json()["data"] == {
        "availability": "not_generated",
        "schema_id": "FullRunReportV1",
        "schema_version": "1.0.0",
        "report": None,
    }


def test_report_full_malformed_or_unsupported_schema_fails_closed(tmp_path, monkeypatch):
    client, _routes, registry = _prepare_full_report_route(tmp_path, monkeypatch)
    run_dir = _register_full_report_run(registry, tmp_path)
    report_path = run_dir / "reports" / "full_run_report_v1.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("{not-json", encoding="utf-8")

    malformed = client.get("/api/thesis/runs/run_report/report-full")
    assert malformed.status_code == 500
    assert malformed.get_json()["errors"][0]["code"] == "full_run_report_invalid_json"

    unsupported = _full_run_report()
    unsupported["schema_version"] = "2.0.0"
    _write_full_run_report(run_dir, _reseal_full_run_report(unsupported))
    response = client.get("/api/thesis/runs/run_report/report-full")
    assert response.status_code == 409
    assert (
        response.get_json()["errors"][0]["code"]
        == "full_run_report_schema_unsupported"
    )


def test_report_full_shape_identity_reference_and_path_errors_fail_closed(tmp_path, monkeypatch):
    client, _routes, registry = _prepare_full_report_route(tmp_path, monkeypatch)
    run_dir = _register_full_report_run(registry, tmp_path)

    invalid_cases: list[tuple[dict, str]] = []

    missing_required = _full_run_report()
    del missing_required["claim"]
    invalid_cases.append((missing_required, "full_run_report_contract_invalid"))

    identity_mismatch = _full_run_report(project_id="other-job")
    invalid_cases.append((identity_mismatch, "full_run_report_identity_mismatch"))

    unknown_arm = _full_run_report()
    unknown_arm["metrics"][0]["arm_values"][0]["arm_id"] = "missing-arm"
    invalid_cases.append(
        (_reseal_full_run_report(unknown_arm), "full_run_report_contract_invalid")
    )

    unknown_artifact = _full_run_report()
    unknown_artifact["metrics"][0]["source_artifact_ids"] = ["missing-artifact"]
    invalid_cases.append(
        (_reseal_full_run_report(unknown_artifact), "full_run_report_contract_invalid")
    )

    unknown_attempt = _full_run_report()
    unknown_attempt["stages"][0]["attempt_run_id"] = "missing-attempt"
    invalid_cases.append(
        (_reseal_full_run_report(unknown_attempt), "full_run_report_contract_invalid")
    )

    unsafe_path = _full_run_report()
    unsafe_path["artifacts"][0]["relative_path"] = "../other-run/translation.json"
    invalid_cases.append(
        (_reseal_full_run_report(unsafe_path), "full_run_report_contract_invalid")
    )

    for report, expected_code in invalid_cases:
        _write_full_run_report(run_dir, report)
        response = client.get("/api/thesis/runs/run_report/report-full")
        assert response.status_code == 500
        assert response.get_json()["errors"][0]["code"] == expected_code


def test_report_full_rejects_evaluation_adversarial_payloads(tmp_path, monkeypatch):
    client, _routes, registry = _prepare_full_report_route(tmp_path, monkeypatch)
    run_dir = _register_full_report_run(registry, tmp_path)

    unknown_root_key = _full_run_report()
    unknown_root_key["unexpected_root_key"] = True
    unknown_root_key = _reseal_full_run_report(unknown_root_key)

    fabricated_comparison = _full_run_report()
    fabricated_comparison["metrics"][0]["comparison"].update(
        {
            "status": "available",
            "baseline_arm_id": "final",
            "candidate_arm_id": "final",
            "delta": 0.0,
            "wins": 1,
            "ties": 0,
            "losses": 0,
        }
    )
    fabricated_comparison["claim"].update(
        {
            "status": "available",
            "verdict": "BETTER",
            "reason_codes": ["fabricated_comparison"],
        }
    )
    fabricated_comparison = _reseal_full_run_report(fabricated_comparison)

    non_finite_metric = _full_run_report()
    non_finite_metric["metrics"][0]["arm_values"][0]["value"] = float("nan")

    missing_report_hash = _full_run_report()
    del missing_report_hash["integrity"]["report_sha256"]

    for report in (
        unknown_root_key,
        fabricated_comparison,
        non_finite_metric,
        missing_report_hash,
    ):
        _write_full_run_report(run_dir, report)
        response = client.get("/api/thesis/runs/run_report/report-full")
        assert response.status_code == 500
        assert (
            response.get_json()["errors"][0]["code"]
            == "full_run_report_contract_invalid"
        )


def test_report_full_rejects_registered_run_dir_outside_jobs_root(tmp_path, monkeypatch):
    client, _routes, registry = _prepare_full_report_route(tmp_path, monkeypatch)
    outside_run_dir = tmp_path.parent / f"{tmp_path.name}-outside" / "run_report"
    _register_full_report_run(
        registry,
        tmp_path,
        run_dir=outside_run_dir,
    )

    response = client.get("/api/thesis/runs/run_report/report-full")

    assert response.status_code == 500
    assert response.get_json()["errors"][0]["code"] == "full_run_report_path_unsafe"


def test_report_full_one_arm_does_not_fabricate_comparison(tmp_path, monkeypatch):
    client, _routes, registry = _prepare_full_report_route(tmp_path, monkeypatch)
    run_dir = _register_full_report_run(registry, tmp_path)
    report = _full_run_report()
    _write_full_run_report(run_dir, report)

    response = client.get("/api/thesis/runs/run_report/report-full")

    persisted = response.get_json()["data"]["report"]
    assert response.status_code == 200
    assert [arm["arm_id"] for arm in persisted["arms"]] == ["final"]
    assert persisted["metrics"][0]["comparison"] == {
        "status": "not_applicable",
        "baseline_arm_id": None,
        "candidate_arm_id": None,
        "delta": None,
        "wins": None,
        "ties": None,
        "losses": None,
    }
    assert persisted["claim"]["verdict"] == "NOT_APPLICABLE"


def test_report_full_s0_s1_delta_and_verdict_are_relayed_exactly(tmp_path, monkeypatch):
    client, _routes, registry = _prepare_full_report_route(tmp_path, monkeypatch)
    run_dir = _register_full_report_run(registry, tmp_path)
    report = _full_run_report(fixture_name="full_run_s0_s1.json")
    _write_full_run_report(run_dir, report)

    response = client.get("/api/thesis/runs/run_report/report-full")

    persisted = response.get_json()["data"]["report"]
    assert response.status_code == 200
    assert [arm["arm_id"] for arm in persisted["arms"]] == ["s1", "s0"]
    assert persisted["metrics"][0]["comparison"]["delta"] == 12.0
    assert persisted["claim"]["verdict"] == "BETTER"


def test_report_full_preserves_persisted_null_usage_facts(tmp_path, monkeypatch):
    client, _routes, registry = _prepare_full_report_route(tmp_path, monkeypatch)
    run_dir = _register_full_report_run(registry, tmp_path)
    report = _full_run_report()
    _write_full_run_report(run_dir, report)

    response = client.get("/api/thesis/runs/run_report/report-full")

    usage = response.get_json()["data"]["report"]["usage"]
    assert response.status_code == 200
    assert usage["status"] == "unavailable"
    assert all(value is None for value in usage["totals"].values())


def test_report_full_isolated_by_project_logical_run_and_resume_attempt(tmp_path, monkeypatch):
    client, _routes, registry = _prepare_full_report_route(tmp_path, monkeypatch)
    shared_run_dir = tmp_path / "jobA" / "one_button" / "shared-report"
    _register_full_report_run(
        registry,
        tmp_path,
        run_id="run_logical",
        job_id="jobA",
        run_dir=shared_run_dir,
    )
    _register_full_report_run(
        registry,
        tmp_path,
        run_id="run_attempt",
        job_id="jobA",
        run_dir=shared_run_dir,
    )
    _register_full_report_run(
        registry,
        tmp_path,
        run_id="run_outsider",
        job_id="jobA",
        run_dir=shared_run_dir,
    )
    _register_full_report_run(
        registry,
        tmp_path,
        run_id="run_other_project",
        job_id="jobB",
        run_dir=shared_run_dir,
    )
    report = _full_run_report(
        logical_run_id="run_logical",
        attempt_run_ids=["run_logical", "run_attempt"],
    )
    _write_full_run_report(shared_run_dir, report)

    assert client.get("/api/thesis/runs/run_logical/report-full").status_code == 200
    assert client.get("/api/thesis/runs/run_attempt/report-full").status_code == 200

    outsider = client.get("/api/thesis/runs/run_outsider/report-full")
    assert outsider.status_code == 500
    assert outsider.get_json()["errors"][0]["code"] == "full_run_report_identity_mismatch"

    wrong_project = client.get("/api/thesis/runs/run_other_project/report-full")
    assert wrong_project.status_code == 500
    assert wrong_project.get_json()["errors"][0]["code"] == "full_run_report_identity_mismatch"

    unknown = client.get("/api/thesis/runs/run_unknown/report-full")
    assert unknown.status_code == 404
    assert unknown.get_json()["errors"][0]["code"] == "run_not_found"


def test_runs_endpoint_refreshes_registry_written_by_replay_process(tmp_path, monkeypatch):
    monkeypatch.setenv("THESIS_JOBS_ROOT", str(tmp_path))
    monkeypatch.setenv("THESIS_TOOL_ROOT", str(TOOL_ROOT))
    monkeypatch.setenv("THESIS_TOOL_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("THESIS_APP_MODE", "cockpit")

    _reset_app_modules()
    app_module = importlib.import_module("app")
    routes = importlib.import_module("routes.thesis_runs")
    from services.thesis_runs import RunRegistry

    backend_registry = RunRegistry(runs_root=tmp_path)
    routes.set_registry(backend_registry)
    client = app_module.create_app().test_client()

    replay_registry = RunRegistry(runs_root=tmp_path)
    event_dir = tmp_path / "run_events"
    event_dir.mkdir(exist_ok=True)
    event_path = event_dir / "replay_external.jsonl"
    event_path.write_text(json.dumps({"event": "run_start", "seq": 1}) + "\n", encoding="utf-8")
    replay_registry.create_run(
        script="snapshot_runs",
        argv=[sys.executable, "-c", "pass"],
        run_id="replay_external",
        event_log_path=str(event_path),
    )
    replay_registry.update_run("replay_external", status="done", exit_code=0)

    runs = client.get("/api/thesis/runs").get_json()["data"]
    assert any(row["run_id"] == "replay_external" for row in runs)
    events = client.get("/api/thesis/runs/replay_external/events?offset=0").get_json()["data"]
    assert events["events"][0]["event"] == "run_start"


def test_version_endpoint_reads_version_file_and_git_sha_field(tmp_path, monkeypatch):
    monkeypatch.setenv("THESIS_JOBS_ROOT", str(tmp_path / "jobs"))
    monkeypatch.setenv("THESIS_TOOL_ROOT", str(TOOL_ROOT))
    monkeypatch.setenv("THESIS_TOOL_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("THESIS_APP_MODE", "cockpit")
    (tmp_path / "VERSION").write_text("9.8.7\n", encoding="utf-8")

    _reset_app_modules()
    app_module = importlib.import_module("app")
    monkeypatch.setattr(app_module, "HANDOFF_ROOT", tmp_path)
    client = app_module.create_app().test_client()

    resp = client.get("/api/version")
    assert resp.status_code == 200
    data = resp.get_json()["data"]
    assert data["version"] == "9.8.7"
    assert data["backend_version"] == "9.8.7"
    assert "git_sha" in data
    assert data["event_schema"] == "one_button_event_v1"


def test_route_allow_api_without_job_id_rejected(tmp_path, monkeypatch):
    db_path = _make_doc_db(tmp_path)
    monkeypatch.setenv("THESIS_JOBS_ROOT", str(tmp_path))
    monkeypatch.setenv("THESIS_TOOL_ROOT", str(TOOL_ROOT))
    monkeypatch.setenv("THESIS_TOOL_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("THESIS_APP_MODE", "cockpit")

    _reset_app_modules()
    app_module = importlib.import_module("app")
    client = app_module.create_app().test_client()
    resp = client.post(
        "/api/thesis/runs",
        json={
            "script": "run_translate",
            "db": str(db_path),
            "chapters": ["ch02"],
            "configs": ["S1"],
            "allow_api": True,
            "confirm_token": "anything",
        },
    )
    assert resp.status_code == 400
    assert resp.get_json()["errors"][0]["code"] == "job_id_required"


def test_runs_endpoint_separate_from_readmodels(tmp_path, monkeypatch):
    from tests.test_thesis_observability import create_observability_fixture
    from tests.test_thesis_scores import _create_d2l_fixture

    create_observability_fixture(tmp_path)
    _create_d2l_fixture(tmp_path / "reports")
    monkeypatch.setenv("THESIS_JOBS_ROOT", str(tmp_path))
    monkeypatch.setenv("THESIS_REPORTS_ROOT", str(tmp_path / "reports"))
    monkeypatch.setenv("THESIS_TOOL_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("THESIS_APP_MODE", "cockpit")

    _reset_app_modules()
    app_module = importlib.import_module("app")
    client = app_module.create_app().test_client()

    runs_response = client.get("/api/thesis/runs")
    assert runs_response.status_code == 200
    runs_data = runs_response.get_json()["data"]
    assert isinstance(runs_data, list)
    assert "blocks" not in runs_response.get_json()
    assert "calls" not in runs_response.get_json()
    assert "headline" not in runs_response.get_json()
    assert "drift" not in runs_response.get_json()

    assert client.get("/api/thesis/datasets/fixture_job").status_code == 200
    assert client.get("/api/thesis/scores/d2l_p1").status_code == 200
    assert client.get("/api/thesis/observability/fixture_job").status_code == 200


def test_d2l_scope_warning_when_job_id_differs(tmp_path):
    from services.thesis_scores import load_scores
    from tests.test_thesis_scores import _create_d2l_fixture

    _create_d2l_fixture(tmp_path)
    data = load_scores("d2l_p1", reports_root=tmp_path)
    assert "scope_warning" in data["meta"]
    assert "d2l_p1" in data["meta"]["scope_warning"]
    assert "d2l_p3" in data["meta"]["scope_warning"]


def test_d2l_no_scope_warning_when_matching(tmp_path):
    from services.thesis_scores import load_scores
    from tests.test_thesis_scores import _create_d2l_fixture

    _create_d2l_fixture(tmp_path)
    data = load_scores("d2l_p3", reports_root=tmp_path)
    assert "scope_warning" not in data["meta"]


def test_ti_scope_warning_when_job_id_differs(tmp_path):
    from services.thesis_scores import load_scores
    from tests.test_thesis_scores import _create_ti_fixture

    _create_ti_fixture(tmp_path)
    data = load_scores("treasure_island_p2", reports_root=tmp_path)
    assert "scope_warning" in data["meta"]


def test_ti_drift_has_status_source_and_target_term_kind(tmp_path):
    from services.thesis_scores import load_scores
    from tests.test_thesis_scores import _create_ti_fixture

    _create_ti_fixture(tmp_path)
    data = load_scores("treasure_island_p2", reports_root=tmp_path)
    assert data["drift"]
    for item in data["drift"]:
        assert item["status_source"] == "derived_from_coverage"
        assert item["target_term_kind"] == "entity_id"


def test_d2l_per_chapter_no_dead_d_branch(tmp_path):
    from services.thesis_scores import load_scores
    from tests.test_thesis_scores import _create_d2l_fixture

    _create_d2l_fixture(tmp_path)
    data = load_scores("d2l_p1", reports_root=tmp_path)
    assert "B_S0" in data["per_chapter"]
    assert "B_S1" in data["per_chapter"]
    for key in data["per_chapter"]:
        assert key.startswith("B_")


def test_route_d2l_launch_returns_component_identity_without_server_paths(
    tmp_path,
    monkeypatch,
):
    client, routes, registry = _prepare_full_report_route(tmp_path, monkeypatch)
    spawned: list[str] = []
    frozen: list[str] = []
    monkeypatch.setattr(
        routes,
        "spawn_run",
        lambda _registry, run_id: spawned.append(run_id),
    )
    monkeypatch.setattr(
        routes,
        "freeze_managed_runtime_for_run",
        lambda _job_id, run_id, **_kwargs: frozen.append(run_id),
    )
    monkeypatch.setattr(
        routes,
        "_d2l_preview_source",
        lambda **_kwargs: {
            "source_binding_sha256": "A" * 64,
            "campaign_config_sha256": "B" * 64,
        },
    )
    monkeypatch.setattr(
        routes,
        "_d2l_launch_binding_sha256",
        lambda **_kwargs: "C" * 64,
    )

    request_payload = {
        "script": "run_d2l_project_campaign",
        "job_id": "jobA",
        "planned_run_id": "run_d2l_route",
        "chapters": [
            "d2l_preliminaries",
            "d2l_linear_networks",
        ],
        "profile": "technical_d2l_v1",
        "hard_total_token_cap": 6_000_000,
        "allow_api": False,
    }
    response = client.post("/api/thesis/runs", json=request_payload)

    assert response.status_code == 201
    data = response.get_json()["data"]
    assert data == {
        "run_id": "run_d2l_route",
        "script": "run_d2l_project_campaign",
        "job_id": "jobA",
        "status": "pending",
        "workflow_run_id": "wf_run_d2l_route",
        "component_id": "translation",
        "component_run_id": "tr_run_d2l_route",
        "component_attempt_id": 1,
        "selected_chapter_ids": [
            "d2l_preliminaries",
            "d2l_linear_networks",
        ],
        "profile_id": "technical_d2l_v1",
        "resumed_from": None,
        "reused": False,
    }
    entry = registry.get_run("run_d2l_route")
    assert entry is not None
    assert entry["argv"][3] == "app-run"
    assert entry["argv"].count("--chapter-id") == 2
    assert "--db" not in entry["argv"]
    assert "--dry-run" in entry["argv"]
    assert spawned == ["run_d2l_route"]
    assert frozen == ["run_d2l_route"]

    detail = client.get("/api/thesis/runs/run_d2l_route").get_json()["data"]
    assert detail["run_dir"] is None
    assert detail["manifest_path"] is None
    assert detail["event_log_path"] is None
    assert detail["log_path"] is None
    assert detail["prompt_preview_token"] is None
    assert detail["registry_status"] == "pending"
    assert detail["component"]["validation"]["state"] == "not_ready"
    assert detail["component"]["transition"]["state"] == "not_ready"
    listed = client.get("/api/thesis/runs").get_json()["data"]
    listed_row = next(row for row in listed if row["run_id"] == "run_d2l_route")
    assert listed_row["registry_status"] == "pending"
    assert listed_row["run_dir"] is None
    assert listed_row["component"]["component_status"] == "not_ready"
    events = client.get("/api/thesis/runs/run_d2l_route/events").get_json()["data"]
    assert events["events"] == []
    assert events["event_log_path"] is None
    assert events["component_events_withheld"] is True

    reused = client.post("/api/thesis/runs", json=request_payload)
    assert reused.status_code == 200
    assert reused.get_json()["data"]["reused"] is True
    assert spawned == ["run_d2l_route"]
    assert frozen == ["run_d2l_route"]


def test_route_d2l_live_estimate_cannot_bypass_dec064_server_lock(
    tmp_path,
    monkeypatch,
):
    client, routes, registry = _prepare_full_report_route(tmp_path, monkeypatch)
    service = importlib.import_module("services.thesis_runs")
    chapters = ["d2l_preliminaries", "d2l_linear_networks"]
    preview = _fake_d2l_preview(chapters)
    launch_binding = "C" * 64
    spawned: list[str] = []
    frozen: list[str] = []

    for module in (service, routes):
        monkeypatch.setattr(module, "_d2l_preview_source", lambda **_kwargs: preview)
        monkeypatch.setattr(module, "_d2l_credential_files", lambda **_kwargs: {})
        monkeypatch.setattr(
            module,
            "_d2l_launch_binding_sha256",
            lambda **_kwargs: launch_binding,
        )
    monkeypatch.setattr(
        routes,
        "spawn_run",
        lambda _registry, run_id: spawned.append(run_id),
    )
    monkeypatch.setattr(
        routes,
        "freeze_managed_runtime_for_run",
        lambda _job_id, run_id, **_kwargs: frozen.append(run_id),
    )

    estimate = client.get(
        "/api/thesis/runs/estimate-preview"
        "?job_id=jobA"
        "&script=run_d2l_project_campaign"
        "&chapters=d2l_preliminaries,d2l_linear_networks"
        "&profile=technical_d2l_v1"
        "&hard_total_token_cap=6000000"
        "&planned_run_id=run_d2l_live"
    )
    assert estimate.status_code == 200
    estimate_data = estimate.get_json()["data"]
    assert estimate_data["workflow_run_id"] == "wf_run_d2l_live"
    assert estimate_data["component_run_id"] == "tr_run_d2l_live"
    assert estimate_data["selected_chapter_ids"] == chapters
    assert estimate_data["run_dir"] is None
    assert estimate_data["manifest_path"] is None
    assert estimate_data["event_log_path"] is None
    assert estimate_data["cost_usd"] is None

    request_payload = {
        "script": "run_d2l_project_campaign",
        "job_id": "jobA",
        "planned_run_id": "run_d2l_live",
        "chapters": chapters,
        "profile": "technical_d2l_v1",
        "hard_total_token_cap": 6_000_000,
        "allow_api": True,
        "confirm_token": estimate_data["confirm_token"],
    }
    launched = client.post("/api/thesis/runs", json=request_payload)
    assert launched.status_code == 403
    assert (
        launched.get_json()["errors"][0]["code"]
        == "direct_d2l_live_start_disabled"
    )
    assert spawned == []
    assert frozen == []
    assert registry.get_run("run_d2l_live") is None

    retried = client.post("/api/thesis/runs", json=request_payload)
    assert retried.status_code == 403
    assert (
        retried.get_json()["errors"][0]["code"]
        == "direct_d2l_live_start_disabled"
    )
    assert spawned == []
    assert frozen == []
    assert registry.get_run("run_d2l_live") is None


def test_route_d2l_component_snapshot_relays_only_validated_package(
    tmp_path,
    monkeypatch,
):
    client, _routes, registry = _prepare_full_report_route(tmp_path, monkeypatch)
    fixture = (
        TOOL_ROOT
        / "pipeline"
        / "tests"
        / "fixtures"
        / "d2l_console_replay_v1"
        / "translation_component"
    )
    campaign_root = tmp_path / "_work" / "d2l_campaign" / "jobA" / "run_snapshot"
    component_root = campaign_root / "component"
    shutil.copytree(fixture, component_root)
    manifest = json.loads(
        (component_root / "component_manifest.json").read_text(encoding="utf-8")
    )
    from pipeline.prepass.d2l_console_replay_contract_v1 import canonical_sha256

    registry.create_run(
        script="run_d2l_project_campaign",
        argv=[sys.executable, "-c", "pass"],
        run_id="run_snapshot",
        job_id="jobA",
        run_dir=str(campaign_root),
        manifest_path=str(component_root / "component_manifest.json"),
        event_log_path=str(component_root / "events.jsonl"),
        workflow_run_id=manifest["workflow_run_id"],
        component_id="translation",
        component_run_id=manifest["component_run_id"],
        component_attempt_id=manifest["component_attempt_id"],
        selected_chapter_ids=manifest["selected_chapter_ids"],
        profile_id="technical_d2l_v1",
        source_binding_sha256=canonical_sha256(manifest["source_binding"]),
    )
    registry.update_run("run_snapshot", status="done", exit_code=0)

    response = client.get("/api/thesis/runs/run_snapshot/component-snapshot")
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["schema"] == "thesis_component_snapshot_read_v1"
    assert data["validation"]["terminal_event"] == "run_done"
    assert data["component_manifest"] == manifest
    assert data["events"]
    assert data["artifact_index"]["artifacts"]
    assert data["scoring_handoff_fragment"]["schema"] == "scoring_handoff_fragment_v1"
    assert data["transition"]["state"] == "ready_for_relay"

    terminal_resume = client.post(
        "/api/thesis/runs/run_snapshot/resume",
        json={},
    )
    assert terminal_resume.status_code == 409
    assert terminal_resume.get_json()["errors"][0]["code"] == "resume_terminal_run"

    registry.update_run("run_snapshot", workflow_run_id="wf_foreign")
    rejected = client.get("/api/thesis/runs/run_snapshot/component-snapshot")
    assert rejected.status_code == 409
    assert rejected.get_json()["errors"][0]["code"] == "d2l_component_not_ready"
    assert rejected.get_json()["data"] is None


def _register_parent_workflow_fixture(tmp_path, registry):
    from pipeline.tests.test_workflow_replay_relay_v1 import (
        COMMIT,
        CREATED_AT,
        JOB_ID,
        WORKFLOW_ID,
        Clock,
        FixtureAdapter,
        _source_bindings,
        _stages,
        _write_snapshot,
    )
    from pipeline.workflow_replay.relay_v1 import WorkflowRelayV1
    from services.workflow_replay import workflow_replay_root

    root = workflow_replay_root(
        jobs_root=tmp_path,
        job_id=JOB_ID,
        workflow_run_id=WORKFLOW_ID,
    )
    relay = WorkflowRelayV1(
        root,
        workflow_run_id=WORKFLOW_ID,
        job_id=JOB_ID,
        source_package_bindings=_source_bindings(),
        stages=_stages(),
        code_commit=COMMIT,
        created_at=CREATED_AT,
        clock=Clock(),
    )
    component_root = tmp_path / "fixture_component"
    snapshot = _write_snapshot(
        component_root,
        component_id="translation",
        run_id="translation_fixture_v1",
        local_stage="translate",
    )
    relay.ingest_component(component_root, adapter=FixtureAdapter(snapshot))
    registry.create_run(
        script="run_d2l_project_campaign",
        argv=[sys.executable, "-c", "pass"],
        run_id="run_parent_replay",
        job_id=JOB_ID,
        workflow_run_id=WORKFLOW_ID,
        component_id="translation",
        component_run_id="translation_fixture_v1",
        component_attempt_id=1,
    )
    return root


def test_route_workflow_replay_reads_validated_parent_cursor_and_artifact(
    tmp_path,
    monkeypatch,
):
    client, _routes, registry = _prepare_full_report_route(tmp_path, monkeypatch)
    _register_parent_workflow_fixture(tmp_path, registry)

    response = client.get(
        "/api/thesis/runs/run_parent_replay/workflow-replay"
        "?after_seq=0&wait_ms=0"
    )
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["schema"] == "workflow_replay_read_v1"
    assert data["run_id"] == "run_parent_replay"
    assert data["workflow_run_id"] == "workflow_fixture_v1"
    assert [event["seq"] for event in data["events"]] == [1, 2, 3, 4]
    assert data["cursor"]["latest_seq"] == 4
    assert data["cursor"]["returned_through_seq"] == 4
    assert data["cursor"]["through_seq"] == 4
    assert data["cursor"]["event_chain_head_sha256"] == (
        data["events"][-1]["integrity"]["event_sha256"]
    )
    assert data["cursor"]["package_revision_sha256"] == (
        data["manifest"]["integrity"]["manifest_sha256"]
    )
    assert data["cursor"]["terminal"] is False
    assert data["actions"]["replay"]["allowed"] is True
    assert data["actions"]["pause"]["allowed"] is True
    assert data["actions"]["score"]["allowed"] is False
    assert "workflow_artifact_missing" in data["actions"]["score"][
        "blocking_reasons"
    ]
    assert data["usage"] is None

    empty = client.get(
        "/api/thesis/runs/run_parent_replay/workflow-replay",
        query_string={"after_seq": 4, "wait_ms": 0},
    )
    assert empty.status_code == 200
    assert empty.get_json()["data"]["events"] == []

    artifact_ref = data["artifact_index"]["artifacts"][0]["binding"]["artifact_ref"]
    artifact = client.get(
        "/api/thesis/runs/run_parent_replay/workflow-replay/artifact",
        query_string={"artifact_ref": artifact_ref},
    )
    assert artifact.status_code == 200
    assert artifact.mimetype == "application/octet-stream"
    assert artifact.data == b'{"ok":true}'
    assert "attachment" in artifact.headers["Content-Disposition"]
    assert data["artifact_links"][artifact_ref].endswith(
        "artifact_ref=components%2Ftranslation%2Ftranslation_fixture_v1"
        "%2Fartifacts%2Ftranslation_output"
    )


def test_route_workflow_score_reports_parent_readiness_blockers(
    tmp_path,
    monkeypatch,
):
    client, _routes, registry = _prepare_full_report_route(
        tmp_path, monkeypatch
    )
    _register_parent_workflow_fixture(tmp_path, registry)

    blocked = client.post(
        "/api/thesis/runs/run_parent_replay/score",
        json={},
    )
    assert blocked.status_code == 409
    assert blocked.get_json()["errors"][0]["code"] == (
        "workflow_scoring_not_ready"
    )
    assert "workflow_artifact_missing" in blocked.get_json()["errors"][0][
        "message"
    ]

    malformed = client.post(
        "/api/thesis/runs/run_parent_replay/score",
        json={"force": True},
    )
    assert malformed.status_code == 400
    assert malformed.get_json()["errors"][0]["code"] == (
        "workflow_score_body_invalid"
    )


def test_workflow_actions_require_materialized_settings_and_live_source(
    tmp_path,
    monkeypatch,
):
    client, _routes, registry = _prepare_full_report_route(
        tmp_path, monkeypatch
    )
    root = _register_parent_workflow_fixture(tmp_path, registry)
    workflow_service = importlib.import_module("services.workflow_replay")
    manifest = json.loads(
        (root / "workflow_manifest.json").read_text(encoding="utf-8")
    )
    entry = registry.get_run("run_parent_replay")
    entry["source_binding_sha256"] = "a" * 64
    monkeypatch.setattr(
        workflow_service,
        "_scoring_runtime_readiness",
        lambda **_kwargs: {
            "allowed": True,
            "blockers": [],
            "runtime": {"status": "ready"},
        },
    )
    scope = {
        "scoring_handoff_status": "validated",
        "settings_status": "materialized",
        "settings_sha256": "b" * 64,
    }

    actions = workflow_service._actions(
        entry,
        manifest,
        jobs_root=tmp_path,
        evaluation_scope=scope,
        source_mode="live",
    )
    assert actions["score"]["allowed"] is True

    pending = workflow_service._actions(
        entry,
        manifest,
        jobs_root=tmp_path,
        evaluation_scope={
            **scope,
            "settings_status": "pending_settings_materialization",
            "settings_sha256": None,
        },
        source_mode="live",
    )
    assert pending["score"]["allowed"] is False
    assert "evaluation_settings_not_materialized" in pending["score"][
        "blocking_reasons"
    ]

    replay = workflow_service._actions(
        entry,
        manifest,
        jobs_root=tmp_path,
        evaluation_scope=scope,
        source_mode="replay",
    )
    assert replay["pause"]["allowed"] is False
    assert replay["resume"]["allowed"] is False
    assert replay["score"]["allowed"] is False
    assert "historical_replay_read_only" in replay["score"][
        "blocking_reasons"
    ]


def test_workflow_usage_read_model_projects_validated_d2l_snapshots():
    from pipeline.prepass.d2l_console_replay_contract_v1 import (
        build_component_usage_snapshot,
    )
    from services.workflow_replay import (
        WorkflowReplayError,
        _usage_read_model,
    )

    usage = {
        "logical_request_id": "request_1",
        "physical_attempt_index": 1,
        "provider_id": "provider",
        "model_id": "model",
        "source_id": "source",
        "masked_quota_bucket": "bucket-***",
        "prompt_tokens": 10,
        "completion_tokens": 2,
        "cached_input_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 12,
        "latency_ms": 10,
        "finish_reason": "stop",
        "cost_usd": None,
        "currency": None,
        "cost_status": "unknown",
        "cache_status": "miss",
        "cache_mechanism": "local_exact_cache",
    }
    accepted = {
        "identity_kind": "provider_attempt",
        "attempt_usage_id": "attempt_1",
        "cache_observation_id": "cache_attempt_1",
        "logical_request_id": "request_1",
        "semantic_attempt_index": 1,
        "transport_retry_ordinal": 0,
        "physical_attempt_index": 1,
        "provider_called": True,
        "source_revision": "source_v1",
        "usage": usage,
    }
    first = build_component_usage_snapshot(
        previous_snapshots=[],
        workflow_run_id="workflow_usage_v1",
        component_run_id="translation_usage_v1",
        component_attempt_id=1,
        stage_id="translator",
        work_id="window_1",
        accepted_usage=accepted,
    )
    final = build_component_usage_snapshot(
        previous_snapshots=[first],
        workflow_run_id="workflow_usage_v1",
        component_run_id="translation_usage_v1",
        component_attempt_id=1,
        stage_id=None,
        work_id=None,
        accepted_usage=None,
        component_final=True,
    )

    def parent_event(payload, component_seq):
        return {
            "event": "usage_snapshot",
            "component": {
                "component_id": "translation",
                "component_run_id": "translation_usage_v1",
                "component_attempt_id": 1,
                "component_attempt_index": 1,
                "component_seq": component_seq,
            },
            "payload": payload,
        }

    events = [parent_event(first, 10), parent_event(final, 11)]
    projected = _usage_read_model(
        events=events,
        typed_artifacts=[],
        workflow_run_id="workflow_usage_v1",
    )

    assert projected is not None
    assert projected["workflow_run_id"] == "workflow_usage_v1"
    assert projected["validated"] is True
    assert projected["workflow_total"] is None
    assert projected["calls"] == [
        {
            "call_id": "attempt_1",
            "attempt_usage_id": "attempt_1",
            "cache_observation_id": "cache_attempt_1",
            "component_id": "translation",
            "component_run_id": "translation_usage_v1",
            "component_attempt_id": 1,
            "component_attempt_index": 1,
            "component_seq": 10,
            "stage_id": "translation.translator",
            "work_id": "window_1",
            "logical_request_id": "request_1",
            "semantic_attempt_index": 1,
            "transport_retry_ordinal": 0,
            "physical_attempt_index": 1,
            "provider_id": "provider",
            "source_id": "source",
            "source_revision": "source_v1",
            "requested_model_id": "model",
            "observed_model_id": None,
            "provider_call_avoided": False,
            "finish_reason": "stop",
            "usage": usage,
        }
    ]
    assert projected["stage_totals"][0]["stage_id"] == "translation.translator"
    assert projected["stage_totals"][0]["total_tokens"] == 12
    assert projected["component_totals"][0]["physical_call_count"] == 1
    assert projected["component_totals"][0]["cost_status"] == "unknown"
    assert projected["component_totals"][0]["cost_usd"] is None

    tampered = json.loads(json.dumps(events))
    tampered[1]["payload"]["previous_snapshot_sha256"] = "0" * 64
    with pytest.raises(WorkflowReplayError) as rejected:
        _usage_read_model(
            events=tampered,
            typed_artifacts=[],
            workflow_run_id="workflow_usage_v1",
        )
    assert rejected.value.code == "workflow_usage_invalid"


def test_workflow_usage_read_model_projects_validated_evaluation_snapshots():
    from pipeline.eval.evaluation_component_usage_v1 import (
        EvaluationComponentUsageTrackerV1,
    )
    from services.workflow_replay import (
        WorkflowReplayError,
        _usage_read_model,
    )

    workflow_run_id = "workflow_eval_usage_v1"
    component_run_id = "evaluation_usage_v1"
    component_attempt_id = "evalcomp_attempt_0001"
    stage_ids = ("preflight", "aggregation")
    target = {
        "source_id": "google-official",
        "source_revision": "row1-v3",
        "physical_quota_bucket_id": "gemini-free-row1",
        "requested_model_id": "gemini-3.5-flash",
        "observed_model_id": "gemini-3.5-flash",
    }
    usage = {
        "schema_version": "llm_attempt_usage_v1",
        "attempt_usage_id": "eval_usage_001",
        "seal_sha256": "1" * 64,
        "logical_request_id": "eval_request_001",
        "logical_request_sha256": "2" * 64,
        "semantic_attempt_index": 1,
        "transport_retry_ordinal": 0,
        "physical_attempt_index": 1,
        "request_id": "provider-request-001",
        "source_id": target["source_id"],
        "source_revision": target["source_revision"],
        "physical_quota_bucket_id": target["physical_quota_bucket_id"],
        "requested_model_id": target["requested_model_id"],
        "observed_model_id": target["observed_model_id"],
        "started_at_utc": "2026-07-23T00:00:00.000Z",
        "finished_at_utc": "2026-07-23T00:00:00.100Z",
        "latency_ms": 100,
        "outcome": "succeeded",
        "finish_reason": "stop",
        "prompt_tokens": 10,
        "cached_input_tokens": 0,
        "completion_tokens": 5,
        "reasoning_tokens": 0,
        "total_tokens": 15,
        "cost_usd": None,
        "cost_status": "unknown",
        "cost_provenance": {
            "kind": "unavailable",
            "reference_id": None,
            "reference_sha256": None,
        },
        "provider_usage_sha256": "3" * 64,
        "error_id": None,
    }
    cache = {
        "schema_version": "cache_observation_v1",
        "observation_id": "eval_cache_001",
        "seal_sha256": "4" * 64,
        "logical_request_id": "eval_request_001",
        "logical_request_sha256": "2" * 64,
        "attempt_usage_id": None,
        "cache_kind": "none",
        "cache_namespace": "evaluation.cache.fixture",
        "cache_key_sha256": None,
        "lookup_status": "not_checked",
        "provider_call_avoided": False,
        "provider_cached_input_tokens": None,
        "reused_artifact_sha256": None,
        "producer_seal_sha256": None,
        "producer_input_bindings_sha256": None,
        "producer_artifact_receipt_sha256": None,
        "observed_at_utc": "2026-07-23T00:00:00.101Z",
    }
    tracker = EvaluationComponentUsageTrackerV1(
        workflow_run_id=workflow_run_id,
        component_run_id=component_run_id,
        stage_ids=stage_ids,
    )
    first = tracker.accept_usage(
        usage,
        stage_id="preflight",
        role_id="evaluation.sf_bt.semantic_judge",
        source_ledger_ref="ledgers/shared_llm_attempts.sqlite",
        execution_target=target,
        component_attempt_id=component_attempt_id,
        component_attempt_index=1,
        accepted_through_component_seq=3,
        current_work_id="chapter_001",
        generated_at="2026-07-23T00:00:01Z",
    )
    second = tracker.accept_cache_observation(
        cache,
        stage_id="preflight",
        role_id="evaluation.sf_bt.semantic_judge",
        source_ledger_ref="ledgers/shared_llm_attempts.sqlite",
        execution_target=target,
        component_attempt_id=component_attempt_id,
        component_attempt_index=1,
        accepted_through_component_seq=4,
        current_work_id="chapter_001",
        generated_at="2026-07-23T00:00:02Z",
    )
    final = tracker.finalize(
        stage_id="aggregation",
        component_attempt_id=component_attempt_id,
        component_attempt_index=1,
        accepted_through_component_seq=5,
        generated_at="2026-07-23T00:00:03Z",
    )
    snapshots = [first, second, final]
    assert all(snapshot is not None for snapshot in snapshots)

    events = []
    artifacts = []
    for snapshot in snapshots:
        digest = snapshot["integrity"]["usage_snapshot_sha256"]
        local_binding = {
            "artifact_ref": f"usage_snapshots/{digest}.json",
            "artifact_kind": "evaluation_component_usage_snapshot_v1",
            "schema_version": "1.0.0",
            "sha256": digest,
            "sha256_kind": (
                "canonical:EvaluationComponentUsageSnapshotV1@1.0.0"
            ),
        }
        events.append(
            {
                "event": "usage_snapshot",
                "component": {
                    "component_id": "evaluation",
                    "component_run_id": component_run_id,
                    "component_attempt_id": component_attempt_id,
                    "component_attempt_index": 1,
                    "component_seq": snapshot[
                        "accepted_through_component_seq"
                    ],
                },
                "payload": {"snapshot": local_binding},
            }
        )
        artifacts.append(
            {
                "binding": {
                    **local_binding,
                    "artifact_ref": (
                        f"components/evaluation/{component_run_id}/artifacts/"
                        f"usage_snapshots/{digest}.json"
                    ),
                },
                "body": snapshot,
            }
        )

    projected = _usage_read_model(
        events=events,
        typed_artifacts=artifacts,
        workflow_run_id=workflow_run_id,
    )
    assert projected is not None
    assert [row["call_id"] for row in projected["calls"]] == [
        "evaluation:eval_usage_001",
        "evaluation:eval_cache_001",
    ]
    assert projected["calls"][0]["stage_id"] == "evaluation.preflight"
    assert projected["calls"][0]["usage"]["total_tokens"] == 15
    assert projected["calls"][0]["usage"]["cost_usd"] is None
    assert projected["calls"][1]["usage"]["cache_status"] == "unknown"
    assert projected["component_totals"] == [
        {
            "component_id": "evaluation",
            "component_run_id": component_run_id,
            "component_attempt_id": component_attempt_id,
            "component_attempt_index": 1,
            "stage_id": None,
            "snapshot_seq": 3,
            "accepted_through_component_seq": 5,
            "physical_call_count": 1,
            "cache_observation_count": 1,
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "reasoning_tokens": 0,
            "cached_input_tokens": 0,
            "total_tokens": 15,
            "cache_hit_count": 0,
            "cache_miss_count": 0,
            "unknown_attempt_count": 0,
            "cost_status": "partial_unknown",
            "cost_usd": None,
            "currency": "USD",
            "snapshot_sha256": final["integrity"][
                "usage_snapshot_sha256"
            ],
        }
    ]

    tampered = json.loads(json.dumps(events))
    tampered[0]["payload"]["snapshot"]["sha256"] = "0" * 64
    with pytest.raises(WorkflowReplayError) as rejected:
        _usage_read_model(
            events=tampered,
            typed_artifacts=artifacts,
            workflow_run_id=workflow_run_id,
        )
    assert rejected.value.code == "workflow_usage_binding_drift"


def test_route_workflow_replay_long_poll_does_not_revalidate_unchanged_parent(
    tmp_path,
    monkeypatch,
):
    client, _routes, registry = _prepare_full_report_route(tmp_path, monkeypatch)
    _register_parent_workflow_fixture(tmp_path, registry)
    workflow_service = importlib.import_module("services.workflow_replay")
    original = workflow_service._load_validated_parent
    validation_calls = []

    def counted(*args, **kwargs):
        validation_calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(workflow_service, "_load_validated_parent", counted)
    response = client.get(
        "/api/thesis/runs/run_parent_replay/workflow-replay",
        query_string={"after_seq": 4, "wait_ms": 120},
    )

    assert response.status_code == 200
    assert response.get_json()["data"]["events"] == []
    assert len(validation_calls) == 1


def test_route_workflow_replay_rejects_unindexed_paths_and_parent_drift(
    tmp_path,
    monkeypatch,
):
    client, _routes, registry = _prepare_full_report_route(tmp_path, monkeypatch)
    root = _register_parent_workflow_fixture(tmp_path, registry)

    traversal = client.get(
        "/api/thesis/runs/run_parent_replay/workflow-replay/artifact",
        query_string={"artifact_ref": "../workflow_manifest.json"},
    )
    assert traversal.status_code == 404
    assert (
        traversal.get_json()["errors"][0]["code"]
        == "workflow_artifact_not_found"
    )

    unknown_query = client.get(
        "/api/thesis/runs/run_parent_replay/workflow-replay?offset=0"
    )
    assert unknown_query.status_code == 400
    assert (
        unknown_query.get_json()["errors"][0]["code"]
        == "workflow_replay_query_invalid"
    )

    artifact_index = json.loads(
        (root / "artifact_index.json").read_text(encoding="utf-8")
    )
    artifact_ref = artifact_index["artifacts"][0]["binding"]["artifact_ref"]
    artifact_path = root.joinpath(*artifact_ref.split("/"))
    artifact_path.write_bytes(b'{"ok":false}')

    drift = client.get(
        "/api/thesis/runs/run_parent_replay/workflow-replay?after_seq=0"
    )
    assert drift.status_code == 409
    assert drift.get_json()["errors"][0]["code"] == "workflow_replay_invalid"


def test_route_workflow_replay_rejects_missing_parent_and_invalid_cursor(
    tmp_path,
    monkeypatch,
):
    client, _routes, registry = _prepare_full_report_route(tmp_path, monkeypatch)
    registry.create_run(
        script="run_d2l_project_campaign",
        argv=[sys.executable, "-c", "pass"],
        run_id="run_missing_parent",
        job_id="job_fixture_v1",
        workflow_run_id="workflow_missing_v1",
    )

    missing = client.get(
        "/api/thesis/runs/run_missing_parent/workflow-replay"
    )
    assert missing.status_code == 409
    assert (
        missing.get_json()["errors"][0]["code"]
        == "workflow_replay_not_ready"
    )

    invalid = client.get(
        "/api/thesis/runs/run_missing_parent/workflow-replay?after_seq=-1"
    )
    assert invalid.status_code == 400
    assert (
        invalid.get_json()["errors"][0]["code"]
        == "workflow_replay_cursor_invalid"
    )


def _workflow_setup_project_fixture():
    from types import SimpleNamespace

    def binding(ref, kind):
        return {
            "artifact_ref": ref,
            "artifact_kind": kind,
            "schema_version": "1.0.0",
            "sha256": "a" * 64,
            "sha256_kind": "physical",
        }

    source_binding = {
        "schema": "canonical_source_binding_v1",
        "document": binding("source/document.json", "document"),
        "structure_manifest": binding(
            "source/structure_manifest.json", "structure_manifest"
        ),
        "asset_manifest": binding(
            "source/asset_manifest.json", "asset_manifest"
        ),
        "admitted_projection": binding(
            "source/admitted_projection_v1.json", "admitted_projection"
        ),
        "normalization_receipt": binding(
            "source/normalization_receipt.json", "normalization_receipt"
        ),
        "package_seal": binding(
            "source/source_lifecycle_v2.json", "source_package_seal"
        ),
    }
    status = {
        "project_id": "projectA",
        "job_id": "jobA",
        "managed": True,
        "prepared": True,
        "lifecycle": "finalized_pre_run",
        "source_identity_sha256": "b" * 64,
    }
    project = SimpleNamespace(
        block_rows=(
            {"chapter_id": "d2l_preliminaries", "block_id": "b1"},
            {"chapter_id": "d2l_preliminaries", "block_id": "b2"},
            {"chapter_id": "d2l_linear_networks", "block_id": "b3"},
        ),
        chapter_rows=(
            {"chapter_id": "d2l_preliminaries", "title": "Preliminaries"},
            {
                "chapter_id": "d2l_linear_networks",
                "title": "Linear Networks",
            },
        ),
        manifest={
            "translatable_chapter_ids": [
                "d2l_preliminaries",
                "d2l_linear_networks",
            ],
        },
        source_snapshot={"package_tree_sha256": "c" * 64},
        source_binding=source_binding,
    )
    return status, project


def _workflow_selection_request(
    *,
    mode="dry_run",
    chapter_ids=None,
    shared_option_id="shared_llm_transport_catalog_v1",
):
    return {
        "schema_id": "WorkflowSetupSelectionV1",
        "schema_version": "1.1.0",
        "execution_mode": mode,
        "chapter_ids": chapter_ids or ["d2l_preliminaries"],
        "shared_option_id": shared_option_id,
        "d2l_settings_option_id": "d2l_workflow_settings_v1",
        "evaluation": {
            "settings_option_id": "evaluation_workflow_settings_v1",
            "selected_chapter_ids": chapter_ids
            or ["d2l_preliminaries"],
            "selected_arm_ids": [
                "s0",
                "s1",
                "community",
                "google_nmt",
                "llm_lc",
            ],
            "selected_scorer_ids": ["sf_qe", "sf_bt", "pj"],
            "highlight_pair": {
                "baseline_arm_id": "s0",
                "candidate_arm_id": "s1",
            },
        },
        "hard_total_token_cap": 6_000_000,
        "reserved_cost_cap_usd": None,
    }


def _workflow_launch_request(preflight, *, job_id="jobA"):
    return {
        "schema_id": "WorkflowLaunchConfirmationV1",
        "schema_version": "1.0.0",
        "script": "run_workflow_orchestrator_v1",
        "job_id": job_id,
        "execution_mode": preflight["execution_mode"],
        "allow_api": preflight["execution_mode"] == "live",
        "workflow_preflight_id": preflight["launch"]["preflight_id"],
        "workflow_preflight_sha256": preflight["launch"][
            "preflight_sha256"
        ],
        "confirm_token": preflight["launch"]["confirm_token"],
        "planned_run_id": preflight["launch"]["planned_run_id"],
    }


def test_route_workflow_setup_allows_translation_and_blocks_unready_scoring(
    tmp_path,
    monkeypatch,
):
    client, routes, registry = _prepare_full_report_route(tmp_path, monkeypatch)
    workflow_service = importlib.import_module("services.workflow_replay")
    thesis_service = importlib.import_module("services.thesis_runs")
    status, project = _workflow_setup_project_fixture()
    monkeypatch.setattr(
        workflow_service,
        "_load_setup_project",
        lambda *_args, **_kwargs: (status, project),
    )
    preview = {
        **_fake_d2l_preview(["d2l_preliminaries"]),
        "source_binding": project.source_binding,
    }
    monkeypatch.setattr(
        thesis_service,
        "_d2l_preview_source",
        lambda **_kwargs: preview,
    )
    monkeypatch.setattr(
        routes,
        "_d2l_preview_source",
        lambda **_kwargs: preview,
    )

    setup_response = client.get("/api/projects/projectA/workflow-setup")
    assert setup_response.status_code == 200
    setup = setup_response.get_json()["data"]
    assert setup["schema_id"] == "WorkflowSetupV1"
    assert setup["schema_version"] == "1.0.0"
    assert setup["live_start_allowed"] is True
    assert setup["launch_phase"] == "translation"
    assert setup["scoring_start_allowed"] is False
    assert "workflow_artifact_missing" in setup[
        "scoring_blocking_reasons"
    ]
    assert "evaluation_app_executor_not_connected" in setup[
        "scoring_blocking_reasons"
    ]
    assert [row["chapter_id"] for row in setup["chapters"]] == [
        "d2l_preliminaries",
        "d2l_linear_networks",
    ]
    assert setup["chapters"][0]["block_count"] == 2
    assert setup["shared_options"][0]["option_id"] == (
        "shared_llm_transport_catalog_v1"
    )
    assert all(
        row["status"] == "missing"
        for row in setup["shared_options"][0]["credential_status"]
    )
    evaluation_option = setup["evaluation_settings_options"][0]
    assert evaluation_option["revision"] == "1.1.0"
    assert evaluation_option["selection_catalog"]["arm_ids"] == [
        "s0",
        "s1",
        "community",
        "google_nmt",
        "llm_lc",
    ]
    assert evaluation_option["selection_catalog"]["scorer_ids"] == [
        "sf_qe",
        "sf_bt",
        "pj",
    ]
    assert setup["defaults"]["evaluation"]["selected_chapter_ids"] == [
        "d2l_preliminaries",
        "d2l_linear_networks",
    ]
    assert "api_key" not in json.dumps(setup).lower()

    preflight_response = client.post(
        "/api/projects/projectA/workflow-setup/preflight",
        json=_workflow_selection_request(mode="live"),
    )
    assert preflight_response.status_code == 200
    preflight = preflight_response.get_json()["data"]
    assert preflight["schema_id"] == "WorkflowPreflightV1"
    assert preflight["schema_version"] == "1.0.0"
    assert preflight["start_allowed"] is False
    assert "workflow_credentials_unavailable" in preflight[
        "blocking_reasons"
    ]
    assert preflight["scoring_start_allowed"] is False
    assert preflight["bounds"]["cost_usd"] is None
    assert preflight["launch"]["confirm_token"]
    assert preflight["normalized_selection"]["schema_version"] == "1.1.0"
    evaluation_selection = preflight["normalized_selection"]["evaluation"]
    assert len(evaluation_selection["selection_sha256"]) == 64
    assert evaluation_selection["selected_arm_ids"] == [
        "s0",
        "s1",
        "community",
        "google_nmt",
        "llm_lc",
    ]
    assert preflight["evaluation_summary"]["settings_status"] == (
        "pending_scoring_handoff"
    )
    assert preflight["evaluation_summary"]["settings_sha256"] is None
    assert preflight["evaluation_summary"]["selection_sha256"] == (
        evaluation_selection["selection_sha256"]
    )

    launched = client.post(
        "/api/thesis/runs",
        json=_workflow_launch_request(preflight),
    )
    assert launched.status_code == 403
    assert (
        launched.get_json()["errors"][0]["code"]
        == "workflow_credentials_unavailable"
    )
    assert registry.list_runs() == []


def test_route_workflow_live_confirmation_launches_real_campaign_path(
    tmp_path,
    monkeypatch,
):
    client, routes, registry = _prepare_full_report_route(tmp_path, monkeypatch)
    workflow_service = importlib.import_module("services.workflow_replay")
    thesis_service = importlib.import_module("services.thesis_runs")
    status, project = _workflow_setup_project_fixture()
    monkeypatch.setattr(
        workflow_service,
        "_load_setup_project",
        lambda *_args, **_kwargs: (status, project),
    )
    monkeypatch.setattr(workflow_service, "LIVE_START_ALLOWED", True)
    monkeypatch.setattr(routes, "LIVE_START_ALLOWED", True)
    monkeypatch.setattr(
        workflow_service,
        "_credential_status",
        lambda: [
            {
                "credential_ref": "credential.shopaikey_gemini_proxy_v1",
                "status": "available",
            },
            {
                "credential_ref": "credential.modelapi_shared_v1",
                "status": "available",
            },
        ],
    )
    monkeypatch.setattr(
        workflow_service,
        "_issue_live_api_token",
        lambda **_kwargs: "sealed-api-gate-token",
    )
    preview = {
        **_fake_d2l_preview(["d2l_preliminaries"]),
        "source_binding": project.source_binding,
    }
    for module in (thesis_service, routes):
        monkeypatch.setattr(
            module,
            "_d2l_preview_source",
            lambda **_kwargs: preview,
        )
    monkeypatch.setattr(routes, "_d2l_credential_files", lambda **_kwargs: {})
    api_gate_calls = []

    def validate_gate(**kwargs):
        api_gate_calls.append(kwargs)
        return kwargs["confirm_token"]

    monkeypatch.setattr(routes, "validate_api_gate", validate_gate)
    spawned = []
    frozen = []
    monkeypatch.setattr(
        routes, "spawn_run", lambda _registry, run_id: spawned.append(run_id)
    )
    monkeypatch.setattr(
        routes,
        "freeze_managed_runtime_for_run",
        lambda _job_id, run_id, **_kwargs: frozen.append(run_id),
    )

    setup = client.get("/api/projects/projectA/workflow-setup")
    assert setup.status_code == 200
    assert setup.get_json()["data"]["live_start_allowed"] is True

    preflight_response = client.post(
        "/api/projects/projectA/workflow-setup/preflight",
        json=_workflow_selection_request(mode="live"),
    )
    assert preflight_response.status_code == 200
    preflight = preflight_response.get_json()["data"]
    assert preflight["start_allowed"] is True
    assert preflight["live_start_allowed"] is True
    assert preflight["blocking_reasons"] == []

    launch = client.post(
        "/api/thesis/runs",
        json=_workflow_launch_request(preflight),
    )
    assert launch.status_code == 201
    data = launch.get_json()["data"]
    assert spawned == [data["run_id"]]
    assert frozen == [data["run_id"]]
    assert api_gate_calls[0]["allow_api"] is True
    assert api_gate_calls[0]["confirm_token"] == "sealed-api-gate-token"
    entry = registry.get_run(data["run_id"])
    assert entry["script"] == "run_workflow_orchestrator_v1"
    assert entry["argv"][1:4] == [
        "-m",
        "pipeline.scripts.run_workflow_orchestrator_v1",
        "translate",
    ]
    assert "--parent-root" in entry["argv"]
    assert entry["allow_api"] is True
    assert "--live" in entry["argv"]
    assert "--dry-run" not in entry["argv"]
    assert entry["evaluation_selection_sha256"] == preflight[
        "normalized_selection"
    ]["evaluation"]["selection_sha256"]
    assert entry["evaluation_selection"]["selected_scorer_ids"] == [
        "sf_qe",
        "sf_bt",
        "pj",
    ]


def test_route_workflow_dry_preflight_launch_initializes_parent_without_api(
    tmp_path,
    monkeypatch,
):
    client, routes, registry = _prepare_full_report_route(tmp_path, monkeypatch)
    workflow_service = importlib.import_module("services.workflow_replay")
    thesis_service = importlib.import_module("services.thesis_runs")
    status, project = _workflow_setup_project_fixture()
    monkeypatch.setattr(
        workflow_service,
        "_load_setup_project",
        lambda *_args, **_kwargs: (status, project),
    )
    preview = {
        **_fake_d2l_preview(["d2l_preliminaries"]),
        "source_binding": project.source_binding,
    }
    for module in (thesis_service, routes):
        monkeypatch.setattr(
            module,
            "_d2l_preview_source",
            lambda **_kwargs: preview,
        )
    spawned = []
    frozen = []
    monkeypatch.setattr(
        routes, "spawn_run", lambda _registry, run_id: spawned.append(run_id)
    )
    monkeypatch.setattr(
        routes,
        "freeze_managed_runtime_for_run",
        lambda _job_id, run_id, **_kwargs: frozen.append(run_id),
    )

    preflight_response = client.post(
        "/api/projects/projectA/workflow-setup/preflight",
        json=_workflow_selection_request(),
    )
    assert preflight_response.status_code == 200
    preflight = preflight_response.get_json()["data"]
    assert preflight["start_allowed"] is True

    mismatched = client.post(
        "/api/thesis/runs",
        json=_workflow_launch_request(preflight, job_id="jobB"),
    )
    assert mismatched.status_code == 409
    assert (
        mismatched.get_json()["errors"][0]["code"]
        == "workflow_launch_binding_invalid"
    )
    assert registry.list_runs() == []
    assert spawned == []
    assert frozen == []

    parent_root = workflow_service.workflow_replay_root(
        jobs_root=tmp_path,
        job_id="jobA",
        workflow_run_id=preflight["identities"]["workflow_run_id"],
    )
    with monkeypatch.context() as scoped:
        def reject_api_gate(**_kwargs):
            raise routes.RunControlError(
                "api_gate_invalid",
                "Synthetic rejected gate.",
                403,
            )

        scoped.setattr(routes, "validate_api_gate", reject_api_gate)
        rejected = client.post(
            "/api/thesis/runs",
            json=_workflow_launch_request(preflight),
        )
    assert rejected.status_code == 403
    assert rejected.get_json()["errors"][0]["code"] == "api_gate_invalid"
    assert not parent_root.exists()
    assert registry.list_runs() == []
    assert spawned == []
    assert frozen == []

    launch = client.post(
        "/api/thesis/runs",
        json=_workflow_launch_request(preflight),
    )
    assert launch.status_code == 201
    data = launch.get_json()["data"]
    assert data["run_id"] == preflight["identities"]["planned_run_id"]
    assert data["workflow_run_id"] == preflight["identities"]["workflow_run_id"]
    assert spawned == [data["run_id"]]
    assert frozen == [data["run_id"]]
    assert registry.get_run(data["run_id"])["allow_api"] is False
    assert registry.get_run(data["run_id"])["script"] == (
        "run_workflow_orchestrator_v1"
    )

    parent = client.get(
        f"/api/thesis/runs/{data['run_id']}/workflow-replay"
        "?after_seq=0&wait_ms=0"
    )
    assert parent.status_code == 200
    parent_data = parent.get_json()["data"]
    assert parent_data["events"] == []
    assert parent_data["manifest"]["stages"][0]["stage_id"] == (
        "translation.preflight"
    )
    assert parent_data["manifest"]["stages"][-1]["stage_id"] == (
        "publication.export"
    )
    assert parent_data["evaluation_scope"]["settings_status"] == (
        "pending_scoring_handoff"
    )
    assert parent_data["evaluation_scope"]["settings_sha256"] is None
    assert parent_data["evaluation_scope"]["selection_sha256"] == preflight[
        "normalized_selection"
    ]["evaluation"]["selection_sha256"]


def test_route_workflow_preflight_rejects_unadvertised_fields_and_ids(
    tmp_path,
    monkeypatch,
):
    client, _routes, _registry = _prepare_full_report_route(tmp_path, monkeypatch)
    workflow_service = importlib.import_module("services.workflow_replay")
    status, project = _workflow_setup_project_fixture()
    monkeypatch.setattr(
        workflow_service,
        "_load_setup_project",
        lambda *_args, **_kwargs: (status, project),
    )
    base = _workflow_selection_request()

    extra = client.post(
        "/api/projects/projectA/workflow-setup/preflight",
        json={**base, "requested_model_id": "arbitrary-model"},
    )
    assert extra.status_code == 400
    assert (
        extra.get_json()["errors"][0]["code"]
        == "workflow_preflight_body_invalid"
    )

    wrong = client.post(
        "/api/projects/projectA/workflow-setup/preflight",
        json={**base, "shared_option_id": "client_fabricated"},
    )
    assert wrong.status_code == 400
    assert (
        wrong.get_json()["errors"][0]["code"]
        == "workflow_shared_settings_invalid"
    )

    invalid_cases = []
    old_schema = json.loads(json.dumps(base))
    old_schema["schema_version"] = "1.0.0"
    invalid_cases.append((old_schema, "workflow_preflight_schema_invalid"))

    reordered_arms = json.loads(json.dumps(base))
    reordered_arms["evaluation"]["selected_arm_ids"] = ["s1", "s0"]
    invalid_cases.append(
        (reordered_arms, "workflow_evaluation_arms_invalid")
    )

    one_arm = json.loads(json.dumps(base))
    one_arm["evaluation"]["selected_arm_ids"] = ["s0"]
    one_arm["evaluation"]["highlight_pair"] = None
    invalid_cases.append((one_arm, "workflow_evaluation_arms_invalid"))

    duplicate_scorer = json.loads(json.dumps(base))
    duplicate_scorer["evaluation"]["selected_scorer_ids"] = [
        "sf_qe",
        "sf_qe",
    ]
    invalid_cases.append(
        (duplicate_scorer, "workflow_evaluation_scorers_invalid")
    )

    outside_parent = json.loads(json.dumps(base))
    outside_parent["evaluation"]["selected_chapter_ids"] = [
        "d2l_linear_networks"
    ]
    invalid_cases.append(
        (outside_parent, "workflow_evaluation_chapters_invalid")
    )

    highlight_outside = json.loads(json.dumps(base))
    highlight_outside["evaluation"]["selected_arm_ids"] = ["s0", "s1"]
    highlight_outside["evaluation"]["highlight_pair"] = {
        "baseline_arm_id": "s0",
        "candidate_arm_id": "community",
    }
    invalid_cases.append(
        (highlight_outside, "workflow_highlight_pair_invalid")
    )

    for payload, code in invalid_cases:
        response = client.post(
            "/api/projects/projectA/workflow-setup/preflight",
            json=payload,
        )
        assert response.status_code == 400
        assert response.get_json()["errors"][0]["code"] == code
