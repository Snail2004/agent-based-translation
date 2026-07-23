from __future__ import annotations

import copy
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence
import uuid

from pipeline.eval.contracts_v1 import (
    CanonicalPolicy,
    ContractValidationError,
    canonicalize,
    require_commit,
    require_enum,
    require_exact_keys,
    require_int,
    require_list,
    require_mapping,
    require_nullable_string,
    require_rfc3339,
    require_sha256,
    require_string,
    seal_payload,
    verify_payload_hash,
)
from pipeline.eval.evaluation_component_usage_v1 import (
    EvaluationComponentUsageTrackerV1,
    validate_evaluation_component_usage_snapshot_chain_v1,
    validate_evaluation_component_usage_snapshot_v1,
)
from pipeline.eval.evaluation_workflow_settings_v1 import (
    EvaluationWorkflowSettingsAuthorityV1,
    validate_evaluation_workflow_settings_v1,
)
from pipeline.eval.workflow_component_v1 import (
    SCHEMA_VERSION,
    build_evaluation_artifact_index_v1,
    build_evaluation_component_event_v1,
    build_evaluation_component_manifest_v1,
    build_scoring_receipt_v1,
    validate_evaluation_artifact_index_v1,
    validate_evaluation_component_event_v1,
    validate_evaluation_component_manifest_v1,
    validate_evaluation_component_stream_v1,
    validate_scoring_handoff_v1,
    validate_scoring_receipt_v1,
    validate_typed_artifact_binding_v1,
)
from pipeline.llm_backend import (
    SharedLlmAttemptLedger,
    validate_llm_run_records,
    validate_resolved_llm_run_seal,
)


__all__ = [
    "EvaluationWorkflowRunContextV1",
    "EvaluationWorkflowComponentWriterV1",
    "benchmark_workflow_stages_v1",
    "validate_evaluation_workflow_component_package_v1",
]


CHECKPOINT_SCHEMA_ID = "EvaluationWorkflowCheckpointV1"
RESUME_INTENT_SCHEMA_ID = "EvaluationWorkflowResumeIntentV1"
_SCORING_HANDOFF_ARTIFACT_REF = "handoffs/scoring_handoff.json"
_CHECKPOINT_HASH_PATH = ("integrity", "checkpoint_sha256")
_CHECKPOINT_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(),
    semantic_sequence_paths=frozenset({("completed_stage_ids",)}),
)


@dataclass(frozen=True, slots=True)
class EvaluationWorkflowRunContextV1:
    workflow_run_id: str
    component_run_id: str
    scoring_handoff: Mapping[str, Any]
    scoring_handoff_artifact_ref: str
    evaluation_profile: Mapping[str, Any]
    workflow_settings: Mapping[str, Any]
    workflow_settings_authority: EvaluationWorkflowSettingsAuthorityV1


def benchmark_workflow_stages_v1(chapter_ids: Sequence[str]) -> tuple[dict[str, Any], ...]:
    stages: list[dict[str, Any]] = [
        {"stage_id": "preflight", "ordinal": 0, "agent": "evaluation_preflight"}
    ]
    for chapter_id in chapter_ids:
        normalized = require_string(chapter_id, path="$.chapter_ids[*]")
        stages.append(
            {
                "stage_id": f"chapter_{normalized}",
                "ordinal": len(stages),
                "agent": "evaluation_chapter_runner",
            }
        )
    stages.append(
        {
            "stage_id": "aggregation",
            "ordinal": len(stages),
            "agent": "evaluation_aggregator",
        }
    )
    return tuple(stages)


