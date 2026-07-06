from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pipeline.lib.event_reader import read_jsonl_events


TOOL_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = Path("data/jobs/d2l_p1/memory.sqlite3")
DEFAULT_PROFILE = "technical_d2l_v1"
DEFAULT_DOC_ID = "d2l"
DEFAULT_CONTEXT_BUDGET = 1500
DEFAULT_EXPERIMENT_PREFIX = "one_button"
DEFAULT_PY311 = os.environ.get("PY311") or os.environ.get("COMET_PYTHON") or ""
DEFAULT_SF_BT_CACHE = Path("data/reports/exp_s0s1_builderv2_v1/sf_bt_pilot_cache.sqlite3")
DEFAULT_PJ_CACHE = Path("data/reports/exp_s0s1_builderv2_v1/pj_probe_cache.sqlite3")
DEFAULT_GOLD_VARIANTS = Path("data/eval/d2l_gold_variants.csv")
FROZEN_DB_SHA256 = "64D98965F8859869931152B2AA814FB03AFBF15E6A9853532FD0EF28B555C715"
EVENT_SCHEMA_VERSION = 1
EVENT_SCHEMA_NAME = "one_button_event_v1"


@dataclass
class StageSpec:
    name: str
    script: str
    argv: list[str]
    artifact_path: Path | None = None
    input_paths: list[Path] = field(default_factory=list)
    stage_event_log_path: Path | None = None
    stdout_log_path: Path | None = None
    cost_bearing: bool = False
    estimate_argv: list[str] | None = None
    confirm_usd: float | None = None
    interpreter: str = sys.executable
    retry_once: bool = False
    never_skip: bool = False
    phase_checkpoint: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class MergedEventWriter:
    def __init__(self, path: Path, *, run_id: str, attempt_id: int) -> None:
        self.path = path
        self.run_id = run_id
        self.attempt_id = int(attempt_id)
        self.seq = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(
        self,
        event: str,
        *,
        stage: str,
        agent: str,
        script: str,
        severity: str = "info",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.seq += 1
        row = {
            "v": EVENT_SCHEMA_VERSION,
            "schema": EVENT_SCHEMA_NAME,
            "event_id": f"{self.run_id}:{self.attempt_id}:orchestrator:{self.seq}",
            "run_id": self.run_id,
            "attempt_id": self.attempt_id,
            "seq": self.seq,
            "ts": datetime.now(UTC).isoformat(),
            "stage": stage,
            "script": script,
            "agent": agent,
            "event": event,
            "event_type": event,
            "severity": severity,
            "payload": _json_safe(payload or {}),
        }
        _append_jsonl_atomic(self.path, [row])
        return row

    def reemit_stage_events(self, events: list[dict[str, Any]], *, stage: StageSpec) -> None:
        rows: list[dict[str, Any]] = []
        for event in events:
            self.seq += 1
            payload = _stage_event_payload(event)
            payload["src_seq"] = event.get("seq")
            payload["src_event_id"] = event.get("event_id")
            payload["src_stage_event_log_path"] = str(stage.stage_event_log_path) if stage.stage_event_log_path else None
            rows.append(
                {
                    "v": EVENT_SCHEMA_VERSION,
                    "schema": EVENT_SCHEMA_NAME,
                    "event_id": f"{self.run_id}:{self.attempt_id}:orchestrator:{self.seq}",
                    "run_id": self.run_id,
                    "attempt_id": self.attempt_id,
                    "seq": self.seq,
                    "ts": datetime.now(UTC).isoformat(),
                    "stage": str(event.get("stage") or stage.name),
                    "script": str(event.get("script") or stage.script),
                    "agent": str(event.get("agent") or stage.metadata.get("agent") or stage.name),
                    "event": str(event.get("event") or event.get("event_type") or "stage_event"),
                    "event_type": str(event.get("event_type") or event.get("event") or "stage_event"),
                    "severity": str(event.get("severity") or "info"),
                    "payload": _json_safe(payload),
                }
            )
        _append_jsonl_atomic(self.path, rows)


def _stage_event_payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = dict(event.get("payload") or {})
    for key in ("pack_summary", "window_id", "config", "profile", "experiment_id", "block_ids"):
        if key in event and event[key] is not None:
            payload[key] = event[key]
    if "pack_summary" not in payload:
        context_summary = payload.get("context_summary") or event.get("context_summary")
        pack_summary = _pack_summary_from_context_summary(context_summary)
        if pack_summary is not None:
            payload["pack_summary"] = pack_summary
    return payload


def _pack_summary_from_context_summary(context_summary: Any) -> dict[str, int] | None:
    if not isinstance(context_summary, dict):
        return None
    injected = context_summary.get("included_count")
    if injected is None:
        return None
    est_tokens = context_summary.get("estimated_tokens", context_summary.get("token_estimate", 0))
    summary = {
        "injected": int(injected or 0),
        "dropped_by_budget": int(context_summary.get("dropped_by_budget_count") or 0),
        "est_tokens": int(est_tokens or 0),
    }
    for source_key, target_key in (
        ("mandatory", "mandatory"),
        ("soft", "soft"),
        ("preserve", "preserve"),
        ("quarantine", "quarantine"),
        ("address", "address"),
    ):
        if source_key in context_summary:
            summary[target_key] = int(context_summary.get(source_key) or 0)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="One-button thesis pipeline orchestrator.")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--chapters", nargs="+", required=True)
    parser.add_argument("--workdb", required=True)
    parser.add_argument("--with-s0", action="store_true")
    parser.add_argument("--budget-cap-usd", type=float, required=True)
    parser.add_argument("--resume", default="")
    parser.add_argument("--event-log", default="")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--attempt-id", type=int, default=1)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--profile", default=DEFAULT_PROFILE)
    parser.add_argument("--doc-id", default=DEFAULT_DOC_ID)
    parser.add_argument("--context-budget", type=int, default=DEFAULT_CONTEXT_BUDGET)
    parser.add_argument("--experiment", default="")
    parser.add_argument("--py311", default=DEFAULT_PY311)
    parser.add_argument("--cache-root", default="")
    parser.add_argument("--sf-bt-cache-db", default=str(DEFAULT_SF_BT_CACHE))
    parser.add_argument("--pj-cache-db", default=str(DEFAULT_PJ_CACHE))
    parser.add_argument("--gold-variants", default=str(DEFAULT_GOLD_VARIANTS))
    parser.add_argument("--estimate-only", action="store_true")
    args = parser.parse_args()
    args.py311 = args.py311 or _default_py311()
    if len(args.chapters) != 1:
        raise SystemExit(
            "run_one_button currently supports exactly one chapter per run; "
            "builder/cascade/PJ stages are chapter-scoped."
        )

    run_id = _safe_id(args.resume or args.run_id or f"onebtn_{uuid.uuid4().hex[:10]}")
    attempt_id = int(args.attempt_id or 1)
    db_path = _resolve(args.db)
    workdb_path = _resolve(args.workdb)
    _assert_frozen_db_baseline(db_path)
    run_dir = TOOL_ROOT / "data" / "jobs" / _safe_id(args.job_id) / "one_button" / run_id
    manifest_path = run_dir / "manifest.json"
    merged_event_log_path = _resolve(args.event_log) if args.event_log else TOOL_ROOT / "data" / "jobs" / "run_events" / f"{run_id}.jsonl"
    experiment = str(args.experiment or f"{DEFAULT_EXPERIMENT_PREFIX}_{run_id}")
    cache_root = _resolve(args.cache_root) if args.cache_root else run_dir / "cache"
    _mkdirs(run_dir, merged_event_log_path.parent, cache_root)

    if args.resume:
        manifest = _load_manifest(manifest_path)
        if not manifest:
            raise SystemExit(f"Cannot resume; manifest not found: {manifest_path}")
        attempt_id = int(manifest.get("attempt", 0) or 0) + 1
    else:
        manifest = _new_manifest(
            args=args,
            run_id=run_id,
            attempt_id=attempt_id,
            run_dir=run_dir,
            db_path=db_path,
            workdb_path=workdb_path,
            event_log_path=merged_event_log_path,
            experiment=experiment,
        )
    writer = MergedEventWriter(merged_event_log_path, run_id=run_id, attempt_id=attempt_id)
    _claim_or_gate(manifest, manifest_path, writer)

    manifest.update(
        {
            "attempt": attempt_id,
            "status": "estimating" if args.estimate_only else "running",
            "owner_pid": os.getpid(),
            "owner_host": socket.gethostname(),
            "owner_started_at": datetime.now(UTC).isoformat(),
            "heartbeat_at": datetime.now(UTC).isoformat(),
        }
    )
    _write_manifest(manifest_path, manifest)

    stages = _build_stage_plan(
        args=args,
        run_dir=run_dir,
        db_path=db_path,
        workdb_path=workdb_path,
        experiment=experiment,
        cache_root=cache_root,
        run_id=run_id,
        attempt_id=attempt_id,
    )
    manifest["stages"] = _merge_manifest_stages(manifest.get("stages") or [], stages)
    _write_manifest(manifest_path, manifest)

    writer.emit(
        "run_resumed" if args.resume else "run_start",
        stage="orchestrator",
        agent="OneButton",
        script="run_one_button",
        payload={
            "run_id": run_id,
            "attempt_id": attempt_id,
            "job_id": args.job_id,
            "chapters": args.chapters,
            "with_s0": bool(args.with_s0),
            "manifest_path": str(manifest_path),
        },
    )

    try:
        preflight_stage = _preflight_stage(args, run_dir, run_id, attempt_id)
        _run_stage(
            preflight_stage,
            manifest=manifest,
            manifest_path=manifest_path,
            writer=writer,
            estimate_only=False,
            budget_cap_usd=args.budget_cap_usd,
        )
        if args.estimate_only:
            estimates = _estimate_all(stages, manifest=manifest, manifest_path=manifest_path, writer=writer)
            manifest["status"] = "estimate_only"
            manifest["estimate_by_stage"] = estimates
            _write_manifest(manifest_path, manifest)
            writer.emit(
                "run_done",
                stage="orchestrator",
                agent="OneButton",
                script="run_one_button",
                payload={"status": "estimate_only", "estimate_by_stage": estimates},
            )
            print(json.dumps({"run_id": run_id, "manifest": str(manifest_path), "estimate_by_stage": estimates}, ensure_ascii=False, indent=2))
            return 0

        cumulative_estimate = 0.0
        for stage in stages:
            if _pause_requested(manifest_path, run_dir):
                manifest["status"] = "paused"
                manifest["paused_at_stage_boundary_before"] = stage.name
                _write_manifest(manifest_path, manifest)
                writer.emit(
                    "gate_pause",
                    stage="orchestrator",
                    agent="OneButton",
                    script="run_one_button",
                    severity="warning",
                    payload={"reason": "paused_by_user", "before_stage": stage.name},
                )
                print(json.dumps({"status": "paused", "before_stage": stage.name, "manifest": str(manifest_path)}, ensure_ascii=False, indent=2))
                return 0
            estimate = _estimate_stage(stage, manifest=manifest, manifest_path=manifest_path, writer=writer)
            cumulative_estimate += float(estimate.get("estimated_cost_cap_usd") or 0.0)
            _emit_cost_snapshot(writer, manifest, stage=stage, estimate=estimate, cumulative_estimate=cumulative_estimate)
            if args.budget_cap_usd and cumulative_estimate > float(args.budget_cap_usd):
                manifest["status"] = "paused"
                manifest["pause_reason"] = "budget_cap_exceeded"
                _write_manifest(manifest_path, manifest)
                writer.emit(
                    "gate_pause",
                    stage=stage.name,
                    agent="OneButton",
                    script=stage.script,
                    severity="warning",
                    payload={
                        "reason": "budget_cap_exceeded",
                        "budget_cap_usd": args.budget_cap_usd,
                        "estimated_cumulative_usd": cumulative_estimate,
                    },
                )
                return 3
            _run_stage(
                stage,
                manifest=manifest,
                manifest_path=manifest_path,
                writer=writer,
                estimate_only=False,
                budget_cap_usd=args.budget_cap_usd,
            )
            if stage.phase_checkpoint:
                manifest[stage.phase_checkpoint] = True
                _write_manifest(manifest_path, manifest)
                writer.emit(
                    "checkpoint",
                    stage=stage.name,
                    agent="OneButton",
                    script=stage.script,
                    payload={"checkpoint": stage.phase_checkpoint},
                )
        manifest["status"] = "done"
        manifest["ended_at"] = datetime.now(UTC).isoformat()
        manifest["workdb_sha256_after"] = _sha256_file(workdb_path) if workdb_path.exists() else None
        manifest["frozen_db_sha256_after"] = _sha256_file(db_path) if db_path.exists() else None
        _write_manifest(manifest_path, manifest)
        writer.emit(
            "run_done",
            stage="orchestrator",
            agent="OneButton",
            script="run_one_button",
            payload={
                "run_id": run_id,
                "manifest_path": str(manifest_path),
                "workdb_sha256_after": manifest.get("workdb_sha256_after"),
                "frozen_db_sha256_after": manifest.get("frozen_db_sha256_after"),
            },
        )
        print(json.dumps({"status": "done", "run_id": run_id, "manifest": str(manifest_path)}, ensure_ascii=False, indent=2))
        return 0
    except KeyboardInterrupt:
        manifest["status"] = "cancelled"
        manifest["ended_at"] = datetime.now(UTC).isoformat()
        _write_manifest(manifest_path, manifest)
        writer.emit("run_cancelled", stage="orchestrator", agent="OneButton", script="run_one_button")
        return 130
    except SystemExit as exc:
        manifest["status"] = "failed"
        manifest["ended_at"] = datetime.now(UTC).isoformat()
        manifest["error"] = f"SystemExit: {exc.code}"
        _write_manifest(manifest_path, manifest)
        writer.emit(
            "run_failed",
            stage=str(manifest.get("active_stage") or "orchestrator"),
            agent="OneButton",
            script="run_one_button",
            severity="error",
            payload={"error": manifest["error"], "exit_code": exc.code},
        )
        raise
    except Exception as exc:
        manifest["status"] = "failed"
        manifest["ended_at"] = datetime.now(UTC).isoformat()
        manifest["error"] = f"{type(exc).__name__}: {exc}"
        _write_manifest(manifest_path, manifest)
        writer.emit(
            "run_failed",
            stage="orchestrator",
            agent="OneButton",
            script="run_one_button",
            severity="error",
            payload={"error": manifest["error"]},
        )
        raise


