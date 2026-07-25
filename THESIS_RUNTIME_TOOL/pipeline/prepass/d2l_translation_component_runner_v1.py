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
from hashlib import sha256
import json
import os
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
    project_artifact_term_batches,
    project_work_journal_term_batches,
    term_work_completed,
    validate_artifact_index,
    validate_component_event_stream,
    validate_component_manifest,
    validate_scoring_handoff_fragment,
    validate_term_lifecycle_event_sequence,
    validate_translation_component_package,
    write_component_manifest_snapshot,
    write_json,
)
from pipeline.prepass.d2l_component_stage_receipt_v1 import (
    D2LStageReceiptError,
    D2LStageObservationJournalWriter,
    STAGE_RECEIPT_SCHEMA,
    observation_journal_state,
    read_observation_journal,
    validate_stage_receipt,
    validate_stage_receipt_against_journal,
)
from pipeline.prepass.d2l_component_writer_lease_v1 import (
    D2LComponentWriterLease,
    D2LComponentWriterLeaseError,
    stage_writer_is_active,
)
from pipeline.prepass.d2l_shared_llm_adapter_v1 import (
    TRANSPORT_RETRY_EXHAUSTED_EXIT_CODE,
)
from pipeline.prepass.d2l_repair_resume_v1 import (
    CHAIN_SCHEMA_VERSION,
    D2LRepairResumeError,
    build_chain_repair_receipt,
    build_repair_receipt,
    validate_chain_repair_paths,
    validate_mechanical_repair_paths,
    validate_repair_receipt,
)
from pipeline.prepass.d2l_stage_work_journal_v1 import (
    read_work_journal,
    work_journal_state,
)
from pipeline.prepass.d2l_stage_process_tree_v1 import (
    D2LGuardedStageProcess,
    D2LStageProcessTreeError,
)


