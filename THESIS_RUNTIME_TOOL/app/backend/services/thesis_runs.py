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

from pipeline.lib.event_reader import read_jsonl_events


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
        "run_experiment_cascade",
        "builder_v2_reelection",
        "preflight_check",
        "score_sf_qe",
        "score_sf_bt",
        "probe_pj",
        "score_pj",
        "agreement_analysis",
        "score_consistency",
        "score_run",
        "run_one_button",
        "run_d2l_project_campaign",
        "snapshot_runs",
    }
)

API_CAPABLE_SCRIPTS = frozenset(
    {
        "run_prepass",
        "run_translate",
        "run_judge",
        "build_index",
        "run_experiment_cascade",
        "score_sf_bt",
        "probe_pj",
        "score_pj",
        "run_one_button",
        "run_d2l_project_campaign",
    }
)

PREFLIGHT_ONLY_FLAGS = {
    "run_prepass": "--preflight-only",
    "run_translate": "--preflight-only",
    "run_experiment_cascade": "--preflight-only",
    "builder_v2_reelection": "--preflight-only",
    "run_one_button": "--estimate-only",
    "run_d2l_project_campaign": "--dry-run",
}

PROMPT_PREVIEW_SUPPORTED = frozenset({"run_translate"})
ESTIMATE_PREVIEW_SUPPORTED = frozenset(
    {
        "run_translate",
        "run_experiment_cascade",
        "builder_v2_reelection",
        "run_one_button",
        "run_d2l_project_campaign",
    }
)