def _build_stage_plan(
    *,
    args: argparse.Namespace,
    run_dir: Path,
    db_path: Path,
    workdb_path: Path,
    experiment: str,
    cache_root: Path,
    run_id: str,
    attempt_id: int,
) -> list[StageSpec]:
    artifacts = run_dir / "artifacts"
    reports = run_dir / "reports"
    stage_events = run_dir / "stage_events"
    stdout = run_dir / "stdout"
    _mkdirs(artifacts, reports, stage_events, stdout, cache_root)
    chapter_slug = "_".join(args.chapters)
    c2_out = artifacts / "builder_c2"
    c3_out = artifacts / "builder_c3"
    c35_out = artifacts / "builder_c35"
    reelection_out = artifacts / "reelection"
    c2_notebook = c2_out / "notebook.json"
    c3_notebook = c3_out / "notebook_audited.json"
    c35_promoted = c35_out / "notebook_promoted.json"
    c35_decollided = c35_out / "notebook_decollided.json"
    c35_final = c35_out / "notebook_final.json"
    configs = ["S1"]
    if args.with_s0:
        configs = ["S0", "S1"]
    configs_csv = ",".join(configs)

    c2_cache = cache_root / "builder_c2_cache.sqlite3"
    c3_cache = cache_root / "builder_c3_cache.sqlite3"
    c35_cache = cache_root / "builder_c35_cache.sqlite3"
    translate_cache = cache_root / "translate_cache.sqlite3"
    cascade_cache = cache_root / "cascade_cache"
    cascade_out = artifacts / "cascade"
    phase1_score = reports / "score_run_phase_1.json"
    final_score = reports / "score_run_final.json"

    stages: list[StageSpec] = []
    stages.append(
        _stage(
            "builder_c2",
            "builder_v2_pilot",
            [
                "--db",
                str(db_path),
                "--doc-id",
                args.doc_id,
                "--chapter",
                args.chapters[0],
                "--out",
                str(c2_out),
                "--cache-db",
                str(c2_cache),
            ],
            artifact_path=c2_notebook,
            input_paths=[db_path],
            stdout_dir=stdout,
            event_dir=stage_events,
            cost_bearing=True,
            estimate_extra=["--estimate-only"],
            confirm_flag="--confirm-usd",
            confirm_usd=max(float(args.budget_cap_usd or 0.0), 0.01),
            metadata={"cache_path": str(c2_cache)},
        )
    )
    stages.append(
        _stage(
            "auditor_c3",
            "builder_v2_c3_audit",
            [
                "--db",
                str(db_path),
                "--notebook",
                str(c2_notebook),
                "--out",
                str(c3_out),
                "--cache-db",
                str(c3_cache),
            ],
            artifact_path=c3_notebook,
            input_paths=[c2_notebook],
            stdout_dir=stdout,
            event_dir=stage_events,
            cost_bearing=True,
            estimate_extra=["--estimate-only"],
            confirm_flag="--confirm-usd",
            confirm_usd=max(float(args.budget_cap_usd or 0.0), 0.01),
            metadata={"cache_path": str(c3_cache)},
        )
    )
    stages.append(
        _stage(
            "decollision_c35",
            "builder_v2_c35_decollision",
            [
                "--db",
                str(db_path),
                "--notebook",
                str(c3_notebook),
                "--out",
                str(c35_out),
                "--cache-db",
                str(c35_cache),
            ],
            artifact_path=c35_final,
            input_paths=[c3_notebook],
            stdout_dir=stdout,
            event_dir=stage_events,
            cost_bearing=True,
            estimate_extra=["--estimate-only"],
            confirm_flag="--confirm-usd",
            confirm_usd=max(float(args.budget_cap_usd or 0.0), 0.01),
            metadata={
                "cache_path": str(c35_cache),
                "decollision_final_notebook_candidates": [str(c35_decollided), str(c35_promoted)],
            },
        )
    )
    stages.append(
        _stage(
            "reelection_watchlist",
            "builder_v2_reelection",
            [
                "--notebook",
                str(c35_final),
                "--db",
                str(db_path),
                "--out-dir",
                str(reelection_out),
                "--preflight-only",
            ],
            artifact_path=reelection_out / "watchlist.json",
            input_paths=[c35_final],
            stdout_dir=stdout,
            event_dir=stage_events,
            pass_event_log=True,
            child_run_id=run_id,
            child_attempt_id=attempt_id,
            metadata={"agent": "ReElection"},
        )
    )
    translate_event_log = stage_events / f"translate.a{attempt_id}.jsonl"
    stages.append(
        _stage(
            "translator",
            "run_translate",
            [
                "--db",
                str(db_path),
                "--workdb",
                str(workdb_path),
                "--chapters",
                *args.chapters,
                "--profile",
                args.profile,
                "--configs",
                *configs,
                "--experiment",
                experiment,
                "--cache",
                str(translate_cache),
                "--report",
                str(reports / "translate.json"),
                "--context-budget",
                str(args.context_budget),
                "--memory-notebook",
                str(c35_final),
                "--event-log",
                str(translate_event_log),
                "--run-id",
                run_id,
                "--attempt-id",
                str(attempt_id),
            ],
            artifact_path=reports / "translate.json",
            input_paths=[c35_final, db_path],
            stdout_dir=stdout,
            event_dir=stage_events,
            stage_event_log_path=translate_event_log,
            cost_bearing=True,
            estimate_argv=[
                sys.executable,
                "-m",
                "pipeline.scripts.run_translate",
                "--db",
                str(db_path),
                "--chapters",
                *args.chapters,
                "--profile",
                args.profile,
                "--configs",
                *configs,
                "--experiment",
                experiment,
                "--cache",
                str(translate_cache),
                "--report",
                str(reports / "translate_preflight.json"),
                "--context-budget",
                str(args.context_budget),
                "--memory-notebook",
                str(c35_final),
                "--preflight-only",
            ],
            metadata={"agent": "Translator", "cache_path": str(translate_cache)},
        )
    )
    stages.append(
        _stage(
            "score_run_phase_1",
            "score_run",
            [
                "--db",
                str(workdb_path),
                "--experiment",
                experiment,
                "--chapters",
                *args.chapters,
                "--configs",
                "S1",
                "--profile",
                args.profile,
                "--gold-variants",
                str(_resolve(args.gold_variants)),
                "--out",
                str(phase1_score),
            ],
            artifact_path=phase1_score,
            input_paths=[workdb_path],
            stdout_dir=stdout,
            event_dir=stage_events,
            phase_checkpoint="phase_1_done",
        )
    )
    stages.append(
        _stage(
            "cascade",
            "run_experiment_cascade",
            [
                "--db",
                str(workdb_path),
                "--frozen-db",
                str(db_path),
                "--experiment",
                experiment,
                "--chapter",
                args.chapters[0],
                "--configs",
                configs_csv,
                "--profile",
                args.profile,
                "--doc-id",
                args.doc_id,
                "--cache-dir",
                str(cascade_cache),
                "--out-dir",
                str(cascade_out),
                "--artifact-prefix",
                f"{chapter_slug}_cascade",
                "--notebook",
                str(c35_final),
                "--term-scope",
                "notebook_plus_gold",
                "--t3-backend",
                "local-lmstudio",
                "--gpt-fallback-on-validation-error",
                "--confirm-usd",
                str(max(float(args.budget_cap_usd or 0.0), 0.01)),
            ],
            artifact_path=cascade_out / f"{chapter_slug}_cascade_summary.json",
            input_paths=[workdb_path, c35_final],
            stdout_dir=stdout,
            event_dir=stage_events,
            cost_bearing=True,
            estimate_extra=["--preflight-only"],
            metadata={"agent": "Cascade", "cache_path": str(cascade_cache)},
        )
    )
    stages.append(
        _stage(
            "sf_qe",
            "score_sf_qe",
            [
                "--db",
                str(workdb_path),
                "--experiment",
                experiment,
                "--chapters",
                ",".join(args.chapters),
                "--configs",
                configs_csv,
                "--out",
                str(reports / "sf_qe.json"),
            ],
            artifact_path=reports / "sf_qe.json",
            input_paths=[workdb_path],
            stdout_dir=stdout,
            event_dir=stage_events,
            interpreter=str(args.py311),
            metadata={"agent": "SF-QE"},
        )
    )
    stages.append(
        _stage(
            "sf_bt",
            "score_sf_bt",
            [
                "--db",
                str(workdb_path),
                "--experiment",
                experiment,
                "--chapters",
                ",".join(args.chapters),
                "--configs",
                configs_csv,
                "--full",
                "--out",
                str(reports / "sf_bt.json"),
                "--cache-db",
                str(_resolve(args.sf_bt_cache_db)),
                "--confirm-usd",
                str(max(float(args.budget_cap_usd or 0.0), 0.01)),
            ],
            artifact_path=reports / "sf_bt.json",
            input_paths=[workdb_path],
            stdout_dir=stdout,
            event_dir=stage_events,
            cost_bearing=True,
            estimate_extra=["--estimate-only"],
            metadata={"agent": "SF-BT", "cache_path": str(_resolve(args.sf_bt_cache_db))},
        )
    )
    if args.with_s0:
        stages.append(
            _stage(
                "pj",
                "score_pj",
                [
                    "--db",
                    str(workdb_path),
                    "--experiment",
                    experiment,
                    "--chapter",
                    args.chapters[0],
                    "--full",
                    "--out",
                    str(reports / "pj.json"),
                    "--cache-db",
                    str(_resolve(args.pj_cache_db)),
                    "--expected-db-sha256",
                    _sha256_file(workdb_path) if workdb_path.exists() else "",
                    "--confirm-usd",
                    str(max(float(args.budget_cap_usd or 0.0), 0.01)),
                ],
                artifact_path=reports / "pj.json",
                input_paths=[workdb_path],
                stdout_dir=stdout,
                event_dir=stage_events,
                cost_bearing=True,
                estimate_extra=["--estimate-only"],
                metadata={"agent": "PJ", "cache_path": str(_resolve(args.pj_cache_db))},
            )
        )
    stages.append(
        _stage(
            "score_run_final",
            "score_run",
            [
                "--db",
                str(workdb_path),
                "--experiment",
                experiment,
                "--chapters",
                *args.chapters,
                "--configs",
                *configs,
                "--profile",
                args.profile,
                "--gold-variants",
                str(_resolve(args.gold_variants)),
                "--out",
                str(final_score),
            ],
            artifact_path=final_score,
            input_paths=[workdb_path],
            stdout_dir=stdout,
            event_dir=stage_events,
            metadata={"agent": "Report"},
        )
    )
    return stages