RUNNER_SCHEMA = "d2l_translation_component_runner_plan_v1_2"
RUNNER_VERSION = "d2l_translation_component_runner_v1_4_exclusive_repair_resume"
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
        repair_code_root: str | Path | None = None,
        repair_reason: str | None = None,
        recover_stale: bool = False,
    ) -> None:
        self.plan = plan if isinstance(plan, ComponentPlan) else ComponentPlan.from_mapping(plan)
        self.root = Path(root).resolve()
        self.stop_after_stage = stop_after_stage
        self.pause_file = Path(pause_file).resolve() if pause_file is not None else None
        self.repair_code_root = (
            Path(repair_code_root).resolve()
            if repair_code_root is not None
            else None
        )
        self.repair_reason = repair_reason
        self.recover_stale = recover_stale
        if stop_after_stage is not None and stop_after_stage not in STAGE_IDS:
            raise ComponentRunnerError("stop_after_stage is not a D2L stage")
        self.manifest: dict[str, Any]
        self.writer: D2LTranslationComponentEventWriter
        self.artifacts: list[dict[str, Any]] = []
        self._current_attempt = 1
        self._resuming = False
        self._previous_checkpoint: dict[str, Any] | None = None
        self._journal_cursor = 0
        self._effective_code_revision = self.plan.code_revision
        self._repair_receipt: dict[str, Any] | None = None
        self._repair_receipt_path: Path | None = None
        self._repair_receipt_register = False
        self._repair_delta: dict[str, Any] | None = None
        self._revision_preflight_done = False
        self._pending_journal_recovery: (
            tuple[StagePlan, Path, dict[str, Any]] | None
        ) = None
        self._term_state_initialized = False
        self._term_rows_by_id: dict[str, dict[str, Any]] = {}
        self._term_batches_by_id: dict[str, str] = {}
        self._term_evidence_by_key: dict[str, dict[str, Any]] = {}
        self._term_prior_rows_by_evidence: dict[
            str, list[dict[str, Any]]
        ] = {}
        self._term_batch_ids_by_evidence: dict[str, list[str]] = {}
        self._term_projection_mode_by_evidence: dict[str, str] = {}
        self._term_checked_evidence_keys: set[str] = set()

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

    def _work_journal_checkpoint_state(self) -> dict[str, dict[str, Any]]:
        journals: dict[str, dict[str, Any]] = {}
        for stage_id in STAGE_IDS:
            relative_ref = f"runtime/work_items/{stage_id}.jsonl"
            path = self.root / relative_ref
            if not path.is_file():
                continue
            state = work_journal_state(read_work_journal(path))
            journals[stage_id] = {
                "journal_ref": relative_ref,
                "journal_sha256": file_sha256(path),
                "entry_count": state["entry_count"],
                "last_entry_sha256": state["last_entry_sha256"],
            }
        return journals

    def _component_events(self) -> list[dict[str, Any]]:
        path = self.root / "events.jsonl"
        if not path.is_file():
            return []
        rows: list[dict[str, Any]] = []
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ComponentRunnerError(
                    f"component event line {line_number} is invalid"
                ) from exc
            if not isinstance(row, dict):
                raise ComponentRunnerError(
                    f"component event line {line_number} must be an object"
                )
            rows.append(row)
        return rows

    def _initialize_term_lifecycle_state(self) -> None:
        if self._term_state_initialized:
            return
        validate_component_event_stream(
            self.root / "events.jsonl",
            manifest=self.manifest,
            require_terminal=False,
        )
        events = self._component_events()
        state = validate_term_lifecycle_event_sequence(events)
        self._term_rows_by_id = {
            str(row_id): dict(row)
            for row_id, row in state["rows_by_id"].items()
        }
        self._term_batches_by_id = dict(state["batches_by_id"])
        running_rows: dict[str, dict[str, Any]] = {}
        seen_batches: set[str] = set()
        for event in events:
            if event["event"] != "term_lifecycle":
                continue
            batch = event["payload"]
            evidence = dict(batch["evidence"])
            evidence_key = canonical_sha256(evidence)
            previous_evidence = self._term_evidence_by_key.get(evidence_key)
            if previous_evidence is not None and previous_evidence != evidence:
                raise ComponentRunnerError(
                    "term lifecycle evidence identity drift"
                )
            if evidence_key not in self._term_prior_rows_by_evidence:
                self._term_prior_rows_by_evidence[evidence_key] = [
                    dict(row) for row in running_rows.values()
                ]
                self._term_batch_ids_by_evidence[evidence_key] = []
            self._term_evidence_by_key[evidence_key] = evidence
            prior_mode = self._term_projection_mode_by_evidence.get(
                evidence_key
            )
            current_mode = str(batch["projection_mode"])
            if prior_mode is not None and prior_mode != current_mode:
                raise ComponentRunnerError(
                    "one term lifecycle evidence has multiple projection modes"
                )
            self._term_projection_mode_by_evidence[evidence_key] = (
                current_mode
            )
            if batch["batch_id"] in seen_batches:
                continue
            self._term_batch_ids_by_evidence[evidence_key].append(
                str(batch["batch_id"])
            )
            seen_batches.add(str(batch["batch_id"]))
            for row in batch["rows"]:
                running_rows[str(row["row_id"])] = dict(row)
        self._term_state_initialized = True

    def _existing_term_evidence(
        self,
        *,
        evidence_ref: str,
        evidence_sha256: str,
    ) -> dict[str, Any] | None:
        matches = [
            evidence
            for evidence in self._term_evidence_by_key.values()
            if (
                (
                    evidence["journal_ref"]
                    if evidence["evidence_kind"] == "work_journal"
                    else evidence["artifact_ref"]
                )
                == evidence_ref
                and (
                    evidence["entry_sha256"]
                    if evidence["evidence_kind"] == "work_journal"
                    else evidence["sha256"]
                )
                == evidence_sha256
            )
        ]
        if len(matches) > 1:
            raise ComponentRunnerError(
                "one term evidence binding has multiple lifecycle identities"
            )
        return None if not matches else dict(matches[0])

    def _validation_event_for_work_entry(
        self,
        *,
        stage_id: str,
        entry: Mapping[str, Any],
        journal_ref: str,
    ) -> dict[str, Any]:
        existing = self._existing_term_evidence(
            evidence_ref=journal_ref,
            evidence_sha256=str(entry["entry_sha256"]),
        )
        events = self._component_events()
        if existing is not None:
            event = next(
                (
                    row
                    for row in events
                    if row["event_id"] == existing["validation_event_id"]
                ),
                None,
            )
            if event is None:
                raise ComponentRunnerError(
                    "existing term lifecycle validation event is missing"
                )
            return event
        candidates = [
            row
            for row in events
            if row["event"] == "validation_passed"
            and row["stage_id"] == stage_id
            and row["payload"]["subject_ref"] == entry["work_item_id"]
        ]
        if not candidates:
            raise ComponentRunnerError(
                "accepted work item lacks a validation_passed event"
            )
        candidates.sort(
            key=lambda row: (
                row["component_attempt_id"]
                != entry["component_attempt_id"],
                row["component_seq"],
            )
        )
        return candidates[0]

    def _emit_term_lifecycle_batches(
        self,
        *,
        stage: StagePlan,
        batches: Sequence[Mapping[str, Any]],
        previous_rows: Sequence[Mapping[str, Any]],
    ) -> None:
        if not batches:
            return
        evidence = dict(batches[0]["evidence"])
        evidence_key = canonical_sha256(evidence)
        expected_ids = [str(batch["batch_id"]) for batch in batches]
        existing_ids = self._term_batch_ids_by_evidence.get(
            evidence_key,
            [],
        )
        if existing_ids != expected_ids[: len(existing_ids)]:
            raise ComponentRunnerError(
                "existing term lifecycle batch does not match evidence projection"
            )
        stored_prior = self._term_prior_rows_by_evidence.get(evidence_key)
        normalized_prior = [dict(row) for row in previous_rows]
        if stored_prior is not None and stored_prior != normalized_prior:
            raise ComponentRunnerError(
                "term lifecycle prior-row projection drift"
            )
        self._term_prior_rows_by_evidence.setdefault(
            evidence_key,
            normalized_prior,
        )
        projection_mode = str(batches[0]["projection_mode"])
        prior_projection_mode = self._term_projection_mode_by_evidence.get(
            evidence_key
        )
        if (
            prior_projection_mode is not None
            and prior_projection_mode != projection_mode
        ):
            raise ComponentRunnerError(
                "term lifecycle projection mode drift"
            )
        self._term_projection_mode_by_evidence[evidence_key] = projection_mode
        for batch in batches:
            if (
                batch["evidence"] != evidence
                or batch["projection_mode"] != projection_mode
            ):
                raise ComponentRunnerError(
                    "term lifecycle batch group is inconsistent"
                )
            batch_id = str(batch["batch_id"])
            batch_sha = str(batch["batch_sha256"])
            existing_batch_sha = self._term_batches_by_id.get(batch_id)
            if existing_batch_sha is not None:
                if existing_batch_sha != batch_sha:
                    raise ComponentRunnerError(
                        "term lifecycle batch ID hash conflict"
                    )
                continue
            for row in batch["rows"]:
                existing = self._term_rows_by_id.get(str(row["row_id"]))
                if existing is not None:
                    if existing["row_sha256"] != row["row_sha256"]:
                        raise ComponentRunnerError(
                            "term lifecycle row ID hash conflict"
                        )
                    raise ComponentRunnerError(
                        "term lifecycle row exists outside its deterministic batch"
                    )
            self.writer.emit(
                "term_lifecycle",
                stage_id=stage.stage_id,
                agent=stage.producer,
                payload=dict(batch),
            )
            self._term_batches_by_id[batch_id] = batch_sha
            self._term_evidence_by_key[evidence_key] = evidence
            self._term_batch_ids_by_evidence.setdefault(
                evidence_key,
                [],
            ).append(batch_id)
            for row in batch["rows"]:
                self._term_rows_by_id[str(row["row_id"])] = dict(row)

    def _project_term_work_entry(
        self,
        *,
        stage: StagePlan,
        journal_ref: str,
        entries: Sequence[Mapping[str, Any]],
        entry: Mapping[str, Any],
        projection_mode: str,
    ) -> None:
        validation_event = self._validation_event_for_work_entry(
            stage_id=stage.stage_id,
            entry=entry,
            journal_ref=journal_ref,
        )
        existing_evidence = self._existing_term_evidence(
            evidence_ref=journal_ref,
            evidence_sha256=str(entry["entry_sha256"]),
        )
        existing_evidence_key = (
            None
            if existing_evidence is None
            else canonical_sha256(existing_evidence)
        )
        if (
            existing_evidence_key is not None
            and existing_evidence_key in self._term_checked_evidence_keys
        ):
            return
        if existing_evidence is None:
            previous_rows = list(self._term_rows_by_id.values())
        else:
            evidence_key = existing_evidence_key
            assert evidence_key is not None
            previous_rows = self._term_prior_rows_by_evidence[evidence_key]
            projection_mode = self._term_projection_mode_by_evidence[
                evidence_key
            ]
        completed = term_work_completed(
            stage.stage_id,
            entries,
            through_journal_seq=int(entry["journal_seq"]),
        )
        batches = project_work_journal_term_batches(
            stage_id=stage.stage_id,
            journal_ref=journal_ref,
            entry=entry,
            validation_event=validation_event,
            previous_rows=previous_rows,
            projection_mode=projection_mode,
            completed=completed,
            total=stage.total,
            unit=stage.unit,
        )
        self._emit_term_lifecycle_batches(
            stage=stage,
            batches=batches,
            previous_rows=previous_rows,
        )
        evidence_key = (
            canonical_sha256(batches[0]["evidence"])
            if batches
            else existing_evidence_key
        )
        if evidence_key is not None:
            self._term_checked_evidence_keys.add(evidence_key)

    def _drain_term_work_journal(
        self,
        stage: StagePlan,
        *,
        projection_mode: str,
    ) -> None:
        if stage.stage_id not in {
            "b1_candidate_discovery",
            "b2_admission_translation",
            "auditor_morphology",
            "auditor_target_collision",
            "auditor_multi_target",
        }:
            return
        self._initialize_term_lifecycle_state()
        journal_ref = f"runtime/work_items/{stage.stage_id}.jsonl"
        entries = read_work_journal(self.root / journal_ref)
        for entry in entries:
            self._project_term_work_entry(
                stage=stage,
                journal_ref=journal_ref,
                entries=entries,
                entry=entry,
                projection_mode=projection_mode,
            )

    def _project_term_artifact(
        self,
        *,
        stage: StagePlan,
        artifact: Mapping[str, Any],
    ) -> None:
        if stage.stage_id not in {"candidate_index", "glossary_seal"}:
            return
        if (
            stage.stage_id == "candidate_index"
            and artifact["schema_version"] != "d2l_candidate_index_v2"
        ) or (
            stage.stage_id == "glossary_seal"
            and artifact["schema_version"]
            != "d2l_terminology_memory_delta_batch_v1"
        ):
            return
        self._initialize_term_lifecycle_state()
        events = self._component_events()
        created_event = next(
            (
                row
                for row in events
                if row["event_id"] == artifact["created_event_id"]
            ),
            None,
        )
        if created_event is None:
            raise ComponentRunnerError(
                "term lifecycle artifact creation event is missing"
            )
        existing_evidence = self._existing_term_evidence(
            evidence_ref=str(artifact["artifact_ref"]),
            evidence_sha256=str(artifact["sha256"]),
        )
        existing_evidence_key = (
            None
            if existing_evidence is None
            else canonical_sha256(existing_evidence)
        )
        if (
            existing_evidence_key is not None
            and existing_evidence_key in self._term_checked_evidence_keys
        ):
            return
        if existing_evidence is None:
            previous_rows = list(self._term_rows_by_id.values())
        else:
            evidence_key = existing_evidence_key
            assert evidence_key is not None
            previous_rows = self._term_prior_rows_by_evidence[evidence_key]
        artifact_path = _relative_path(
            self.root,
            artifact["relative_path"],
            "term lifecycle artifact relative_path",
        )
        stage_row = self._stage_row(stage.stage_id)
        batches = project_artifact_term_batches(
            artifact=artifact,
            artifact_value=_load_json(
                artifact_path,
                "term lifecycle artifact",
            ),
            created_event=created_event,
            previous_rows=previous_rows,
            completed=int(stage_row["progress"]["completed"]),
            total=stage_row["progress"]["total"],
            unit=str(stage_row["progress"]["unit"]),
            through_work_id=stage.work_id,
        )
        self._emit_term_lifecycle_batches(
            stage=stage,
            batches=batches,
            previous_rows=previous_rows,
        )
        evidence_key = (
            canonical_sha256(batches[0]["evidence"])
            if batches
            else existing_evidence_key
        )
        if evidence_key is not None:
            self._term_checked_evidence_keys.add(evidence_key)

    def _project_registered_stage_term_artifacts(
        self,
        stage: StagePlan,
    ) -> None:
        for artifact in sorted(
            (
                row
                for row in self.artifacts
                if row["producer_stage_id"] == stage.stage_id
            ),
            key=lambda row: row["created_event_id"],
        ):
            self._project_term_artifact(stage=stage, artifact=artifact)

    def _backfill_term_lifecycle(self) -> None:
        self._initialize_term_lifecycle_state()
        stages = {stage.stage_id: stage for stage in self.plan.stages}
        for stage_id in (
            "b1_candidate_discovery",
            "b2_admission_translation",
            "auditor_morphology",
            "auditor_target_collision",
            "auditor_multi_target",
        ):
            self._drain_term_work_journal(
                stages[stage_id],
                projection_mode="resume_backfill",
            )
        created_seq: dict[str, int] = {
            str(event["event_id"]): int(event["component_seq"])
            for event in self._component_events()
            if event["event"] == "artifact_created"
        }
        for artifact in sorted(
            self.artifacts,
            key=lambda row: created_seq.get(
                str(row["created_event_id"]),
                10**12,
            ),
        ):
            stage = stages[str(artifact["producer_stage_id"])]
            self._project_term_artifact(stage=stage, artifact=artifact)

    def _semantic_contract_sha256(self) -> str:
        plan = self.plan.canonical_mapping()
        stage_contracts = []
        for stage in plan["stages"]:
            stage_contracts.append(
                {
                    key: stage[key]
                    for key in (
                        "stage_id",
                        "producer",
                        "artifact_specs",
                        "total",
                        "unit",
                        "work_id",
                        "mode",
                        "receipt_ref",
                    )
                }
            )
        return canonical_sha256(
            {
                "pipeline_id": plan["pipeline_id"],
                "pipeline_version": plan["pipeline_version"],
                "source_binding": plan["source_binding"],
                "config_sha256": plan["config_sha256"],
                "selected_chapter_ids": plan["selected_chapter_ids"],
                "scoring_handoff_fragment_ref": plan[
                    "scoring_handoff_fragment_ref"
                ],
                "stage_contracts": stage_contracts,
            }
        )

    def _git_command(self, *args: str) -> subprocess.CompletedProcess:
        if self.repair_code_root is None:
            raise ComponentRunnerError("repair code root is unavailable")
        try:
            return subprocess.run(
                ["git", "-C", str(self.repair_code_root), *args],
                check=True,
                capture_output=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ComponentRunnerError(
                "cannot verify repair Git lineage"
            ) from exc

    def _runtime_code_revision(self) -> str:
        if self.repair_code_root is None:
            return self.plan.code_revision
        revision = (
            self._git_command("rev-parse", "HEAD")
            .stdout.decode("ascii")
            .strip()
            .lower()
        )
        if len(revision) != 40 or any(
            character not in "0123456789abcdef" for character in revision
        ):
            raise ComponentRunnerError("runtime Git revision is invalid")
        status = self._git_command(
            "status",
            "--porcelain",
            "--untracked-files=no",
        ).stdout
        if status.strip():
            raise ComponentRunnerError(
                "runtime Git tree has tracked changes; commit before Resume"
            )
        return revision

    def _preflight_runtime_revision(self) -> None:
        if self._revision_preflight_done:
            return
        observed_revision = self._runtime_code_revision()
        parent = self._latest_indexed_repair_anchor()
        if observed_revision == self.plan.code_revision:
            if parent is not None:
                raise ComponentRunnerError(
                    "runtime revision regressed behind the indexed repair chain"
                )
            if self.repair_reason is not None:
                raise ComponentRunnerError(
                    "repair reason was supplied without a code revision change"
                )
            self._effective_code_revision = observed_revision
            self._repair_delta = None
            self._revision_preflight_done = True
            return
        baseline_revision = self.plan.code_revision
        path_validator = validate_mechanical_repair_paths
        if parent is not None:
            if parent["sealed_code_revision"] != self.plan.code_revision:
                raise ComponentRunnerError(
                    "indexed repair chain does not bind the sealed revision"
                )
            baseline_revision = str(parent["effective_code_revision"])
            if observed_revision == baseline_revision:
                if self.repair_reason is not None:
                    raise ComponentRunnerError(
                        "repair reason was supplied without a new code revision"
                    )
                self._effective_code_revision = observed_revision
                self._repair_delta = {
                    "mode": "reuse",
                    "parent": parent,
                }
                self._revision_preflight_done = True
                return
            path_validator = validate_chain_repair_paths
        if not isinstance(self.repair_reason, str) or not self.repair_reason:
            raise ComponentRunnerError(
                "runtime code revision changed; explicit repair reason is required"
            )
        ancestor = subprocess.run(
            [
                "git",
                "-C",
                str(self.repair_code_root),
                "merge-base",
                "--is-ancestor",
                baseline_revision,
                observed_revision,
            ],
            capture_output=True,
            timeout=30,
        )
        if ancestor.returncode != 0:
            raise ComponentRunnerError(
                "repair revision must descend from the sealed baseline"
            )
        changed_raw = self._git_command(
            "diff",
            "--name-only",
            "-z",
            baseline_revision,
            observed_revision,
        ).stdout
        changed_paths = sorted(
            {
                value.decode("utf-8")
                for value in changed_raw.split(b"\0")
                if value
            }
        )
        if not changed_paths:
            raise ComponentRunnerError("repair Git delta is empty")
        try:
            changed_paths = path_validator(changed_paths)
        except D2LRepairResumeError as exc:
            raise ComponentRunnerError(str(exc)) from exc
        delta = self._git_command(
            "diff",
            "--binary",
            baseline_revision,
            observed_revision,
        ).stdout
        self._effective_code_revision = observed_revision
        self._repair_delta = {
            "mode": "chain" if parent is not None else "direct",
            "observed_revision": observed_revision,
            "baseline_revision": baseline_revision,
            "changed_paths": changed_paths,
            "git_delta_sha256": sha256(delta).hexdigest().upper(),
            "parent": parent,
        }
        self._revision_preflight_done = True

    def _latest_indexed_repair_anchor(self) -> dict[str, Any] | None:
        if not self.manifest_path.is_file() or not self.index_path.is_file():
            return None
        validate_translation_component_package(
            self.root,
            require_terminal=False,
        )
        manifest = validate_component_manifest(
            _load_json(self.manifest_path, "component manifest")
        )
        index = validate_artifact_index(
            _load_json(self.index_path, "artifact index"),
            manifest=manifest,
            artifact_root=self.root,
        )
        repairs = [
            dict(row)
            for row in index["artifacts"]
            if row["artifact_kind"] == "d2l_component_repair_receipt"
        ]
        if not repairs:
            return None
        latest_attempt = max(int(row["component_attempt_id"]) for row in repairs)
        latest = [
            row
            for row in repairs
            if int(row["component_attempt_id"]) == latest_attempt
        ]
        if len(latest) != 1:
            raise ComponentRunnerError("indexed repair chain fork detected")
        artifact = latest[0]
        if (
            artifact["availability"] != "available"
            or artifact["sha256_kind"] != "physical"
        ):
            raise ComponentRunnerError(
                "latest repair artifact is not physically available"
            )
        receipt_path = _relative_path(
            self.root,
            artifact["relative_path"],
            "repair artifact relative_path",
        )
        if file_sha256(receipt_path) != artifact["sha256"]:
            raise ComponentRunnerError("indexed repair receipt hash drift")
        try:
            receipt = validate_repair_receipt(
                _load_json(receipt_path, "indexed repair receipt")
            )
        except D2LRepairResumeError as exc:
            raise ComponentRunnerError(str(exc)) from exc
        if (
            artifact["schema_version"] != receipt["schema_version"]
            or int(artifact["component_attempt_id"])
            != int(receipt["next_component_attempt_id"])
            or receipt["workflow_run_id"] != self.plan.workflow_run_id
            or receipt["component_run_id"] != self.plan.component_run_id
            or receipt["semantic_contract_sha256"]
            != self._semantic_contract_sha256()
            or receipt["runner_plan_sha256"] != self.plan.plan_sha256
        ):
            raise ComponentRunnerError(
                "indexed repair receipt identity mismatch"
            )
        expected_metadata = {
            "repair_kind": receipt["repair_kind"],
            "baseline_code_revision": receipt["baseline_code_revision"],
            "effective_code_revision": receipt["effective_code_revision"],
        }
        if receipt["schema_version"] == CHAIN_SCHEMA_VERSION:
            expected_metadata["parent_repair_artifact_ref"] = receipt[
                "parent_repair_artifact_ref"
            ]
            expected_parents = [receipt["parent_repair_artifact_ref"]]
            sealed_revision = receipt["sealed_code_revision"]
        else:
            expected_parents = []
            sealed_revision = receipt["baseline_code_revision"]
        if (
            artifact["metadata"] != expected_metadata
            or artifact["parent_artifact_refs"] != expected_parents
        ):
            raise ComponentRunnerError(
                "indexed repair artifact metadata mismatch"
            )
        return {
            "artifact_ref": artifact["artifact_ref"],
            "receipt_ref": artifact["relative_path"],
            "receipt_sha256": artifact["sha256"],
            "receipt": receipt,
            "receipt_path": receipt_path,
            "sealed_code_revision": sealed_revision,
            "effective_code_revision": receipt["effective_code_revision"],
        }

    def _prepare_repair_receipt(
        self,
        *,
        current: Mapping[str, Any],
        resume: Mapping[str, Any],
    ) -> None:
        self._preflight_runtime_revision()
        repair_delta = self._repair_delta
        if repair_delta is None:
            return
        if repair_delta["mode"] == "reuse":
            parent = repair_delta["parent"]
            self._repair_receipt = dict(parent["receipt"])
            self._repair_receipt_path = Path(parent["receipt_path"])
            self._repair_receipt_register = False
            return
        previous_attempt = int(current["component_attempt_id"])
        common = {
            "workflow_run_id": str(current["workflow_run_id"]),
            "component_run_id": str(current["component_run_id"]),
            "previous_component_attempt_id": previous_attempt,
            "stage_id": str(resume["stage_id"]),
            "checkpoint_ref": str(resume["checkpoint_ref"]),
            "checkpoint_sha256": str(resume["checkpoint_sha256"]),
            "reason_code": self.repair_reason,
            "effective_code_revision": str(repair_delta["observed_revision"]),
            "semantic_contract_sha256": self._semantic_contract_sha256(),
            "runner_plan_sha256": self.plan.plan_sha256,
            "git_delta_sha256": str(repair_delta["git_delta_sha256"]),
            "changed_paths": list(repair_delta["changed_paths"]),
            "created_at": _timestamp(),
        }
        if repair_delta["mode"] == "chain":
            parent = repair_delta["parent"]
            receipt = build_chain_repair_receipt(
                **common,
                sealed_code_revision=self.plan.code_revision,
                baseline_code_revision=str(
                    repair_delta["baseline_revision"]
                ),
                parent_repair_artifact_ref=str(parent["artifact_ref"]),
                parent_repair_receipt_ref=str(parent["receipt_ref"]),
                parent_repair_receipt_sha256=str(parent["receipt_sha256"]),
                parent_effective_code_revision=str(
                    parent["effective_code_revision"]
                ),
            )
            relative_ref = (
                "runtime/repair_receipts/"
                f"repair_chain_a{previous_attempt + 1:04d}.json"
            )
        else:
            receipt = build_repair_receipt(
                **common,
                baseline_code_revision=self.plan.code_revision,
            )
            relative_ref = (
                "runtime/repair_receipts/"
                f"repair_a{previous_attempt + 1:04d}.json"
            )
        receipt_path = self.root / relative_ref
        if receipt_path.exists():
            if receipt["schema_version"] == CHAIN_SCHEMA_VERSION:
                raise ComponentRunnerError(
                    "unindexed chained repair receipt path collision"
                )
            if _load_json(receipt_path, "repair receipt") != receipt:
                raise ComponentRunnerError("repair receipt path already drifted")
        else:
            write_json(receipt_path, receipt)
        self._repair_receipt = receipt
        self._repair_receipt_path = receipt_path
        self._repair_receipt_register = True

    def run(self, *, resume: bool = False) -> dict[str, Any]:
        try:
            with D2LComponentWriterLease(self.root):
                if stage_writer_is_active(self.root):
                    raise ComponentRunnerError(
                        "stage writer lease is held by a surviving subprocess"
                    )
                return self._run_exclusive(resume=resume)
        except D2LComponentWriterLeaseError as exc:
            raise ComponentRunnerError(str(exc)) from exc

    def _run_exclusive(self, *, resume: bool) -> dict[str, Any]:
        if resume:
            if not self.root.is_dir():
                raise ComponentRunnerError("component root is missing during resume")
            current = _load_json(self.manifest_path, "component manifest")
            # Git lineage and the closed mechanical path policy must pass
            # before stale recovery can append a checkpoint or event.
            self._preflight_runtime_revision()
            if current.get("status") == "running":
                if not self.recover_stale:
                    raise ComponentRunnerError(
                        "running component requires explicit stale-attempt recovery"
                    )
                self._recover_stale_attempt()
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

    def _recover_stale_attempt(self) -> None:
        self.manifest = validate_component_manifest(
            _load_json(self.manifest_path, "component manifest")
        )
        if self.manifest["status"] != "running":
            raise ComponentRunnerError(
                "stale-attempt recovery requires a running component"
            )
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
        if any(
            self.manifest[key] != expected
            for key, expected in expected_immutable.items()
        ):
            raise ComponentRunnerError(
                "stale-attempt identity does not match the sealed plan"
            )
        stage_id = str(self.manifest["active_stage_id"])
        stage = next(
            (row for row in self.plan.stages if row.stage_id == stage_id),
            None,
        )
        if stage is None:
            raise ComponentRunnerError(
                "stale-attempt active stage is not recoverable"
            )
        stage_status = self._stage_row(stage_id)["status"]
        attempt_events = [
            row
            for row in self._component_events()
            if row["component_attempt_id"]
            == self.manifest["component_attempt_id"]
        ]
        pre_stage_resume_crash = (
            stage_status in {"pending", "paused"}
            and any(row["event"] == "run_resumed" for row in attempt_events)
            and not any(row["event"] == "stage_start" for row in attempt_events)
            and all(
                row["event"]
                in {"run_resumed", "artifact_created", "term_lifecycle"}
                for row in attempt_events
            )
        )
        if stage_status != "running" and not pre_stage_resume_crash:
            raise ComponentRunnerError(
                "stale-attempt active stage is not recoverable"
            )
        index = _load_json(self.index_path, "artifact index")
        self.artifacts = list(index.get("artifacts") or [])
        self._current_attempt = int(self.manifest["component_attempt_id"])
        try:
            journal_entries = read_observation_journal(
                self.observation_journal_path
            )
        except D2LStageReceiptError as exc:
            if "unterminated final row" not in str(exc):
                raise
            receipt_path, receipt = (
                self._quarantine_incomplete_observation_tail(
                    stage,
                    register=False,
                )
            )
            self._pending_journal_recovery = (
                stage,
                receipt_path,
                receipt,
            )
            journal_entries = read_observation_journal(
                self.observation_journal_path
            )
        validate_translation_component_package(
            self.root,
            require_terminal=False,
        )
        self._journal_cursor = len(journal_entries)
        self.writer = D2LTranslationComponentEventWriter(
            self.root / "events.jsonl",
            manifest=self.manifest,
            component_attempt_id=self._current_attempt,
            recover_existing_attempt=True,
        )
        self._pause(
            stage_id,
            "stale_process_recovered",
            project_term_lifecycle=False,
        )

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
        if checkpoint_state.get("work_journals", {}) != (
            self._work_journal_checkpoint_state()
        ):
            raise ComponentRunnerError("resume work journal lineage mismatch")
        self._prepare_repair_receipt(current=current, resume=resume)
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
                "reason_code": (
                    "resume_after_code_repair"
                    if self._repair_receipt is not None
                    else "resume_after_pause"
                ),
            },
        )
        if self._pending_journal_recovery is not None:
            recovery_stage, receipt_path, receipt = (
                self._pending_journal_recovery
            )
            self._register_recovery_receipt(
                stage=recovery_stage,
                receipt_path=receipt_path,
                receipt=receipt,
            )
            self._pending_journal_recovery = None
        self._register_repair_receipt(stage_id=str(resume["stage_id"]))
        self._quarantine_unpublished_stage_outputs(
            stage_id=str(resume["stage_id"]),
            previous_component_attempt_id=self._current_attempt - 1,
            paused_reason=str(resume["paused_reason"]),
        )
        # Project accepted historical terminology only after the Resume
        # boundary is durable and before any stage subprocess can run.
        self._backfill_term_lifecycle()

    def _register_repair_receipt(self, *, stage_id: str) -> None:
        if self._repair_receipt is None or self._repair_receipt_path is None:
            return
        if not self._repair_receipt_register:
            return
        chained = self._repair_receipt["schema_version"] == CHAIN_SCHEMA_VERSION
        artifact_ref = (
            f"art_component_repair_chain_a{self._current_attempt:04d}"
            if chained
            else f"art_component_repair_a{self._current_attempt:04d}"
        )
        if artifact_ref in {row["artifact_ref"] for row in self.artifacts}:
            raise ComponentRunnerError("repair receipt was already registered")
        parent_refs = (
            [self._repair_receipt["parent_repair_artifact_ref"]]
            if chained
            else []
        )
        metadata = {
            "repair_kind": self._repair_receipt["repair_kind"],
            "baseline_code_revision": self._repair_receipt[
                "baseline_code_revision"
            ],
            "effective_code_revision": self._repair_receipt[
                "effective_code_revision"
            ],
        }
        if chained:
            metadata["parent_repair_artifact_ref"] = self._repair_receipt[
                "parent_repair_artifact_ref"
            ]
        row = {
            "workflow_run_id": self.manifest["workflow_run_id"],
            "flow_kind": self.manifest["flow_kind"],
            "component_id": COMPONENT_ID,
            "component_run_id": self.manifest["component_run_id"],
            "component_attempt_id": self._current_attempt,
            "artifact_ref": artifact_ref,
            "artifact_kind": "d2l_component_repair_receipt",
            "schema_version": self._repair_receipt["schema_version"],
            "sha256": file_sha256(self._repair_receipt_path),
            "sha256_kind": "physical",
            "producer_stage_id": stage_id,
            "parent_artifact_refs": parent_refs,
            "created_event_id": self.writer.next_event_id,
            "relative_path": str(
                self._repair_receipt_path.relative_to(self.root)
            ).replace("\\", "/"),
            "availability": "available",
            "metadata": metadata,
        }
        self.artifacts.append(row)
        self._write_index()
        self.writer.emit(
            "artifact_created",
            stage_id=stage_id,
            agent="d2l_component_runner",
            payload={
                "artifact_ref": row["artifact_ref"],
                "artifact_kind": row["artifact_kind"],
                "schema_version": row["schema_version"],
                "sha256": row["sha256"],
                "sha256_kind": row["sha256_kind"],
                "parent_artifact_refs": parent_refs,
            },
        )

    def _quarantine_unpublished_stage_outputs(
        self,
        *,
        stage_id: str,
        previous_component_attempt_id: int,
        paused_reason: str,
    ) -> None:
        recoverable_prefixes = (
            "stage_process_",
            "stage_output_contract_failed",
            "observation_journal_",
            "transport_retry_exhausted",
            "stale_process_recovered",
        )
        if not paused_reason.startswith(recoverable_prefixes):
            return
        stage = next(
            (row for row in self.plan.stages if row.stage_id == stage_id),
            None,
        )
        if stage is None:
            raise ComponentRunnerError(
                "resume stage is absent from the sealed plan"
            )
        published_refs = {row["artifact_ref"] for row in self.artifacts}
        candidates: list[tuple[Mapping[str, Any], Path]] = []
        for spec in stage.artifact_specs:
            if spec["artifact_ref"] in published_refs:
                continue
            path = _relative_path(
                self.root,
                spec["relative_path"],
                f"{stage_id}.unpublished_output",
            )
            if path.is_file():
                candidates.append((spec, path))
        if not candidates:
            return

        recovery_root = (
            self.root
            / "runtime"
            / "unpublished_outputs"
            / f"a{previous_component_attempt_id:04d}"
            / stage_id
        )
        recovered: list[dict[str, Any]] = []
        for spec, source_path in candidates:
            relative_source = str(
                source_path.relative_to(self.root)
            ).replace("\\", "/")
            target_path = recovery_root / relative_source
            target_path.parent.mkdir(parents=True, exist_ok=True)
            source_sha = file_sha256(source_path)
            source_size = source_path.stat().st_size
            if target_path.exists():
                if (
                    file_sha256(target_path) != source_sha
                    or target_path.stat().st_size != source_size
                ):
                    raise ComponentRunnerError(
                        "unpublished output recovery target drift"
                    )
                source_path.unlink()
            else:
                source_path.replace(target_path)
            recovered.append(
                {
                    "artifact_ref": str(spec["artifact_ref"]),
                    "original_relative_path": relative_source,
                    "quarantined_relative_path": str(
                        target_path.relative_to(self.root)
                    ).replace("\\", "/"),
                    "sha256": source_sha,
                    "size": source_size,
                }
            )

        receipt_path = recovery_root / "receipt.json"
        receipt = {
            "schema_version": "d2l_unpublished_stage_output_recovery_v1",
            "workflow_run_id": self.manifest["workflow_run_id"],
            "component_run_id": self.manifest["component_run_id"],
            "previous_component_attempt_id": previous_component_attempt_id,
            "next_component_attempt_id": self._current_attempt,
            "stage_id": stage_id,
            "paused_reason": paused_reason,
            "recovered_outputs": recovered,
            "created_at": _timestamp(),
        }
        receipt["integrity"] = {
            "payload_sha256": canonical_sha256(receipt)
        }
        if receipt_path.exists():
            if _load_json(
                receipt_path,
                "unpublished output recovery receipt",
            ) != receipt:
                raise ComponentRunnerError(
                    "unpublished output recovery receipt drift"
                )
        else:
            write_json(receipt_path, receipt)
        self._register_unpublished_output_receipt(
            stage_id=stage_id,
            receipt_path=receipt_path,
            receipt=receipt,
        )

    def _register_unpublished_output_receipt(
        self,
        *,
        stage_id: str,
        receipt_path: Path,
        receipt: Mapping[str, Any],
    ) -> None:
        artifact_ref = (
            "art_unpublished_output_recovery_"
            f"a{self._current_attempt:04d}_{stage_id}"
        )
        if artifact_ref in {row["artifact_ref"] for row in self.artifacts}:
            raise ComponentRunnerError(
                "unpublished output recovery was already registered"
            )
        row = {
            "workflow_run_id": self.manifest["workflow_run_id"],
            "flow_kind": self.manifest["flow_kind"],
            "component_id": COMPONENT_ID,
            "component_run_id": self.manifest["component_run_id"],
            "component_attempt_id": self._current_attempt,
            "artifact_ref": artifact_ref,
            "artifact_kind": "d2l_unpublished_stage_output_recovery",
            "schema_version": str(receipt["schema_version"]),
            "sha256": file_sha256(receipt_path),
            "sha256_kind": "physical",
            "producer_stage_id": stage_id,
            "parent_artifact_refs": [],
            "created_event_id": self.writer.next_event_id,
            "relative_path": str(
                receipt_path.relative_to(self.root)
            ).replace("\\", "/"),
            "availability": "available",
            "metadata": {
                "paused_reason": str(receipt["paused_reason"]),
                "recovered_output_count": len(
                    receipt["recovered_outputs"]
                ),
            },
        }
        self.artifacts.append(row)
        self._write_index()
        self.writer.emit(
            "artifact_created",
            stage_id=stage_id,
            agent="d2l_component_runner",
            severity="warning",
            payload={
                "artifact_ref": row["artifact_ref"],
                "artifact_kind": row["artifact_kind"],
                "schema_version": row["schema_version"],
                "sha256": row["sha256"],
                "sha256_kind": row["sha256_kind"],
                "parent_artifact_refs": [],
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
            try:
                self._emit_stage_receipt(stage)
                self._register_stage_artifacts(stage)
            except Exception as exc:
                self.writer.emit(
                    "validation_failed",
                    stage_id=stage.stage_id,
                    agent="d2l_component_runner",
                    severity="error",
                    payload={
                        "validator_id": "d2l_stage_output_contract_v1",
                        "subject_ref": stage.stage_id,
                        "reason_codes": [
                            "stage_output_contract_failed",
                            type(exc).__name__,
                        ],
                        "retryable": True,
                    },
                )
                raise _PauseRun(
                    stage.stage_id,
                    "stage_output_contract_failed",
                ) from exc
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
            self._project_registered_stage_term_artifacts(stage)
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
        self._drain_term_work_journal(
            stage,
            projection_mode="live",
        )
        cwd = self.root if stage.cwd is None else Path(stage.cwd).resolve()
        started = time.monotonic()
        process_log_root = (
            self.root
            / "runtime"
            / "stage_process_logs"
            / f"attempt_{self._current_attempt:04d}"
        )
        process_log_root.mkdir(parents=True, exist_ok=True)
        stdout_path = process_log_root / f"{stage.stage_id}.stdout.log"
        stderr_path = process_log_root / f"{stage.stage_id}.stderr.log"
        if stdout_path.exists() or stderr_path.exists():
            raise ComponentRunnerError("stage process diagnostic path already exists")
        pause_was_present_at_start = bool(
            self.pause_file is not None and self.pause_file.is_file()
        )
        environment = None
        if self._effective_code_revision != self.plan.code_revision:
            if self._repair_receipt_path is None:
                raise ComponentRunnerError(
                    "effective repair revision lacks a repair receipt"
                )
            environment = dict(os.environ)
            environment["THESIS_D2L_EFFECTIVE_CODE_REVISION"] = (
                self._effective_code_revision
            )
            environment["THESIS_D2L_REPAIR_RECEIPT_REF"] = str(
                self._repair_receipt_path.relative_to(self.root)
            ).replace("\\", "/")
            environment["THESIS_D2L_REPAIR_RECEIPT_SHA256"] = file_sha256(
                self._repair_receipt_path
            )
        process: D2LGuardedStageProcess | None = None
        with stdout_path.open("xb") as stdout_handle, stderr_path.open(
            "xb"
        ) as stderr_handle:
            try:
                process = D2LGuardedStageProcess(
                    component_root=self.root,
                    stage_id=stage.stage_id,
                    command=list(stage.command),
                    cwd=cwd,
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    env=environment,
                )
            except (OSError, D2LStageProcessTreeError) as exc:
                self._emit_repairable_stage_failure(
                    stage,
                    reason_code="stage_process_launch_failed",
                    detail_code=type(exc).__name__,
                )
                raise _PauseRun(
                    stage.stage_id,
                    "stage_process_launch_failed",
                ) from exc
            try:
                while process.poll() is None:
                    self._drain_observation_journal(
                        stage, allow_incomplete_tail=True
                    )
                    self._drain_term_work_journal(
                        stage,
                        projection_mode="live",
                    )
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
                        self._drain_after_process(stage)
                        self.pause_file.unlink(missing_ok=True)
                        raise _PauseRun(stage.stage_id, "user_requested_pause")
                    if (
                        stage.timeout_seconds is not None
                        and time.monotonic() - started > stage.timeout_seconds
                    ):
                        process.kill()
                        process.wait(timeout=5)
                        self._drain_after_process(stage)
                        self._emit_repairable_stage_failure(
                            stage,
                            reason_code="stage_process_timeout",
                            detail_code="timeout",
                        )
                        raise _PauseRun(stage.stage_id, "stage_process_timeout")
                    time.sleep(0.05)
                self._drain_after_process(stage)
                if process.returncode == TRANSPORT_RETRY_EXHAUSTED_EXIT_CODE:
                    raise _PauseRun(
                        stage.stage_id, "transport_retry_exhausted"
                    )
                if process.returncode != 0:
                    self._emit_repairable_stage_failure(
                        stage,
                        reason_code="stage_process_exit_nonzero",
                        detail_code=f"exit_{process.returncode}",
                    )
                    raise _PauseRun(
                        stage.stage_id,
                        f"stage_process_exit_{process.returncode}",
                    )
            finally:
                process.close()

    def _drain_after_process(self, stage: StagePlan) -> None:
        try:
            self._drain_observation_journal(
                stage,
                allow_incomplete_tail=False,
            )
            self._drain_term_work_journal(
                stage,
                projection_mode="live",
            )
        except D2LStageReceiptError as exc:
            if "unterminated final row" not in str(exc):
                raise
            self._quarantine_incomplete_observation_tail(stage)
            self._drain_observation_journal(
                stage,
                allow_incomplete_tail=False,
            )
            self._drain_term_work_journal(
                stage,
                projection_mode="live",
            )
            self._emit_repairable_stage_failure(
                stage,
                reason_code="observation_journal_incomplete_tail",
                detail_code="unterminated_final_row",
            )
            raise _PauseRun(
                stage.stage_id,
                "observation_journal_incomplete_tail",
            ) from exc

    def _quarantine_incomplete_observation_tail(
        self,
        stage: StagePlan,
        *,
        register: bool = True,
    ) -> tuple[Path, dict[str, Any]]:
        path = self.observation_journal_path
        raw = path.read_bytes()
        if not raw or raw.endswith((b"\n", b"\r")):
            raise ComponentRunnerError(
                "observation journal recovery found no incomplete tail"
            )
        prefix_end = raw.rfind(b"\n") + 1
        prefix = raw[:prefix_end]
        tail = raw[prefix_end:]
        if not tail:
            raise ComponentRunnerError(
                "observation journal recovery tail is empty"
            )
        read_observation_journal(path, allow_incomplete_tail=True)

        recovery_root = (
            self.root
            / "runtime"
            / "journal_recovery"
            / f"a{self._current_attempt:04d}"
        )
        recovery_root.mkdir(parents=True, exist_ok=True)
        tail_path = recovery_root / f"{stage.stage_id}.tail.bin"
        receipt_path = recovery_root / f"{stage.stage_id}.receipt.json"
        if tail_path.exists() and tail_path.read_bytes() != tail:
            raise ComponentRunnerError(
                "observation journal recovery tail path drift"
            )
        if not tail_path.exists():
            tail_path.write_bytes(tail)
        receipt = {
            "schema_version": "d2l_observation_journal_recovery_v1",
            "workflow_run_id": self.manifest["workflow_run_id"],
            "component_run_id": self.manifest["component_run_id"],
            "component_attempt_id": self._current_attempt,
            "stage_id": stage.stage_id,
            "work_id": stage.work_id,
            "reason_code": "unterminated_final_row",
            "journal_ref": str(path.relative_to(self.root)).replace("\\", "/"),
            "original_journal_sha256": sha256(raw).hexdigest().upper(),
            "retained_prefix_sha256": sha256(prefix).hexdigest().upper(),
            "quarantined_tail_ref": str(
                tail_path.relative_to(self.root)
            ).replace("\\", "/"),
            "quarantined_tail_sha256": sha256(tail).hexdigest().upper(),
            "quarantined_tail_size": len(tail),
            "created_at": _timestamp(),
        }
        receipt["integrity"] = {
            "payload_sha256": canonical_sha256(receipt)
        }
        if receipt_path.exists():
            if _load_json(
                receipt_path,
                "observation journal recovery receipt",
            ) != receipt:
                raise ComponentRunnerError(
                    "observation journal recovery receipt drift"
                )
        else:
            write_json(receipt_path, receipt)

        replacement = path.with_suffix(path.suffix + ".recovery.tmp")
        replacement.write_bytes(prefix)
        os.replace(replacement, path)
        if path.read_bytes() != prefix:
            raise ComponentRunnerError(
                "observation journal recovery replacement drift"
            )
        if register:
            self._register_recovery_receipt(
                stage=stage,
                receipt_path=receipt_path,
                receipt=receipt,
            )
        return receipt_path, receipt

    def _register_recovery_receipt(
        self,
        *,
        stage: StagePlan,
        receipt_path: Path,
        receipt: Mapping[str, Any],
    ) -> None:
        artifact_ref = (
            "art_observation_recovery_"
            f"a{self._current_attempt:04d}_{stage.stage_id}"
        )
        if artifact_ref in {row["artifact_ref"] for row in self.artifacts}:
            raise ComponentRunnerError(
                "observation recovery receipt was already registered"
            )
        row = {
            "workflow_run_id": self.manifest["workflow_run_id"],
            "flow_kind": self.manifest["flow_kind"],
            "component_id": COMPONENT_ID,
            "component_run_id": self.manifest["component_run_id"],
            "component_attempt_id": self._current_attempt,
            "artifact_ref": artifact_ref,
            "artifact_kind": "d2l_observation_journal_recovery",
            "schema_version": str(receipt["schema_version"]),
            "sha256": file_sha256(receipt_path),
            "sha256_kind": "physical",
            "producer_stage_id": stage.stage_id,
            "parent_artifact_refs": [],
            "created_event_id": self.writer.next_event_id,
            "relative_path": str(
                receipt_path.relative_to(self.root)
            ).replace("\\", "/"),
            "availability": "available",
            "metadata": {
                "reason_code": str(receipt["reason_code"]),
                "quarantined_tail_sha256": str(
                    receipt["quarantined_tail_sha256"]
                ),
                "quarantined_tail_size": int(
                    receipt["quarantined_tail_size"]
                ),
            },
        }
        self.artifacts.append(row)
        self._write_index()
        self.writer.emit(
            "artifact_created",
            stage_id=stage.stage_id,
            agent="d2l_component_runner",
            severity="warning",
            payload={
                "artifact_ref": row["artifact_ref"],
                "artifact_kind": row["artifact_kind"],
                "schema_version": row["schema_version"],
                "sha256": row["sha256"],
                "sha256_kind": row["sha256_kind"],
                "parent_artifact_refs": [],
            },
        )

    def _emit_repairable_stage_failure(
        self,
        stage: StagePlan,
        *,
        reason_code: str,
        detail_code: str,
    ) -> None:
        self.writer.emit(
            "validation_failed",
            stage_id=stage.stage_id,
            agent="d2l_component_runner",
            severity="error",
            payload={
                "validator_id": "d2l_stage_process_v1",
                "subject_ref": stage.stage_id,
                "reason_codes": [reason_code, detail_code],
                "retryable": True,
            },
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
        pending: list[tuple[str, Path, dict[str, Any], list[str]]] = []
        existing_refs = {row["artifact_ref"] for row in self.artifacts}
        for spec in stage.artifact_specs:
            ref = _nonempty_string(spec["artifact_ref"], "artifact_ref")
            if ref in existing_refs:
                raise ComponentRunnerError(f"artifact_ref was already published: {ref}")
            path = _relative_path(self.root, spec["relative_path"], f"{ref}.relative_path")
            if not path.is_file():
                raise ComponentRunnerError(f"declared artifact is missing: {spec['relative_path']}")
            parent_refs = spec["parent_artifact_refs"]
            if not isinstance(parent_refs, list) or any(
                not isinstance(item, str) or not item for item in parent_refs
            ):
                raise ComponentRunnerError(f"{ref}.parent_artifact_refs is invalid")
            pending.append((ref, path, dict(spec), list(parent_refs)))

        for ref, path, spec, parent_refs in pending:
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
                "parent_artifact_refs": parent_refs,
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

    def _pause(
        self,
        stage_id: str,
        reason: str,
        *,
        project_term_lifecycle: bool = True,
    ) -> None:
        stage = next(item for item in self.plan.stages if item.stage_id == stage_id)
        if self.observation_journal_path.is_file():
            self._drain_observation_journal(
                stage,
                allow_incomplete_tail=False,
            )
            if project_term_lifecycle:
                self._drain_term_work_journal(
                    stage,
                    projection_mode="live",
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
            "work_journals": self._work_journal_checkpoint_state(),
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
    repair_code_root: str | Path | None = None,
    repair_reason: str | None = None,
    recover_stale: bool = False,
) -> dict[str, Any]:
    plan = ComponentPlan.from_mapping(_load_json(Path(plan_path), "runner plan"))
    return D2LTranslationComponentRunner(
        plan,
        root,
        stop_after_stage=stop_after_stage,
        pause_file=pause_file,
        repair_code_root=repair_code_root,
        repair_reason=repair_reason,
        recover_stale=recover_stale,
    ).run(resume=resume)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a D2L Translation component plan")
    parser.add_argument("--plan", required=True)
    parser.add_argument("--component-root", required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--stop-after-stage")
    parser.add_argument("--pause-file")
    parser.add_argument("--code-root")
    parser.add_argument("--repair-reason")
    parser.add_argument("--recover-stale", action="store_true")
    args = parser.parse_args()
    result = run_from_plan_file(
        args.plan,
        args.component_root,
        resume=args.resume,
        stop_after_stage=args.stop_after_stage,
        pause_file=args.pause_file,
        repair_code_root=args.code_root,
        repair_reason=args.repair_reason,
        recover_stale=args.recover_stale,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
