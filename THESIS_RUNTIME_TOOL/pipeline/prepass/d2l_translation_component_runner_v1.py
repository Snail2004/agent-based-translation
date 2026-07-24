"""D2L Translation component runner with App Replay V1 observability.

The runner is deliberately declarative.  Existing D2L stage implementations are
supplied as an explicit stage plan and remain the semantic authority.  This
module only executes the plan, records component-local lifecycle facts, and
publishes the isolated package required by the neutral workflow relay.

It never creates a parent workflow manifest, a parent event stream, a global
sequence, or a five-arm scoring handoff.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from pipeline.prepass.d2l_console_replay_contract_v1 import (
    COMPONENT_ID,
    D2LTranslationComponentEventWriter,
    STAGE_IDS,
    build_checkpoint,
    build_component_manifest,
    build_component_usage_snapshot,
    build_stage_plan,
    canonical_sha256,
    file_sha256,
    validate_artifact_index,
    validate_component_manifest,
    validate_scoring_handoff_fragment,
    validate_translation_component_package,
    write_component_manifest_snapshot,
    write_json,
)
from pipeline.prepass.d2l_component_stage_receipt_v1 import (
    D2LStageObservationJournalWriter,
    STAGE_RECEIPT_SCHEMA,
    observation_journal_state,
    read_observation_journal,
    validate_stage_receipt,
    validate_stage_receipt_against_journal,
)
from pipeline.prepass.d2l_shared_llm_adapter_v1 import (
    TRANSPORT_RETRY_EXHAUSTED_EXIT_CODE,
)


RUNNER_SCHEMA = "d2l_translation_component_runner_plan_v1_2"
RUNNER_VERSION = "d2l_translation_component_runner_v1_2"
_FORBIDDEN_PLAN_KEYS = {
    "raw_prompt",
    "raw_response",
    "prompt_text",
    "response_text",
    "api_key",
    "secret",
    "gold",
    "oracle",
    "reference_text",
}


class ComponentRunnerError(RuntimeError):
    """Raised when the declarative component runner cannot proceed safely."""


def _timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ComponentRunnerError(f"{label} is missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ComponentRunnerError(f"{label} is not valid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ComponentRunnerError(f"{label} must be a JSON object")
    return value


def _reject_forbidden_keys(value: Any, label: str = "plan") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).lower() in _FORBIDDEN_PLAN_KEYS:
                raise ComponentRunnerError(f"{label} contains forbidden key: {key}")
            _reject_forbidden_keys(child, f"{label}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_forbidden_keys(child, f"{label}[{index}]")


def _nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ComponentRunnerError(f"{label} must be a non-empty string")
    return value


def _relative_path(root: Path, value: Any, label: str) -> Path:
    text = _nonempty_string(value, label)
    path = Path(text)
    if path.is_absolute() or ".." in path.parts:
        raise ComponentRunnerError(f"{label} must be package-relative")
    resolved = (root / path).resolve()
    if root.resolve() not in resolved.parents:
        raise ComponentRunnerError(f"{label} escapes component root")
    return resolved


def _require_sha(value: Any, label: str) -> str:
    text = _nonempty_string(value, label).upper()
    if len(text) != 64 or any(char not in "0123456789ABCDEF" for char in text):
        raise ComponentRunnerError(f"{label} must be a SHA-256")
    return text


def _safe_command(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not value or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise ComponentRunnerError(f"{label} must be a non-empty argv array")
    # A command is executed without a shell.  Rejecting shell metacharacters in
    # the plan is an additional guard against accidentally turning a data plan
    # into a command script.
    forbidden = {"&", "|", ";", ">", "<", "`"}
    if any(item in forbidden for item in value):
        raise ComponentRunnerError(f"{label} contains shell syntax")
    return list(value)


@dataclass(frozen=True)
class StagePlan:
    stage_id: str
    producer: str
    command: tuple[str, ...] | None
    cwd: str | None
    artifact_specs: tuple[dict[str, Any], ...]
    total: int | None
    unit: str
    work_id: str
    mode: str
    timeout_seconds: int | None
    receipt_ref: str | None


@dataclass(frozen=True)
class ComponentPlan:
    workflow_run_id: str
    component_run_id: str
    pipeline_id: str
    pipeline_version: str
    source_binding: dict[str, Any]
    config_sha256: str
    code_revision: str
    selected_chapter_ids: tuple[str, ...]
    stages: tuple[StagePlan, ...]
    scoring_handoff_fragment_ref: str

    def canonical_mapping(self) -> dict[str, Any]:
        return {
            "schema": RUNNER_SCHEMA,
            "workflow_run_id": self.workflow_run_id,
            "component_run_id": self.component_run_id,
            "pipeline_id": self.pipeline_id,
            "pipeline_version": self.pipeline_version,
            "source_binding": self.source_binding,
            "config_sha256": self.config_sha256,
            "code_revision": self.code_revision,
            "selected_chapter_ids": list(self.selected_chapter_ids),
            "stages": [
                {
                    "stage_id": stage.stage_id,
                    "producer": stage.producer,
                    "command": None if stage.command is None else list(stage.command),
                    "cwd": stage.cwd,
                    "artifact_specs": [dict(spec) for spec in stage.artifact_specs],
                    "total": stage.total,
                    "unit": stage.unit,
                    "work_id": stage.work_id,
                    "mode": stage.mode,
                    "timeout_seconds": stage.timeout_seconds,
                    "receipt_ref": stage.receipt_ref,
                }
                for stage in self.stages
            ],
            "scoring_handoff_fragment_ref": self.scoring_handoff_fragment_ref,
        }

    @property
    def plan_sha256(self) -> str:
        return canonical_sha256(self.canonical_mapping())

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ComponentPlan":
        _reject_forbidden_keys(value)
        if value.get("schema") != RUNNER_SCHEMA:
            raise ComponentRunnerError("plan.schema is invalid")
        required = {
            "schema",
            "workflow_run_id",
            "component_run_id",
            "pipeline_id",
            "pipeline_version",
            "source_binding",
            "config_sha256",
            "code_revision",
            "selected_chapter_ids",
            "stages",
            "scoring_handoff_fragment_ref",
        }
        unknown = set(value) - required
        missing = required - set(value)
        if missing or unknown:
            raise ComponentRunnerError(
                f"plan keys mismatch; missing={sorted(missing)}, unknown={sorted(unknown)}"
            )
        chapters = value["selected_chapter_ids"]
        if not isinstance(chapters, list) or not chapters or any(
            not isinstance(item, str) or not item for item in chapters
        ):
            raise ComponentRunnerError("selected_chapter_ids must be a non-empty string array")
        raw_stages = value["stages"]
        if not isinstance(raw_stages, list):
            raise ComponentRunnerError("stages must be an array")
        if [row.get("stage_id") for row in raw_stages if isinstance(row, Mapping)] != list(STAGE_IDS):
            raise ComponentRunnerError("plan stages must exactly match D2L Translation V1")
        stages: list[StagePlan] = []
        artifact_refs: set[str] = set()
        for index, raw in enumerate(raw_stages):
            if not isinstance(raw, Mapping):
                raise ComponentRunnerError(f"stages[{index}] must be an object")
            required_stage = {
                "stage_id",
                "producer",
                "command",
                "cwd",
                "artifact_specs",
                "total",
                "unit",
                "work_id",
                "mode",
                "timeout_seconds",
                "receipt_ref",
            }
            if set(raw) != required_stage:
                raise ComponentRunnerError(f"stages[{index}] keys mismatch")
            stage_id = _nonempty_string(raw["stage_id"], f"stages[{index}].stage_id")
            producer = _nonempty_string(raw["producer"], f"stages[{index}].producer")
            command = None if raw["command"] is None else tuple(
                _safe_command(raw["command"], f"stages[{index}].command")
            )
            cwd = None if raw["cwd"] is None else _nonempty_string(
                raw["cwd"], f"stages[{index}].cwd"
            )
            artifact_specs = raw["artifact_specs"]
            if not isinstance(artifact_specs, list):
                raise ComponentRunnerError(f"stages[{index}].artifact_specs must be an array")
            normalized_specs: list[dict[str, Any]] = []
            for spec_index, spec in enumerate(artifact_specs):
                if not isinstance(spec, Mapping):
                    raise ComponentRunnerError(f"artifact_specs[{spec_index}] must be an object")
                spec_required = {
                    "artifact_ref",
                    "artifact_kind",
                    "schema_version",
                    "relative_path",
                    "parent_artifact_refs",
                    "metadata",
                }
                if set(spec) != spec_required:
                    raise ComponentRunnerError(
                        f"stages[{index}].artifact_specs[{spec_index}] keys mismatch"
                    )
                artifact_ref = _nonempty_string(
                    spec["artifact_ref"],
                    f"stages[{index}].artifact_specs[{spec_index}].artifact_ref",
                )
                if artifact_ref in artifact_refs:
                    raise ComponentRunnerError(
                        f"artifact_ref is declared more than once: {artifact_ref}"
                    )
                artifact_refs.add(artifact_ref)
                if not isinstance(spec["metadata"], Mapping):
                    raise ComponentRunnerError(
                        f"stages[{index}].artifact_specs[{spec_index}].metadata must be an object"
                    )
                normalized_specs.append(dict(spec))
            total = raw["total"]
            if total is not None and (isinstance(total, bool) or not isinstance(total, int) or total < 0):
                raise ComponentRunnerError(f"stages[{index}].total must be null or >= 0")
            mode = raw["mode"]
            if mode not in {"execute", "reused"}:
                raise ComponentRunnerError(f"stages[{index}].mode is invalid")
            if mode == "execute" and command is None:
                raise ComponentRunnerError(f"stages[{index}] execute mode requires command")
            if mode == "reused" and command is not None:
                raise ComponentRunnerError(f"stages[{index}] reused mode cannot have command")
            timeout = raw["timeout_seconds"]
            if timeout is not None and (
                isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0
            ):
                raise ComponentRunnerError(f"stages[{index}].timeout_seconds is invalid")
            receipt_ref = raw["receipt_ref"]
            if receipt_ref is not None:
                receipt_ref = _nonempty_string(
                    receipt_ref,
                    f"stages[{index}].receipt_ref",
                )
                receipt_path = Path(receipt_ref)
                if receipt_path.is_absolute() or ".." in receipt_path.parts:
                    raise ComponentRunnerError(
                        f"stages[{index}].receipt_ref must be package-relative"
                    )
                receipt_specs = [
                    spec
                    for spec in normalized_specs
                    if spec["relative_path"] == receipt_ref
                    and spec["artifact_kind"] == "d2l_stage_event_receipt"
                    and spec["schema_version"] == STAGE_RECEIPT_SCHEMA
                ]
                if len(receipt_specs) != 1:
                    raise ComponentRunnerError(
                        f"stages[{index}].receipt_ref must have one matching artifact spec"
                    )
            if mode == "reused" and receipt_ref is not None:
                raise ComponentRunnerError(
                    f"stages[{index}] reused mode cannot replay a child receipt"
                )
            stages.append(
                StagePlan(
                    stage_id=stage_id,
                    producer=producer,
                    command=command,
                    cwd=cwd,
                    artifact_specs=tuple(normalized_specs),
                    total=total,
                    unit=_nonempty_string(raw["unit"], f"stages[{index}].unit"),
                    work_id=_nonempty_string(raw["work_id"], f"stages[{index}].work_id"),
                    mode=mode,
                    timeout_seconds=timeout,
                    receipt_ref=receipt_ref,
                )
            )
        fragment_ref = _nonempty_string(
            value["scoring_handoff_fragment_ref"], "scoring_handoff_fragment_ref"
        )
        source_binding = value["source_binding"]
        if not isinstance(source_binding, Mapping):
            raise ComponentRunnerError("source_binding must be an object")
        return cls(
            workflow_run_id=_nonempty_string(value["workflow_run_id"], "workflow_run_id"),
            component_run_id=_nonempty_string(value["component_run_id"], "component_run_id"),
            pipeline_id=_nonempty_string(value["pipeline_id"], "pipeline_id"),
            pipeline_version=_nonempty_string(value["pipeline_version"], "pipeline_version"),
            source_binding=dict(source_binding),
            config_sha256=_require_sha(value["config_sha256"], "config_sha256"),
            code_revision=_nonempty_string(value["code_revision"], "code_revision"),
            selected_chapter_ids=tuple(chapters),
            stages=tuple(stages),
            scoring_handoff_fragment_ref=fragment_ref,
        )

class D2LTranslationComponentRunner:
    """Execute an explicit D2L stage plan and publish a component package."""

    def __init__(
        self,
        plan: ComponentPlan | Mapping[str, Any],
        root: str | Path,
        *,
        stop_after_stage: str | None = None,
        pause_file: str | Path | None = None,
    ) -> None:
        self.plan = plan if isinstance(plan, ComponentPlan) else ComponentPlan.from_mapping(plan)
        self.root = Path(root).resolve()
        self.stop_after_stage = stop_after_stage
        self.pause_file = Path(pause_file).resolve() if pause_file is not None else None
        if stop_after_stage is not None and stop_after_stage not in STAGE_IDS:
            raise ComponentRunnerError("stop_after_stage is not a D2L stage")
        self.manifest: dict[str, Any]
        self.writer: D2LTranslationComponentEventWriter
        self.artifacts: list[dict[str, Any]] = []
        self._current_attempt = 1
        self._resuming = False
        self._previous_checkpoint: dict[str, Any] | None = None
        self._journal_cursor = 0

    @property
    def manifest_path(self) -> Path:
        return self.root / "component_manifest.json"

    @property
    def index_path(self) -> Path:
        return self.root / "artifact_index.json"

    @property
    def observation_journal_path(self) -> Path:
        return self.root / "runtime/component_observations.jsonl"

    @staticmethod
    def _latest_usage_snapshot_sha256(
        entries: Sequence[Mapping[str, Any]],
    ) -> str | None:
        snapshots = [
            entry["observation"]["payload"]
            for entry in entries
            if entry["observation"]["event"] == "usage_snapshot"
        ]
        return None if not snapshots else str(snapshots[-1]["snapshot_sha256"])

    def run(self, *, resume: bool = False) -> dict[str, Any]:
        if resume:
            if not self.root.is_dir():
                raise ComponentRunnerError("component root is missing during resume")
            self._open_resume()
        else:
            self.root.mkdir(parents=True, exist_ok=True)
            self._start_new()
        try:
            self._execute_remaining_stages()
            return self._finish_success()
        except _PauseRun as pause:
            self._pause(pause.stage_id, pause.reason)
            return validate_translation_component_package(self.root, require_terminal=False)
        except Exception as exc:
            self._fail(exc)
            raise

    def _start_new(self) -> None:
        if (
            self.manifest_path.exists()
            or (self.root / "events.jsonl").exists()
            or self.observation_journal_path.exists()
        ):
            raise ComponentRunnerError("component root already contains a run; use resume")
        self.root.mkdir(parents=True, exist_ok=True)
        stages = build_stage_plan()
        for row, stage in zip(stages, self.plan.stages, strict=True):
            row["producer"] = stage.producer
            row["progress"] = {
                "completed": 0,
                "total": stage.total,
                "unit": stage.unit,
            }
        self.manifest = build_component_manifest(
            workflow_run_id=self.plan.workflow_run_id,
            component_run_id=self.plan.component_run_id,
            component_attempt_id=1,
            pipeline_id=self.plan.pipeline_id,
            pipeline_version=self.plan.pipeline_version,
            source_binding=self.plan.source_binding,
            config_sha256=self.plan.config_sha256,
            code_revision=self.plan.code_revision,
            selected_chapter_ids=self.plan.selected_chapter_ids,
            started_at=_timestamp(),
            updated_at=_timestamp(),
            stages=stages,
        )
        write_component_manifest_snapshot(self.root, self.manifest)
        self._write_index()
        self.writer = D2LTranslationComponentEventWriter(
            self.root / "events.jsonl", manifest=self.manifest, component_attempt_id=1
        )
        # Construct the immutable revision reference directly from the current
        # manifest bytes; no directory scan becomes an authority.
        manifest_ref = f"manifest_revisions/{file_sha256(self.manifest_path)}.json"
        self.writer.emit(
            "run_start",
            stage_id=None,
            agent="d2l_component_runner",
            payload={
                "manifest_ref": manifest_ref,
                "manifest_sha256": file_sha256(self.manifest_path),
                "selected_chapter_ids": list(self.plan.selected_chapter_ids),
            },
        )
        self._journal_cursor = 0
        self._set_status("running", active_stage_id=STAGE_IDS[0])

    def _open_resume(self) -> None:
        # Validate the complete paused package before changing the current
        # manifest/index attempt or invoking any child command.
        package_validation = validate_translation_component_package(
            self.root, require_terminal=False
        )
        self.manifest = _load_json(self.manifest_path, "component manifest")
        current = validate_component_manifest(self.manifest)
        expected_immutable = {
            "workflow_run_id": self.plan.workflow_run_id,
            "component_run_id": self.plan.component_run_id,
            "pipeline_id": self.plan.pipeline_id,
            "pipeline_version": self.plan.pipeline_version,
            "source_binding": self.plan.source_binding,
            "config_sha256": self.plan.config_sha256,
            "code_revision": self.plan.code_revision,
            "selected_chapter_ids": list(self.plan.selected_chapter_ids),
        }
        if any(current[key] != expected for key, expected in expected_immutable.items()):
            raise ComponentRunnerError("resume identity does not match the sealed plan")
        current_stage_definitions = [
            (row["stage_id"], row["producer"], row["progress"]["unit"])
            for row in current["stages"]
        ]
        plan_stage_definitions = [
            (stage.stage_id, stage.producer, stage.unit)
            for stage in self.plan.stages
        ]
        if current_stage_definitions != plan_stage_definitions:
            raise ComponentRunnerError("resume stage plan does not match the sealed component")
        if current["status"] != "paused":
            raise ComponentRunnerError("only a paused component can be resumed")
        resume = current["resume"]
        if not resume["resume_available"]:
            raise ComponentRunnerError("paused component has no resumable checkpoint")
        checkpoint_path = _relative_path(self.root, resume["checkpoint_ref"], "resume.checkpoint_ref")
        if file_sha256(checkpoint_path) != resume["checkpoint_sha256"]:
            raise ComponentRunnerError("resume checkpoint hash drift")
        self._previous_checkpoint = _load_json(checkpoint_path, "checkpoint")
        checkpoint_state = self._previous_checkpoint.get("state")
        if not isinstance(checkpoint_state, Mapping):
            raise ComponentRunnerError("resume checkpoint state must be an object")
        checkpoint_plan_sha = checkpoint_state.get("runner_plan_sha256")
        if checkpoint_state.get("runner_plan_schema") != RUNNER_SCHEMA:
            raise ComponentRunnerError("resume checkpoint has no sealed runner plan")
        if checkpoint_plan_sha != self.plan.plan_sha256:
            raise ComponentRunnerError("resume runner plan hash mismatch")
        journal_entries = read_observation_journal(self.observation_journal_path)
        journal_state = observation_journal_state(journal_entries)
        if (
            checkpoint_state.get("observation_journal_entry_count")
            != journal_state["entry_count"]
            or checkpoint_state.get("observation_journal_last_entry_sha256")
            != journal_state["last_entry_sha256"]
        ):
            raise ComponentRunnerError("resume observation journal lineage mismatch")
        latest_usage_sha = self._latest_usage_snapshot_sha256(journal_entries)
        if (
            checkpoint_state.get("latest_usage_snapshot_sha256")
            != latest_usage_sha
            or package_validation.get("latest_usage_snapshot_sha256")
            != latest_usage_sha
        ):
            raise ComponentRunnerError("resume usage snapshot lineage mismatch")
        self._journal_cursor = len(journal_entries)
        self._current_attempt = int(current["component_attempt_id"]) + 1
        self.manifest = dict(current)
        self.manifest["component_attempt_id"] = self._current_attempt
        self.manifest["status"] = "running"
        self.manifest["active_stage_id"] = resume["stage_id"]
        self.manifest["resume"] = {
            "resume_available": False,
            "checkpoint_ref": None,
            "checkpoint_sha256": None,
            "stage_id": None,
            "work_id": None,
            "paused_reason": None,
        }
        self._rewrite_index_attempt()
        write_component_manifest_snapshot(self.root, self.manifest)
        self.writer = D2LTranslationComponentEventWriter(
            self.root / "events.jsonl",
            manifest=self.manifest,
            component_attempt_id=self._current_attempt,
        )
        self.writer.emit(
            "run_resumed",
            stage_id=None,
            agent="d2l_component_runner",
            payload={
                "previous_component_attempt_id": self._current_attempt - 1,
                "checkpoint_ref": resume["checkpoint_ref"],
                "checkpoint_sha256": resume["checkpoint_sha256"],
                "reason_code": "resume_after_pause",
            },
        )

    def _execute_remaining_stages(self) -> None:
        stage_rows = {row["stage_id"]: row for row in self.manifest["stages"]}
        for stage in self.plan.stages:
            row = stage_rows[stage.stage_id]
            if row["status"] in {"succeeded", "skipped", "reused"}:
                continue
            self._start_stage(stage)
            if stage.mode == "execute":
                self._execute_command(stage)
            self._emit_stage_receipt(stage)
            self._register_stage_artifacts(stage)
            self.writer.emit(
                "validation_passed",
                stage_id=stage.stage_id,
                agent="d2l_component_runner",
                payload={
                    "validator_id": "d2l_component_artifact_validator_v1",
                    "subject_ref": stage.stage_id,
                    "reason_codes": ["stage_artifacts_valid"],
                    "retryable": False,
                },
            )
            outcome = "reused" if stage.mode == "reused" else "succeeded"
            self._finish_stage(stage, outcome)
            if self.stop_after_stage == stage.stage_id:
                next_stage = self._next_pending_stage(stage.stage_id)
                if next_stage is None:
                    raise ComponentRunnerError("stop_after_stage cannot pause after the final stage")
                raise _PauseRun(next_stage.stage_id, "bounded_test_pause")
            if self.pause_file is not None and self.pause_file.is_file():
                next_stage = self._next_pending_stage(stage.stage_id)
                if next_stage is None:
                    return
                raise _PauseRun(next_stage.stage_id, "user_requested_pause")

    def _start_stage(self, stage: StagePlan) -> None:
        self._set_status("running", active_stage_id=stage.stage_id)
        row = self._stage_row(stage.stage_id)
        row["status"] = "running"
        row["started_at"] = _timestamp()
        row["current_work_id"] = stage.work_id
        row["progress"] = {"completed": 0, "total": stage.total, "unit": stage.unit}
        self._save_manifest()
        self.writer.emit(
            "stage_start",
            stage_id=stage.stage_id,
            agent=stage.producer,
            payload={
                "progress": dict(row["progress"]),
                "current_work_id": stage.work_id,
            },
        )
        self.writer.emit(
            "work_started",
            stage_id=stage.stage_id,
            agent=stage.producer,
            payload={
                "work_kind": stage.unit,
                "work_id": stage.work_id,
                "progress": dict(row["progress"]),
            },
        )

    def _execute_command(self, stage: StagePlan) -> None:
        assert stage.command is not None
        cwd = self.root if stage.cwd is None else Path(stage.cwd).resolve()
        started = time.monotonic()
        pause_was_present_at_start = bool(
            self.pause_file is not None and self.pause_file.is_file()
        )
        process = subprocess.Popen(
            list(stage.command),
            cwd=cwd,
            shell=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        while process.poll() is None:
            self._drain_observation_journal(stage, allow_incomplete_tail=True)
            if (
                self.pause_file is not None
                and self.pause_file.is_file()
                and not pause_was_present_at_start
            ):
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                self._drain_observation_journal(
                    stage, allow_incomplete_tail=False
                )
                self.pause_file.unlink(missing_ok=True)
                raise _PauseRun(stage.stage_id, "user_requested_pause")
            if (
                stage.timeout_seconds is not None
                and time.monotonic() - started > stage.timeout_seconds
            ):
                process.kill()
                process.wait(timeout=5)
                self._drain_observation_journal(
                    stage, allow_incomplete_tail=False
                )
                raise ComponentRunnerError(f"stage {stage.stage_id} timed out")
            time.sleep(0.05)
        self._drain_observation_journal(stage, allow_incomplete_tail=False)
        if process.returncode == TRANSPORT_RETRY_EXHAUSTED_EXIT_CODE:
            raise _PauseRun(stage.stage_id, "transport_retry_exhausted")
        if process.returncode != 0:
            raise ComponentRunnerError(
                f"stage {stage.stage_id} returned exit code {process.returncode}"
            )

    def _drain_observation_journal(
        self,
        stage: StagePlan,
        *,
        allow_incomplete_tail: bool,
    ) -> None:
        entries = read_observation_journal(
            self.observation_journal_path,
            allow_incomplete_tail=allow_incomplete_tail,
        )
        if self._journal_cursor > len(entries):
            raise ComponentRunnerError("observation journal was truncated")
        for entry in entries[self._journal_cursor :]:
            if (
                entry["workflow_run_id"] != self.manifest["workflow_run_id"]
                or entry["component_run_id"] != self.manifest["component_run_id"]
                or entry["component_attempt_id"] != self._current_attempt
                or entry["stage_id"] != stage.stage_id
                or entry["producer"] != stage.producer
                or entry["work_id"] != stage.work_id
            ):
                raise ComponentRunnerError(
                    "observation journal entry is foreign to the active stage"
                )
            observation = entry["observation"]
            event_name = observation["event"]
            if event_name == "work_progress":
                progress = dict(observation["payload"]["progress"])
                row = self._stage_row(stage.stage_id)
                row["progress"] = progress
                row["current_work_id"] = stage.work_id
                self._save_manifest()
            self.writer.emit(
                event_name,
                stage_id=(
                    None
                    if event_name in {"cost_snapshot", "usage_snapshot"}
                    else stage.stage_id
                ),
                agent=observation["agent"],
                severity=observation["severity"],
                ts=observation["ts"],
                payload=observation["payload"],
            )
            self._journal_cursor += 1

    def _register_stage_artifacts(self, stage: StagePlan) -> None:
        for spec in stage.artifact_specs:
            ref = _nonempty_string(spec["artifact_ref"], "artifact_ref")
            if ref in {row["artifact_ref"] for row in self.artifacts}:
                raise ComponentRunnerError(f"artifact_ref was already published: {ref}")
            path = _relative_path(self.root, spec["relative_path"], f"{ref}.relative_path")
            if not path.is_file():
                raise ComponentRunnerError(f"declared artifact is missing: {spec['relative_path']}")
            parent_refs = spec["parent_artifact_refs"]
            if not isinstance(parent_refs, list) or any(
                not isinstance(item, str) or not item for item in parent_refs
            ):
                raise ComponentRunnerError(f"{ref}.parent_artifact_refs is invalid")
            row = {
                "workflow_run_id": self.manifest["workflow_run_id"],
                "flow_kind": self.manifest["flow_kind"],
                "component_id": COMPONENT_ID,
                "component_run_id": self.manifest["component_run_id"],
                "component_attempt_id": self._current_attempt,
                "artifact_ref": ref,
                "artifact_kind": _nonempty_string(spec["artifact_kind"], f"{ref}.artifact_kind"),
                "schema_version": _nonempty_string(spec["schema_version"], f"{ref}.schema_version"),
                "sha256": file_sha256(path),
                "sha256_kind": "physical",
                "producer_stage_id": stage.stage_id,
                "parent_artifact_refs": list(parent_refs),
                "created_event_id": self.writer.next_event_id,
                "relative_path": str(path.relative_to(self.root)).replace("\\", "/"),
                "availability": "available",
                "metadata": dict(spec["metadata"]),
            }
            self.artifacts.append(row)
            self._write_index()
            self.writer.emit(
                "artifact_created",
                stage_id=stage.stage_id,
                agent=stage.producer,
                payload={
                    "artifact_ref": row["artifact_ref"],
                    "artifact_kind": row["artifact_kind"],
                    "schema_version": row["schema_version"],
                    "sha256": row["sha256"],
                    "sha256_kind": row["sha256_kind"],
                    "parent_artifact_refs": row["parent_artifact_refs"],
                },
            )

    def _emit_stage_receipt(self, stage: StagePlan) -> None:
        if stage.receipt_ref is None:
            return
        receipt_path = _relative_path(
            self.root,
            stage.receipt_ref,
            f"{stage.stage_id}.receipt_ref",
        )
        receipt = validate_stage_receipt(
            _load_json(receipt_path, f"{stage.stage_id} stage receipt"),
            manifest=self.manifest,
            stage_id=stage.stage_id,
            producer=stage.producer,
            work_id=stage.work_id,
            start_component_seq=self.writer.component_seq,
        )
        journal_entries = read_observation_journal(self.observation_journal_path)
        matching_journal_entries = [
            entry
            for entry in journal_entries
            if entry["workflow_run_id"] == receipt["workflow_run_id"]
            and entry["component_run_id"] == receipt["component_run_id"]
            and entry["component_attempt_id"] == receipt["component_attempt_id"]
            and entry["stage_id"] == receipt["stage_id"]
            and entry["producer"] == receipt["producer"]
            and entry["work_id"] == receipt["work_id"]
        ]
        if matching_journal_entries:
            validate_stage_receipt_against_journal(
                receipt,
                journal_entries=journal_entries,
            )
            if self._journal_cursor != len(journal_entries):
                raise ComponentRunnerError(
                    "stage receipt was published before journal observations were emitted"
                )
            return
        for observation in receipt["observations"]:
            event_name = observation["event"]
            self.writer.emit(
                event_name,
                stage_id=(
                    None
                    if event_name in {"cost_snapshot", "usage_snapshot"}
                    else stage.stage_id
                ),
                agent=observation["agent"],
                severity=observation["severity"],
                ts=observation["ts"],
                payload=observation["payload"],
            )

    def _finish_stage(self, stage: StagePlan, outcome: str) -> None:
        row = self._stage_row(stage.stage_id)
        row["status"] = outcome
        row["ended_at"] = _timestamp()
        row["current_work_id"] = None
        observed_total = row["progress"]["total"]
        row["progress"] = {
            "completed": (
                row["progress"]["completed"]
                if observed_total is None
                else observed_total
            ),
            "total": observed_total,
            "unit": stage.unit,
        }
        row["artifact_refs"] = [
            item["artifact_ref"]
            for item in self.artifacts
            if item["producer_stage_id"] == stage.stage_id
        ]
        self._save_manifest()
        self.writer.emit(
            "stage_done",
            stage_id=stage.stage_id,
            agent=stage.producer,
            payload={
                "outcome": outcome,
                "reason_code": "stage_complete",
                "progress": dict(row["progress"]),
            },
        )

    def _pause(self, stage_id: str, reason: str) -> None:
        stage = next(item for item in self.plan.stages if item.stage_id == stage_id)
        if self.observation_journal_path.is_file():
            self._drain_observation_journal(
                stage,
                allow_incomplete_tail=False,
            )
        journal_entries = read_observation_journal(self.observation_journal_path)
        journal_state = observation_journal_state(journal_entries)
        stage_row = self._stage_row(stage_id)
        if stage_row["status"] == "running":
            stage_row["status"] = "paused"
            stage_row["current_work_id"] = stage.work_id
        state = {
            "completed_stage_ids": [
                row["stage_id"]
                for row in self.manifest["stages"]
                if row["status"] in {"succeeded", "reused", "skipped"}
            ],
            "next_stage_id": stage_id,
            "component_attempt_id": self._current_attempt,
            "runner_plan_schema": RUNNER_SCHEMA,
            "runner_plan_sha256": self.plan.plan_sha256,
            "observation_journal_entry_count": journal_state["entry_count"],
            "observation_journal_last_entry_sha256": journal_state[
                "last_entry_sha256"
            ],
            "latest_usage_snapshot_sha256": self._latest_usage_snapshot_sha256(
                journal_entries
            ),
        }
        checkpoint_ref = f"checkpoints/checkpoint_a{self._current_attempt}_{stage_id}.json"
        checkpoint = build_checkpoint(
            manifest=self.manifest,
            checkpoint_ref=checkpoint_ref,
            stage_id=stage_id,
            work_id=stage.work_id,
            resume_available=True,
            paused_reason=reason,
            created_at=_timestamp(),
            state=state,
        )
        checkpoint_path = self.root / checkpoint_ref
        write_json(checkpoint_path, checkpoint)
        checkpoint_sha = file_sha256(checkpoint_path)
        self.writer.emit(
            "checkpoint",
            stage_id=stage_id,
            agent="d2l_component_runner",
            payload={
                "checkpoint_ref": checkpoint_ref,
                "checkpoint_sha256": checkpoint_sha,
                "stage_id": stage_id,
                "work_id": stage.work_id,
                "resume_available": True,
                "paused_reason": reason,
            },
        )
        self.manifest["status"] = "paused"
        self.manifest["active_stage_id"] = stage_id
        self.manifest["resume"] = {
            "resume_available": True,
            "checkpoint_ref": checkpoint_ref,
            "checkpoint_sha256": checkpoint_sha,
            "stage_id": stage_id,
            "work_id": stage.work_id,
            "paused_reason": reason,
        }
        self._save_manifest()

    def _emit_component_final_usage_snapshot(self) -> None:
        entries = read_observation_journal(self.observation_journal_path)
        if self._journal_cursor != len(entries):
            raise ComponentRunnerError(
                "component final usage cannot skip durable journal observations"
            )
        snapshots = [
            dict(entry["observation"]["payload"])
            for entry in entries
            if entry["observation"]["event"] == "usage_snapshot"
        ]
        if snapshots and snapshots[-1]["snapshot_kind"] == "component_final":
            return
        snapshot = build_component_usage_snapshot(
            previous_snapshots=snapshots,
            workflow_run_id=self.manifest["workflow_run_id"],
            component_run_id=self.manifest["component_run_id"],
            component_attempt_id=self._current_attempt,
            stage_id=None,
            work_id=None,
            accepted_usage=None,
            component_final=True,
        )
        journal_writer = D2LStageObservationJournalWriter(
            path=self.observation_journal_path,
            workflow_run_id=self.manifest["workflow_run_id"],
            component_run_id=self.manifest["component_run_id"],
            component_attempt_id=self._current_attempt,
            stage_id=STAGE_IDS[-1],
            producer="d2l_component_runner",
            work_id="component_final_usage",
        )
        observation = {
            "event": "usage_snapshot",
            "agent": "d2l_component_runner",
            "severity": "info",
            "ts": _timestamp(),
            "payload": snapshot,
        }
        journal_writer.append(observation)
        self._journal_cursor += 1
        self.writer.emit(
            "usage_snapshot",
            stage_id=None,
            agent=observation["agent"],
            severity=observation["severity"],
            ts=observation["ts"],
            payload=snapshot,
        )

    def _fail(self, exc: Exception) -> None:
        if getattr(self.writer, "_terminal", False):
            return
        failed_stage = self.manifest.get("active_stage_id")
        if failed_stage is not None:
            row = self._stage_row(failed_stage)
            if row["status"] == "running":
                row["status"] = "failed"
                row["ended_at"] = _timestamp()
                row["current_work_id"] = None
                row["artifact_refs"] = [
                    item["artifact_ref"]
                    for item in self.artifacts
                    if item["producer_stage_id"] == failed_stage
                ]
                self.writer.emit(
                    "validation_failed",
                    stage_id=failed_stage,
                    agent="d2l_component_runner",
                    severity="error",
                    payload={
                        "validator_id": "d2l_component_stage_execution_v1",
                        "subject_ref": failed_stage,
                        "reason_codes": [type(exc).__name__],
                        "retryable": False,
                    },
                )
                self.writer.emit(
                    "stage_done",
                    stage_id=failed_stage,
                    agent=row["producer"],
                    severity="error",
                    payload={
                        "outcome": "failed",
                        "reason_code": type(exc).__name__,
                        "progress": dict(row["progress"]),
                    },
                )
        self.manifest["status"] = "failed"
        self.manifest["active_stage_id"] = None
        self._save_manifest()
        self._emit_component_final_usage_snapshot()
        self.writer.emit(
            "run_failed",
            stage_id=None,
            agent="d2l_component_runner",
            severity="error",
            payload={
                "failed_stage_id": failed_stage,
                "error_code": type(exc).__name__,
                "message": str(exc),
                "retryable": False,
                "checkpoint_ref": None,
                "checkpoint_sha256": None,
            },
        )

    def _finish_success(self) -> dict[str, Any]:
        fragment_path = _relative_path(
            self.root,
            self.plan.scoring_handoff_fragment_ref,
            "scoring_handoff_fragment_ref",
        )
        if not fragment_path.is_file():
            raise ComponentRunnerError("scoring handoff fragment is missing")
        fragment = _load_json(fragment_path, "scoring handoff fragment")
        validate_scoring_handoff_fragment(fragment)
        if fragment["workflow_run_id"] != self.manifest["workflow_run_id"]:
            raise ComponentRunnerError("scoring fragment workflow identity mismatch")
        if fragment["translation_component_run_id"] != self.manifest["component_run_id"]:
            raise ComponentRunnerError("scoring fragment component identity mismatch")
        if fragment["translation_component_attempt_id"] != self._current_attempt:
            raise ComponentRunnerError("scoring fragment attempt does not match current attempt")
        if fragment["artifact_ref"] not in {row["artifact_ref"] for row in self.artifacts}:
            raise ComponentRunnerError("scoring fragment must be declared by the stage plan")
        self.manifest["status"] = "succeeded"
        self.manifest["active_stage_id"] = None
        self.manifest["scoring_handoff_fragment_ref"] = self.plan.scoring_handoff_fragment_ref
        self.manifest["resume"] = {
            "resume_available": False,
            "checkpoint_ref": None,
            "checkpoint_sha256": None,
            "stage_id": None,
            "work_id": None,
            "paused_reason": None,
        }
        self._save_manifest()
        # Child stages own physical-attempt usage.  Until a child supplies a
        # sealed usage receipt, omitting cost_snapshot is more truthful than
        # fabricating zero calls or zero tokens.
        validate_translation_component_package(self.root, require_terminal=False)
        self._emit_component_final_usage_snapshot()
        self.writer.emit(
            "run_done",
            stage_id=None,
            agent="d2l_component_runner",
            payload={
                "artifact_index_ref": "artifact_index.json",
                "artifact_index_sha256": file_sha256(self.index_path),
                "scoring_handoff_fragment_ref": self.plan.scoring_handoff_fragment_ref,
                "scoring_handoff_fragment_sha256": file_sha256(fragment_path),
                "outcome": "succeeded",
            },
        )
        return validate_translation_component_package(self.root)

    def _next_pending_stage(self, current_stage_id: str) -> StagePlan | None:
        seen = False
        for stage in self.plan.stages:
            if stage.stage_id == current_stage_id:
                seen = True
                continue
            if seen and self._stage_row(stage.stage_id)["status"] == "pending":
                return stage
        return None

    def _stage_row(self, stage_id: str) -> dict[str, Any]:
        for row in self.manifest["stages"]:
            if row["stage_id"] == stage_id:
                return row
        raise ComponentRunnerError(f"stage is absent from manifest: {stage_id}")

    def _set_status(self, status: str, *, active_stage_id: str | None) -> None:
        self.manifest["status"] = status
        self.manifest["active_stage_id"] = active_stage_id

    def _save_manifest(self) -> None:
        self.manifest["updated_at"] = _timestamp()
        validate_component_manifest(self.manifest)
        write_component_manifest_snapshot(self.root, self.manifest)
        if hasattr(self, "writer"):
            self.writer.manifest = validate_component_manifest(self.manifest)

    def _write_index(self) -> None:
        payload = {
            "schema": "d2l_translation_artifact_index_v1",
            "workflow_run_id": self.manifest["workflow_run_id"],
            "flow_kind": self.manifest["flow_kind"],
            "component_id": COMPONENT_ID,
            "component_run_id": self.manifest["component_run_id"],
            "component_attempt_id": self.manifest["component_attempt_id"],
            "artifacts": self.artifacts,
        }
        write_json(self.index_path, payload)

    def _rewrite_index_attempt(self) -> None:
        if not self.index_path.is_file():
            raise ComponentRunnerError("artifact index is missing during resume")
        index = _load_json(self.index_path, "artifact index")
        index["component_attempt_id"] = self._current_attempt
        self.artifacts = list(index.get("artifacts") or [])
        write_json(self.index_path, index)
        validate_artifact_index(index, manifest=self.manifest, artifact_root=self.root)


class _PauseRun(Exception):
    def __init__(self, stage_id: str, reason: str) -> None:
        super().__init__(reason)
        self.stage_id = stage_id
        self.reason = reason


def run_from_plan_file(
    plan_path: str | Path,
    root: str | Path,
    *,
    resume: bool = False,
    stop_after_stage: str | None = None,
    pause_file: str | Path | None = None,
) -> dict[str, Any]:
    plan = ComponentPlan.from_mapping(_load_json(Path(plan_path), "runner plan"))
    return D2LTranslationComponentRunner(
        plan,
        root,
        stop_after_stage=stop_after_stage,
        pause_file=pause_file,
    ).run(resume=resume)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a D2L Translation component plan")
    parser.add_argument("--plan", required=True)
    parser.add_argument("--component-root", required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--stop-after-stage")
    parser.add_argument("--pause-file")
    args = parser.parse_args()
    result = run_from_plan_file(
        args.plan,
        args.component_root,
        resume=args.resume,
        stop_after_stage=args.stop_after_stage,
        pause_file=args.pause_file,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