class EvaluationWorkflowComponentWriterV1:
    def __init__(
        self,
        root: Path,
        context: EvaluationWorkflowRunContextV1,
        *,
        generated_at: str,
        producer_code_commit: str,
        stages: Sequence[Mapping[str, Any]],
        allow_create: bool,
    ) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.generated_at = require_rfc3339(generated_at, path="$.generated_at")
        self.producer_code_commit = require_commit(
            producer_code_commit, path="$.producer_code_commit"
        )
        self.handoff = validate_scoring_handoff_v1(context.scoring_handoff)
        self.workflow_run_id = require_string(
            context.workflow_run_id, path="$.workflow_run_id"
        )
        if self.workflow_run_id != self.handoff["workflow_run_id"]:
            raise ContractValidationError(
                "workflow_binding",
                "$.workflow_run_id",
                "workflow context and scoring handoff disagree",
            )
        self.component_run_id = require_string(
            context.component_run_id, path="$.component_run_id"
        )
        if context.scoring_handoff_artifact_ref != _SCORING_HANDOFF_ARTIFACT_REF:
            raise ContractValidationError(
                "handoff_binding",
                "$.scoring_handoff_artifact_ref",
                "Evaluation requires the parent-owned scoring handoff reference",
            )
        self.handoff_binding = {
            "artifact_ref": _SCORING_HANDOFF_ARTIFACT_REF,
            "artifact_kind": "scoring_handoff_v1",
            "schema_version": SCHEMA_VERSION,
            "sha256": self.handoff["integrity"]["handoff_sha256"],
            "sha256_kind": "canonical:ScoringHandoffV1@1.0.0",
        }
        self.handoff_binding = validate_typed_artifact_binding_v1(
            self.handoff_binding, path="$.scoring_handoff"
        )
        self.evaluation_profile = validate_typed_artifact_binding_v1(
            context.evaluation_profile, path="$.evaluation_profile"
        )
        self.workflow_settings_authority = context.workflow_settings_authority
        self.workflow_settings = validate_evaluation_workflow_settings_v1(
            context.workflow_settings,
            authority=self.workflow_settings_authority,
            scoring_handoff=self.handoff,
        )
        if self.workflow_settings["evaluation_profile_ref"] != self.evaluation_profile:
            raise ContractValidationError(
                "settings_profile_binding",
                "$.workflow_settings.evaluation_profile_ref",
                "workflow settings and Evaluation profile disagree",
            )
        self.workflow_settings_binding = validate_typed_artifact_binding_v1(
            {
                "artifact_ref": "workflow_settings.json",
                "artifact_kind": "evaluation_workflow_settings_v1",
                "schema_version": self.workflow_settings["schema_version"],
                "sha256": self.workflow_settings["settings_sha256"],
                "sha256_kind": "canonical:EvaluationWorkflowSettingsV1@1.0.0",
            },
            path="$.workflow_settings",
        )
        self.stages = tuple(copy.deepcopy(list(stages)))
        self.stage_ids = tuple(
            require_string(stage["stage_id"], path="$.stages[*].stage_id")
            for stage in self.stages
        )
        self.workflow_settings_path = self.root / "workflow_settings.json"
        self.manifest_path = self.root / "component_manifest.json"
        self.manifest_revisions_root = self.root / "manifest_revisions"
        self.event_records_root = self.root / "event_records"
        self.events_path = self.root / "events.jsonl"
        self.artifact_index_path = self.root / "artifact_index.json"
        self.receipt_path = self.root / "scoring_receipt.json"
        self.checkpoints_root = self.root / "checkpoints"
        self.usage_snapshots_root = self.root / "usage_snapshots"
        self.resume_intent_path = self.root / ".resume_intent.json"
        self._events: list[dict[str, Any]] = []
        self._manifest_revisions: list[dict[str, Any]] = []
        self._artifact_rows: list[dict[str, Any]] = []
        self._usage_tracker = EvaluationComponentUsageTrackerV1(
            workflow_run_id=self.workflow_run_id,
            component_run_id=self.component_run_id,
            stage_ids=self.stage_ids,
        )
        self.created_new = False
        self.recovered_resume = False

        if self.manifest_path.exists():
            self.recovered_resume = self._recover_resume_intent()
            self._load_existing()
            return
        component_paths = (
            self.events_path,
            self.artifact_index_path,
            self.receipt_path,
            self.workflow_settings_path,
            self.manifest_revisions_root,
            self.event_records_root,
        )
        if any(path.exists() for path in component_paths):
            raise ContractValidationError(
                "component_partial",
                str(self.root),
                "workflow component files exist without a current manifest",
            )
        if not allow_create:
            raise ContractValidationError(
                "replay_history_missing",
                str(self.root),
                "cannot retrofit replay records onto an already-started benchmark",
            )
        self.created_new = True
        _write_immutable_json(self.workflow_settings_path, self.workflow_settings)
        self.manifest = self._build_manifest(
            attempt_index=1,
            revision=1,
            previous_manifest_sha256=None,
        )
        self._persist_manifest_revision(self.manifest)
        receipt = build_scoring_receipt_v1(
            self.handoff,
            handoff_artifact_ref=self.handoff_binding["artifact_ref"],
            evaluation_component_run_id=self.component_run_id,
            evaluation_component_attempt_id="evalcomp_attempt_0001",
            accepted_at=self.generated_at,
            producer_code_commit=self.producer_code_commit,
            status="accepted",
        )
        _write_immutable_json(self.receipt_path, receipt)
        started = self.append_event(
            "component_started",
            stage_id="__component__",
            agent="runner",
            severity="info",
            payload={"stage_count": len(self.stages)},
        )
        self.add_artifact(
            "workflow_settings.json",
            artifact_kind="evaluation_workflow_settings_v1",
            schema_version=self.workflow_settings["schema_version"],
            stage_id="preflight",
            created_by_event_id=started["event_id"],
            parent_artifact_refs=(),
        )
        self.add_artifact(
            "scoring_receipt.json",
            artifact_kind="scoring_receipt_v1",
            schema_version=SCHEMA_VERSION,
            stage_id="preflight",
            created_by_event_id=started["event_id"],
            parent_artifact_refs=(),
        )

    @property
    def component_attempt_index(self) -> int:
        return self.manifest["component_attempt_index"]

    @property
    def component_attempt_id(self) -> str:
        return self.manifest["component_attempt_id"]

    @property
    def events(self) -> tuple[dict[str, Any], ...]:
        return tuple(copy.deepcopy(self._events))

    @property
    def terminal_event(self) -> str | None:
        if not self._events:
            return None
        event = self._events[-1]["event"]
        return event if event in {"component_done", "component_failed"} else None

    @property
    def is_halted(self) -> bool:
        return bool(self._events and self._events[-1]["event"] == "component_halted")

    def stage_state(self, stage_id: str) -> str:
        states = self._stage_states()
        if stage_id not in states:
            raise ContractValidationError(
                "stage_binding", "$.stage_id", f"unknown Evaluation stage: {stage_id}"
            )
        return states[stage_id]

    def start_or_resume(self) -> bool:
        if self.terminal_event is not None:
            return False
        if not self.is_halted:
            return False
        checkpoint = self._latest_checkpoint_binding()
        previous_attempt_id = self.component_attempt_id
        previous_manifest_sha256 = self.manifest["integrity"]["manifest_sha256"]
        next_attempt = self.component_attempt_index + 1
        next_manifest = self._build_manifest(
            attempt_index=next_attempt,
            revision=self.manifest["manifest_revision"] + 1,
            previous_manifest_sha256=previous_manifest_sha256,
        )
        resume_event = build_evaluation_component_event_v1(
            next_manifest,
            component_seq=len(self._events) + 1,
            component_attempt_id=next_manifest["component_attempt_id"],
            component_attempt_index=next_manifest["component_attempt_index"],
            ts=self.generated_at,
            stage_id="__component__",
            agent="runner",
            event="component_resumed",
            severity="info",
            payload={
                "resumed_from_attempt_id": previous_attempt_id,
                "checkpoint": checkpoint,
            },
            previous_event_sha256=self._events[-1]["integrity"]["event_sha256"],
        )
        next_artifact_index = build_evaluation_artifact_index_v1(
            next_manifest,
            generated_at=self.generated_at,
            artifacts=self._artifact_rows,
            producer_code_commit=self.producer_code_commit,
        )
        intent = _build_resume_intent(
            workflow_run_id=self.workflow_run_id,
            component_run_id=self.component_run_id,
            previous_manifest_sha256=previous_manifest_sha256,
            previous_event_sha256=self._events[-1]["integrity"]["event_sha256"],
            next_manifest=next_manifest,
            resume_event=resume_event,
            next_artifact_index=next_artifact_index,
        )
        _write_json_atomic(self.resume_intent_path, intent)
        self.recovered_resume = self._recover_resume_intent()
        self._load_existing()
        return True

    def _recover_resume_intent(self) -> bool:
        if not self.resume_intent_path.exists():
            return False
        intent = _validate_resume_intent(_load_json(self.resume_intent_path))
        if intent["workflow_run_id"] != self.workflow_run_id:
            raise ContractValidationError(
                "resume_intent_binding", str(self.resume_intent_path), "foreign workflow"
            )
        if intent["component_run_id"] != self.component_run_id:
            raise ContractValidationError(
                "resume_intent_binding", str(self.resume_intent_path), "foreign component"
            )
        current_manifest = validate_evaluation_component_manifest_v1(
            _load_json(self.manifest_path)
        )
        next_manifest = intent["next_manifest"]
        current_hash = current_manifest["integrity"]["manifest_sha256"]
        next_hash = next_manifest["integrity"]["manifest_sha256"]
        if current_hash not in {intent["previous_manifest_sha256"], next_hash}:
            raise ContractValidationError(
                "resume_intent_lineage",
                str(self.manifest_path),
                "current manifest is outside the pending Resume transaction",
            )
        if next_manifest["previous_manifest_sha256"] != intent["previous_manifest_sha256"]:
            raise ContractValidationError(
                "resume_intent_lineage",
                str(self.resume_intent_path),
                "next manifest does not extend the sealed previous manifest",
            )

        event_paths = sorted(self.event_records_root.glob("*.json"))
        events = [_load_json(path) for path in event_paths]
        resume_event = intent["resume_event"]
        expected_sequence = resume_event["component_seq"]
        if len(events) not in {expected_sequence - 1, expected_sequence}:
            raise ContractValidationError(
                "resume_intent_event",
                str(self.event_records_root),
                "event stream advanced outside the pending Resume transaction",
            )
        if not events or events[expected_sequence - 2]["integrity"]["event_sha256"] != intent[
            "previous_event_sha256"
        ]:
            raise ContractValidationError(
                "resume_intent_event",
                str(self.event_records_root),
                "Resume predecessor event drift",
            )
        if len(events) == expected_sequence and events[-1] != resume_event:
            raise ContractValidationError(
                "resume_intent_event",
                str(self.event_records_root),
                "persisted Resume event differs from the sealed intent",
            )

        revision_path = self.manifest_revisions_root / (
            f"{next_manifest['manifest_revision']:04d}_{next_hash}.json"
        )
        _write_immutable_json(revision_path, next_manifest)
        event_path = self.event_records_root / f"{expected_sequence:08d}.json"
        _write_immutable_json(event_path, resume_event)
        committed_events = [*events[: expected_sequence - 1], resume_event]
        _write_bytes_atomic(
            self.events_path,
            b"".join(_json_bytes(event) for event in committed_events),
        )
        _write_json_atomic(self.artifact_index_path, intent["next_artifact_index"])
        _write_json_atomic(self.manifest_path, next_manifest)
        self.resume_intent_path.unlink()
        return True

    def append_event(
        self,
        event: str,
        *,
        stage_id: str,
        agent: str,
        severity: str,
        payload: Mapping[str, Any],
        detail: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        previous = (
            self._events[-1]["integrity"]["event_sha256"] if self._events else None
        )
        row = build_evaluation_component_event_v1(
            self.manifest,
            component_seq=len(self._events) + 1,
            component_attempt_id=self.component_attempt_id,
            component_attempt_index=self.component_attempt_index,
            ts=self.generated_at,
            stage_id=stage_id,
            agent=agent,
            event=event,
            severity=severity,
            payload=payload,
            previous_event_sha256=previous,
            detail=detail,
        )
        _write_immutable_json(
            self.event_records_root / f"{row['component_seq']:08d}.json", row
        )
        self._events.append(row)
        self._persist_event_projection()
        self._validate_stream()
        return copy.deepcopy(row)

    def start_stage(self, stage_id: str, *, work_total: int, work_unit: str) -> None:
        state = self.stage_state(stage_id)
        if state == "succeeded":
            return
        if state not in {"pending", "halted"}:
            raise ContractValidationError(
                "stage_state", "$.stage_id", f"stage {stage_id} is {state}"
            )
        self.append_event(
            "stage_start",
            stage_id=stage_id,
            agent=self._stage_agent(stage_id),
            severity="info",
            payload={"work_total": work_total, "work_unit": work_unit},
        )

    def progress(
        self,
        stage_id: str,
        *,
        completed: int,
        total: int,
        unit: str,
        current_work_id: str | None,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        self.append_event(
            "progress",
            stage_id=stage_id,
            agent=self._stage_agent(stage_id),
            severity="info",
            payload={
                "completed": completed,
                "total": total,
                "unit": unit,
                "current_work_id": current_work_id,
            },
            detail=detail,
        )

    def validation_passed(self, stage_id: str, *, validator_id: str) -> dict[str, Any]:
        return self.append_event(
            "validation_passed",
            stage_id=stage_id,
            agent=self._stage_agent(stage_id),
            severity="info",
            payload={"validator_id": validator_id},
        )

    def validation_failed(
        self, stage_id: str, *, validator_id: str, reason_code: str
    ) -> dict[str, Any]:
        return self.append_event(
            "validation_failed",
            stage_id=stage_id,
            agent=self._stage_agent(stage_id),
            severity="error",
            payload={"validator_id": validator_id, "reason_code": reason_code},
        )

    def complete_stage(self, stage_id: str, *, outcome: str = "succeeded") -> None:
        self.append_event(
            "stage_done",
            stage_id=stage_id,
            agent=self._stage_agent(stage_id),
            severity="info" if outcome in {"succeeded", "skipped"} else "warning",
            payload={"outcome": outcome},
        )

    def persist_checkpoint(
        self,
        *,
        stage_id: str,
        work_id: str | None,
        benchmark_status: Mapping[str, Any],
        chapter_checkpoint_ref: str | None = None,
    ) -> dict[str, Any]:
        status = require_mapping(benchmark_status, path="$.benchmark_status")
        chapter_binding = None
        parents = ["scoring_receipt.json"]
        if chapter_checkpoint_ref is not None:
            chapter_binding = self.file_binding(
                chapter_checkpoint_ref,
                artifact_kind="benchmark_chapter_checkpoint_v1",
                schema_version=SCHEMA_VERSION,
            )
            parents.append(chapter_checkpoint_ref)
        checkpoint = _build_checkpoint(
            manifest=self.manifest,
            stage_id=stage_id,
            work_id=work_id,
            benchmark_status=status,
            chapter_checkpoint=chapter_binding,
            completed_stage_ids=tuple(
                stage["stage_id"]
                for stage in self.stages
                if self.stage_state(stage["stage_id"]) == "succeeded"
            ),
            created_at=self.generated_at,
            producer_code_commit=self.producer_code_commit,
        )
        canonical_hash = checkpoint["integrity"]["checkpoint_sha256"]
        relative_path = f"checkpoints/{canonical_hash}.json"
        _write_immutable_json(self.root / relative_path, checkpoint)
        binding = self.file_binding(
            relative_path,
            artifact_kind="evaluation_workflow_checkpoint_v1",
            schema_version=SCHEMA_VERSION,
        )
        event = self.append_event(
            "checkpoint",
            stage_id=stage_id,
            agent=self._stage_agent(stage_id),
            severity="info",
            payload={"checkpoint": binding, "work_id": work_id},
        )
        self.add_artifact(
            relative_path,
            artifact_kind="evaluation_workflow_checkpoint_v1",
            schema_version=SCHEMA_VERSION,
            stage_id=stage_id,
            created_by_event_id=event["event_id"],
            parent_artifact_refs=parents,
        )
        return binding

    def halt(self, *, reason_code: str) -> None:
        self.append_event(
            "component_halted",
            stage_id="__component__",
            agent="runner",
            severity="warning",
            payload={"reason_code": reason_code, "resume_available": True},
        )

    def done(self) -> None:
        self._finalize_usage(stage_id="aggregation")
        self.append_event(
            "component_done",
            stage_id="__component__",
            agent="runner",
            severity="info",
            payload={"outcome": "succeeded"},
        )

    def failed(self, *, reason_code: str) -> None:
        self._finalize_usage(stage_id=self._latest_non_pending_stage_id())
        self.append_event(
            "component_failed",
            stage_id="__component__",
            agent="runner",
            severity="error",
            payload={"outcome": "failed", "reason_code": reason_code},
        )

    def file_binding(
        self, relative_path: str, *, artifact_kind: str, schema_version: str
    ) -> dict[str, str]:
        relative = validate_typed_artifact_binding_v1(
            {
                "artifact_ref": relative_path,
                "artifact_kind": artifact_kind,
                "schema_version": schema_version,
                "sha256": "0" * 64,
                "sha256_kind": "physical",
            },
            path="$.artifact",
        )["artifact_ref"]
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise ContractValidationError(
                "artifact_path", relative_path, "artifact is missing or outside component root"
            )
        return {
            "artifact_ref": relative,
            "artifact_kind": require_string(artifact_kind, path="$.artifact_kind"),
            "schema_version": require_string(schema_version, path="$.schema_version"),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "sha256_kind": "physical",
        }

    def add_artifact(
        self,
        relative_path: str,
        *,
        artifact_kind: str,
        schema_version: str,
        stage_id: str,
        created_by_event_id: str,
        parent_artifact_refs: Sequence[str],
    ) -> dict[str, Any]:
        binding = self.file_binding(
            relative_path,
            artifact_kind=artifact_kind,
            schema_version=schema_version,
        )
        candidate = {
            "artifact": binding,
            "stage_id": stage_id,
            "created_by_event_id": created_by_event_id,
            "parent_artifact_refs": list(parent_artifact_refs),
        }
        for row in self._artifact_rows:
            if row["artifact"]["artifact_ref"] == relative_path:
                if row["artifact"] != binding or row["parent_artifact_refs"] != list(
                    parent_artifact_refs
                ):
                    raise ContractValidationError(
                        "artifact_conflict", relative_path, "artifact binding changed on Resume"
                    )
                return copy.deepcopy(row)
        self._artifact_rows.append(candidate)
        self._persist_artifact_index()
        return copy.deepcopy(candidate)

    def sync_usage_from_ledger(
        self,
        ledger: SharedLlmAttemptLedger,
        *,
        stage_id: str,
        current_work_id: str | None,
        execution_binding: Mapping[str, Any],
    ) -> tuple[dict[str, Any], ...]:
        """Project newly accepted shared-ledger facts into immutable Console snapshots."""

        if self.terminal_event is not None:
            raise ContractValidationError(
                "usage_terminal",
                str(self.events_path),
                "cannot append usage after a terminal component event",
            )
        if self.stage_state(stage_id) != "running":
            raise ContractValidationError(
                "usage_stage_state",
                "$.stage_id",
                "usage may be projected only while its Evaluation stage is running",
            )
        binding = require_mapping(execution_binding, path="$.execution_binding")
        require_exact_keys(
            binding,
            required={
                "evaluation_logical_run_id",
                "evaluation_attempt_run_id",
                "evaluation_profile_id",
                "evaluation_profile_sha256",
            },
            path="$.execution_binding",
        )
        run_id = require_string(
            binding["evaluation_logical_run_id"],
            path="$.execution_binding.evaluation_logical_run_id",
        )
        attempt_run_id = require_string(
            binding["evaluation_attempt_run_id"],
            path="$.execution_binding.evaluation_attempt_run_id",
        )
        profile_id = require_string(
            binding["evaluation_profile_id"],
            path="$.execution_binding.evaluation_profile_id",
        )
        profile_sha256 = require_sha256(
            binding["evaluation_profile_sha256"],
            path="$.execution_binding.evaluation_profile_sha256",
        )
        if profile_sha256 != self.evaluation_profile["sha256"]:
            raise ContractValidationError(
                "usage_profile_binding",
                "$.execution_binding.evaluation_profile_sha256",
                "usage profile differs from the profile sealed in workflow settings",
            )
        ledger_path = ledger.path.resolve()
        if not ledger_path.is_relative_to(self.root):
            raise ContractValidationError(
                "usage_ledger_path",
                str(ledger_path),
                "shared usage ledger must be inside the Evaluation component root",
            )
        source_ledger_ref = ledger_path.relative_to(self.root).as_posix()
        all_seals = [
            validate_resolved_llm_run_seal(row)
            for row in ledger.list_records("seal")
        ]
        selected_seals = [
            row
            for row in all_seals
            if row["run_id"] == run_id and row["attempt_run_id"] == attempt_run_id
        ]
        for seal in selected_seals:
            if seal["workstream"] != "evaluation":
                raise ContractValidationError(
                    "usage_workstream",
                    "$.shared_ledger.seals",
                    "Evaluation usage snapshots cannot accept another workstream's seal",
                )
            if (
                seal["profile"]["record"]["profile_id"] != profile_id
                or seal["profile"]["sha256"] != profile_sha256
            ):
                raise ContractValidationError(
                    "usage_profile_binding",
                    "$.shared_ledger.seals",
                    "sealed provider attempt differs from the Evaluation execution profile",
                )
        all_usage = ledger.list_records("usage")
        all_errors = ledger.list_records("error")
        all_cache = ledger.list_records("cache")
        all_receipts = ledger.list_records("artifact_receipt")
        evidence: list[tuple[str, str, str, dict[str, Any], dict[str, Any]]] = []
        for seal in selected_seals:
            seal_hash = seal["seal_sha256"]
            validated = validate_llm_run_records(
                seal=seal,
                usage_rows=[
                    row for row in all_usage if row["seal_sha256"] == seal_hash
                ],
                error_rows=[
                    row for row in all_errors if row["seal_sha256"] == seal_hash
                ],
                cache_observations=[
                    row for row in all_cache if row["seal_sha256"] == seal_hash
                ],
                producer_seals=all_seals,
                reusable_artifact_receipts=all_receipts,
                certify_limits=True,
            )
            for row in validated["usage_rows"]:
                evidence.append(
                    (
                        row["finished_at_utc"],
                        "usage",
                        row["attempt_usage_id"],
                        seal,
                        row,
                    )
                )
            for row in validated["cache_observations"]:
                evidence.append(
                    (
                        row["observed_at_utc"],
                        "cache",
                        row["observation_id"],
                        seal,
                        row,
                    )
                )
        emitted: list[dict[str, Any]] = []
        for _, kind, _, seal, row in sorted(
            evidence, key=lambda item: (item[0], item[1], item[2])
        ):
            target = _execution_target_from_seal(seal)
            common = {
                "stage_id": stage_id,
                "role_id": seal["role_id"],
                "source_ledger_ref": source_ledger_ref,
                "execution_target": target,
                "component_attempt_id": self.component_attempt_id,
                "component_attempt_index": self.component_attempt_index,
                "accepted_through_component_seq": len(self._events) + 1,
                "current_work_id": current_work_id,
                "generated_at": self.generated_at,
            }
            snapshot = (
                self._usage_tracker.accept_usage(row, **common)
                if kind == "usage"
                else self._usage_tracker.accept_cache_observation(row, **common)
            )
            if snapshot is not None:
                self._persist_usage_snapshot(snapshot)
                emitted.append(snapshot)
        return tuple(copy.deepcopy(emitted))

    def _persist_usage_snapshot(
        self, snapshot: Mapping[str, Any]
    ) -> dict[str, Any]:
        tracker_snapshots = self._usage_tracker.snapshots
        previous_snapshot = (
            None if len(tracker_snapshots) < 2 else tracker_snapshots[-2]
        )
        normalized = validate_evaluation_component_usage_snapshot_v1(
            snapshot,
            previous_snapshot=previous_snapshot,
            stage_ids=self.stage_ids,
        )
        canonical_hash = normalized["integrity"]["usage_snapshot_sha256"]
        relative_path = f"usage_snapshots/{canonical_hash}.json"
        _write_immutable_json(self.root / relative_path, normalized)
        binding = validate_typed_artifact_binding_v1(
            {
                "artifact_ref": relative_path,
                "artifact_kind": "evaluation_component_usage_snapshot_v1",
                "schema_version": normalized["schema_version"],
                "sha256": canonical_hash,
                "sha256_kind": (
                    "canonical:EvaluationComponentUsageSnapshotV1@1.0.0"
                ),
            },
            path="$.usage_snapshot",
        )
        component_level = normalized["current_record"]["kind"] == "final"
        event = self.append_event(
            "usage_snapshot",
            stage_id="__component__" if component_level else normalized["stage_id"],
            agent=(
                "runner"
                if component_level
                else self._stage_agent(normalized["stage_id"])
            ),
            severity="info",
            payload={"snapshot": binding},
        )
        if event["component_seq"] != normalized["accepted_through_component_seq"]:
            raise ContractValidationError(
                "usage_component_sequence",
                relative_path,
                "usage snapshot does not bind the event that published it",
            )
        self.add_artifact(
            relative_path,
            artifact_kind="evaluation_component_usage_snapshot_v1",
            schema_version=normalized["schema_version"],
            stage_id=normalized["stage_id"],
            created_by_event_id=event["event_id"],
            parent_artifact_refs=(
                "workflow_settings.json",
                "scoring_receipt.json",
            ),
        )
        return copy.deepcopy(event)

    def _finalize_usage(self, *, stage_id: str) -> None:
        snapshot = self._usage_tracker.finalize(
            stage_id=stage_id,
            component_attempt_id=self.component_attempt_id,
            component_attempt_index=self.component_attempt_index,
            accepted_through_component_seq=len(self._events) + 1,
            generated_at=self.generated_at,
        )
        if snapshot is not None:
            self._persist_usage_snapshot(snapshot)

    def validate_package(self, *, require_terminal: bool = False) -> dict[str, Any]:
        return validate_evaluation_workflow_component_package_v1(
            self.root, self.handoff, require_terminal=require_terminal
        )

    def _build_manifest(
        self,
        *,
        attempt_index: int,
        revision: int,
        previous_manifest_sha256: str | None,
    ) -> dict[str, Any]:
        return build_evaluation_component_manifest_v1(
            workflow_run_id=self.workflow_run_id,
            component_run_id=self.component_run_id,
            component_attempt_id=f"evalcomp_attempt_{attempt_index:04d}",
            component_attempt_index=attempt_index,
            manifest_revision=revision,
            previous_manifest_sha256=previous_manifest_sha256,
            created_at=self.generated_at,
            producer_code_commit=self.producer_code_commit,
            scoring_handoff=self.handoff_binding,
            scoring_receipt_ref="scoring_receipt.json",
            accepted_input_set_sha256=self.handoff["input_set_sha256"],
            evaluation_profile=self.evaluation_profile,
            workflow_settings=self.workflow_settings_binding,
            stages=self.stages,
        )

    def _persist_manifest_revision(self, manifest: Mapping[str, Any]) -> None:
        revision = manifest["manifest_revision"]
        digest = manifest["integrity"]["manifest_sha256"]
        _write_immutable_json(
            self.manifest_revisions_root / f"{revision:04d}_{digest}.json", manifest
        )
        _write_json_atomic(self.manifest_path, manifest)
        self._manifest_revisions.append(copy.deepcopy(dict(manifest)))

    def _persist_event_projection(self) -> None:
        payload = b"".join(_json_bytes(event) for event in self._events)
        _write_bytes_atomic(self.events_path, payload)

    def _persist_artifact_index(self) -> None:
        index = build_evaluation_artifact_index_v1(
            self.manifest,
            generated_at=self.generated_at,
            artifacts=self._artifact_rows,
            producer_code_commit=self.producer_code_commit,
        )
        _write_json_atomic(self.artifact_index_path, index)

    def _load_existing(self) -> None:
        self.manifest = validate_evaluation_component_manifest_v1(
            _load_json(self.manifest_path)
        )
        if self.manifest["workflow_run_id"] != self.workflow_run_id:
            raise ContractValidationError(
                "workflow_binding", str(self.manifest_path), "foreign workflow component"
            )
        if self.manifest["component_run_id"] != self.component_run_id:
            raise ContractValidationError(
                "component_binding", str(self.manifest_path), "component run ID changed"
            )
        if self.manifest["scoring_handoff"] != self.handoff_binding:
            raise ContractValidationError(
                "handoff_binding", str(self.manifest_path), "scoring handoff changed"
            )
        if self.manifest["evaluation_profile"] != self.evaluation_profile:
            raise ContractValidationError(
                "profile_binding", str(self.manifest_path), "evaluation profile changed"
            )
        persisted_settings = validate_evaluation_workflow_settings_v1(
            _load_json(self.workflow_settings_path),
            authority=self.workflow_settings_authority,
            scoring_handoff=self.handoff,
        )
        if persisted_settings != self.workflow_settings:
            raise ContractValidationError(
                "settings_binding",
                str(self.workflow_settings_path),
                "workflow settings changed",
            )
        if self.manifest.get("workflow_settings") != self.workflow_settings_binding:
            raise ContractValidationError(
                "settings_binding",
                str(self.manifest_path),
                "manifest workflow settings binding changed",
            )
        if self.manifest["stages"] != list(self.stages):
            raise ContractValidationError(
                "stage_binding", str(self.manifest_path), "Evaluation stage plan changed"
            )
        revision_paths = sorted(self.manifest_revisions_root.glob("*.json"))
        self._manifest_revisions = [
            validate_evaluation_component_manifest_v1(_load_json(path))
            for path in revision_paths
        ]
        if not self._manifest_revisions or self._manifest_revisions[-1] != self.manifest:
            raise ContractValidationError(
                "manifest_lineage", str(self.manifest_path), "current manifest is not latest revision"
            )
        for index, revision in enumerate(self._manifest_revisions, start=1):
            if revision["manifest_revision"] != index:
                raise ContractValidationError(
                    "manifest_lineage", str(self.manifest_revisions_root), "revision gap"
                )
            expected_previous = (
                None
                if index == 1
                else self._manifest_revisions[index - 2]["integrity"]["manifest_sha256"]
            )
            if revision["previous_manifest_sha256"] != expected_previous:
                raise ContractValidationError(
                    "manifest_lineage", str(self.manifest_revisions_root), "revision hash chain drift"
                )
        event_paths = sorted(self.event_records_root.glob("*.json"))
        self._events = [_load_json(path) for path in event_paths]
        self._validate_stream()
        projection = _load_jsonl(self.events_path)
        if projection != self._events:
            raise ContractValidationError(
                "event_projection", str(self.events_path), "event projection differs from immutable records"
            )
        receipt = _load_json(self.receipt_path)
        receipt = validate_scoring_receipt_v1(receipt, handoff=self.handoff)
        if receipt["scoring_handoff"] != self.handoff_binding:
            raise ContractValidationError(
                "handoff_binding",
                str(self.receipt_path),
                "scoring receipt handoff binding changed",
            )
        if self.manifest["scoring_receipt_ref"] != "scoring_receipt.json":
            raise ContractValidationError(
                "receipt_binding",
                str(self.manifest_path),
                "component receipt reference must remain component-local",
            )
        index = validate_evaluation_artifact_index_v1(
            _load_json(self.artifact_index_path), manifest=self.manifest
        )
        self._artifact_rows = copy.deepcopy(index["artifacts"])
        _verify_physical_artifacts(self.root, self._artifact_rows)
        self._restore_usage_tracker()

    def _restore_usage_tracker(self) -> None:
        snapshots = [
            _load_json(self.root / row["artifact"]["artifact_ref"])
            for row in self._artifact_rows
            if row["artifact"]["artifact_kind"]
            == "evaluation_component_usage_snapshot_v1"
        ]
        snapshots.sort(
            key=lambda row: require_int(
                row.get("snapshot_index"),
                path="$usage_snapshot.snapshot_index",
                minimum=1,
            )
        )
        snapshots = list(
            validate_evaluation_component_usage_snapshot_chain_v1(
                snapshots,
                workflow_run_id=self.workflow_run_id,
                component_run_id=self.component_run_id,
                stage_ids=self.stage_ids,
            )
        )
        self._usage_tracker = EvaluationComponentUsageTrackerV1(
            workflow_run_id=self.workflow_run_id,
            component_run_id=self.component_run_id,
            stage_ids=self.stage_ids,
            snapshots=snapshots,
        )

    def _validate_stream(self) -> None:
        prior = [
            revision
            for revision in self._manifest_revisions
            if revision["integrity"]["manifest_sha256"]
            != self.manifest["integrity"]["manifest_sha256"]
        ]
        validate_evaluation_component_stream_v1(
            self.manifest, self._events, manifest_revisions=prior
        )

    def _stage_states(self) -> dict[str, str]:
        states = {stage["stage_id"]: "pending" for stage in self.stages}
        for event in self._events:
            event_type = event["event"]
            if event_type == "stage_start":
                states[event["stage_id"]] = "running"
            elif event_type == "stage_done":
                states[event["stage_id"]] = event["payload"]["outcome"]
            elif event_type == "component_halted":
                for stage_id, state in tuple(states.items()):
                    if state == "running":
                        states[stage_id] = "halted"
        return states

    def _stage_agent(self, stage_id: str) -> str:
        for stage in self.stages:
            if stage["stage_id"] == stage_id:
                return stage["agent"]
        raise ContractValidationError(
            "stage_binding", "$.stage_id", f"unknown Evaluation stage: {stage_id}"
        )

    def _latest_checkpoint_binding(self) -> dict[str, Any]:
        for event in reversed(self._events):
            if event["event"] == "checkpoint":
                return copy.deepcopy(event["payload"]["checkpoint"])
        raise ContractValidationError(
            "resume_checkpoint",
            str(self.events_path),
            "halted component has no durable checkpoint",
        )

    def _latest_non_pending_stage_id(self) -> str:
        states = self._stage_states()
        for stage in reversed(self.stages):
            if states[stage["stage_id"]] != "pending":
                return stage["stage_id"]
        return self.stage_ids[0]


def validate_evaluation_workflow_component_package_v1(
    root: Path,
    handoff: Mapping[str, Any],
    *,
    require_terminal: bool = False,
) -> dict[str, Any]:
    package_root = root.resolve()
    accepted_handoff = validate_scoring_handoff_v1(handoff)
    manifest = validate_evaluation_component_manifest_v1(
        _load_json(package_root / "component_manifest.json")
    )
    revisions = [
        validate_evaluation_component_manifest_v1(_load_json(path))
        for path in sorted((package_root / "manifest_revisions").glob("*.json"))
    ]
    if not revisions or revisions[-1] != manifest:
        raise ContractValidationError(
            "manifest_lineage", str(package_root), "current manifest is not latest revision"
        )
    for index, revision in enumerate(revisions, start=1):
        if revision["manifest_revision"] != index:
            raise ContractValidationError(
                "manifest_lineage", str(package_root), "manifest revision gap"
            )
        expected_previous = (
            None
            if index == 1
            else revisions[index - 2]["integrity"]["manifest_sha256"]
        )
        if revision["previous_manifest_sha256"] != expected_previous:
            raise ContractValidationError(
                "manifest_lineage", str(package_root), "manifest revision hash-chain drift"
            )
    if manifest["scoring_receipt_ref"] != "scoring_receipt.json":
        raise ContractValidationError(
            "receipt_binding", str(package_root), "receipt reference escapes component package"
        )
    expected_handoff_binding = {
        "artifact_ref": "handoffs/scoring_handoff.json",
        "artifact_kind": "scoring_handoff_v1",
        "schema_version": SCHEMA_VERSION,
        "sha256": accepted_handoff["integrity"]["handoff_sha256"],
        "sha256_kind": "canonical:ScoringHandoffV1@1.0.0",
    }
    if manifest["scoring_handoff"] != expected_handoff_binding:
        raise ContractValidationError(
            "handoff_binding", str(package_root), "manifest binds a foreign scoring handoff"
        )
    settings = validate_evaluation_workflow_settings_v1(
        _load_json(package_root / "workflow_settings.json"),
        scoring_handoff=accepted_handoff,
    )
    expected_settings_binding = {
        "artifact_ref": "workflow_settings.json",
        "artifact_kind": "evaluation_workflow_settings_v1",
        "schema_version": settings["schema_version"],
        "sha256": settings["settings_sha256"],
        "sha256_kind": "canonical:EvaluationWorkflowSettingsV1@1.0.0",
    }
    if manifest.get("workflow_settings") != expected_settings_binding:
        raise ContractValidationError(
            "settings_binding",
            str(package_root),
            "manifest binds foreign workflow settings",
        )
    if settings["evaluation_profile_ref"] != manifest["evaluation_profile"]:
        raise ContractValidationError(
            "settings_profile_binding",
            str(package_root),
            "workflow settings and manifest profile disagree",
        )
    events = _load_jsonl(package_root / "events.jsonl")
    immutable_events = [
        _load_json(path)
        for path in sorted((package_root / "event_records").glob("*.json"))
    ]
    if events != immutable_events:
        raise ContractValidationError(
            "event_projection", str(package_root / "events.jsonl"), "event projection drift"
        )
    normalized_events = validate_evaluation_component_stream_v1(
        manifest, events, manifest_revisions=revisions[:-1]
    )
    receipt = validate_scoring_receipt_v1(
        _load_json(package_root / "scoring_receipt.json"), handoff=accepted_handoff
    )
    if receipt["scoring_handoff"] != expected_handoff_binding:
        raise ContractValidationError(
            "handoff_binding",
            str(package_root / "scoring_receipt.json"),
            "receipt binds a foreign scoring handoff",
        )
    if manifest["workflow_run_id"] != accepted_handoff["workflow_run_id"]:
        raise ContractValidationError(
            "workflow_binding", str(package_root), "manifest and handoff disagree"
        )
    if manifest["accepted_input_set_sha256"] != accepted_handoff["input_set_sha256"]:
        raise ContractValidationError(
            "input_set_hash", str(package_root), "manifest and handoff input set disagree"
        )
    index = validate_evaluation_artifact_index_v1(
        _load_json(package_root / "artifact_index.json"), manifest=manifest
    )
    event_ids = {event["event_id"] for event in normalized_events}
    stage_ids = {stage["stage_id"] for stage in manifest["stages"]}
    for artifact_index, artifact_row in enumerate(index["artifacts"]):
        if artifact_row["created_by_event_id"] not in event_ids:
            raise ContractValidationError(
                "artifact_event_binding",
                f"$artifact_index.artifacts[{artifact_index}].created_by_event_id",
                "artifact points to an event outside the component stream",
            )
        if artifact_row["stage_id"] not in stage_ids:
            raise ContractValidationError(
                "artifact_stage_binding",
                f"$artifact_index.artifacts[{artifact_index}].stage_id",
                "artifact points to an unknown component stage",
            )
    _verify_physical_artifacts(package_root, index["artifacts"])
    settings_artifacts = [
        row
        for row in index["artifacts"]
        if row["artifact"]["artifact_ref"] == "workflow_settings.json"
    ]
    if len(settings_artifacts) != 1 or settings_artifacts[0]["artifact"][
        "artifact_kind"
    ] != "evaluation_workflow_settings_v1":
        raise ContractValidationError(
            "settings_artifact",
            str(package_root),
            "artifact index must contain the exact workflow settings artifact",
        )
    usage_artifact_rows = [
        row
        for row in index["artifacts"]
        if row["artifact"]["artifact_kind"]
        == "evaluation_component_usage_snapshot_v1"
    ]
    usage_snapshots = [
        _load_json(package_root / row["artifact"]["artifact_ref"])
        for row in usage_artifact_rows
    ]
    paired_usage = sorted(
        zip(usage_snapshots, usage_artifact_rows, strict=True),
        key=lambda pair: require_int(
            pair[0].get("snapshot_index"),
            path="$usage_snapshot.snapshot_index",
            minimum=1,
        ),
    )
    usage_snapshots = [pair[0] for pair in paired_usage]
    usage_artifact_rows = [pair[1] for pair in paired_usage]
    usage_snapshots = list(
        validate_evaluation_component_usage_snapshot_chain_v1(
            usage_snapshots,
            workflow_run_id=manifest["workflow_run_id"],
            component_run_id=manifest["component_run_id"],
            stage_ids=tuple(stage["stage_id"] for stage in manifest["stages"]),
        )
    )
    usage_events = [
        event for event in normalized_events if event["event"] == "usage_snapshot"
    ]
    if len(usage_events) != len(usage_snapshots):
        raise ContractValidationError(
            "usage_snapshot_coverage",
            str(package_root),
            "usage events and indexed snapshots must have exact cover",
        )
    for snapshot, artifact_row, event in zip(
        usage_snapshots, usage_artifact_rows, usage_events, strict=True
    ):
        expected_binding = {
            "artifact_ref": artifact_row["artifact"]["artifact_ref"],
            "artifact_kind": "evaluation_component_usage_snapshot_v1",
            "schema_version": snapshot["schema_version"],
            "sha256": snapshot["integrity"]["usage_snapshot_sha256"],
            "sha256_kind": (
                "canonical:EvaluationComponentUsageSnapshotV1@1.0.0"
            ),
        }
        if event["payload"]["snapshot"] != expected_binding:
            raise ContractValidationError(
                "usage_snapshot_binding",
                event["event_id"],
                "usage event binds foreign snapshot bytes",
            )
        if snapshot["accepted_through_component_seq"] != event["component_seq"]:
            raise ContractValidationError(
                "usage_component_sequence",
                event["event_id"],
                "usage snapshot does not name its publishing event",
            )
        if artifact_row["created_by_event_id"] != event["event_id"]:
            raise ContractValidationError(
                "usage_snapshot_event",
                artifact_row["artifact"]["artifact_ref"],
                "usage artifact creator differs from its publishing event",
            )
    terminal = normalized_events[-1]["event"] in {
        "component_done",
        "component_failed",
    }
    if terminal:
        if (
            not usage_snapshots
            or usage_snapshots[-1]["current_record"]["kind"] != "final"
            or len(normalized_events) < 2
            or normalized_events[-2]["event"] != "usage_snapshot"
        ):
            raise ContractValidationError(
                "usage_final",
                str(package_root),
                "terminal Evaluation component requires a final usage snapshot",
            )
    elif usage_snapshots and usage_snapshots[-1]["current_record"]["kind"] == "final":
        raise ContractValidationError(
            "usage_final",
            str(package_root),
            "nonterminal Evaluation component cannot publish final usage",
        )
    if require_terminal and normalized_events[-1]["event"] not in {
        "component_done",
        "component_failed",
    }:
        raise ContractValidationError(
            "component_terminal", str(package_root), "component package is not terminal"
        )
    return {
        "manifest": manifest,
        "events": tuple(copy.deepcopy(normalized_events)),
        "receipt": receipt,
        "workflow_settings": settings,
        "usage_snapshots": tuple(copy.deepcopy(usage_snapshots)),
        "artifact_index": index,
    }


def _execution_target_from_seal(seal: Mapping[str, Any]) -> dict[str, Any]:
    normalized = validate_resolved_llm_run_seal(seal)
    primary = normalized["primary"]
    return {
        "source_id": primary["source"]["source_id"],
        "source_revision": primary["source"]["source_revision"],
        "physical_quota_bucket_id": primary["source"][
            "physical_quota_bucket_id"
        ],
        "requested_model_id": primary["target"]["requested_model_id"],
        "observed_model_id": primary["capability"]["observed_model_id"],
    }


def _build_checkpoint(
    *,
    manifest: Mapping[str, Any],
    stage_id: str,
    work_id: str | None,
    benchmark_status: Mapping[str, Any],
    chapter_checkpoint: Mapping[str, Any] | None,
    completed_stage_ids: Sequence[str],
    created_at: str,
    producer_code_commit: str,
) -> dict[str, Any]:
    status = require_mapping(benchmark_status, path="$.benchmark_status")
    required_status = {
        "state",
        "current_chapter_id",
        "completed_chapter_count",
        "last_event_sequence",
        "last_event_sha256",
        "manifest_sha256",
    }
    missing = required_status - set(status)
    if missing:
        raise ContractValidationError(
            "checkpoint_status", "$.benchmark_status", f"missing status fields: {sorted(missing)}"
        )
    chapter = (
        None
        if chapter_checkpoint is None
        else validate_typed_artifact_binding_v1(
            chapter_checkpoint, path="$.chapter_checkpoint"
        )
    )
    draft = {
        "schema_id": CHECKPOINT_SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "workflow_run_id": manifest["workflow_run_id"],
        "component_run_id": manifest["component_run_id"],
        "component_attempt_id": manifest["component_attempt_id"],
        "component_attempt_index": manifest["component_attempt_index"],
        "stage_id": require_string(stage_id, path="$.stage_id"),
        "work_id": require_nullable_string(work_id, path="$.work_id"),
        "completed_stage_ids": [
            require_string(item, path="$.completed_stage_ids[*]")
            for item in completed_stage_ids
        ],
        "benchmark_state": {
            "state": require_string(status["state"], path="$.benchmark_status.state"),
            "current_chapter_id": require_nullable_string(
                status["current_chapter_id"], path="$.benchmark_status.current_chapter_id"
            ),
            "completed_chapter_count": require_int(
                status["completed_chapter_count"],
                path="$.benchmark_status.completed_chapter_count",
                minimum=0,
            ),
            "last_event_sequence": require_int(
                status["last_event_sequence"],
                path="$.benchmark_status.last_event_sequence",
                minimum=1,
            ),
            "last_event_sha256": require_sha256(
                status["last_event_sha256"], path="$.benchmark_status.last_event_sha256"
            ),
            "manifest_sha256": require_sha256(
                status["manifest_sha256"], path="$.benchmark_status.manifest_sha256"
            ),
        },
        "chapter_checkpoint": chapter,
        "created_at": require_rfc3339(created_at, path="$.created_at"),
        "producer": {
            "workstream": "evaluation",
            "component": "workflow_component_writer_v1",
            "component_version": SCHEMA_VERSION,
            "code_commit": require_commit(
                producer_code_commit, path="$.producer_code_commit"
            ),
        },
        "integrity": {"checkpoint_sha256": "0" * 64},
    }
    return _validate_checkpoint(
        seal_payload(draft, policy=_CHECKPOINT_POLICY, hash_path=_CHECKPOINT_HASH_PATH)
    )


def _build_resume_intent(
    *,
    workflow_run_id: str,
    component_run_id: str,
    previous_manifest_sha256: str,
    previous_event_sha256: str,
    next_manifest: Mapping[str, Any],
    resume_event: Mapping[str, Any],
    next_artifact_index: Mapping[str, Any],
) -> dict[str, Any]:
    draft = {
        "schema_id": RESUME_INTENT_SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "workflow_run_id": workflow_run_id,
        "component_run_id": component_run_id,
        "previous_manifest_sha256": previous_manifest_sha256,
        "previous_event_sha256": previous_event_sha256,
        "next_manifest": copy.deepcopy(dict(next_manifest)),
        "resume_event": copy.deepcopy(dict(resume_event)),
        "next_artifact_index": copy.deepcopy(dict(next_artifact_index)),
        "integrity": {"resume_intent_sha256": "0" * 64},
    }
    draft["integrity"]["resume_intent_sha256"] = _resume_intent_sha256(draft)
    return _validate_resume_intent(draft)


def _validate_resume_intent(value: Mapping[str, Any]) -> dict[str, Any]:
    row = require_mapping(value, path="$resume_intent")
    require_exact_keys(
        row,
        required={
            "schema_id",
            "schema_version",
            "workflow_run_id",
            "component_run_id",
            "previous_manifest_sha256",
            "previous_event_sha256",
            "next_manifest",
            "resume_event",
            "next_artifact_index",
            "integrity",
        },
        path="$resume_intent",
    )
    integrity = require_mapping(row["integrity"], path="$resume_intent.integrity")
    require_exact_keys(
        integrity,
        required={"resume_intent_sha256"},
        path="$resume_intent.integrity",
    )
    next_manifest = validate_evaluation_component_manifest_v1(row["next_manifest"])
    resume_event = validate_evaluation_component_event_v1(row["resume_event"])
    if (
        resume_event["event"] != "component_resumed"
        or resume_event["stage_id"] != "__component__"
        or resume_event["agent"] != "runner"
        or resume_event["severity"] != "info"
        or resume_event["manifest_sha256"]
        != next_manifest["integrity"]["manifest_sha256"]
        or resume_event["component_attempt_id"] != next_manifest["component_attempt_id"]
        or resume_event["component_attempt_index"]
        != next_manifest["component_attempt_index"]
        or resume_event["previous_event_sha256"] != row["previous_event_sha256"]
    ):
        raise ContractValidationError(
            "resume_intent_event",
            "$resume_intent.resume_event",
            "Resume event is not bound to the sealed next manifest",
        )
    next_artifact_index = validate_evaluation_artifact_index_v1(
        row["next_artifact_index"], manifest=next_manifest
    )
    normalized = {
        "schema_id": require_enum(
            row["schema_id"], {RESUME_INTENT_SCHEMA_ID}, path="$resume_intent.schema_id"
        ),
        "schema_version": require_enum(
            row["schema_version"], {SCHEMA_VERSION}, path="$resume_intent.schema_version"
        ),
        "workflow_run_id": require_string(
            row["workflow_run_id"], path="$resume_intent.workflow_run_id"
        ),
        "component_run_id": require_string(
            row["component_run_id"], path="$resume_intent.component_run_id"
        ),
        "previous_manifest_sha256": require_sha256(
            row["previous_manifest_sha256"],
            path="$resume_intent.previous_manifest_sha256",
        ),
        "previous_event_sha256": require_sha256(
            row["previous_event_sha256"], path="$resume_intent.previous_event_sha256"
        ),
        "next_manifest": next_manifest,
        "resume_event": resume_event,
        "next_artifact_index": next_artifact_index,
        "integrity": {
            "resume_intent_sha256": require_sha256(
                integrity["resume_intent_sha256"],
                path="$resume_intent.integrity.resume_intent_sha256",
            )
        },
    }
    if normalized["workflow_run_id"] != next_manifest["workflow_run_id"]:
        raise ContractValidationError(
            "resume_intent_binding", "$resume_intent.workflow_run_id", "workflow drift"
        )
    if normalized["component_run_id"] != next_manifest["component_run_id"]:
        raise ContractValidationError(
            "resume_intent_binding", "$resume_intent.component_run_id", "component drift"
        )
    if normalized["previous_manifest_sha256"] != next_manifest["previous_manifest_sha256"]:
        raise ContractValidationError(
            "resume_intent_lineage",
            "$resume_intent.previous_manifest_sha256",
            "manifest predecessor drift",
        )
    if normalized["integrity"]["resume_intent_sha256"] != _resume_intent_sha256(
        normalized
    ):
        raise ContractValidationError(
            "resume_intent_hash",
            "$resume_intent.integrity.resume_intent_sha256",
            "Resume intent hash drift",
        )
    return copy.deepcopy(normalized)


def _resume_intent_sha256(value: Mapping[str, Any]) -> str:
    material = copy.deepcopy(dict(value))
    material["integrity"] = {"resume_intent_sha256": "0" * 64}
    encoded = json.dumps(
        material,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_checkpoint(value: Mapping[str, Any]) -> dict[str, Any]:
    row = require_mapping(value, path="$checkpoint")
    require_exact_keys(
        row,
        required={
            "schema_id",
            "schema_version",
            "workflow_run_id",
            "component_run_id",
            "component_attempt_id",
            "component_attempt_index",
            "stage_id",
            "work_id",
            "completed_stage_ids",
            "benchmark_state",
            "chapter_checkpoint",
            "created_at",
            "producer",
            "integrity",
        },
        path="$checkpoint",
    )
    state = require_mapping(row["benchmark_state"], path="$checkpoint.benchmark_state")
    require_exact_keys(
        state,
        required={
            "state",
            "current_chapter_id",
            "completed_chapter_count",
            "last_event_sequence",
            "last_event_sha256",
            "manifest_sha256",
        },
        path="$checkpoint.benchmark_state",
    )
    producer = require_mapping(row["producer"], path="$checkpoint.producer")
    require_exact_keys(
        producer,
        required={"workstream", "component", "component_version", "code_commit"},
        path="$checkpoint.producer",
    )
    integrity = require_mapping(row["integrity"], path="$checkpoint.integrity")
    require_exact_keys(
        integrity, required={"checkpoint_sha256"}, path="$checkpoint.integrity"
    )
    normalized = {
        "schema_id": require_enum(
            row["schema_id"], {CHECKPOINT_SCHEMA_ID}, path="$checkpoint.schema_id"
        ),
        "schema_version": require_enum(
            row["schema_version"], {SCHEMA_VERSION}, path="$checkpoint.schema_version"
        ),
        "workflow_run_id": require_string(
            row["workflow_run_id"], path="$checkpoint.workflow_run_id"
        ),
        "component_run_id": require_string(
            row["component_run_id"], path="$checkpoint.component_run_id"
        ),
        "component_attempt_id": require_string(
            row["component_attempt_id"], path="$checkpoint.component_attempt_id"
        ),
        "component_attempt_index": require_int(
            row["component_attempt_index"],
            path="$checkpoint.component_attempt_index",
            minimum=1,
        ),
        "stage_id": require_string(row["stage_id"], path="$checkpoint.stage_id"),
        "work_id": require_nullable_string(row["work_id"], path="$checkpoint.work_id"),
        "completed_stage_ids": [
            require_string(item, path="$checkpoint.completed_stage_ids[*]")
            for item in require_list(
                row["completed_stage_ids"], path="$checkpoint.completed_stage_ids"
            )
        ],
        "benchmark_state": {
            "state": require_string(state["state"], path="$checkpoint.benchmark_state.state"),
            "current_chapter_id": require_nullable_string(
                state["current_chapter_id"],
                path="$checkpoint.benchmark_state.current_chapter_id",
            ),
            "completed_chapter_count": require_int(
                state["completed_chapter_count"],
                path="$checkpoint.benchmark_state.completed_chapter_count",
                minimum=0,
            ),
            "last_event_sequence": require_int(
                state["last_event_sequence"],
                path="$checkpoint.benchmark_state.last_event_sequence",
                minimum=1,
            ),
            "last_event_sha256": require_sha256(
                state["last_event_sha256"],
                path="$checkpoint.benchmark_state.last_event_sha256",
            ),
            "manifest_sha256": require_sha256(
                state["manifest_sha256"], path="$checkpoint.benchmark_state.manifest_sha256"
            ),
        },
        "chapter_checkpoint": (
            None
            if row["chapter_checkpoint"] is None
            else validate_typed_artifact_binding_v1(
                row["chapter_checkpoint"], path="$checkpoint.chapter_checkpoint"
            )
        ),
        "created_at": require_rfc3339(row["created_at"], path="$checkpoint.created_at"),
        "producer": {
            "workstream": require_enum(
                producer["workstream"], {"evaluation"}, path="$checkpoint.producer.workstream"
            ),
            "component": require_enum(
                producer["component"],
                {"workflow_component_writer_v1"},
                path="$checkpoint.producer.component",
            ),
            "component_version": require_enum(
                producer["component_version"],
                {SCHEMA_VERSION},
                path="$checkpoint.producer.component_version",
            ),
            "code_commit": require_commit(
                producer["code_commit"], path="$checkpoint.producer.code_commit"
            ),
        },
        "integrity": {
            "checkpoint_sha256": require_sha256(
                integrity["checkpoint_sha256"],
                path="$checkpoint.integrity.checkpoint_sha256",
            )
        },
    }
    if not verify_payload_hash(
        normalized, policy=_CHECKPOINT_POLICY, hash_path=_CHECKPOINT_HASH_PATH
    ):
        raise ContractValidationError(
            "checkpoint_hash",
            "$checkpoint.integrity.checkpoint_sha256",
            "checkpoint hash drift",
        )
    result = canonicalize(normalized, policy=_CHECKPOINT_POLICY)
    assert isinstance(result, dict)
    return result


def _verify_physical_artifacts(root: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    for index, row in enumerate(rows):
        artifact = row["artifact"]
        if artifact["sha256_kind"] != "physical":
            continue
        path = (root / artifact["artifact_ref"]).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ContractValidationError(
                "artifact_path", f"$.artifacts[{index}]", "indexed artifact is missing"
            )
        observed = hashlib.sha256(path.read_bytes()).hexdigest()
        if observed != artifact["sha256"]:
            raise ContractValidationError(
                "artifact_hash", f"$.artifacts[{index}]", "indexed artifact hash drift"
            )


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)),
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ContractValidationError("component_json", str(path), "invalid JSON") from exc
    if not isinstance(value, dict):
        raise ContractValidationError("component_json", str(path), "expected JSON object")
    return value


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ContractValidationError("component_jsonl", str(path), "cannot read JSONL") from exc
    result: list[dict[str, Any]] = []
    for index, line in enumerate(lines, start=1):
        if not line:
            raise ContractValidationError(
                "component_jsonl", f"{path}:{index}", "blank JSONL row"
            )
        try:
            row = json.loads(
                line,
                parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)),
            )
        except (json.JSONDecodeError, ValueError) as exc:
            raise ContractValidationError(
                "component_jsonl", f"{path}:{index}", "invalid JSONL row"
            ) from exc
        if not isinstance(row, dict):
            raise ContractValidationError(
                "component_jsonl", f"{path}:{index}", "expected object row"
            )
        result.append(row)
    return result


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def _write_immutable_json(path: Path, value: Mapping[str, Any]) -> None:
    encoded = _json_bytes(value)
    if path.exists():
        if path.read_bytes() != encoded:
            raise ContractValidationError(
                "immutable_conflict", str(path), "immutable workflow record differs"
            )
        return
    _write_bytes_atomic(path, encoded)


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    _write_bytes_atomic(path, _json_bytes(value))


def _write_bytes_atomic(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("xb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
