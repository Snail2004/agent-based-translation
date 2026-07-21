"""Routes for thesis run control (APP-C01)."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from flask import Blueprint, request

from pipeline.eval.contracts_v1 import ContractValidationError
from pipeline.eval.full_run_report_v1 import (
    SCHEMA_ID as _FULL_RUN_REPORT_SCHEMA_ID,
    SCHEMA_VERSION as _FULL_RUN_REPORT_SCHEMA_VERSION,
    validate_full_run_report,
)
from routes.common import error, ok
from services.project_runtime import (
    ProjectRuntimeError,
    freeze_managed_runtime_for_run,
)
from services.thesis_runs import (
    RunControlError,
    RunRegistry,
    build_argv,
    build_resume_argv_from_entry,
    cancel_run,
    generate_estimate_preview,
    generate_prompt_preview,
    issue_estimate_token_for_argv,
    one_button_paths,
    read_events,
    read_log,
    resolve_job_db,
    spawn_run,
    validate_api_gate,
    validate_job_id,
    validate_run_id,
    validate_script,
)


bp = Blueprint("thesis_runs", __name__)

_FULL_RUN_REPORT_FILENAME = "full_run_report_v1.json"

_registry: RunRegistry | None = None


def _get_registry() -> RunRegistry:
    global _registry
    if _registry is None:
        from config import THESIS_JOBS_ROOT

        _registry = RunRegistry(runs_root=THESIS_JOBS_ROOT)
    return _registry


def set_registry(registry: RunRegistry) -> None:
    """Allow tests to inject a registry."""
    global _registry
    _registry = registry


def _validate_planned_run_reuse(
    existing: dict,
    *,
    expected: dict,
) -> None:
    identity_fields = (
        "job_id",
        "script",
        "argv",
        "cwd",
        "config",
        "configs",
        "seed",
        "model",
        "prompt_version",
        "cache_path",
        "experiment",
        "allow_api",
        "dry_run_policy",
        "event_log_path",
        "run_dir",
        "manifest_path",
        "attempt_index",
        "resumed_from",
    )
    mismatches = [
        field
        for field in identity_fields
        if existing.get(field) != expected.get(field)
    ]
    if mismatches:
        raise RunControlError(
            "planned_run_id_collision",
            "planned_run_id is already bound to a different run identity: "
            + ", ".join(mismatches),
            409,
        )


def _resolve_resume_root(registry: RunRegistry, entry: dict) -> dict:
    job_id = validate_job_id(entry.get("job_id"), required=True)
    lineage_fields = ("run_dir", "manifest_path", "event_log_path")
    lineage_identity = {field: entry.get(field) for field in lineage_fields}
    current = entry
    seen: set[str] = set()
    while True:
        try:
            current_id = validate_run_id(current.get("run_id"), required=True)
        except RunControlError as exc:
            raise RunControlError(
                "resume_ancestry_invalid",
                "Resume ancestry contains an invalid run_id.",
                409,
            ) from exc
        if current_id in seen:
            raise RunControlError(
                "resume_ancestry_invalid",
                "Resume ancestry contains a cycle.",
                409,
            )
        seen.add(current_id)
        if current.get("script") != "run_one_button" or current.get("job_id") != job_id:
            raise RunControlError(
                "resume_ancestry_invalid",
                "Resume ancestry crosses a job or script boundary.",
                409,
            )
        if any(
            current.get(field) != lineage_identity[field]
            for field in lineage_fields
        ):
            raise RunControlError(
                "resume_ancestry_invalid",
                "Resume ancestry crosses a run-artifact boundary.",
                409,
            )
        parent_id = current.get("resumed_from")
        if parent_id is None or parent_id == "":
            return current
        try:
            parent_id = validate_run_id(parent_id, required=True)
        except RunControlError as exc:
            raise RunControlError(
                "resume_ancestry_invalid",
                "Resume ancestry contains an invalid parent run_id.",
                409,
            ) from exc
        parent = registry.get_run(parent_id)
        if parent is None:
            raise RunControlError(
                "resume_ancestry_invalid",
                "Resume ancestry references a missing parent run.",
                409,
            )
        current = parent


@bp.post("/thesis/runs")
def create_run():
    try:
        body = request.get_json(force=True) or {}
        script = validate_script(body.get("script", ""))
        allow_api = bool(body.get("allow_api", False))
        job_id = validate_job_id(body.get("job_id"))
        python_exe = _python_exe()
        tool_root = _tool_root()
        jobs_root = _jobs_root()
        registry = _get_registry()
        db = resolve_job_db(db=body.get("db"), job_id=job_id, jobs_root=jobs_root)
        planned_run_id = validate_run_id(body.get("planned_run_id"))
        extra_args = _body_list(body, "extra_args")
        event_log_path = None
        run_dir = None
        manifest_path = None
        workdb = body.get("workdb")
        if script == "run_translate" and allow_api and job_id:
            if planned_run_id is None:
                raise RunControlError(
                    "planned_run_id_required",
                    "run_translate allow_api=true requires planned_run_id from prompt-preview.",
                    403,
                )
            event_log_path = jobs_root / "run_events" / f"{planned_run_id}.jsonl"
        if script == "run_one_button":
            job_id = validate_job_id(job_id, required=True)
            if allow_api and planned_run_id is None:
                raise RunControlError(
                    "planned_run_id_required",
                    "run_one_button allow_api=true requires planned_run_id from estimate-preview.",
                    403,
                )
            if planned_run_id is None:
                planned_run_id = registry.new_run_id()
            paths = one_button_paths(jobs_root=jobs_root, job_id=job_id, run_id=planned_run_id)
            event_log_path = paths["event_log_path"]
            run_dir = paths["run_dir"]
            manifest_path = paths["manifest_path"]
            workdb = workdb or str(paths["workdb"])
        if job_id and planned_run_id is None:
            planned_run_id = registry.new_run_id()

        argv = build_argv(
            script=script,
            python_exe=python_exe,
            job_id=job_id,
            db=db,
            source=body.get("source"),
            doc_id=body.get("doc_id"),
            chapters=_body_list(body, "chapters"),
            configs=_body_list(body, "configs"),
            config=body.get("config"),
            config_file=body.get("config_file") or body.get("llm_config"),
            profile=body.get("profile"),
            experiment=body.get("experiment"),
            cache=body.get("cache"),
            report=body.get("report"),
            workdb=workdb,
            budget_cap_usd=_body_float(body, "budget_cap_usd"),
            with_s0=bool(body.get("with_s0", False)),
            out=body.get("out"),
            prepass=body.get("prepass"),
            mode=body.get("mode"),
            compare=body.get("compare"),
            human=body.get("human"),
            project=body.get("project"),
            chroma=body.get("chroma"),
            gold_variants=body.get("gold_variants"),
            context_budget=body.get("context_budget"),
            extra_args=extra_args,
            allow_api=allow_api,
            freeze=bool(body.get("freeze", False)),
            memory_report=body.get("memory_report"),
            smoke_query=body.get("smoke_query"),
            event_log=str(event_log_path) if event_log_path else None,
            run_id=planned_run_id,
        )

        expected_run_identity = {
            "job_id": job_id,
            "script": script,
            "argv": argv,
            "cwd": str(tool_root),
            "config": body.get("config"),
            "configs": _body_list(body, "configs"),
            "seed": body.get("seed"),
            "model": body.get("model"),
            "prompt_version": body.get("prompt_version"),
            "cache_path": body.get("cache"),
            "experiment": body.get("experiment"),
            "allow_api": allow_api,
            "dry_run_policy": (
                "api_enabled_confirmed"
                if allow_api
                else "preflight_only_for_api_scripts_where_available"
            ),
            "event_log_path": str(event_log_path) if event_log_path else None,
            "run_dir": str(run_dir) if run_dir else None,
            "manifest_path": str(manifest_path) if manifest_path else None,
            "attempt_index": None,
            "resumed_from": None,
        }
        existing = registry.get_run(planned_run_id) if planned_run_id else None
        if existing is not None:
            _validate_planned_run_reuse(existing, expected=expected_run_identity)

        consumed_token = validate_api_gate(
            allow_api=allow_api,
            script=script,
            confirm_token=body.get("confirm_token"),
            job_id=job_id,
            argv=argv,
        )

        if job_id and planned_run_id:
            freeze_managed_runtime_for_run(
                job_id,
                planned_run_id,
                jobs_root=jobs_root,
            )
        if existing is not None:
            return ok(
                {
                    "run_id": existing["run_id"],
                    "status": existing["status"],
                    "run_dir": existing.get("run_dir"),
                    "manifest_path": existing.get("manifest_path"),
                    "reused": True,
                }
            )

        entry = registry.create_run(
            script=script,
            argv=argv,
            cwd=str(tool_root),
            config=body.get("config"),
            configs=_body_list(body, "configs"),
            seed=body.get("seed"),
            model=body.get("model"),
            prompt_version=body.get("prompt_version"),
            cache_path=body.get("cache"),
            job_id=job_id,
            experiment=body.get("experiment"),
            allow_api=allow_api,
            prompt_preview_token=consumed_token,
            dry_run_policy=(
                "api_enabled_confirmed"
                if allow_api
                else "preflight_only_for_api_scripts_where_available"
            ),
            run_id=planned_run_id,
            event_log_path=str(event_log_path) if event_log_path else None,
            run_dir=str(run_dir) if run_dir else None,
            manifest_path=str(manifest_path) if manifest_path else None,
        )
        spawn_run(registry, entry["run_id"])
        return ok(
            {
                "run_id": entry["run_id"],
                "status": entry["status"],
                "run_dir": entry.get("run_dir"),
                "manifest_path": entry.get("manifest_path"),
            },
            status=201,
        )
    except RunControlError as exc:
        return error(exc.code, exc.message, exc.status)
    except ProjectRuntimeError as exc:
        return error(exc.code, str(exc), exc.status)


@bp.get("/thesis/runs")
def list_runs():
    return ok(_get_registry().list_runs())


@bp.get("/thesis/runs/prompt-preview")
def prompt_preview():
    try:
        preview = generate_prompt_preview(
            job_id=request.args.get("job_id", ""),
            script=request.args.get("script", "run_translate"),
            db=request.args.get("db"),
            chapters=_query_list("chapters"),
            configs=_query_list("configs"),
            config=request.args.get("config"),
            profile=request.args.get("profile"),
            experiment=request.args.get("experiment"),
            cache=request.args.get("cache"),
            report=request.args.get("report"),
            context_budget=_query_int("context_budget"),
            extra_args=_query_list("extra_args"),
            python_exe=_python_exe(),
            tool_root=_tool_root(),
            jobs_root=_jobs_root(),
        )
        return ok(preview)
    except RunControlError as exc:
        return error(exc.code, exc.message, exc.status)


@bp.get("/thesis/runs/estimate-preview")
def estimate_preview():
    try:
        resume_run_id = validate_run_id(request.args.get("resume_run_id"))
        if resume_run_id:
            registry = _get_registry()
            entry = registry.get_run(resume_run_id)
            if entry is None:
                raise RunControlError("run_not_found", f"Run {resume_run_id} not found.", 404)
            argv = build_resume_argv_from_entry(entry)
            job_id = validate_job_id(entry.get("job_id"), required=True)
            token = issue_estimate_token_for_argv(
                job_id=job_id,
                script="run_one_button",
                argv=argv,
                preview_kind="resume_estimate_only",
            )
            return ok(
                {
                    "preview_kind": "resume_estimate_only",
                    "job_id": job_id,
                    "script": "run_one_button",
                    "resume_run_id": resume_run_id,
                    "confirm_token": token,
                    "confirm_token_ttl_seconds": 30 * 60,
                    "argv_preview": argv,
                    "estimate_argv_preview": [*argv, "--estimate-only"],
                    "estimate_by_stage": _manifest_estimates(entry),
                    "read_only": True,
                    "policy": {
                        "confirm_token_binding": "job_id + script + exact resume argv digest",
                        "resume_note": "Resume uses the original argv, removes --estimate-only, and appends --resume.",
                    },
                }
            )
        preview = generate_estimate_preview(
            job_id=request.args.get("job_id", ""),
            script=request.args.get("script", ""),
            db=request.args.get("db"),
            chapters=_query_list("chapters"),
            configs=_query_list("configs"),
            config=request.args.get("config"),
            profile=request.args.get("profile"),
            experiment=request.args.get("experiment"),
            cache=request.args.get("cache"),
            report=request.args.get("report"),
            context_budget=_query_int("context_budget"),
            extra_args=_query_list("extra_args"),
            planned_run_id=request.args.get("planned_run_id"),
            workdb=request.args.get("workdb"),
            budget_cap_usd=_query_float("budget_cap_usd"),
            with_s0=_query_bool("with_s0"),
            python_exe=_python_exe(),
            tool_root=_tool_root(),
            jobs_root=_jobs_root(),
        )
        return ok(preview)
    except RunControlError as exc:
        return error(exc.code, exc.message, exc.status)


@bp.get("/thesis/runs/<run_id>")
def run_detail(run_id: str):
    entry = _get_registry().get_run(run_id)
    if entry is None:
        return error("run_not_found", f"Run {run_id} not found.", 404)
    return ok(entry)


@bp.get("/thesis/runs/<run_id>/log")
def run_log(run_id: str):
    try:
        offset = int(request.args.get("offset", "0"))
        return ok(read_log(_get_registry(), run_id, offset=offset))
    except ValueError:
        return error("invalid_offset", "offset must be an integer.", 400)
    except RunControlError as exc:
        return error(exc.code, exc.message, exc.status)


@bp.get("/thesis/runs/<run_id>/events")
def run_events(run_id: str):
    try:
        offset = int(request.args.get("offset", "0"))
        max_bytes = int(request.args.get("max_bytes", str(256 * 1024)))
        return ok(
            read_events(
                _get_registry(),
                run_id,
                offset=offset,
                max_bytes=max_bytes,
                jobs_root=_jobs_root(),
            )
        )
    except ValueError:
        return error("invalid_offset", "offset and max_bytes must be integers.", 400)
    except RunControlError as exc:
        return error(exc.code, exc.message, exc.status)


@bp.post("/thesis/runs/<run_id>/cancel")
def cancel_thesis_run(run_id: str):
    try:
        entry = cancel_run(_get_registry(), run_id)
        return ok({"run_id": entry["run_id"], "status": entry["status"]})
    except RunControlError as exc:
        return error(exc.code, exc.message, exc.status)


@bp.post("/thesis/runs/<run_id>/pause")
def pause_thesis_run(run_id: str):
    try:
        pause_path = _pause_file_for_run(run_id)
        pause_path.parent.mkdir(parents=True, exist_ok=True)
        pause_path.write_text("paused_by_user\n", encoding="utf-8", newline="\n")
        return ok({"run_id": run_id, "paused": True, "pause_file": str(pause_path)})
    except RunControlError as exc:
        return error(exc.code, exc.message, exc.status)


@bp.delete("/thesis/runs/<run_id>/pause")
def unpause_thesis_run(run_id: str):
    try:
        pause_path = _pause_file_for_run(run_id)
        if pause_path.exists():
            pause_path.unlink()
        return ok({"run_id": run_id, "paused": False, "pause_file": str(pause_path)})
    except RunControlError as exc:
        return error(exc.code, exc.message, exc.status)


@bp.get("/thesis/runs/<run_id>/manifest")
def run_manifest(run_id: str):
    try:
        manifest_path = _manifest_path_for_run(run_id)
        if not manifest_path.exists():
            raise RunControlError("manifest_not_found", f"Manifest not found for run {run_id}.", 404)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        return ok(
            {
                "run_id": run_id,
                "manifest_path": str(manifest_path),
                "status": manifest.get("status"),
                "attempt": manifest.get("attempt"),
                "stages": manifest.get("stages") or [],
                "estimate_by_stage": manifest.get("estimate_by_stage") or [],
                "paused_at_stage_boundary_before": manifest.get("paused_at_stage_boundary_before"),
                "error": manifest.get("error"),
                "manifest": manifest,
            }
        )
    except json.JSONDecodeError as exc:
        return error("manifest_invalid_json", f"Manifest is not valid JSON: {exc}", 500)
    except RunControlError as exc:
        return error(exc.code, exc.message, exc.status)


@bp.get("/thesis/runs/<run_id>/block-preview")
def run_block_preview(run_id: str):
    try:
        limit = int(request.args.get("limit", "12"))
        limit = max(1, min(limit, 100))
        entry = _run_entry(run_id)
        workdb_path = _workdb_path_from_entry(entry)
        if workdb_path is None or not workdb_path.exists():
            return ok({"blocks": [], "source": "none"})
        return ok({"blocks": _read_block_preview(workdb_path, limit=limit), "source": "translation_runs"})
    except ValueError:
        return error("invalid_limit", "limit must be an integer.", 400)
    except sqlite3.Error as exc:
        return error("workdb_read_failed", f"Could not read workdb: {exc}", 500)
    except RunControlError as exc:
        return error(exc.code, exc.message, exc.status)


@bp.get("/thesis/runs/<run_id>/watchlist")
def run_watchlist(run_id: str):
    try:
        entry = _run_entry(run_id)
        watchlist_path = _watchlist_path_from_entry(entry)
        if watchlist_path is None or not watchlist_path.exists():
            return ok({"watchlist": []})
        payload = json.loads(watchlist_path.read_text(encoding="utf-8"))
        if isinstance(payload, list):
            watchlist = payload
        elif isinstance(payload, dict):
            watchlist = payload.get("watchlist") or []
        else:
            watchlist = []
        return ok({"watchlist": watchlist})
    except json.JSONDecodeError as exc:
        return error("watchlist_invalid_json", f"Watchlist is not valid JSON: {exc}", 500)
    except RunControlError as exc:
        return error(exc.code, exc.message, exc.status)


@bp.get("/thesis/runs/<run_id>/report-summary")
def run_report_summary(run_id: str):
    try:
        entry = _run_entry(run_id)
        reports_dir = _reports_dir_from_entry(entry)
        if reports_dir is None:
            return ok(_empty_report_summary())
        phase_1_path = _path_under_jobs(reports_dir / "score_run_phase_1.json", "report_path")
        final_path = _path_under_jobs(reports_dir / "score_run_final.json", "report_path")
        phase_1 = _read_json_optional(phase_1_path)
        final = _read_json_optional(final_path)
        return ok(_build_report_summary(phase_1, final))
    except RunControlError as exc:
        return error(exc.code, exc.message, exc.status)


@bp.get("/thesis/runs/<run_id>/report-full")
def run_full_report(run_id: str):
    """Relay an Evaluation-owned FullRunReportV1 projection without deriving facts."""
    try:
        entry = _run_entry(run_id)
        report_path = _full_run_report_path_from_entry(entry)
        if report_path is None or not report_path.exists():
            return ok(_full_run_report_unavailable())
        try:
            raw_report = report_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RunControlError(
                "full_run_report_read_failed",
                f"Could not read FullRunReportV1: {exc}",
                500,
            ) from exc
        try:
            report = json.loads(raw_report)
        except json.JSONDecodeError as exc:
            raise RunControlError(
                "full_run_report_invalid_json",
                f"FullRunReportV1 is not valid JSON: {exc}",
                500,
            ) from exc
        try:
            validate_full_run_report(report)
        except ContractValidationError as exc:
            if exc.code == "enum" and exc.path in {"$.schema_id", "$.schema_version"}:
                raise RunControlError(
                    "full_run_report_schema_unsupported",
                    f"Unsupported FullRunReportV1 schema: {exc}",
                    409,
                ) from exc
            raise RunControlError(
                "full_run_report_contract_invalid",
                f"FullRunReportV1 contract validation failed: {exc}",
                500,
            ) from exc
        _validate_full_report_transport(
            report,
            entry=entry,
            route_run_id=run_id,
            run_dir=report_path.parent.parent,
        )
        return ok(
            {
                "availability": "available",
                "schema_id": _FULL_RUN_REPORT_SCHEMA_ID,
                "schema_version": _FULL_RUN_REPORT_SCHEMA_VERSION,
                "report": report,
            }
        )
    except RunControlError as exc:
        return error(exc.code, exc.message, exc.status)


@bp.post("/thesis/runs/<run_id>/resume")
def resume_thesis_run(run_id: str):
    try:
        body = request.get_json(force=True) or {}
        registry = _get_registry()
        entry = registry.get_run(run_id)
        if entry is None:
            raise RunControlError("run_not_found", f"Run {run_id} not found.", 404)
        if _is_pid_alive(entry.get("pid")):
            raise RunControlError(
                "run_still_active",
                "Run vẫn đang chạy; hãy Cancel trước rồi Resume.",
                409,
            )
        resume_root = _resolve_resume_root(registry, entry)
        manifest_path = _manifest_path_for_entry(entry)
        manifest = {}
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise RunControlError(
                "resume_manifest_invalid",
                "Resume manifest must be a JSON object.",
                409,
            )
        argv = build_resume_argv_from_entry(entry)
        job_id = validate_job_id(entry.get("job_id"), required=True)
        pause_path = _pause_file_from_entry(entry)
        try:
            manifest_attempt = int(manifest.get("attempt") or 0)
            entry_attempt = int(entry.get("attempt_index") or 0)
        except (TypeError, ValueError) as exc:
            raise RunControlError(
                "resume_attempt_invalid",
                "Resume attempt metadata must contain integers.",
                409,
            ) from exc
        attempt_index = max(manifest_attempt, entry_attempt) + 1
        consumed_token = validate_api_gate(
            allow_api=True,
            script="run_one_button",
            confirm_token=body.get("confirm_token"),
            job_id=job_id,
            argv=argv,
        )
        new_run_id = registry.new_run_id()
        freeze_managed_runtime_for_run(
            job_id,
            resume_root["run_id"],
            jobs_root=registry.runs_root,
        )
        if pause_path.exists():
            pause_path.unlink()
        new_log_path = registry.runs_root / "run_logs" / f"{new_run_id}.log"
        new_entry = registry.create_run(
            script="run_one_button",
            argv=argv,
            cwd=entry.get("cwd"),
            job_id=job_id,
            experiment=entry.get("experiment"),
            allow_api=True,
            prompt_preview_token=consumed_token,
            dry_run_policy="api_enabled_confirmed_resume",
            event_log_path=entry.get("event_log_path"),
            run_dir=entry.get("run_dir"),
            manifest_path=entry.get("manifest_path"),
            run_id=new_run_id,
            attempt_index=attempt_index,
            resumed_from=run_id,
            attempt_log_path=str(new_log_path),
        )
        spawn_run(registry, new_entry["run_id"])
        return ok(
            {
                "run_id": new_entry["run_id"],
                "resumed_from": run_id,
                "status": new_entry["status"],
                "attempt_index": attempt_index,
                "manifest_path": new_entry.get("manifest_path"),
            },
            status=201,
        )
    except json.JSONDecodeError as exc:
        return error("manifest_invalid_json", f"Manifest is not valid JSON: {exc}", 500)
    except RunControlError as exc:
        return error(exc.code, exc.message, exc.status)
    except ProjectRuntimeError as exc:
        return error(exc.code, str(exc), exc.status)


def _is_pid_alive(pid: object) -> bool:
    try:
        value = int(pid or 0)
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            process_query_limited_information = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                process_query_limited_information,
                False,
                value,
            )
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
        except Exception:
            pass
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {value}", "/FO", "CSV", "/NH"],
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
            )
        except Exception:
            return False
        return str(value) in result.stdout
    try:
        os.kill(value, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _body_list(body: dict, key: str) -> list[str]:
    value = body.get(key)
    if value is None:
        return []
    if isinstance(value, str):
        return [item for part in value.split(",") for item in part.split() if item]
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)]


def _query_list(key: str) -> list[str]:
    values = request.args.getlist(key)
    result: list[str] = []
    for value in values:
        result.extend(item for part in str(value).split(",") for item in part.split() if item)
    return result


def _query_int(key: str) -> int | None:
    value = request.args.get(key)
    if value in (None, ""):
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise RunControlError("invalid_int", f"{key} must be an integer.", 400) from exc


def _query_float(key: str) -> float | None:
    value = request.args.get(key)
    if value in (None, ""):
        return None
    try:
        return float(value)
    except ValueError as exc:
        raise RunControlError("invalid_float", f"{key} must be a number.", 400) from exc


def _body_float(body: dict, key: str) -> float | None:
    value = body.get(key)
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise RunControlError("invalid_float", f"{key} must be a number.", 400) from exc


def _query_bool(key: str) -> bool:
    value = request.args.get(key)
    if value in (None, ""):
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _manifest_estimates(entry: dict) -> list[dict]:
    try:
        path = _manifest_path_for_entry(entry)
    except RunControlError:
        return []
    if not path.exists():
        return []
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return manifest.get("estimate_by_stage") or []


def _run_entry(run_id: str) -> dict:
    entry = _get_registry().get_run(run_id)
    if entry is None:
        raise RunControlError("run_not_found", f"Run {run_id} not found.", 404)
    return entry


def _manifest_path_for_run(run_id: str) -> Path:
    return _manifest_path_for_entry(_run_entry(run_id))


def _manifest_path_for_entry(entry: dict) -> Path:
    raw_path = entry.get("manifest_path")
    if not raw_path:
        raise RunControlError("run_manifest_unavailable", "Run does not have a manifest_path.", 404)
    return _path_under_jobs(raw_path, "manifest_path")


def _pause_file_for_run(run_id: str) -> Path:
    entry = _get_registry().get_run(run_id)
    if entry is None:
        raise RunControlError("run_not_found", f"Run {run_id} not found.", 404)
    return _pause_file_from_entry(entry)


def _pause_file_from_entry(entry: dict) -> Path:
    raw_dir = entry.get("run_dir")
    if not raw_dir:
        raise RunControlError("run_pause_unavailable", "Run does not have a run_dir.", 404)
    return _path_under_jobs(raw_dir, "run_dir") / "PAUSE"


def _run_dir_from_entry(entry: dict) -> Path | None:
    raw_dir = entry.get("run_dir")
    if raw_dir:
        return _path_under_jobs(raw_dir, "run_dir")
    return None


def _watchlist_path_from_entry(entry: dict) -> Path | None:
    run_dir = _run_dir_from_entry(entry)
    if run_dir is None:
        return None
    return _path_under_jobs(run_dir / "artifacts" / "reelection" / "watchlist.json", "watchlist_path")


def _reports_dir_from_entry(entry: dict) -> Path | None:
    run_dir = _run_dir_from_entry(entry)
    if run_dir is None:
        return None
    return _path_under_jobs(run_dir / "reports", "reports_path")


def _full_run_report_path_from_entry(entry: dict) -> Path | None:
    try:
        run_dir = _run_dir_from_entry(entry)
    except RunControlError as exc:
        raise RunControlError(
            "full_run_report_path_unsafe",
            "Registered run_dir is outside THESIS_JOBS_ROOT.",
            500,
        ) from exc
    if run_dir is None:
        return None
    try:
        return _path_under_jobs(
            run_dir / "reports" / _FULL_RUN_REPORT_FILENAME,
            "full_run_report_path",
        )
    except RunControlError as exc:
        raise RunControlError(
            "full_run_report_path_unsafe",
            "FullRunReportV1 path is outside THESIS_JOBS_ROOT.",
            500,
        ) from exc


def _full_run_report_unavailable() -> dict:
    return {
        "availability": "not_generated",
        "schema_id": _FULL_RUN_REPORT_SCHEMA_ID,
        "schema_version": _FULL_RUN_REPORT_SCHEMA_VERSION,
        "report": None,
    }


def _validate_full_report_transport(
    report: dict,
    *,
    entry: dict,
    route_run_id: str,
    run_dir: Path,
) -> None:
    # The shared Evaluation validator has already established this closed shape.
    root = report
    identity = root["identity"]

    registered_project_id = str(entry.get("job_id") or "")
    if not registered_project_id or identity["project_id"] != registered_project_id:
        raise RunControlError(
            "full_run_report_identity_mismatch",
            "Report project_id does not match the registered run.",
            500,
        )

    attempt_run_ids = identity["attempt_run_ids"]
    if (
        route_run_id != identity["logical_run_id"]
        and route_run_id not in attempt_run_ids
    ):
        raise RunControlError(
            "full_run_report_identity_mismatch",
            "Route run_id is not the report logical run or a registered report attempt.",
            500,
        )

    for index, artifact in enumerate(root["artifacts"]):
        relative_path = artifact["relative_path"]
        if relative_path is not None:
            _validate_full_report_run_root_path(
                relative_path,
                run_dir,
                f"artifacts[{index}].relative_path",
            )


def _validate_full_report_run_root_path(
    relative_path: str,
    run_dir: Path,
    field_path: str,
) -> None:
    resolved = (run_dir / Path(relative_path)).resolve()
    try:
        resolved.relative_to(run_dir.resolve())
    except ValueError as exc:
        raise RunControlError(
            "full_run_report_path_unsafe",
            f"{field_path} escapes the registered run directory.",
            500,
        ) from exc


def _workdb_path_from_entry(entry: dict) -> Path | None:
    manifest_path = None
    try:
        manifest_path = _manifest_path_for_entry(entry)
    except RunControlError:
        pass
    if manifest_path and manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            manifest = {}
        raw_workdb = manifest.get("workdb_path") or manifest.get("workdb")
        if raw_workdb:
            return _path_under_jobs(raw_workdb, "workdb_path")
    run_dir = _run_dir_from_entry(entry)
    if run_dir is not None:
        return _path_under_jobs(run_dir / "workdb.sqlite3", "workdb_path")
    return None


def _read_block_preview(workdb_path: Path, *, limit: int) -> list[dict]:
    uri = f"file:{workdb_path.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            """
            WITH ranked AS (
                SELECT
                    tr.block_id,
                    b.text AS source_text,
                    tr.output_text AS target_text,
                    tr.model,
                    tr.window_id,
                    tr.config,
                    b.order_index,
                    ROW_NUMBER() OVER (
                        PARTITION BY tr.block_id, tr.config
                        ORDER BY COALESCE(tr.created_at, '') DESC, tr.run_id DESC
                    ) AS rn
                FROM translation_runs tr
                JOIN blocks b ON b.block_id = tr.block_id
                WHERE tr.output_text IS NOT NULL AND TRIM(tr.output_text) <> ''
            )
            SELECT block_id, source_text, target_text, model, window_id, config
            FROM ranked
            WHERE rn = 1
            ORDER BY COALESCE(window_id, ''), order_index, block_id, config
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [
        {
            "block_id": row["block_id"],
            "source_text": row["source_text"],
            "target_text": row["target_text"],
            "model": row["model"],
            "window_id": row["window_id"],
            "config": row["config"],
        }
        for row in rows
    ]


