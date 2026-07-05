"""Tests for APP-C01 RunControl.

These tests stay 0-API.  They do launch one real frozen pipeline script
(`snapshot_runs`) to prove module invocation and cwd are wired correctly.
"""
from __future__ import annotations

import importlib
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
TOOL_ROOT = BACKEND_ROOT.parents[1]
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
        ):
            sys.modules.pop(name, None)


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
    assert str(tmp_path / "jobA" / "one_button" / "run_onebtn" / "workdb.sqlite3") in preview_data["argv_preview"]

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
    assert spawned == [data["run_id"]]
    assert not (run_dir / "PAUSE").exists()
    new_entry = client.get(f"/api/thesis/runs/{data['run_id']}").get_json()["data"]
    assert new_entry["resumed_from"] == "run_resume"
    assert new_entry["attempt_index"] == 2
    assert new_entry["attempt_log_path"].endswith(f"{data['run_id']}.log")
    assert "--estimate-only" not in new_entry["argv"]
    assert "--run-id" not in new_entry["argv"]
    assert new_entry["argv"][-2:] == ["--resume", "run_resume"]


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