def _stage(
    name: str,
    script: str,
    extra_args: list[str],
    *,
    artifact_path: Path | None,
    input_paths: list[Path],
    stdout_dir: Path,
    event_dir: Path,
    interpreter: str = sys.executable,
    cost_bearing: bool = False,
    estimate_extra: list[str] | None = None,
    estimate_argv: list[str] | None = None,
    confirm_flag: str | None = None,
    confirm_usd: float | None = None,
    pass_event_log: bool = False,
    child_run_id: str | None = None,
    child_attempt_id: int | None = None,
    stage_event_log_path: Path | None = None,
    retry_once: bool = False,
    never_skip: bool = False,
    phase_checkpoint: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> StageSpec:
    stage_event_log = stage_event_log_path
    argv = [interpreter, "-m", f"pipeline.scripts.{script}", *[str(item) for item in extra_args]]
    if pass_event_log:
        stage_event_log = stage_event_log or event_dir / f"{name}.a{int(child_attempt_id or 1)}.jsonl"
        argv += ["--event-log", str(stage_event_log)]
        argv += ["--run-id", str(child_run_id or name)]
        argv += ["--attempt-id", str(int(child_attempt_id or 1))]
    if confirm_flag and confirm_usd is not None:
        argv += [confirm_flag, str(confirm_usd)]
    if estimate_argv is None and estimate_extra:
        estimate_argv = [interpreter, "-m", f"pipeline.scripts.{script}", *[str(item) for item in extra_args], *estimate_extra]
    return StageSpec(
        name=name,
        script=script,
        argv=argv,
        artifact_path=artifact_path,
        input_paths=input_paths,
        stage_event_log_path=stage_event_log,
        stdout_log_path=stdout_dir / f"{name}.log",
        cost_bearing=cost_bearing,
        estimate_argv=estimate_argv,
        confirm_usd=confirm_usd,
        interpreter=interpreter,
        retry_once=retry_once,
        never_skip=never_skip,
        phase_checkpoint=phase_checkpoint,
        metadata=metadata or {},
    )


def _preflight_stage(args: argparse.Namespace, run_dir: Path, run_id: str, attempt_id: int) -> StageSpec:
    event_dir = run_dir / "stage_events"
    stdout_dir = run_dir / "stdout"
    report_path = run_dir / "reports" / "preflight_check.json"
    stage_event_log = event_dir / f"preflight_check.a{attempt_id}.jsonl"
    argv = [
        sys.executable,
        "-m",
        "pipeline.scripts.preflight_check",
        "--python",
        str(args.py311),
        "--event-log",
        str(stage_event_log),
        "--run-id",
        run_id,
        "--attempt-id",
        str(attempt_id),
    ]
    return StageSpec(
        name="preflight_check",
        script="preflight_check",
        argv=argv,
        artifact_path=None,
        input_paths=[],
        stage_event_log_path=stage_event_log,
        stdout_log_path=stdout_dir / "preflight_check.log",
        never_skip=True,
        metadata={"agent": "PreflightCheck"},
    )


def _run_stage(
    stage: StageSpec,
    *,
    manifest: dict[str, Any],
    manifest_path: Path,
    writer: MergedEventWriter,
    estimate_only: bool,
    budget_cap_usd: float,
) -> None:
    _materialize_dynamic_stage_args(stage, manifest)
    stage_record = _stage_record(manifest, stage.name)
    expected = _stage_digests(stage)
    if _stage_can_skip(stage_record, expected, stage):
        stage_record.update({"status": "done", "skipped": True, "skip_reason": "resume_digest_match"})
        _write_manifest(manifest_path, manifest)
        writer.emit(
            "stage_skipped",
            stage=stage.name,
            agent=str(stage.metadata.get("agent") or stage.name),
            script=stage.script,
            payload={"reason": "resume_digest_match", "artifact_path": str(stage.artifact_path) if stage.artifact_path else None},
        )
        return
    if estimate_only:
        return

    stage_record.update(
        {
            "name": stage.name,
            "script": stage.script,
            "status": "running",
            "started_at": datetime.now(UTC).isoformat(),
            "argv_digest": expected["argv_digest"],
            "input_artifact_sha": expected["input_artifact_sha"],
            "artifact_path": str(stage.artifact_path) if stage.artifact_path else None,
            "stage_event_log_path": str(stage.stage_event_log_path) if stage.stage_event_log_path else None,
            "stdout_log_path": str(stage.stdout_log_path) if stage.stdout_log_path else None,
            "cost_bearing": stage.cost_bearing,
            "metadata": stage.metadata,
        }
    )
    manifest["active_stage"] = stage.name
    manifest["heartbeat_at"] = datetime.now(UTC).isoformat()
    _write_manifest(manifest_path, manifest)
    writer.emit(
        "stage_start",
        stage=stage.name,
        agent=str(stage.metadata.get("agent") or stage.name),
        script=stage.script,
        payload={
            "argv_digest": expected["argv_digest"],
            "artifact_path": str(stage.artifact_path) if stage.artifact_path else None,
            "stage_event_log_path": str(stage.stage_event_log_path) if stage.stage_event_log_path else None,
            "stdout_log_path": str(stage.stdout_log_path) if stage.stdout_log_path else None,
        },
    )
    attempts = 2 if stage.retry_once else 1
    returncode = 1
    stage_offset = 0
    last_stage_event_ts = time.monotonic()
    for attempt in range(1, attempts + 1):
        _mkdirs(stage.stdout_log_path.parent if stage.stdout_log_path else manifest_path.parent)
        with (stage.stdout_log_path or Path(os.devnull)).open("a", encoding="utf-8", newline="\n") as log_fh:
            log_fh.write(f"\n[OneButton] stage={stage.name} attempt={attempt} cwd={TOOL_ROOT}\n")
            log_fh.write(f"[OneButton] argv={json.dumps(_redact_argv(stage.argv), ensure_ascii=False)}\n")
            log_fh.flush()
            proc = subprocess.Popen(
                [str(item) for item in stage.argv],
                cwd=TOOL_ROOT,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
            stage_record["active_child_pid"] = proc.pid
            _write_manifest(manifest_path, manifest)
            last_heartbeat = 0.0
            try:
                while proc.poll() is None:
                    stage_offset, last_stage_event_ts = _drain_stage_events(stage, writer, stage_offset, last_stage_event_ts)
                    now = time.monotonic()
                    if now - last_heartbeat >= 30:
                        _heartbeat(writer, manifest, manifest_path, stage, child_pid=proc.pid, last_stage_event_ts=last_stage_event_ts, child_returncode=None)
                        last_heartbeat = now
                    time.sleep(0.5)
            except KeyboardInterrupt:
                _kill_process_tree(proc.pid)
                raise
            returncode = int(proc.returncode or 0)
            stage_offset, last_stage_event_ts = _drain_stage_events(stage, writer, stage_offset, last_stage_event_ts)
        if returncode == 0:
            break
        if attempt < attempts:
            writer.emit(
                "warning",
                stage=stage.name,
                agent=str(stage.metadata.get("agent") or stage.name),
                script=stage.script,
                severity="warning",
                payload={"message": "stage failed; retrying once", "exit_code": returncode},
            )

    if stage.name == "decollision_c35":
        status = _decollision_status(stage)
        stage_record["decollision_status"] = status
        _write_decollision_final(stage, status)
    artifact_sha = _hash_path(stage.artifact_path) if stage.artifact_path and stage.artifact_path.exists() else None
    stage_record.update(
        {
            "status": "done" if returncode == 0 else "failed",
            "ended_at": datetime.now(UTC).isoformat(),
            "exit_code": returncode,
            "artifact_sha": artifact_sha,
            "workdb_sha_after_stage": _sha256_file(Path(manifest["workdb_path"])) if Path(manifest["workdb_path"]).exists() else None,
        }
    )
    manifest["active_stage"] = None
    manifest["heartbeat_at"] = datetime.now(UTC).isoformat()
    _write_manifest(manifest_path, manifest)
    writer.emit(
        "stage_done" if returncode == 0 else "error",
        stage=stage.name,
        agent=str(stage.metadata.get("agent") or stage.name),
        script=stage.script,
        severity="info" if returncode == 0 else "error",
        payload={
            "exit_code": returncode,
            "artifact_path": str(stage.artifact_path) if stage.artifact_path else None,
            "artifact_sha": artifact_sha,
            "stdout_log_path": str(stage.stdout_log_path) if stage.stdout_log_path else None,
            "decollision_status": stage_record.get("decollision_status"),
        },
    )
    if returncode != 0:
        raise SystemExit(f"Stage {stage.name} failed with exit code {returncode}. See {stage.stdout_log_path}")


def _estimate_all(
    stages: list[StageSpec],
    *,
    manifest: dict[str, Any],
    manifest_path: Path,
    writer: MergedEventWriter,
) -> list[dict[str, Any]]:
    estimates = []
    for stage in stages:
        estimates.append(_estimate_stage(stage, manifest=manifest, manifest_path=manifest_path, writer=writer))
    return estimates


def _estimate_stage(
    stage: StageSpec,
    *,
    manifest: dict[str, Any],
    manifest_path: Path,
    writer: MergedEventWriter,
) -> dict[str, Any]:
    record = _stage_record(manifest, stage.name)
    cached_estimate = record.get("estimate")
    if isinstance(cached_estimate, dict):
        missing_inputs_now = [str(path) for path in stage.input_paths if not Path(path).exists()]
        if cached_estimate.get("estimate_source") != "blocked_missing_inputs" or missing_inputs_now:
            return dict(cached_estimate)
    estimate = {
        "stage": stage.name,
        "script": stage.script,
        "cost_bearing": stage.cost_bearing,
        "estimated_cost_cap_usd": 0.0,
        "cache_path": stage.metadata.get("cache_path"),
        "estimate_source": "static_zero",
    }
    missing_inputs = [str(path) for path in stage.input_paths if not Path(path).exists()]
    if missing_inputs:
        estimate.update(
            {
                "estimate_source": "blocked_missing_inputs",
                "missing_inputs": missing_inputs,
            }
        )
        record["estimate"] = estimate
        _write_manifest(manifest_path, manifest)
        writer.emit(
            "warning",
            stage=stage.name,
            agent=str(stage.metadata.get("agent") or stage.name),
            script=stage.script,
            severity="warning",
            payload={"message": "estimate skipped because upstream artifacts are missing", "missing_inputs": missing_inputs},
        )
        return estimate
    if stage.estimate_argv:
        estimate_report = _estimate_report_path(stage)
        _mkdirs(estimate_report.parent)
        argv = _with_estimate_output(stage, estimate_report)
        stdout_path = (stage.stdout_log_path or estimate_report.with_suffix(".log")).with_name(f"{stage.name}_estimate.log")
        with stdout_path.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(f"[OneButton estimate] argv={json.dumps(_redact_argv(argv), ensure_ascii=False)}\n")
            fh.flush()
            proc = subprocess.run(
                [str(item) for item in argv],
                cwd=TOOL_ROOT,
                stdout=fh,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                check=False,
            )
        estimate.update(
            {
                "estimate_source": "script_estimate",
                "estimate_argv_digest": _argv_digest(argv),
                "estimate_stdout_log_path": str(stdout_path),
                "estimate_exit_code": proc.returncode,
            }
        )
        loaded: dict[str, Any] = {}
        actual_report = _estimate_report_path(stage, argv)
        estimate["estimate_report_path"] = str(actual_report) if actual_report.exists() else None
        if actual_report.exists():
            loaded = _read_json(actual_report)
        if estimate_report.exists():
            # Fallback for scripts where the orchestrator forced an output
            # path.  Scripts with baked-in --report/--out use actual_report.
            loaded = loaded or _read_json(estimate_report)
        if not loaded:
            loaded = _read_last_json_object(stdout_path)
        if loaded:
            estimate.update(_extract_cost_estimate(loaded))
        if proc.returncode != 0:
            estimate["warning"] = "estimate_command_failed"
    record["estimate"] = estimate
    _write_manifest(manifest_path, manifest)
    writer.emit(
        "cost_snapshot",
        stage=stage.name,
        agent=str(stage.metadata.get("agent") or stage.name),
        script=stage.script,
        payload=estimate,
    )
    return estimate


def _with_estimate_output(stage: StageSpec, estimate_report: Path) -> list[str]:
    argv = list(stage.estimate_argv or [])
    if stage.script in {"score_sf_bt", "score_pj"}:
        argv += ["--out", str(estimate_report)]
    elif stage.script == "run_translate":
        if "--report" not in argv:
            argv += ["--report", str(estimate_report)]
    elif stage.script in {"builder_v2_pilot", "builder_v2_c3_audit", "builder_v2_c35_decollision"}:
        # These scripts write estimates inside --out; no separate report flag.
        pass
    return argv


def _estimate_report_path(stage: StageSpec, argv: list[str] | None = None) -> Path:
    if argv is not None:
        explicit = _estimate_report_path_from_argv(stage, argv)
        if explicit is not None:
            return explicit
    if stage.artifact_path is not None:
        return stage.artifact_path.parent / f"{stage.name}_estimate.json"
    return TOOL_ROOT / "data" / "tmp" / f"{stage.name}_estimate.json"


def _estimate_report_path_from_argv(stage: StageSpec, argv: list[str]) -> Path | None:
    if stage.script == "run_translate":
        report = _argv_flag_value(argv, "--report")
        return _resolve(report) if report else None
    if stage.script in {"score_sf_bt", "score_pj"}:
        out = _argv_flag_value(argv, "--out")
        return _resolve(out) if out else None
    if stage.script == "run_experiment_cascade":
        out_dir = _argv_flag_value(argv, "--out-dir")
        prefix = _argv_flag_value(argv, "--artifact-prefix")
        if out_dir and prefix:
            return _resolve(out_dir) / f"{prefix}_preflight.json"
    return None


def _argv_flag_value(argv: list[str], flag: str) -> str | None:
    for idx, item in enumerate(argv):
        if str(item) == flag and idx + 1 < len(argv):
            return str(argv[idx + 1])
    return None


def _extract_cost_estimate(payload: dict[str, Any]) -> dict[str, Any]:
    t3_total = payload.get("t3_estimate_total") if isinstance(payload, dict) else None
    if isinstance(t3_total, dict) and "cost_cap_usd" in t3_total:
        try:
            result: dict[str, Any] = {"estimated_cost_cap_usd": float(t3_total["cost_cap_usd"])}
            for key in (
                "calls",
                "prompt_tokens_estimate",
                "output_tokens_cap",
                "input_cost_estimate_usd",
                "output_cost_cap_usd",
                "prompt_tokens_max_single_call",
            ):
                if key in t3_total:
                    result[f"cascade_{key}"] = t3_total[key]
            return result
        except (TypeError, ValueError):
            pass
    candidates = [
        payload,
        payload.get("estimate") if isinstance(payload, dict) else None,
        payload.get("summary") if isinstance(payload, dict) else None,
        payload.get("preflight") if isinstance(payload, dict) else None,
    ]
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        for key in (
            "estimated_cost_cap_usd",
            "estimated_cost_usd_cap",
            "estimated_cost_cap",
            "cap_cost_usd",
        ):
            if key in candidate:
                try:
                    return {"estimated_cost_cap_usd": float(candidate[key])}
                except (TypeError, ValueError):
                    pass
        if "upper_total_all_configs" in candidate:
            token_evidence = {
                "estimated_cost_cap_usd": 0.0,
                "upper_total_all_configs": candidate.get("upper_total_all_configs"),
                "note": "run_translate preflight reports token cap, not dollar cap",
            }
            cost = _translate_preflight_cost_cap(payload)
            if cost:
                token_evidence.update(cost)
            return token_evidence
    return {}


def _translate_preflight_cost_cap(payload: dict[str, Any]) -> dict[str, Any]:
    preflight = payload.get("preflight") if isinstance(payload.get("preflight"), dict) else payload
    configs = preflight.get("configs") if isinstance(preflight, dict) else None
    if not isinstance(configs, dict):
        return {}
    config_path = None
    run_meta = payload.get("run") if isinstance(payload.get("run"), dict) else None
    if run_meta:
        config_path = run_meta.get("llm_config")
    if not config_path:
        config_path = "pipeline/configs/llm_translate.yaml"
    try:
        from pipeline.agents.llm_config import load_llm_config

        pricing = load_llm_config(_resolve(config_path)).pricing
    except Exception:
        pricing = {"input": 0.25, "output": 2.0}
    input_price = float(pricing.get("input", 0.25))
    output_price = float(pricing.get("output", 2.0))
    prompt_tokens = 0
    output_tokens = 0
    for row in configs.values():
        if not isinstance(row, dict):
            continue
        prompt = int(row.get("prompt_tokens_total_est") or 0)
        total = int(row.get("upper_total_with_max_output") or prompt)
        prompt_tokens += prompt
        output_tokens += max(total - prompt, 0)
    cap = (prompt_tokens * input_price + output_tokens * output_price) / 1_000_000
    return {
        "estimated_cost_cap_usd": cap,
        "translation_prompt_tokens_est": prompt_tokens,
        "translation_output_tokens_cap": output_tokens,
        "pricing": {"input": input_price, "output": output_price},
    }


def _emit_cost_snapshot(
    writer: MergedEventWriter,
    manifest: dict[str, Any],
    *,
    stage: StageSpec,
    estimate: dict[str, Any],
    cumulative_estimate: float,
) -> None:
    writer.emit(
        "cost_snapshot",
        stage=stage.name,
        agent="OneButton",
        script=stage.script,
        payload={
            "stage": stage.name,
            "estimated_cost_cap_usd": estimate.get("estimated_cost_cap_usd"),
            "estimated_cumulative_usd": round(cumulative_estimate, 12),
            "budget_cap_usd": manifest.get("budget_cap_usd"),
            "cache_path": estimate.get("cache_path"),
        },
    )


def _drain_stage_events(
    stage: StageSpec,
    writer: MergedEventWriter,
    offset: int,
    last_stage_event_ts: float,
) -> tuple[int, float]:
    if not stage.stage_event_log_path:
        return offset, last_stage_event_ts
    result = read_jsonl_events(stage.stage_event_log_path, offset=offset, max_bytes=262144)
    events = result.get("events") or []
    if events:
        writer.reemit_stage_events(events, stage=stage)
        last_stage_event_ts = time.monotonic()
    return int(result.get("offset") or offset), last_stage_event_ts


def _heartbeat(
    writer: MergedEventWriter,
    manifest: dict[str, Any],
    manifest_path: Path,
    stage: StageSpec,
    *,
    child_pid: int | None,
    last_stage_event_ts: float,
    child_returncode: int | None,
) -> None:
    now = time.monotonic()
    payload = {
        "active_child_pid": child_pid,
        "stage": stage.name,
        "last_stage_event_age_sec": round(now - last_stage_event_ts, 3),
        "child_returncode": child_returncode,
    }
    manifest["heartbeat_at"] = datetime.now(UTC).isoformat()
    manifest["heartbeat"] = payload
    _write_manifest(manifest_path, manifest)
    writer.emit("heartbeat", stage=stage.name, agent="OneButton", script=stage.script, payload=payload)


def _new_manifest(
    *,
    args: argparse.Namespace,
    run_id: str,
    attempt_id: int,
    run_dir: Path,
    db_path: Path,
    workdb_path: Path,
    event_log_path: Path,
    experiment: str,
) -> dict[str, Any]:
    frozen_sha = _sha256_file(db_path) if db_path.exists() else None
    return {
        "v": 1,
        "schema": "one_button_manifest_v1",
        "run_id": run_id,
        "attempt": attempt_id,
        "job_id": args.job_id,
        "chapters": list(args.chapters),
        "with_s0": bool(args.with_s0),
        "profile": args.profile,
        "experiment": experiment,
        "run_dir": str(run_dir),
        "db_path": str(db_path),
        "workdb_path": str(workdb_path),
        "event_log_path": str(event_log_path),
        "budget_cap_usd": float(args.budget_cap_usd or 0.0),
        "status": "created",
        "created_at": datetime.now(UTC).isoformat(),
        "frozen_db_sha256_before": frozen_sha,
        "frozen_db_sha256_expected": FROZEN_DB_SHA256,
        "workdb_sha256_before": _sha256_file(workdb_path) if workdb_path.exists() else None,
        "argv_digest": _argv_digest(sys.argv),
        "platform": platform.platform(),
        "python": sys.executable,
        "stages": [],
    }


def _merge_manifest_stages(existing: list[dict[str, Any]], stages: list[StageSpec]) -> list[dict[str, Any]]:
    by_name = {str(row.get("name")): row for row in existing if isinstance(row, dict)}
    merged = []
    for stage in stages:
        row = by_name.get(stage.name, {"name": stage.name})
        row.setdefault("status", "pending")
        row.update(
            {
                "script": stage.script,
                "artifact_path": str(stage.artifact_path) if stage.artifact_path else None,
                "stage_event_log_path": str(stage.stage_event_log_path) if stage.stage_event_log_path else None,
                "stdout_log_path": str(stage.stdout_log_path) if stage.stdout_log_path else None,
                "cost_bearing": stage.cost_bearing,
                "metadata": stage.metadata,
            }
        )
        merged.append(row)
    return merged


def _stage_record(manifest: dict[str, Any], name: str) -> dict[str, Any]:
    stages = manifest.setdefault("stages", [])
    for row in stages:
        if row.get("name") == name:
            return row
    row = {"name": name, "status": "pending"}
    stages.append(row)
    return row


def _stage_digests(stage: StageSpec) -> dict[str, str]:
    return {
        "argv_digest": _argv_digest(stage.argv),
        "input_artifact_sha": _hash_inputs(stage.input_paths),
    }


def _stage_can_skip(record: dict[str, Any], expected: dict[str, str], stage: StageSpec) -> bool:
    if stage.never_skip:
        return False
    if record.get("status") != "done":
        return False
    if record.get("argv_digest") != expected["argv_digest"]:
        return False
    if record.get("input_artifact_sha") != expected["input_artifact_sha"]:
        return False
    if stage.artifact_path and not stage.artifact_path.exists():
        return False
    if stage.artifact_path and record.get("artifact_sha") != _hash_path(stage.artifact_path):
        return False
    return True


def _claim_or_gate(manifest: dict[str, Any], manifest_path: Path, writer: MergedEventWriter) -> None:
    prior_pid = int(manifest.get("owner_pid") or 0)
    if prior_pid and prior_pid != os.getpid() and _pid_alive(prior_pid):
        heartbeat = manifest.get("heartbeat_at")
        writer.emit(
            "gate_pause",
            stage="orchestrator",
            agent="OneButton",
            script="run_one_button",
            severity="warning",
            payload={"reason": "possibly_zombie", "owner_pid": prior_pid, "heartbeat_at": heartbeat},
        )
        raise SystemExit(f"Refusing second writer: owner pid {prior_pid} appears alive.")
    if prior_pid and prior_pid != os.getpid():
        manifest["status"] = "failed_resumable"
        manifest["stale_owner_pid"] = prior_pid
        _write_manifest(manifest_path, manifest)


def _assert_frozen_db_baseline(db_path: Path) -> None:
    if not db_path.exists():
        raise SystemExit(f"Frozen DB not found: {db_path}")
    sha = _sha256_file(db_path)
    if sha != FROZEN_DB_SHA256:
        raise SystemExit(
            f"Frozen DB hash mismatch: {sha}; expected {FROZEN_DB_SHA256}. "
            "Refusing one-button run."
        )


def _default_py311() -> str:
    if DEFAULT_PY311:
        return DEFAULT_PY311
    if sys.version_info[:2] == (3, 11):
        return sys.executable
    candidates = []
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        candidates.append(Path(local_appdata) / "Programs" / "Python" / "Python311" / "python.exe")
    candidates.extend(
        Path(path)
        for path in (
            shutil.which("python3.11"),
            shutil.which("python311"),
        )
        if path
    )
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(Path(candidate))
    return sys.executable


def _pause_requested(manifest_path: Path, run_dir: Path) -> bool:
    """Check pause state.

    The PAUSE file next to the manifest is the official control plane.
    `pause_requested` in the manifest is best-effort compatibility only because
    frequent manifest rewrites can overwrite a UI edit made from an older copy.
    """
    if (run_dir / "PAUSE").exists():
        return True
    manifest = _load_manifest(manifest_path)
    return bool(manifest.get("pause_requested"))


def _materialize_dynamic_stage_args(stage: StageSpec, manifest: dict[str, Any]) -> None:
    if stage.name != "pj":
        return
    workdb = Path(str(manifest.get("workdb_path") or ""))
    if not workdb.exists():
        return
    sha = _sha256_file(workdb)
    _replace_flag_value(stage.argv, "--expected-db-sha256", sha)
    if stage.estimate_argv:
        _replace_flag_value(stage.estimate_argv, "--expected-db-sha256", sha)


def _replace_flag_value(argv: list[str], flag: str, value: str) -> None:
    for idx, item in enumerate(argv):
        if str(item) == flag and idx + 1 < len(argv):
            argv[idx + 1] = str(value)
            return
    argv.extend([flag, str(value)])


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _kill_process_tree(pid: int) -> None:
    if pid <= 0:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return
    try:
        os.kill(pid, 15)
    except OSError:
        pass


def _decollision_status(stage: StageSpec) -> dict[str, Any]:
    candidates = stage.metadata.get("decollision_final_notebook_candidates") or []
    existing = [path for path in candidates if Path(path).exists()]
    return {
        "final_notebook_path": existing[0] if existing else str(stage.artifact_path) if stage.artifact_path else None,
        "notebook_decollided_exists": any(str(path).endswith("notebook_decollided.json") for path in existing),
        "notebook_promoted_exists": any(str(path).endswith("notebook_promoted.json") for path in existing),
    }


def _write_decollision_final(stage: StageSpec, status: dict[str, Any]) -> None:
    if not stage.artifact_path:
        return
    source = status.get("final_notebook_path")
    if not source:
        return
    source_path = Path(str(source))
    if not source_path.exists():
        return
    stage.artifact_path.parent.mkdir(parents=True, exist_ok=True)
    if source_path.resolve() == stage.artifact_path.resolve():
        return
    shutil.copyfile(source_path, stage.artifact_path)


def _hash_inputs(paths: list[Path]) -> str:
    h = hashlib.sha256()
    if not paths:
        h.update(b"no-inputs")
    for path in paths:
        resolved = _resolve(path)
        h.update(str(resolved).encode("utf-8"))
        h.update(b"\0")
        h.update((_hash_path(resolved) or "missing").encode("ascii"))
        h.update(b"\0")
    return h.hexdigest().upper()


def _hash_path(path: Path | None) -> str | None:
    if path is None:
        return None
    path = _resolve(path)
    if not path.exists():
        return None
    if path.is_file():
        return _sha256_file(path)
    h = hashlib.sha256()
    for child in sorted(path.rglob("*")):
        if child.is_file():
            h.update(str(child.relative_to(path)).replace("\\", "/").encode("utf-8"))
            h.update(b"\0")
            h.update((_sha256_file(child) or "").encode("ascii"))
            h.update(b"\0")
    return h.hexdigest().upper()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def _argv_digest(argv: list[str]) -> str:
    payload = json.dumps(_argv_for_digest(argv), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest().upper()


def _argv_for_digest(argv: list[str]) -> list[str]:
    volatile_flags = {"--event-log", "--run-id", "--attempt-id"}
    normalized: list[str] = []
    skip_next = False
    for item in [str(value) for value in argv]:
        if skip_next:
            skip_next = False
            continue
        if item in volatile_flags:
            skip_next = True
            continue
        normalized.append(item)
    return normalized


def _read_json(path: Path) -> dict[str, Any]:
    try:
        with Path(path).open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _read_last_json_object(path: Path) -> dict[str, Any]:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except Exception:
        return {}
    decoder = json.JSONDecoder()
    best: dict[str, Any] = {}
    cost_keys = {
        "estimated_cost_cap_usd",
        "estimated_cost_usd_cap",
        "cap_cost_usd",
        "upper_total_all_configs",
    }
    for idx, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            value, _end = decoder.raw_decode(text[idx:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            if any(key in value for key in cost_keys):
                return value
            best = value
    return best


def _load_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return _read_json(path)


def _write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(_json_safe(manifest), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    # os.replace is atomic but on Windows raises PermissionError (WinError 5) when the
    # destination is briefly locked by OneDrive sync, antivirus, or a concurrent reader.
    # The heartbeat rewrites the manifest often, so on long runs a transient lock is
    # eventually hit and would crash the whole run. Retry with short backoff.
    last_err: PermissionError | None = None
    for attempt in range(10):
        try:
            os.replace(tmp, path)
            return
        except PermissionError as err:
            last_err = err
            time.sleep(0.1 * (attempt + 1))
    raise last_err  # type: ignore[misc]


def _append_jsonl_atomic(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(json.dumps(_json_safe(row), ensure_ascii=False, sort_keys=True) + "\n" for row in rows).encode("utf-8")
    binary_flag = getattr(os, "O_BINARY", 0)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND | binary_flag)
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)


def _mkdirs(*paths: Path) -> None:
    for path in paths:
        Path(path).mkdir(parents=True, exist_ok=True)


def _resolve(path: str | Path) -> Path:
    path = Path(path)
    if path.is_absolute():
        return path.resolve()
    return (TOOL_ROOT / path).resolve()


def _safe_id(value: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in str(value or "").strip())
    if not safe:
        raise SystemExit("Empty id is not allowed.")
    return safe


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return repr(value)


def _redact_argv(argv: list[str]) -> list[str]:
    redacted: list[str] = []
    skip = False
    for item in argv:
        if skip:
            redacted.append("[redacted]")
            skip = False
            continue
        text = str(item)
        redacted.append(text)
        if text.lower() in {"--api-key", "--token", "--key"}:
            skip = True
    return redacted


if __name__ == "__main__":
    raise SystemExit(main())