def _empty_report_summary() -> dict:
    return {
        "phase_1": {"present": False, "metrics": [], "configs": None},
        "final": {
            "present": False,
            "metrics": [],
            "verdict": None,
            "report_path": None,
        },
        "compare": {"present": False, "gap": None},
        "consistency": {"present": False},
    }


def _read_json_optional(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RunControlError("report_invalid_json", f"Report is not valid JSON: {exc}", 500) from exc
    if not isinstance(payload, dict):
        raise RunControlError("report_invalid_shape", "Report JSON must be an object.", 500)
    return payload


def _build_report_summary(phase_1: dict | None, final: dict | None) -> dict:
    summary = _empty_report_summary()
    if phase_1:
        summary["phase_1"] = {
            "present": True,
            "metrics": _score_run_metrics(phase_1),
            "configs": phase_1.get("configs") or None,
            "report_path": "reports/score_run_phase_1.json",
        }
    if final:
        summary["final"] = {
            "present": True,
            "metrics": _score_run_metrics(final),
            "verdict": _score_run_verdict(final),
            "stage_gate": _score_run_stage_gate_digest(final),
            "report_path": "reports/score_run_final.json",
        }
        summary["compare"] = _score_run_compare(final)
        summary["consistency"] = _score_run_consistency_projection(final)
    return summary


def _score_run_metrics(report: dict) -> list[dict]:
    configs = _score_run_configs(report)
    multi = len(configs) > 1
    metrics: list[dict] = []
    for config in configs:
        label_prefix = f"{config} " if multi else ""
        key_suffix = f"_{config}" if multi else ""
        consistency = ((report.get("D_registry_consistency") or {}).get(config) or {})
        if consistency.get("overall") is not None:
            metrics.append(
                {
                    "key": f"TC{key_suffix}",
                    "label": f"{label_prefix}term consistency",
                    "value": _round_metric(consistency.get("overall")),
                    "unit": "ratio",
                    "status": None,
                }
            )
        gold = (((report.get("B_gold_occurrence_adherence") or {}).get(config) or {}).get("flat") or {})
        if gold.get("adherence_lower") is not None:
            metrics.append(
                {
                    "key": f"TA{key_suffix}",
                    "label": f"{label_prefix}gold adherence",
                    "value": _round_metric(gold.get("adherence_lower")),
                    "unit": "ratio",
                    "status": None,
                }
            )
        registry = ((report.get("A_registry_occurrence_adherence") or {}).get(config) or {})
        if registry.get("adherence_lower") is not None:
            metrics.append(
                {
                    "key": f"TA_REGISTRY{key_suffix}",
                    "label": f"{label_prefix}registry adherence",
                    "value": _round_metric(registry.get("adherence_lower")),
                    "unit": "ratio",
                    "status": None,
                }
            )
    _score_run_apply_relative_metric_status(metrics)
    return metrics


def _score_run_apply_relative_metric_status(metrics: list[dict]) -> None:
    by_key = {str(row.get("key")): row for row in metrics}
    for key, s1_row in list(by_key.items()):
        if not key.endswith("_S1"):
            continue
        s0_row = by_key.get(key[:-3] + "_S0")
        if not s0_row:
            continue
        s0_value = s0_row.get("value")
        s1_value = s1_row.get("value")
        if s0_value is None or s1_value is None:
            continue
        try:
            delta = float(s1_value) - float(s0_value)
        except (TypeError, ValueError):
            continue
        s1_row["status"] = "good" if delta >= -0.000001 else "warn"


def _score_run_configs(report: dict) -> list[str]:
    configs = [str(item) for item in (report.get("configs") or []) if str(item).strip()]
    if configs:
        return configs
    found: set[str] = set()
    for key in ("D_registry_consistency", "B_gold_occurrence_adherence", "A_registry_occurrence_adherence"):
        value = report.get(key)
        if isinstance(value, dict):
            found.update(str(item) for item in value.keys())
    return sorted(found)


def _round_metric(value: object) -> float | None:
    try:
        return round(float(value), 6)
    except (TypeError, ValueError):
        return None


def _score_run_verdict(report: dict) -> dict:
    gate = report.get("stage_gate") if isinstance(report.get("stage_gate"), dict) else {}
    reasons: list[str] = []
    for key, value in gate.items():
        if isinstance(value, dict):
            for sub_key, sub_value in value.items():
                if sub_value is False:
                    reasons.append(f"{key}:{sub_key}")
        elif value is False:
            reasons.append(str(key))
    return {"pass": not reasons if gate else None, "reasons": reasons}


def _score_run_stage_gate_digest(report: dict) -> dict:
    gate = report.get("stage_gate") if isinstance(report.get("stage_gate"), dict) else {}
    failed: list[str] = []
    passed = 0
    total = 0

    def visit(prefix: str, value: object) -> None:
        nonlocal passed, total
        if isinstance(value, dict):
            for sub_key, sub_value in value.items():
                visit(f"{prefix}.{sub_key}" if prefix else str(sub_key), sub_value)
            return
        if isinstance(value, bool):
            total += 1
            if value:
                passed += 1
            else:
                failed.append(prefix)

    for key, value in gate.items():
        visit(str(key), value)
    return {
        "present": bool(gate),
        "passed": passed,
        "total": total,
        "all_ok": (not failed) if total else None,
        "failed": failed,
    }


def _score_run_compare(report: dict) -> dict:
    configs = _score_run_configs(report)
    if len(configs) < 2:
        return {"present": False, "gap": None}
    s0, s1 = ("S0", "S1") if {"S0", "S1"}.issubset(set(configs)) else (configs[0], configs[1])
    gaps: dict[str, float] = {}
    for key, label in (
        ("D_registry_consistency", "TC"),
        ("B_gold_occurrence_adherence", "TA"),
        ("A_registry_occurrence_adherence", "TA_REGISTRY"),
    ):
        if key == "B_gold_occurrence_adherence":
            left = ((((report.get(key) or {}).get(s0) or {}).get("flat") or {}).get("adherence_lower"))
            right = ((((report.get(key) or {}).get(s1) or {}).get("flat") or {}).get("adherence_lower"))
        else:
            left = ((report.get(key) or {}).get(s0) or {}).get("adherence_lower")
            right = ((report.get(key) or {}).get(s1) or {}).get("adherence_lower")
            if key == "D_registry_consistency":
                left = ((report.get(key) or {}).get(s0) or {}).get("overall")
                right = ((report.get(key) or {}).get(s1) or {}).get("overall")
        if left is not None and right is not None:
            gaps[label] = _round_metric(float(right) - float(left))  # type: ignore[arg-type]
    return {"present": bool(gaps), "gap": gaps or None, "baseline": s0, "candidate": s1}


def _score_run_consistency_projection(report: dict) -> dict:
    consistency_root = report.get("D_registry_consistency")
    if not isinstance(consistency_root, dict):
        return {"present": False}
    configs = [
        cfg
        for cfg in _score_run_configs(report)
        if isinstance(consistency_root.get(cfg), dict)
    ]
    if not configs:
        return {"present": False}

    overall: dict[str, float | None] = {}
    by_tier: dict[str, dict] = {}
    terms_by_source: dict[str, dict[str, dict]] = {}
    injected_tiers = {"hard", "soft", "preserve"}

    for cfg in configs:
        cfg_consistency = consistency_root.get(cfg) or {}
        overall[cfg] = _round_metric(cfg_consistency.get("overall"))
        by_tier[cfg] = cfg_consistency.get("by_tier") or {}
        for term in cfg_consistency.get("terms_all") or []:
            if not isinstance(term, dict):
                continue
            source_term = str(term.get("source_term") or "").strip()
            if not source_term:
                continue
            tier = str(term.get("constraint_strength") or "").strip()
            if tier == "ignore_for_consistency":
                continue
            status = str(term.get("status") or "").strip()
            if status == "consistent":
                existing = terms_by_source.get(source_term)
                if existing and any(
                    str(row.get("status") or "") != "consistent"
                    for row in existing.values()
                ):
                    existing[cfg] = term
                continue
            terms_by_source.setdefault(source_term, {})[cfg] = term

    notable_terms: list[dict] = []
    for source_term, rows in terms_by_source.items():
        s0_row = rows.get("S0")
        s1_row = rows.get("S1")
        if s1_row is None:
            s1_rows = (((consistency_root.get("S1") or {}).get("terms_all") or [])
                       if isinstance(consistency_root.get("S1"), dict) else [])
            for candidate in s1_rows:
                if isinstance(candidate, dict) and str(candidate.get("source_term") or "").strip() == source_term:
                    if str(candidate.get("constraint_strength") or "").strip() != "ignore_for_consistency":
                        s1_row = candidate
                    break
        if s0_row is None:
            s0_rows = (((consistency_root.get("S0") or {}).get("terms_all") or [])
                       if isinstance(consistency_root.get("S0"), dict) else [])
            for candidate in s0_rows:
                if isinstance(candidate, dict) and str(candidate.get("source_term") or "").strip() == source_term:
                    if str(candidate.get("constraint_strength") or "").strip() != "ignore_for_consistency":
                        s0_row = candidate
                    break

        primary_row = s1_row or s0_row or next(iter(rows.values()))
        tier = str(primary_row.get("constraint_strength") or "").strip()
        s0_status = str((s0_row or {}).get("status") or "")
        s1_status = str((s1_row or {}).get("status") or "")
        fixed_by_injection = (
            bool(s0_row)
            and bool(s1_row)
            and s0_status != "consistent"
            and s1_status == "consistent"
            and tier in injected_tiers
        )
        by_config: dict[str, dict] = {}
        for cfg, row in (("S0", s0_row), ("S1", s1_row)):
            if not row:
                continue
            by_config[cfg] = {
                "status": row.get("status"),
                "forms": row.get("forms_used") or {},
                "target_term": row.get("target_term"),
            }
        for cfg, row in rows.items():
            if cfg not in by_config:
                by_config[cfg] = {
                    "status": row.get("status"),
                    "forms": row.get("forms_used") or {},
                    "target_term": row.get("target_term"),
                }
        notable_terms.append(
            {
                "source_term": source_term,
                "tier": tier,
                "by_config": by_config,
                "fixed_by_injection": fixed_by_injection,
            }
        )

    def _notable_sort_key(item: dict) -> tuple[int, str, str]:
        s1_status = str(((item.get("by_config") or {}).get("S1") or {}).get("status") or "consistent")
        fixed = bool(item.get("fixed_by_injection"))
        is_s1_non_consistent = s1_status != "consistent"
        return (
            0 if fixed else 1 if is_s1_non_consistent else 2,
            str(item.get("tier") or ""),
            str(item.get("source_term") or "").casefold(),
        )

    notable_terms = sorted(notable_terms, key=_notable_sort_key)
    fixed_terms = [item for item in notable_terms if item.get("fixed_by_injection")]
    capped_terms = fixed_terms[:]
    for item in notable_terms:
        if item.get("fixed_by_injection"):
            continue
        if len(capped_terms) >= 50:
            break
        capped_terms.append(item)

    return {
        "present": True,
        "configs": configs,
        "overall": overall,
        "by_tier": by_tier,
        "notable_terms": capped_terms,
    }


def _path_under_jobs(raw_path: str | Path, field: str) -> Path:
    root = _jobs_root()
    path = Path(str(raw_path)).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise RunControlError(
            f"invalid_{field}",
            f"{field} is outside THESIS_JOBS_ROOT.",
            500,
        ) from exc
    return path


def _tool_root() -> Path:
    from config import THESIS_TOOL_ROOT

    return Path(THESIS_TOOL_ROOT).resolve()


def _jobs_root() -> Path:
    from config import THESIS_JOBS_ROOT

    return Path(THESIS_JOBS_ROOT).resolve()


def _python_exe() -> str:
    from config import THESIS_PYTHON_EXE

    return THESIS_PYTHON_EXE or sys.executable