D2L_PROJECT_CAMPAIGN_SCRIPT = "run_d2l_project_campaign"
D2L_COMPONENT_ID = "translation"
D2L_PROFILE_ID = "technical_d2l_v1"


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
    run_identity_digest: str | None = None


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

    def refresh(self) -> None:
        rows: dict[str, dict[str, Any]] = {}
        if self._registry_path.exists():
            with open(self._registry_path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        rows[str(entry["run_id"])] = entry
                    except (json.JSONDecodeError, KeyError, TypeError):
                        continue
        with self._lock:
            self._runs = rows

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
        attempt_index: int | None = None,
        resumed_from: str | None = None,
        attempt_log_path: str | None = None,
        run_dir: str | None = None,
        manifest_path: str | None = None,
        workflow_run_id: str | None = None,
        component_id: str | None = None,
        component_run_id: str | None = None,
        component_attempt_id: int | None = None,
        selected_chapter_ids: list[str] | None = None,
        profile_id: str | None = None,
        source_binding_sha256: str | None = None,
        campaign_config_sha256: str | None = None,
        campaign_seal_sha256: str | None = None,
        launch_binding_sha256: str | None = None,
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
            "run_dir": run_dir,
            "manifest_path": manifest_path,
            "attempt_index": attempt_index,
            "resumed_from": resumed_from,
            "attempt_log_path": attempt_log_path,
            "workflow_run_id": workflow_run_id,
            "component_id": component_id,
            "component_run_id": component_run_id,
            "component_attempt_id": component_attempt_id,
            "selected_chapter_ids": list(selected_chapter_ids or []),
            "profile_id": profile_id,
            "source_binding_sha256": source_binding_sha256,
            "campaign_config_sha256": campaign_config_sha256,
            "campaign_seal_sha256": campaign_seal_sha256,
            "launch_binding_sha256": launch_binding_sha256,
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
        self.refresh()
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
                    "run_dir": r.get("run_dir"),
                    "manifest_path": r.get("manifest_path"),
                    "attempt_index": r.get("attempt_index"),
                    "resumed_from": r.get("resumed_from"),
                    "workflow_run_id": r.get("workflow_run_id"),
                    "component_id": r.get("component_id"),
                    "component_run_id": r.get("component_run_id"),
                    "component_attempt_id": r.get("component_attempt_id"),
                    "selected_chapter_ids": r.get("selected_chapter_ids") or [],
                    "profile_id": r.get("profile_id"),
                    "source_binding_sha256": r.get("source_binding_sha256"),
                    "campaign_config_sha256": r.get("campaign_config_sha256"),
                    "campaign_seal_sha256": r.get("campaign_seal_sha256"),
                    "launch_binding_sha256": r.get("launch_binding_sha256"),
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


def one_button_paths(*, jobs_root: Path, job_id: str, run_id: str) -> dict[str, Path]:
    """Return canonical one-button paths and reject escapes from data/jobs.

    The UI-2 contract requires RunControl to know the run directory before
    spawn, so these paths are derived from job_id + run_id rather than from
    client-provided paths.
    """
    job = validate_job_id(job_id, required=True)
    run = validate_run_id(run_id, required=True)
    root = Path(jobs_root).resolve()
    run_dir = (root / job / "one_button" / run).resolve()
    event_log = (root / "run_events" / f"{run}.jsonl").resolve()
    # The translate stage's workdb must NOT live under the frozen DB's job dir
    # (data/jobs/<job>/): run_translate refuses any --workdb inside the frozen
    # DB's parent directory to protect the read-only frozen DB. Place it under a
    # dedicated work root (a sibling of run_events, still inside
    # THESIS_JOBS_ROOT) so it stays managed but clear of the job dir.
    workdb = (root / "_work" / "one_button" / job / run / "workdb.sqlite3").resolve()
    for candidate in (run_dir, event_log, workdb):
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise RunControlError(
                "invalid_one_button_path",
                "Resolved one-button path is outside THESIS_JOBS_ROOT.",
                500,
            ) from exc
    return {
        "run_dir": run_dir,
        "manifest_path": run_dir / "manifest.json",
        "workdb": workdb,
        "event_log_path": event_log,
    }


def d2l_campaign_paths(*, jobs_root: Path, job_id: str, run_id: str) -> dict[str, Path]:
    """Return server-owned paths for one D2L component lineage.

    The source job remains outside this tree and is read by the campaign
    preparer.  The campaign root is the only writable authority for the
    component package, checkpoint and isolated work database.
    """

    job = validate_job_id(job_id, required=True)
    run = validate_run_id(run_id, required=True)
    root = Path(jobs_root).resolve()
    campaign_root = (root / "_work" / "d2l_campaign" / job / run).resolve()
    values = {
        "campaign_root": campaign_root,
        "component_root": campaign_root / "component",
        "manifest_path": campaign_root / "component" / "component_manifest.json",
        "event_log_path": campaign_root / "component" / "events.jsonl",
        "runtime_root": root / "_runtime" / "d2l" / job / run,
    }
    for candidate in values.values():
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise RunControlError(
                "invalid_d2l_campaign_path",
                "Resolved D2L campaign path is outside THESIS_JOBS_ROOT.",
                500,
            ) from exc
    return values


def d2l_component_ids(run_id: str) -> dict[str, str]:
    run = validate_run_id(run_id, required=True)
    return {
        "workflow_run_id": f"wf_{run}",
        "component_id": D2L_COMPONENT_ID,
        "component_run_id": f"tr_{run}",
        "reserved_evaluation_component_run_id": f"eval_{run}",
    }


def _d2l_credential_files(*, required: bool) -> dict[str, Path]:
    """Resolve external credential *paths* without reading their contents."""

    bindings = (
        (
            "credential.shopaikey_gemini_proxy_v1",
            "THESIS_D2L_SHOPAPI_CREDENTIAL_FILE",
        ),
        ("credential.modelapi_shared_v1", "THESIS_D2L_MODELAPI_CREDENTIAL_FILE"),
    )
    result: dict[str, Path] = {}
    for credential_ref, env_name in bindings:
        raw = os.environ.get(env_name, "").strip()
        if not raw:
            if required:
                raise RunControlError(
                    "d2l_credential_path_missing",
                    f"{env_name} is required for a live D2L campaign.",
                    503,
                )
            continue
        path = Path(raw).expanduser()
        if not path.is_absolute() or not path.is_file():
            raise RunControlError(
                "d2l_credential_path_invalid",
                f"{env_name} must name an existing absolute credential file.",
                503,
            )
        result[credential_ref] = path.resolve()
    if required and len(result) != len(bindings):
        raise RunControlError(
            "d2l_credential_path_incomplete",
            "Live D2L credential bindings do not cover the sealed sources.",
            503,
        )
    return result


def _d2l_code_revision(tool_root: Path) -> str:
    from pipeline.prepass.d2l_project_campaign_v2 import resolve_code_revision

    try:
        return resolve_code_revision(tool_root, require_clean=True)
    except Exception as exc:
        raise RunControlError(
            "d2l_code_not_sealable",
            "D2L live campaign requires a clean committed runtime tree.",
            409,
        ) from exc


def _d2l_semantic_role_preview(row: dict[str, Any]) -> dict[str, Any]:
    """Project one sealed campaign role without flattening its source schema."""

    prompt = row["prompt"]
    generation = row["generation"]
    output_contract = row["output_contract"]
    return {
        "role_id": row["role_id"],
        "stage_id": row["stage_id"],
        "model_id": row["model_id"],
        "source_id": row["source_id"],
        "prompt_id": prompt["id"],
        "prompt_sha256": prompt["sha256"],
        "response_schema_sha256": row["response_schema_sha256"],
        "validator_id": row["validator_id"],
        "validator_sha256": row["validator_sha256"],
        "max_input_tokens": generation["max_input_tokens"],
        "max_output_tokens": generation["max_output_tokens"],
        "temperature": generation["temperature"],
        "reasoning_effort": generation["reasoning_effort"],
        "verbosity": generation["verbosity"],
        "structured_output_mode": output_contract["structured_output_mode"],
        "output_envelope": output_contract["envelope"],
        "semantic_retry_cap": row["semantic_retry_cap"],
        "semantic_role_sha256": row["semantic_role_sha256"],
    }


def _d2l_preview_source(
    *,
    job_root: Path,
    chapters: list[str],
    workflow_run_id: str,
    component_run_id: str,
    hard_total_token_cap: int | None,
    reserved_cost_cap_usd: str | None,
    tool_root: Path,
) -> dict[str, Any]:
    """Build a read-only D2L forecast from the canonical project package."""

    from pipeline.prepass.d2l_project_campaign_v2 import (
        build_campaign_config,
        build_selected_universe,
        load_project,
        select_chapters,
    )
    from pipeline.prepass.d2l_console_replay_contract_v1 import canonical_sha256

    try:
        project = load_project(job_root, verify_tree=True)
        selection_mode, selected = select_chapters(
            project,
            chapter_ids=chapters,
        )
        universe = build_selected_universe(
            project,
            selection_mode=selection_mode,
            selected_chapter_ids=selected,
        )
        revision = _d2l_code_revision(tool_root)
        config = build_campaign_config(
            project,
            universe,
            workflow_run_id=workflow_run_id,
            component_run_id=component_run_id,
            code_revision=revision,
            hard_total_token_cap=hard_total_token_cap,
            reserved_cost_cap_usd=reserved_cost_cap_usd,
        )
    except RunControlError:
        raise
    except Exception as exc:
        raise RunControlError(
            "d2l_preview_failed",
            f"D2L campaign preview could not validate the source package: {exc}",
            409,
        ) from exc

    limits = config["limits"]
    chapter_counts = []
    for chapter in universe["chapters"]:
        chapter_counts.append(
            {
                "chapter_id": chapter["chapter_id"],
                "block_count": sum(
                    int(value)
                    for value in (chapter.get("channel_counts") or {}).values()
                ),
                "channel_counts": dict(chapter.get("channel_counts") or {}),
            }
        )
    return {
        "project_id": project.manifest["project_id"],
        "source_binding": project.source_binding,
        "source_binding_sha256": canonical_sha256(project.source_binding),
        "source_db_sha256": project.source_db_sha256,
        "selected_chapter_ids": list(selected),
        "selected_block_count": int(universe["block_count"]),
        "selected_universe_sha256": universe["integrity"]["payload_sha256"],
        "chapter_counts": chapter_counts,
        "channel_counts": dict(universe["channel_counts"]),
        "window_counts": {
            "b1": universe["window_estimates"]["b1"]["window_count"],
            "translator_per_arm": universe["window_estimates"]["translator"]["window_count"],
        },
        "forecast_total_tokens": limits["forecast_total_tokens"],
        "forecast_token_range": dict(limits["forecast_token_range"]),
        "forecast_status": limits["forecast_status"],
        "hard_total_token_cap": limits["hard_total_token_cap"],
        "theoretical_role_reserve_tokens": limits["theoretical_role_reserve_tokens"],
        "hard_physical_attempt_cap": limits["hard_physical_attempt_cap"],
        "campaign_config_sha256": config["integrity"]["payload_sha256"],
        "reserved_cost_cap_usd": limits["reserved_cost_cap_usd"],
        "semantic_roles": [
            _d2l_semantic_role_preview(row)
            for row in config["semantic_roles"]
        ],
        "transport_sources": [
            {
                "source_id": row["source_id"],
                "source_revision": row["source_revision"],
                "credential_family": row["credential_family"],
                "physical_quota_bucket_id": row["physical_quota_bucket_id"],
                "supported_model_ids": list(row["supported_model_ids"]),
                "output_mode": row["output_mode"],
            }
            for row in config["transport_sources"].values()
        ],
        "cost_usd": None,
        "cost_basis": limits["cost_basis"],
        "profile_id": config["profile_id"],
        "pipeline_version": config["pipeline_version"],
        "code_revision": revision,
    }


def _d2l_launch_binding_sha256(
    *,
    job_id: str,
    planned_run_id: str,
    workflow_run_id: str,
    component_run_id: str,
    preview: dict[str, Any],
) -> str:
    import hashlib

    payload = {
        "schema": "d2l_app_launch_binding_v1",
        "job_id": validate_job_id(job_id, required=True),
        "planned_run_id": validate_run_id(planned_run_id, required=True),
        "workflow_run_id": workflow_run_id,
        "component_run_id": component_run_id,
        "profile_id": preview["profile_id"],
        "selected_chapter_ids": list(preview["selected_chapter_ids"]),
        "source_binding_sha256": preview["source_binding_sha256"],
        "source_db_sha256": preview["source_db_sha256"],
        "selected_universe_sha256": preview["selected_universe_sha256"],
        "campaign_config_sha256": preview["campaign_config_sha256"],
        "code_revision": preview["code_revision"],
        "hard_total_token_cap": preview["hard_total_token_cap"],
        "reserved_cost_cap_usd": preview["reserved_cost_cap_usd"],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest().upper()


def validate_api_gate(
    *,
    allow_api: bool,
    script: str,
    confirm_token: str | None,
    job_id: str | None,
    argv: list[str],
    run_identity_digest: str | None = None,
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
        if issued.run_identity_digest != run_identity_digest:
            raise RunControlError(
                "confirm_token_identity_mismatch",
                "confirm_token does not match the sealed source/config/run identity.",
                403,
            )
        _active_tokens.pop(token, None)
    return token


def build_resume_argv_from_entry(entry: dict[str, Any]) -> list[str]:
    """Build the exact real argv for resuming a one-button run.

    Resume is a real run, not an estimate replay.  Callers must pass the
    returned argv through validate_api_gate when allow_api=true.
    """
    script = entry.get("script")
    if script not in {"run_one_button", D2L_PROJECT_CAMPAIGN_SCRIPT}:
        raise RunControlError(
            "resume_not_supported",
            "This script does not expose a sealed resume contract.",
            400,
        )
    run_id = validate_run_id(str(entry.get("run_id") or ""), required=True)
    argv = [str(item) for item in (entry.get("argv") or [])]
    if not argv:
        raise RunControlError("resume_argv_missing", "Original run argv is missing.", 500)
    if script == D2L_PROJECT_CAMPAIGN_SCRIPT:
        cleaned = _strip_flags_with_values(
            argv,
            value_flags={
                "--workflow-run-id",
                "--component-run-id",
                "--chapter-id",
                "--chapter-range",
            },
            bare_flags={"--resume", "--all-chapters"},
            value_flag_counts={"--chapter-range": 2},
        )
        cleaned.append("--resume")
        validate_args(cleaned[3:])
        return cleaned
    cleaned = _strip_flags_with_values(
        argv,
        value_flags={"--run-id", "--attempt-id", "--resume"},
        bare_flags={"--estimate-only"},
    )
    cleaned.extend(["--resume", run_id])
    validate_args(cleaned[3:])
    return cleaned


def issue_estimate_token_for_argv(
    *,
    job_id: str,
    script: str,
    argv: list[str],
    preview_kind: str = "estimate_only",
    run_identity_digest: str | None = None,
) -> str:
    job = validate_job_id(job_id, required=True)
    validate_script(script)
    return _issue_preview_token(
        job_id=job,
        script=script,
        argv=argv,
        preview_kind=preview_kind,
        run_identity_digest=run_identity_digest,
    )


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


def generate_estimate_preview(
    *,
    job_id: str,
    script: str,
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
    planned_run_id: str | None = None,
    workdb: str | None = None,
    budget_cap_usd: float | None = None,
    with_s0: bool = False,
    hard_total_token_cap: int | None = None,
    reserved_cost_cap_usd: str | None = None,
    python_exe: str | None = None,
    tool_root: Path | None = None,
    jobs_root: Path | None = None,
) -> dict[str, Any]:
    script = validate_script(script)
    job = validate_job_id(job_id, required=True)
    if script not in ESTIMATE_PREVIEW_SUPPORTED:
        raise RunControlError(
            "estimate_preview_not_supported",
            f"Estimate preview is currently supported for {sorted(ESTIMATE_PREVIEW_SUPPORTED)} only.",
            400,
        )
    tool = Path(tool_root or Path.cwd()).resolve()
    jobs = Path(jobs_root or tool / "data" / "jobs").resolve()
    db_path = resolve_job_db(db=db, job_id=job, jobs_root=jobs)
    planned = validate_run_id(planned_run_id) or f"run_{uuid.uuid4().hex[:12]}"
    event_log_path = _event_log_path(jobs, planned)
    manifest_path = None
    run_dir = None
    resolved_workdb = workdb
    d2l_forecast: dict[str, Any] | None = None
    d2l_ids: dict[str, str] | None = None
    d2l_credentials: dict[str, Path] = {}
    d2l_paths: dict[str, Path] | None = None
    d2l_launch_binding: str | None = None
    if script == "run_one_button":
        paths = one_button_paths(jobs_root=jobs, job_id=job, run_id=planned)
        event_log_path = paths["event_log_path"]
        run_dir = paths["run_dir"]
        manifest_path = paths["manifest_path"]
        resolved_workdb = resolved_workdb or str(paths["workdb"])
    elif script == D2L_PROJECT_CAMPAIGN_SCRIPT:
        chapters_list = _list(chapters)
        if not chapters_list:
            raise RunControlError("chapters_required", "chapters are required for a D2L campaign.", 400)
        if len(set(chapters_list)) != len(chapters_list):
            raise RunControlError("duplicate_chapters", "D2L chapter selection contains duplicates.", 400)
        if profile not in (None, "", D2L_PROFILE_ID):
            raise RunControlError(
                "d2l_profile_invalid",
                f"D2L campaign requires profile {D2L_PROFILE_ID}.",
                400,
            )
        d2l_ids = d2l_component_ids(planned)
        d2l_paths = d2l_campaign_paths(jobs_root=jobs, job_id=job, run_id=planned)
        d2l_forecast = _d2l_preview_source(
            job_root=(jobs / job).resolve(),
            chapters=chapters_list,
            workflow_run_id=d2l_ids["workflow_run_id"],
            component_run_id=d2l_ids["component_run_id"],
            hard_total_token_cap=hard_total_token_cap,
            reserved_cost_cap_usd=reserved_cost_cap_usd,
            tool_root=tool,
        )
        d2l_credentials = _d2l_credential_files(required=True)
        d2l_launch_binding = _d2l_launch_binding_sha256(
            job_id=job,
            planned_run_id=planned,
            workflow_run_id=d2l_ids["workflow_run_id"],
            component_run_id=d2l_ids["component_run_id"],
            preview=d2l_forecast,
        )
    common_kwargs = dict(
        script=script,
        python_exe=python_exe,
        job_id=job,
        db=db_path,
        chapters=chapters,
        configs=configs,
        config=config,
        profile=profile,
        experiment=experiment,
        cache=cache,
        report=report,
        context_budget=context_budget,
        extra_args=extra_args,
        event_log=str(event_log_path),
        run_id=planned,
        workdb=resolved_workdb,
        budget_cap_usd=budget_cap_usd,
        with_s0=with_s0,
        hard_total_token_cap=hard_total_token_cap,
        reserved_cost_cap_usd=reserved_cost_cap_usd,
        campaign_root=str(d2l_paths["campaign_root"]) if d2l_paths else None,
        job_root=str((jobs / job).resolve()) if d2l_paths else None,
        workflow_run_id=d2l_ids["workflow_run_id"] if d2l_ids else None,
        component_run_id=d2l_ids["component_run_id"] if d2l_ids else None,
        code_root=str(tool) if d2l_paths else None,
        runtime_root=str(d2l_paths["runtime_root"]) if d2l_paths else None,
        credential_files=d2l_credentials if d2l_paths else None,
        )
    estimate_argv = build_argv(allow_api=False, **common_kwargs)
    run_argv = build_argv(allow_api=True, **common_kwargs)
    token = _issue_preview_token(
        job_id=job,
        script=script,
        argv=run_argv,
        preview_kind="estimate_only",
        run_identity_digest=d2l_launch_binding,
    )
    response = {
        "preview_kind": "estimate_only",
        "job_id": job,
        "script": script,
        "confirm_token": token,
        "planned_run_id": planned,
        "event_log_path": str(event_log_path),
        "run_dir": str(run_dir) if run_dir else None,
        "manifest_path": str(manifest_path) if manifest_path else None,
        "confirm_token_ttl_seconds": _CONFIRM_TOKEN_TTL_SECONDS,
        "estimate_argv_preview": _redact_argv(estimate_argv),
        "argv_preview": _redact_argv(run_argv),
        "estimate_by_stage": [
            {
                "script": script,
                "estimate_source": "script_preflight_or_estimate_only",
                "cost_usd_estimate": None,
            }
        ],
        "read_only": True,
        "policy": {
            "confirm_token_binding": "job_id + script + exact argv digest",
            "orchestrator_note": "One-button orchestrator consumes this interface in a later step.",
        },
    }
    if d2l_forecast is not None:
        # Keep server-managed filesystem roots out of the browser contract.
        public_argv = [
            python_exe or sys.executable,
            "-m",
            f"pipeline.scripts.{D2L_PROJECT_CAMPAIGN_SCRIPT}",
            "app-run",
            "[server-managed campaign/source/credential arguments]",
        ]
        response.update(
            {
                "workflow_run_id": d2l_ids["workflow_run_id"],
                "component_id": D2L_COMPONENT_ID,
                "component_run_id": d2l_ids["component_run_id"],
                "component_attempt_id": 1,
                "selected_chapter_ids": d2l_forecast["selected_chapter_ids"],
                "source_binding": d2l_forecast["source_binding"],
                "source_binding_sha256": d2l_forecast["source_binding_sha256"],
                "source_db_sha256": d2l_forecast["source_db_sha256"],
                "selected_universe_sha256": d2l_forecast["selected_universe_sha256"],
                "selected_block_count": d2l_forecast["selected_block_count"],
                "chapter_counts": d2l_forecast["chapter_counts"],
                "channel_counts": d2l_forecast["channel_counts"],
                "window_counts": d2l_forecast["window_counts"],
                "forecast_total_tokens": d2l_forecast["forecast_total_tokens"],
                "forecast_token_range": d2l_forecast["forecast_token_range"],
                "forecast_status": d2l_forecast["forecast_status"],
                "hard_total_token_cap": d2l_forecast["hard_total_token_cap"],
                "theoretical_role_reserve_tokens": d2l_forecast["theoretical_role_reserve_tokens"],
                "hard_physical_attempt_cap": d2l_forecast["hard_physical_attempt_cap"],
                "campaign_config_sha256": d2l_forecast["campaign_config_sha256"],
                "reserved_cost_cap_usd": d2l_forecast["reserved_cost_cap_usd"],
                "semantic_roles": d2l_forecast["semantic_roles"],
                "transport_sources": d2l_forecast["transport_sources"],
                "cost_usd": None,
                "cost_basis": d2l_forecast["cost_basis"],
                "profile_id": d2l_forecast["profile_id"],
                "pipeline_version": d2l_forecast["pipeline_version"],
                "code_revision": d2l_forecast["code_revision"],
                "launch_binding_sha256": d2l_launch_binding,
                "run_dir": None,
                "manifest_path": None,
                "event_log_path": None,
                "estimate_argv_preview": public_argv,
                "argv_preview": public_argv,
                "estimate_by_stage": [
                    {
                        "stage_id": "campaign",
                        "estimated_tokens": d2l_forecast["forecast_total_tokens"],
                        "cost_usd": None,
                        "cost_status": "unknown",
                    }
                ],
            }
        )
    return response


def _issue_preview_token(
    *,
    job_id: str,
    script: str,
    argv: list[str],
    preview_kind: str,
    run_identity_digest: str | None = None,
) -> str:
    token = uuid.uuid4().hex
    issued = PreviewToken(
        token=token,
        job_id=job_id,
        script=script,
        argv_digest=_argv_digest(argv),
        issued_at=time.time(),
        preview_kind=preview_kind,
        run_identity_digest=run_identity_digest,
    )
    with _token_lock:
        _active_tokens[token] = issued
    return token


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
                    creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                )
                registry.update_run(run_id, status="running", pid=proc.pid)
                proc.wait()
                current = registry.get_run(run_id) or {}
                if current.get("status") == "cancelled":
                    return
                if current.get("script") == D2L_PROJECT_CAMPAIGN_SCRIPT:
                    component_status = _read_d2l_component_status(current)
                    if component_status in {"succeeded", "paused"}:
                        status = "done" if proc.returncode == 0 else "failed"
                    elif component_status in {"failed", "cancelled"}:
                        status = "failed"
                    else:
                        # A successful process without a valid component
                        # manifest is not a successful pipeline run.
                        status = "error" if proc.returncode == 0 else "failed"
                else:
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


def _read_d2l_component_status(entry: dict[str, Any]) -> str | None:
    raw_path = entry.get("manifest_path")
    if not raw_path:
        return None
    try:
        payload = json.loads(Path(str(raw_path)).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    value = payload.get("status")
    return str(value) if isinstance(value, str) else None


def cancel_run(registry: RunRegistry, run_id: str) -> dict[str, Any]:
    registry.refresh()
    entry = registry.get_run(run_id)
    if entry is None:
        raise RunControlError("run_not_found", f"Run {run_id} not found.", 404)
    pid = int(entry.get("pid") or 0)
    if entry.get("status") not in {"pending", "running"}:
        raise RunControlError(
            "run_not_cancellable",
            f"Run {run_id} is not running or pending.",
            409,
        )
    if pid > 0:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            try:
                os.kill(pid, 15)
            except OSError:
                pass
    updated = registry.update_run(
        run_id,
        status="cancelled",
        exit_code=-15,
        ended_at=_utc_now(),
    )
    return updated or entry


def read_log(registry: RunRegistry, run_id: str, *, offset: int = 0) -> dict[str, Any]:
    registry.refresh()
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
    max_bytes: int = 256 * 1024,
    jobs_root: Path | None = None,
) -> dict[str, Any]:
    registry.refresh()
    entry = registry.get_run(run_id)
    if entry is None:
        raise RunControlError("run_not_found", f"Run {run_id} not found.", 404)

    root = Path(jobs_root or registry.runs_root).resolve()
    event_root = (root / "run_events").resolve()
    raw_path = entry.get("event_log_path")
    safe_offset = max(int(offset or 0), 0)
    safe_max_bytes = max(64, min(int(max_bytes or 256 * 1024), 1024 * 1024))
    result = {
        "events": [],
        "offset": safe_offset,
        "truncated": False,
        "partial_line": False,
        "max_bytes": safe_max_bytes,
    }

    if entry.get("script") == D2L_PROJECT_CAMPAIGN_SCRIPT:
        # D2L child events belong to the component package.  The Console
        # consumes the neutral parent replay stream; exposing this stream here
        # would make the browser a second relay and duplicate lifecycle facts.
        return {
            "run_id": run_id,
            "events": [],
            "offset": safe_offset,
            "truncated": False,
            "partial_line": False,
            "max_bytes": safe_max_bytes,
            "running": entry["status"] == "running",
            "status": entry["status"],
            "exit_code": entry["exit_code"],
            "event_log_path": None,
            "component_events_withheld": True,
        }

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
        result = read_jsonl_events(event_path, offset=safe_offset, max_bytes=safe_max_bytes)

    return {
        "run_id": run_id,
        "events": result["events"],
        "offset": result["offset"],
        "truncated": result["truncated"],
        "partial_line": result["partial_line"],
        "max_bytes": result["max_bytes"],
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
    job_id: str | None = None,
    workdb: str | None = None,
    budget_cap_usd: float | None = None,
    with_s0: bool = False,
    hard_total_token_cap: int | None = None,
    reserved_cost_cap_usd: str | None = None,
    campaign_root: str | None = None,
    job_root: str | None = None,
    workflow_run_id: str | None = None,
    component_run_id: str | None = None,
    code_root: str | None = None,
    runtime_root: str | None = None,
    credential_files: dict[str, Path] | None = None,
    resume: bool = False,
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
    # The D2L campaign has an explicit mutually-exclusive --dry-run/--live
    # mode.  Do not append a second generic --dry-run flag below.
    if script == D2L_PROJECT_CAMPAIGN_SCRIPT:
        flag = None

    extra = _list(extra_args)

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
    elif script == "run_one_button":
        if not _has_flag(extra, "--job-id"):
            _append_required(argv, "--job-id", job_id, "job_id")
        if not _has_flag(extra, "--chapters"):
            _append_required_list(argv, "--chapters", chapters, "chapters")
        if not _has_flag(extra, "--workdb"):
            _append_required(argv, "--workdb", workdb, "workdb")
        if not _has_flag(extra, "--budget-cap-usd"):
            _append_required(argv, "--budget-cap-usd", budget_cap_usd, "budget_cap_usd")
        if with_s0 and not _has_flag(extra, "--with-s0"):
            argv.append("--with-s0")
        if db and not _has_flag(extra, "--db"):
            argv += ["--db", str(db)]
        if profile and not _has_flag(extra, "--profile"):
            argv += ["--profile", str(profile)]
        if experiment and not _has_flag(extra, "--experiment"):
            argv += ["--experiment", str(experiment)]
        if context_budget is not None and not _has_flag(extra, "--context-budget"):
            argv += ["--context-budget", str(int(context_budget))]
        if cache and not _has_flag(extra, "--cache-root"):
            argv += ["--cache-root", str(cache)]
        if event_log:
            argv += ["--event-log", str(event_log)]
        if run_id:
            argv += ["--run-id", str(validate_run_id(run_id, required=True))]
    elif script == D2L_PROJECT_CAMPAIGN_SCRIPT:
        argv.append("app-run")
        _append_required(argv, "--job-root", job_root, "job_root")
        _append_required(argv, "--campaign-root", campaign_root, "campaign_root")
        if not resume:
            _append_required(argv, "--workflow-run-id", workflow_run_id, "workflow_run_id")
            _append_required(argv, "--component-run-id", component_run_id, "component_run_id")
            chapter_rows = _list(chapters)
            if not chapter_rows:
                raise RunControlError("missing_arg", "chapters is required for this script.", 400)
            for chapter_id in chapter_rows:
                argv += ["--chapter-id", chapter_id]
        if hard_total_token_cap is not None:
            argv += ["--hard-total-token-cap", str(int(hard_total_token_cap))]
        if reserved_cost_cap_usd is not None:
            argv += ["--reserved-cost-cap-usd", str(reserved_cost_cap_usd)]
        if code_root:
            argv += ["--code-root", str(code_root)]
        if resume:
            argv.append("--resume")
        argv.append("--live" if allow_api else "--dry-run")
        if runtime_root:
            argv += ["--runtime-root", str(runtime_root)]
        if allow_api:
            for credential_ref, path in sorted((credential_files or {}).items()):
                argv += ["--credential-file", f"{credential_ref}={Path(path).resolve()}"]
    elif script in {
        "run_experiment_cascade",
        "builder_v2_reelection",
        "preflight_check",
        "score_sf_qe",
        "score_sf_bt",
        "probe_pj",
        "score_pj",
        "agreement_analysis",
    }:
        # One-button scripts have heterogeneous CLIs.  Until the orchestrator
        # owns a typed command model, RunControl passes validated extra_args
        # through and only adds generic cost-gate/event flags where supported.
        pass

    if flag and flag not in argv:
        argv.append(flag)
    if extra:
        argv.extend(extra)

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


def _append_required(argv: list[str], flag: str, value: Any | None, field: str) -> None:
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


def _has_flag(args: list[str], flag: str) -> bool:
    return any(str(item) == flag for item in args)


def _strip_flags_with_values(
    argv: list[str],
    *,
    value_flags: set[str],
    bare_flags: set[str],
    value_flag_counts: dict[str, int] | None = None,
) -> list[str]:
    cleaned: list[str] = []
    counts = {flag: 1 for flag in value_flags}
    counts.update(value_flag_counts or {})
    index = 0
    while index < len(argv):
        value = str(argv[index])
        if value in counts:
            index += 1 + counts[value]
            continue
        if value in bare_flags:
            index += 1
            continue
        cleaned.append(value)
        index += 1
    return cleaned


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
        if str(item).lower() in {
            "--api-key",
            "--key",
            "--token",
            "--credential-file",
        }:
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
