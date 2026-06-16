"""RunControl service for APP-C01.

The cockpit may launch frozen pipeline scripts and tail their logs, but it must
not become a second pipeline engine.  The two safety properties that matter most
here are:

* real scripts are launched as ``python -m pipeline.scripts.<name>`` with
  ``cwd=THESIS_RUNTIME_TOOL``;
* ``allow_api=False`` cannot accidentally spend API quota.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any


_SHELL_META_RE = re.compile(r"[;&|`$(){}!<>'\"\n\r]")
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_RUN_ID_RE = _JOB_ID_RE
_CONFIRM_TOKEN_TTL_SECONDS = 30 * 60

ALLOWLIST = frozenset(
    {
        "run_prepass",
        "build_memory",
        "build_index",
        "run_translate",
        "run_judge",
        "score_consistency",
        "score_run",
        "snapshot_runs",
    }
)

API_CAPABLE_SCRIPTS = frozenset(
    {
        "run_prepass",
        "run_translate",
        "run_judge",
        "build_index",
    }
)

PREFLIGHT_ONLY_FLAGS = {
    "run_prepass": "--preflight-only",
    "run_translate": "--preflight-only",
}

PROMPT_PREVIEW_SUPPORTED = frozenset({"run_translate"})


class RunControlError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass(frozen=True)
class PreviewToken:
    token: str
    job_id: str
    script: str
    argv_digest: str
    issued_at: float
    preview_kind: str


_active_tokens: dict[str, PreviewToken] = {}
_token_lock = threading.Lock()


class RunRegistry:
    """Persist run provenance as JSONL and keep the latest snapshot in memory."""

    def __init__(self, runs_root: Path | None = None):
        self._runs_root = runs_root or Path.cwd()
        self._runs_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._runs: dict[str, dict[str, Any]] = {}
        self._log_dir = self._runs_root / "run_logs"
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._registry_path = self._runs_root / "thesis_runs.jsonl"
        self._load()

    @property
    def runs_root(self) -> Path:
        return self._runs_root

    def new_run_id(self) -> str:
        with self._lock:
            while True:
                run_id = f"run_{uuid.uuid4().hex[:12]}"
                if run_id not in self._runs:
                    return run_id

    def _load(self) -> None:
        if not self._registry_path.exists():
            return
        with open(self._registry_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    self._runs[str(entry["run_id"])] = entry
                except (json.JSONDecodeError, KeyError, TypeError):
                    continue

    def _append(self, entry: dict[str, Any]) -> None:
        with open(self._registry_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def create_run(
        self,
        *,
        script: str,
        argv: list[str],
        cwd: str | None = None,
        config: str | None = None,
        configs: list[str] | None = None,
        seed: int | None = None,
        model: str | None = None,
        prompt_version: str | None = None,
        cache_path: str | None = None,
        job_id: str | None = None,
        experiment: str | None = None,
        allow_api: bool = False,
        prompt_preview_token: str | None = None,
        dry_run_policy: str | None = None,
        run_id: str | None = None,
        event_log_path: str | None = None,
    ) -> dict[str, Any]:
        run_id = validate_run_id(run_id) if run_id else self.new_run_id()
        now = _utc_now()
        log_path = str(self._log_dir / f"{run_id}.log")
        entry = {
            "run_id": run_id,
            "script": script,
            "argv": argv,
            "cwd": cwd,
            "config": config,
            "configs": configs or [],
            "seed": seed,
            "model": model,
            "prompt_version": prompt_version,
            "cache_path": cache_path,
            "job_id": job_id,
            "experiment": experiment,
            "allow_api": allow_api,
            "prompt_preview_token": prompt_preview_token,
            "dry_run_policy": dry_run_policy,
            "event_log_path": event_log_path,
            "status": "pending",
            "pid": None,
            "started_at": now,
            "ended_at": None,
            "exit_code": None,
            "log_path": log_path,
        }
        with self._lock:
            self._runs[run_id] = entry
            self._append(entry)
        return dict(entry)

    def update_run(self, run_id: str, **updates: Any) -> dict[str, Any] | None:
        with self._lock:
            entry = self._runs.get(run_id)
            if entry is None:
                return None
            entry.update(updates)
            self._append(entry)
            return dict(entry)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        if not _RUN_ID_RE.match(str(run_id or "")):
            return None
        with self._lock:
            entry = self._runs.get(run_id)
            return dict(entry) if entry else None

    def list_runs(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = [
                {
                    "run_id": r["run_id"],
                    "script": r["script"],
                    "status": r["status"],
                    "started_at": r["started_at"],
                    "ended_at": r["ended_at"],
                    "exit_code": r["exit_code"],
                    "job_id": r.get("job_id"),
                    "allow_api": bool(r.get("allow_api")),
                    "event_log_path": r.get("event_log_path"),
                }
                for r in self._runs.values()
            ]
        return sorted(rows, key=lambda row: str(row["started_at"]), reverse=True)


def validate_script(script: str) -> str:
    value = str(script or "").strip()
    if value not in ALLOWLIST:
        raise RunControlError(
            "script_not_allowed",
            f"Script '{value}' is not in the allowlist: {sorted(ALLOWLIST)}.",
            400,
        )
    return value


def validate_args(args: list[str]) -> list[str]:
    clean: list[str] = []
    for arg in args:
        value = str(arg)
        if _SHELL_META_RE.search(value):
            raise RunControlError(
                "invalid_arg",
                f"Argument contains forbidden shell meta-character: {value!r}.",
                400,
            )
        clean.append(value)
    return clean


def validate_job_id(job_id: str | None, *, required: bool = False) -> str | None:
    if job_id is None or str(job_id).strip() == "":
        if required:
            raise RunControlError("job_id_required", "job_id is required.", 400)
        return None
    value = str(job_id).strip()
    if not _JOB_ID_RE.match(value):
        raise RunControlError("invalid_job_id", "Invalid job_id.", 400)
    return value


def validate_run_id(run_id: str | None, *, required: bool = False) -> str | None:
    if run_id is None or str(run_id).strip() == "":
        if required:
            raise RunControlError("run_id_required", "run_id is required.", 400)
        return None
    value = str(run_id).strip()
    if not _RUN_ID_RE.match(value):
        raise RunControlError("invalid_run_id", "Invalid run_id.", 400)
    return value


def resolve_job_db(
    *,
    db: str | None,
    job_id: str | None,
    jobs_root: Path,
) -> str | None:
    if db:
        return str(db)
    if job_id:
        return str((jobs_root / job_id / "memory.sqlite3").resolve())
    return None


def validate_api_gate(
    *,
    allow_api: bool,
    script: str,
    confirm_token: str | None,
    job_id: str | None,
    argv: list[str],
) -> str | None:
    """Enforce the cost gate.

    A real API run must be tied to a successful prompt-preview token.  The token
    is one-time and bound to the exact argv that will be launched.
    """
    validate_script(script)
    if not allow_api:
        return None

    job = validate_job_id(job_id, required=True)
    if script not in API_CAPABLE_SCRIPTS:
        raise RunControlError(
            "allow_api_not_applicable",
            f"{script} is deterministic and does not need allow_api=true.",
            400,
        )
    if not confirm_token or not str(confirm_token).strip():
        raise RunControlError(
            "confirm_token_required",
            "allow_api=true requires a confirm_token issued by prompt-preview.",
            403,
        )

    token = str(confirm_token).strip()
    digest = _argv_digest(argv)
    with _token_lock:
        issued = _active_tokens.get(token)
        if issued is None:
            raise RunControlError("confirm_token_invalid", "Unknown confirm_token.", 403)
        if time.time() - issued.issued_at > _CONFIRM_TOKEN_TTL_SECONDS:
            _active_tokens.pop(token, None)
            raise RunControlError("confirm_token_expired", "confirm_token expired.", 403)
        if issued.job_id != job or issued.script != script or issued.argv_digest != digest:
            raise RunControlError(
                "confirm_token_mismatch",
                "confirm_token does not match this job/script/argv.",
                403,
            )
        _active_tokens.pop(token, None)
    return token


def generate_prompt_preview(
    *,
    job_id: str,
    script: str = "run_translate",
    db: str | None = None,
    chapters: list[str] | None = None,
    configs: list[str] | None = None,
    config: str | None = None,
    profile: str | None = None,
    experiment: str | None = None,
    cache: str | None = None,
    report: str | None = None,
    context_budget: int | None = None,
    extra_args: list[str] | None = None,
    python_exe: str | None = None,
    tool_root: Path | None = None,
    jobs_root: Path | None = None,
) -> dict[str, Any]:
    script = validate_script(script)
    job = validate_job_id(job_id, required=True)
    if script not in PROMPT_PREVIEW_SUPPORTED:
        raise RunControlError(
            "prompt_preview_not_supported",
            f"Prompt preview is currently supported for {sorted(PROMPT_PREVIEW_SUPPORTED)} only.",
            400,
        )

    tool = Path(tool_root or Path.cwd()).resolve()
    jobs = Path(jobs_root or tool / "data" / "jobs").resolve()
    db_path = resolve_job_db(db=db, job_id=job, jobs_root=jobs)
    if db_path is None:
        raise RunControlError("db_required", "db or job_id is required for prompt preview.", 400)
    chapter_list = _list(chapters)
    if not chapter_list:
        raise RunControlError("chapters_required", "chapters are required for prompt preview.", 400)

    planned_run_id = f"run_{uuid.uuid4().hex[:12]}"
    event_log_path = _event_log_path(jobs, planned_run_id)
    argv = build_argv(
        script=script,
        python_exe=python_exe,
        db=db_path,
        chapters=chapter_list,
        configs=configs,
        config=config,
        profile=profile,
        experiment=experiment,
        cache=cache,
        report=report,
        context_budget=context_budget,
        extra_args=extra_args,
        allow_api=True,
        event_log=str(event_log_path),
        run_id=planned_run_id,
    )
    preview = _render_translate_prompt_preview(
        db=db_path,
        chapters=chapter_list,
        configs=configs,
        config=config,
        profile=profile,
        cache=cache,
        context_budget=context_budget,
        tool_root=tool,
    )

    token = uuid.uuid4().hex
    issued = PreviewToken(
        token=token,
        job_id=job,
        script=script,
        argv_digest=_argv_digest(argv),
        issued_at=time.time(),
        preview_kind="real_translate_prompt",
    )
    with _token_lock:
        _active_tokens[token] = issued

    return {
        **preview,
        "job_id": job,
        "script": script,
        "confirm_token": token,
        "planned_run_id": planned_run_id,
        "event_log_path": str(event_log_path),
        "confirm_token_ttl_seconds": _CONFIRM_TOKEN_TTL_SECONDS,
        "argv_preview": _redact_argv(argv),
        "read_only": True,
        "cache_plan": {
            "cache_path": cache or "data/jobs/translate_cache.sqlite3",
            "allow_api_false_policy": (
                "RunControl appends --preflight-only for run_translate when allow_api=false; "
                "a full translation run requires this confirm_token."
            ),
            "allow_api_true_policy": "Cache hits are reused; cache misses may call the provider.",
        },
    }


def spawn_run(registry: RunRegistry, run_id: str) -> None:
    entry = registry.get_run(run_id)
    if entry is None:
        return

    argv = [str(item) for item in entry["argv"]]
    cwd = entry.get("cwd") or None
    log_path = entry["log_path"]

    def _worker() -> None:
        try:
            with open(log_path, "w", encoding="utf-8") as log_fh:
                log_fh.write(f"[RunControl] cwd={cwd or os.getcwd()}\n")
                log_fh.write(f"[RunControl] argv={json.dumps(_redact_argv(argv), ensure_ascii=False)}\n")
                log_fh.flush()
                proc = subprocess.Popen(
                    argv,
                    cwd=cwd,
                    stdout=log_fh,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                )
                registry.update_run(run_id, status="running", pid=proc.pid)
                proc.wait()
                status = "done" if proc.returncode == 0 else "failed"
                registry.update_run(
                    run_id,
                    status=status,
                    exit_code=proc.returncode,
                    ended_at=_utc_now(),
                )
        except Exception as exc:
            registry.update_run(
                run_id,
                status="error",
                exit_code=-1,
                ended_at=_utc_now(),
            )
            try:
                with open(log_path, "a", encoding="utf-8") as log_fh:
                    log_fh.write(f"\n[RunControl ERROR] {exc}\n")
            except OSError:
                pass

    threading.Thread(target=_worker, daemon=True, name=f"run-{run_id}").start()


def read_log(registry: RunRegistry, run_id: str, *, offset: int = 0) -> dict[str, Any]:
    entry = registry.get_run(run_id)
    if entry is None:
        raise RunControlError("run_not_found", f"Run {run_id} not found.", 404)
    safe_offset = max(int(offset or 0), 0)
    log_path = entry["log_path"]
    content = ""
    new_offset = safe_offset
    if os.path.exists(log_path):
        with open(log_path, "r", encoding="utf-8") as fh:
            fh.seek(safe_offset)
            content = fh.read()
            new_offset = fh.tell()
    return {
        "run_id": run_id,
        "log": content,
        "offset": new_offset,
        "running": entry["status"] == "running",
        "status": entry["status"],
        "exit_code": entry["exit_code"],
    }


def read_events(
    registry: RunRegistry,
    run_id: str,
    *,
    offset: int = 0,
    jobs_root: Path | None = None,
) -> dict[str, Any]:
    entry = registry.get_run(run_id)
    if entry is None:
        raise RunControlError("run_not_found", f"Run {run_id} not found.", 404)

    root = Path(jobs_root or registry.runs_root).resolve()
    event_root = (root / "run_events").resolve()
    raw_path = entry.get("event_log_path")
    events: list[dict[str, Any]] = []
    safe_offset = max(int(offset or 0), 0)
    new_offset = safe_offset

    if raw_path:
        event_path = Path(str(raw_path)).resolve()
        try:
            event_path.relative_to(event_root)
        except ValueError as exc:
            raise RunControlError(
                "invalid_event_log_path",
                "Run event log path is outside THESIS_JOBS_ROOT/run_events.",
                500,
            ) from exc
        if event_path.exists():
            with open(event_path, "r", encoding="utf-8") as fh:
                fh.seek(safe_offset)
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        events.append({"event": "parse_error", "raw": line[:500]})
                new_offset = fh.tell()

    return {
        "run_id": run_id,
        "events": events,
        "offset": new_offset,
        "running": entry["status"] == "running",
        "status": entry["status"],
        "exit_code": entry["exit_code"],
        "event_log_path": raw_path,
    }


def build_argv(
    *,
    script: str,
    python_exe: str | None = None,
    db: str | None = None,
    source: str | None = None,
    doc_id: str | None = None,
    chapters: list[str] | None = None,
    configs: list[str] | None = None,
    config: str | None = None,
    config_file: str | None = None,
    profile: str | None = None,
    experiment: str | None = None,
    cache: str | None = None,
    report: str | None = None,
    out: str | None = None,
    prepass: str | None = None,
    mode: str | None = None,
    compare: str | None = None,
    human: str | None = None,
    project: str | None = None,
    chroma: str | None = None,
    gold_variants: str | None = None,
    context_budget: int | None = None,
    extra_args: list[str] | None = None,
    allow_api: bool = False,
    freeze: bool = False,
    memory_report: str | None = None,
    smoke_query: str | None = None,
    event_log: str | None = None,
    run_id: str | None = None,
) -> list[str]:
    script = validate_script(script)
    exe = python_exe or sys.executable
    argv = [exe, "-m", f"pipeline.scripts.{script}"]

    if not allow_api and script in API_CAPABLE_SCRIPTS:
        flag = PREFLIGHT_ONLY_FLAGS.get(script)
        if flag is None:
            raise RunControlError(
                "dry_run_not_supported",
                f"{script} can call an API and has no safe dry-run mode in APP-C01. "
                "Use allow_api=true with a prompt-preview token, or run it outside the cockpit.",
                400,
            )
    else:
        flag = None

    if script == "run_translate":
        _append_required(argv, "--db", db, "db")
        _append_required_list(argv, "--chapters", chapters, "chapters")
        if profile:
            argv += ["--profile", str(profile)]
        if configs:
            argv += ["--configs", *_list(configs)]
        elif config:
            argv += ["--config", str(config)]
        if experiment:
            argv += ["--experiment", str(experiment)]
        if config_file:
            argv += ["--config-file", str(config_file)]
        if cache:
            argv += ["--cache", str(cache)]
        if report:
            argv += ["--report", str(report)]
        if context_budget is not None:
            argv += ["--context-budget", str(int(context_budget))]
        if event_log:
            argv += ["--event-log", str(event_log)]
            if run_id:
                argv += ["--run-id", str(validate_run_id(run_id, required=True))]
    elif script == "run_prepass":
        if db:
            argv += ["--db", str(db)]
        if source:
            argv += ["--source", str(source)]
        if doc_id:
            argv += ["--doc-id", str(doc_id)]
        _append_required_list(argv, "--chapters", chapters, "chapters")
        if out:
            argv += ["--out", str(out)]
        if mode:
            argv += ["--mode", str(mode)]
        if config_file:
            argv += ["--config", str(config_file)]
        if cache:
            argv += ["--cache", str(cache)]
        if freeze:
            argv.append("--freeze")
        if memory_report:
            argv += ["--memory-report", str(memory_report)]
    elif script == "build_memory":
        _append_required(argv, "--source", source, "source")
        _append_required(argv, "--prepass", prepass, "prepass")
        _append_required(argv, "--db", db, "db")
        if freeze:
            argv.append("--freeze")
        if report:
            argv += ["--report", str(report)]
    elif script == "build_index":
        _append_required(argv, "--db", db, "db")
        _append_required(argv, "--chroma", chroma, "chroma")
        _append_required_list(argv, "--chapters", chapters, "chapters")
        if config_file:
            argv += ["--config-file", str(config_file)]
        if cache:
            argv += ["--cache", str(cache)]
        if out:
            argv += ["--out", str(out)]
        if smoke_query is not None:
            argv += ["--smoke-query", str(smoke_query)]
    elif script == "run_judge":
        _append_required(argv, "--db", db, "db")
        _append_required(argv, "--experiment", experiment, "experiment")
        _append_required(argv, "--compare", compare, "compare")
        _append_required_list(argv, "--chapters", chapters, "chapters")
        _append_required(argv, "--out", out, "out")
        if human:
            argv += ["--human", str(human)]
        if config_file:
            argv += ["--config", str(config_file)]
        if cache:
            argv += ["--cache", str(cache)]
    elif script == "score_consistency":
        _append_required(argv, "--project", project, "project")
        _append_required(argv, "--out", out, "out")
    elif script == "score_run":
        _append_required(argv, "--db", db, "db")
        if experiment:
            argv += ["--experiment", str(experiment)]
        if config:
            argv += ["--config", str(config)]
        if prepass:
            argv += ["--prepass", str(prepass)]
        if source:
            argv += ["--source", str(source)]
        if chapters:
            argv += ["--chapters", *_list(chapters)]
        if profile:
            argv += ["--profile", str(profile)]
        if gold_variants:
            argv += ["--gold-variants", str(gold_variants)]
        _append_required(argv, "--out", out, "out")
    elif script == "snapshot_runs":
        _append_required(argv, "--db", db, "db")
        _append_required(argv, "--out", out, "out")

    if flag and flag not in argv:
        argv.append(flag)
    if extra_args:
        argv.extend(_list(extra_args))

    validate_args(argv[3:])
    return argv


def _render_translate_prompt_preview(
    *,
    db: str,
    chapters: list[str],
    configs: list[str] | None,
    config: str | None,
    profile: str | None,
    cache: str | None,
    context_budget: int | None,
    tool_root: Path,
) -> dict[str, Any]:
    _ensure_tool_import_path(tool_root)
    from pipeline.agents.llm_client import estimate_prompt_tokens
    from pipeline.agents.llm_config import load_llm_config
    from pipeline.retrieval.context_builder import build_context_pack, plan_anchors
    from pipeline.translate.prompt import build_messages, prompt_version_for_config
    from pipeline.translate.profiles import get_profile
    from pipeline.translate.windower import build_windows

    db_path = _resolve_tool_path(db, tool_root)
    if not db_path.exists():
        raise RunControlError("db_not_found", f"DB not found: {db_path}", 404)
    profile_obj = get_profile(profile or "literary_v1")
    selected_configs = [item.upper() for item in (_list(configs) or ([config.upper()] if config else ["S0"]))]
    budget = int(context_budget or 500)
    llm_config = load_llm_config(tool_root / "pipeline" / "configs" / "llm_translate.yaml")

    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        doc_id = _single_doc_id(connection)
        windows = build_windows(
            connection,
            doc_id,
            chapters,
            block_types=profile_obj.translatable_block_types,
        )
        if not windows:
            raise RunControlError("no_windows", "No translation windows found for preview.", 400)

        estimates_by_config: dict[str, dict[str, Any]] = {}
        representative: dict[str, Any] | None = None
        representative_score = -1
        for cfg in selected_configs:
            estimates: list[int] = []
            injected_counts: list[int] = []
            for window in windows:
                blocks = _fetch_window_blocks(connection, window)
                context_pack = None
                score = 0
                if cfg == "S1":
                    anchors = plan_anchors(connection, blocks, profile_name=profile_obj.name)
                    context_pack = build_context_pack(
                        connection,
                        window,
                        anchors,
                        budget_tokens=budget,
                    )
                    score = (
                        len(getattr(context_pack, "glossary_lines", []) or [])
                        + len(getattr(context_pack, "entity_lines", []) or [])
                        + len(getattr(context_pack, "address_lines", []) or [])
                    )
                    injected_counts.append(score)
                messages = build_messages(
                    blocks,
                    prompt_version=prompt_version_for_config(cfg, profile_obj.name),
                    config=cfg,
                    context_pack=context_pack,
                    profile_name=profile_obj.name,
                )
                prompt_tokens = estimate_prompt_tokens(
                    messages,
                    response_format={"type": "json_object"},
                )
                estimates.append(prompt_tokens)
                current_score = score * 100000 + prompt_tokens
                if representative is None or current_score > representative_score:
                    representative_score = current_score
                    representative = {
                        "config": cfg,
                        "window_id": window.window_id,
                        "block_ids": list(window.block_ids),
                        "prompt_version": prompt_version_for_config(cfg, profile_obj.name),
                        "prompt_tokens_est": prompt_tokens,
                        "messages": messages,
                        "context_pack": (
                            context_pack.to_dict()
                            if context_pack is not None and hasattr(context_pack, "to_dict")
                            else None
                        ),
                    }
            total_prompt = sum(estimates)
            estimates_by_config[cfg] = {
                "windows": len(estimates),
                "prompt_tokens_min": min(estimates) if estimates else 0,
                "prompt_tokens_avg": round(mean(estimates), 2) if estimates else 0,
                "prompt_tokens_max": max(estimates) if estimates else 0,
                "prompt_tokens_total_est": total_prompt,
                "upper_total_with_max_output": total_prompt + len(estimates) * llm_config.max_output_tokens,
                "injected_terms_min": min(injected_counts) if injected_counts else 0,
                "injected_terms_avg": round(mean(injected_counts), 2) if injected_counts else 0,
                "injected_terms_max": max(injected_counts) if injected_counts else 0,
            }
    finally:
        connection.close()

    assert representative is not None
    return {
        "preview_kind": "real_translate_prompt",
        "db": str(db_path),
        "chapters": chapters,
        "profile": profile_obj.name,
        "configs": selected_configs,
        "token_estimate": {
            "configs": estimates_by_config,
            "upper_total_all_configs": sum(
                item["upper_total_with_max_output"] for item in estimates_by_config.values()
            ),
            "daily_token_cap": llm_config.daily_token_cap,
            "prompt_token_cap": llm_config.prompt_token_cap,
            "max_output_tokens_per_call": llm_config.max_output_tokens,
        },
        "representative_prompt": representative,
        "cache_path": cache or "data/jobs/translate_cache.sqlite3",
    }


def _fetch_window_blocks(connection: sqlite3.Connection, window: Any) -> list[dict[str, Any]]:
    placeholders = ",".join("?" * len(window.block_ids))
    rows = connection.execute(
        f"""
        SELECT block_id, doc_id, chapter_id, order_index, block_type,
               text AS clean_text, original_text AS source_text
        FROM blocks
        WHERE block_id IN ({placeholders})
        ORDER BY order_index
        """,
        list(window.block_ids),
    ).fetchall()
    return [dict(row) for row in rows]


def _single_doc_id(connection: sqlite3.Connection) -> str:
    row = connection.execute("SELECT doc_id FROM documents ORDER BY doc_id LIMIT 1").fetchone()
    if row is None:
        raise RunControlError("empty_db", "No document found in DB.", 400)
    return str(row["doc_id"])


def _append_required(argv: list[str], flag: str, value: str | None, field: str) -> None:
    if value is None or str(value).strip() == "":
        raise RunControlError("missing_arg", f"{field} is required for this script.", 400)
    argv.extend([flag, str(value)])


def _append_required_list(
    argv: list[str],
    flag: str,
    values: list[str] | None,
    field: str,
) -> None:
    rows = _list(values)
    if not rows:
        raise RunControlError("missing_arg", f"{field} is required for this script.", 400)
    argv.extend([flag, *rows])


def _list(values: list[str] | tuple[str, ...] | str | None) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        chunks = []
        for part in values.split(","):
            chunks.extend(item for item in part.split() if item)
        return [str(item).strip() for item in chunks if str(item).strip()]
    return [str(item).strip() for item in values if str(item).strip()]


def _argv_digest(argv: list[str]) -> str:
    import hashlib

    payload = json.dumps(argv[1:], ensure_ascii=False, sort_keys=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _redact_argv(argv: list[str]) -> list[str]:
    redacted: list[str] = []
    skip_next = False
    for item in argv:
        if skip_next:
            redacted.append("[redacted]")
            skip_next = False
            continue
        redacted.append(str(item))
        if str(item).lower() in {"--api-key", "--key", "--token"}:
            skip_next = True
    return redacted


def _event_log_path(jobs_root: Path, run_id: str) -> Path:
    safe_run_id = validate_run_id(run_id, required=True)
    return Path(jobs_root).resolve() / "run_events" / f"{safe_run_id}.jsonl"


def _ensure_tool_import_path(tool_root: Path) -> None:
    value = str(tool_root.resolve())
    if value not in sys.path:
        sys.path.insert(0, value)


def _resolve_tool_path(value: str, tool_root: Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return tool_root / path


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
