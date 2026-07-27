"""Current Literary chapter-loop executor over the existing stage CLIs.

This module is wiring only.  It resolves roots from sealed receipts, invokes
the existing stage owners once, and projects their artifacts into Console
history.  It contains no literary or language interpretation.
"""

from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, Mapping, Sequence

from pipeline.literary.b1_cross_chapter_decision_ledger_v1 import (
    verify_decision_ledger_v1,
)
from pipeline.literary.b1_enrich_local_auditor_v1 import (
    plan_b1_enrich_local_audit_batches_v1,
)
from pipeline.literary.b2_context_v1 import (
    build_b2_windows_v1,
    load_b2_phase_a_profile,
)
from pipeline.literary.b2_live_canary_v1 import load_b2_canary_profile_v1
from pipeline.literary.b2_recovery_batch_v1 import MAX_BATCH_COMPONENTS_V1
from pipeline.literary.b2_slim_speaker_recovery_v1 import (
    build_b2_slim_speaker_recovery_index_v1,
    load_b2_slim_speaker_source_v1,
)
from pipeline.literary.b3_parked_identity_v2 import (
    build_parked_identity_index_v2,
    empty_parked_identity_index_v2,
)
from pipeline.literary.b3_temporal_auditor_v1 import (
    STATE_REVIEW_ROUTES,
    build_b3_temporal_review_overlay_v1,
    validate_b3_temporal_audit_response_v1,
    verify_b3_temporal_review_overlay_v1,
    verify_b3_temporal_review_packet_v1,
)
from pipeline.literary.b3_temporal_context_v1 import (
    load_b2_temporal_input_v1,
    load_b3_temporal_profile_v1,
)
from pipeline.literary.b3_temporal_context_v7 import (
    build_b3_temporal_cross_chapter_bundle_v7,
)
from pipeline.literary.b3_temporal_prefix_v1 import build_b3_temporal_prefix_v1
from pipeline.literary.chapter_cycle_orchestrator_v1 import (
    ApiCallPermit,
    ChapterCycleStage,
    ChapterCycleStagePause,
    StageExecutionResult,
)
from pipeline.literary.chapter_loop_bindings_v1 import (
    ChapterLoopRuntimeBindingsV1,
    StageBindingV1,
)
from pipeline.literary.chapter_loop_observability_v1 import (
    LiteraryChapterLoopHistoryV1,
)
from pipeline.literary.chapter_source_document_v1 import (
    chapter_from_document_v1,
    load_literary_source_document_v1,
)
from pipeline.literary.checkpoint import canonical_hash, file_sha256
from pipeline.literary.identity_split_correction_v1 import (
    LiteraryIdentitySplitCorrectionError,
    verify_identity_split_bundle_v1,
)
from pipeline.literary.relation_correction_overlay_v1 import (
    LiteraryRelationCorrectionError,
    verify_relation_correction_bundle_v1,
)


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_ROOT = RUNTIME_ROOT.parent
DESIGN_DOC = REPOSITORY_ROOT / "design" / "LITERARY_PROMPT_DESIGN.md"
EFFECTIVE_STAGE_ROOTS_SCHEMA = "literary_effective_stage_roots_v1"
EFFECTIVE_STAGE_ROOTS_RELATIVE_PATH = Path(
    "corrections/effective_stage_roots.json"
)

_OPTIONAL_INPUTS = {
    ("b1_scan", "prior_cards"),
    ("b1_scan", "previous_summary_root"),
    ("b1_enrich", "prior_cards"),
    ("b1_enrich", "previous_summary_root"),
    ("b1_registry_writer", "prior_cards"),
    ("b1_registry_writer", "reconciled_projection"),
    ("identity_apply", "ledger"),
    ("identity_apply", "decisions"),
    ("b1_to_b2_input", "reconciled_projection"),
    ("b2_frame_interaction", "prior_b2_root"),
    ("speaker_recovery", "runtime_profile"),
    ("b3_temporal", "speaker_recovery_root"),
    ("b3_temporal", "identity_hearing_root"),
    ("b3_temporal", "prior_b3_roots"),
    ("b3_auditor", "runtime_profile"),
    ("b3_apply", "overlays"),
    ("b3_apply", "reconciled_projection"),
    ("b0_summary", "speaker_recovery_root"),
    ("b0_summary", "prior_summary_root"),
}


class LiteraryChapterLoopExecutorError(RuntimeError):
    pass


class LiteraryChapterLoopExecutorV1:
    def __init__(
        self,
        *,
        run_root: Path,
        plan: Mapping[str, Any],
        stage_bindings: Mapping[str, StageBindingV1],
        runtime_bindings: ChapterLoopRuntimeBindingsV1,
        credential_file: Path | None,
        scheduler_root: Path | None,
        history: LiteraryChapterLoopHistoryV1,
    ) -> None:
        self.run_root = Path(run_root).resolve()
        self.plan = dict(plan)
        self.bindings = dict(stage_bindings)
        self.runtime = runtime_bindings
        self.credential_file = (
            Path(credential_file).resolve() if credential_file is not None else None
        )
        self.scheduler_root = (
            Path(scheduler_root).resolve() if scheduler_root is not None else None
        )
        self.history = history
        self.document_path = Path(str(plan["document_path"])).resolve()
        self.document = load_literary_source_document_v1(self.document_path)
        self.frozen_db = Path(str(plan["frozen_db_path"])).resolve()
        self.run_id = self.run_root.name
        if not self.frozen_db.is_file():
            raise LiteraryChapterLoopExecutorError("frozen database is absent")
        self._effective_stage_roots: dict[str, Path] = {}
        self._effective_stage_root_rows: dict[str, Mapping[str, Any]] = {}
        self._effective_stage_roots_manifest_sha256: str | None = None
        self._refresh_effective_stage_roots()

    def __call__(
        self, stage: ChapterCycleStage, permit: ApiCallPermit
    ) -> StageExecutionResult:
        binding = self._binding(stage)
        if stage.requires_api:
            if self.credential_file is None or not self.credential_file.is_file():
                raise ChapterCycleStagePause(
                    "integrity_or_lineage", "credential file is absent"
                )
            if self.scheduler_root is None or not self.scheduler_root.is_dir():
                raise ChapterCycleStagePause(
                    "integrity_or_lineage", "scheduler root is absent"
                )
        resolved = self.resolve_inputs(stage, strict=True)
        output_root = self.stage_output_root(stage)
        self.history.emit(
            "stage_start",
            stage=stage.stage_id,
            agent=_agent(stage.stage_name),
            script=binding.script or "checkpoint",
            payload={
                "chapter_id": stage.chapter_id,
                "resolved_inputs": _event_resolved_inputs(
                    resolved, self.run_root
                ),
                "conditional": binding.conditional,
            },
        )
        should_run, skip_reason = self._condition(stage, resolved)
        if not should_run:
            result = StageExecutionResult(
                status="skipped",
                payload={
                    "output_root": str(output_root),
                    "resolved_inputs": _public_resolved_inputs(resolved),
                    "skip_reason": skip_reason,
                    "artifact_refs": [],
                    "production_publish_performed": False,
                },
                call_disposition="not_required" if stage.requires_api else "code_only",
            )
            self.history.emit(
                "stage_skipped",
                stage=stage.stage_id,
                agent=_agent(stage.stage_name),
                script=binding.script or "checkpoint",
                payload={"chapter_id": stage.chapter_id, "reason": skip_reason},
            )
            return result
        if stage.stage_name == "checkpoint":
            return StageExecutionResult(
                status="accepted",
                payload={
                    "output_root": str(output_root),
                    "resolved_inputs": _public_resolved_inputs(resolved),
                    "artifact_refs": [],
                    "production_publish_performed": False,
                },
                call_disposition="code_only",
            )

        expected_calls = self.expected_calls(stage, resolved)
        if expected_calls > int(
            self.plan["logical_call_caps_by_role"].get(stage.stage_role, 0)
        ):
            raise ChapterCycleStagePause(
                "api_call_cap",
                f"{stage.stage_id} requires {expected_calls} calls above sealed role cap",
            )
        command = self.build_command(stage, resolved, output_root, expected_calls)
        command_record = self._write_command_record(
            stage=stage,
            command=command,
            resolved=resolved,
            expected_calls=expected_calls,
        )
        report_path = output_root / binding.report
        recovered_existing_output = _existing_stage_output_is_complete_v1(
            stage_name=stage.stage_name,
            report_path=report_path,
        )
        self.history.emit(
            "work_started",
            stage=stage.stage_id,
            agent=_agent(stage.stage_name),
            script=binding.script or "",
            payload={
                "chapter_id": stage.chapter_id,
                "expected_calls": expected_calls,
                "command_ref": command_record.relative_to(self.run_root).as_posix(),
                "recovered_existing_output": recovered_existing_output,
            },
        )
        if expected_calls and not recovered_existing_output:
            if stage.requires_api:
                for ordinal in range(1, expected_calls + 1):
                    permit.reserve(f"{stage.stage_name}_{ordinal:03d}")
            self.history.emit(
                "request_sent",
                stage=stage.stage_id,
                agent=_agent(stage.stage_name),
                script=binding.script or "",
                payload={"logical_call_count": expected_calls},
            )
        completed = (
            subprocess.CompletedProcess(command, 0, "", "")
            if recovered_existing_output
            else self._run_stage(stage, command, output_root)
        )
        if not report_path.is_file():
            raise ChapterCycleStagePause(
                "whole_response_contract",
                f"{stage.stage_id} produced no report: {binding.report}",
            )
        report = _read_json(report_path, f"{stage.stage_id} report")
        self._validate_report(stage, report)
        self._validate_required_outputs(stage, binding, output_root)
        persisted_attempt_count = permit.attempt_count()
        observed_calls = _reported_calls(report)
        if observed_calls is not None:
            if recovered_existing_output:
                if observed_calls > persisted_attempt_count:
                    raise ChapterCycleStagePause(
                        "integrity_or_lineage",
                        f"{stage.stage_id} reported {observed_calls} calls, "
                        f"but only {persisted_attempt_count} persisted call permits exist",
                    )
            elif observed_calls != expected_calls:
                raise ChapterCycleStagePause(
                    "integrity_or_lineage",
                    f"{stage.stage_id} reported {observed_calls} calls, "
                    f"expected {expected_calls}",
                )
        model_call_observed = stage.requires_api and persisted_attempt_count > 0
        fingerprint = (
            _request_fingerprint(output_root)
            if model_call_observed
            else None
        )
        history_result = self.history.record_stage(
            stage_id=stage.stage_id,
            stage_name=stage.stage_name,
            chapter_id=stage.chapter_id,
            stage_root=output_root,
            output_names=binding.outputs,
            parent_artifact_refs=_parent_artifact_refs(resolved, self.run_root),
            report_path=report_path,
            status="accepted",
        )
        if model_call_observed:
            self.history.emit(
                "response_received",
                stage=stage.stage_id,
                agent=_agent(stage.stage_name),
                script=binding.script or "",
                payload={
                    "logical_call_count": (
                        expected_calls
                        if expected_calls
                        else persisted_attempt_count
                    ),
                    "request_fingerprint": fingerprint,
                },
            )
        self.history.emit(
            "validation_passed",
            stage=stage.stage_id,
            agent=_agent(stage.stage_name),
            script=binding.script or "",
            payload={"report_ref": report_path.relative_to(self.run_root).as_posix()},
        )
        return StageExecutionResult(
            status="accepted",
            payload={
                "output_root": str(output_root),
                "resolved_inputs": _public_resolved_inputs(resolved),
                "artifact_refs": [
                    row["artifact_ref"] for row in history_result["artifacts"]
                ],
                "semantic_delta_hash": history_result["semantic_delta"]["delta_hash"],
                "command_record_sha256": file_sha256(command_record),
                "exit_code": completed.returncode,
                "recovered_existing_output": recovered_existing_output,
                "production_publish_performed": False,
            },
            call_disposition=(
                "called" if model_call_observed else (
                    "not_required" if stage.requires_api else "code_only"
                )
            ),
            request_fingerprint=fingerprint,
            model_actual=(
                self.runtime.stages[stage.stage_name].model_id
                if model_call_observed
                else None
            ),
            resilience_report_hash=(
                canonical_hash(report) if model_call_observed else None
            ),
            attempt_count=persisted_attempt_count,
            retry_count=0,
            fallback_count=0,
            semantic_pending_count=0,
        )

    def stage_output_root(self, stage: ChapterCycleStage) -> Path:
        return (
            self.run_root
            / "artifacts"
            / "chapters"
            / f"ch{stage.chapter_ordinal:03d}"
            / stage.stage_name
        )

    def resolve_inputs(
        self, stage: ChapterCycleStage, *, strict: bool
    ) -> dict[str, Any]:
        binding = self._binding(stage)
        resolved: dict[str, Any] = {}
        for input_name, expression in binding.inputs.items():
            value = self._resolve_expression(
                stage=stage,
                input_name=input_name,
                expression=expression,
                strict=strict,
            )
            if value is None and (stage.stage_name, input_name) not in _OPTIONAL_INPUTS:
                if strict:
                    raise ChapterCycleStagePause(
                        "integrity_or_lineage",
                        f"{stage.stage_id} lacks required input {input_name} from {expression}",
                    )
            if strict and value is not None:
                _ensure_resolved_value_exists(
                    value,
                    stage_id=stage.stage_id,
                    input_name=input_name,
                )
            resolved[input_name] = value
        if (
            stage.stage_name == "b2_frame_interaction"
            and resolved.get("canary_profile") is not None
        ):
            package = _read_json(
                Path(resolved["source_run_root"]) / "b2_registry_input.json",
                "B2 registry input",
            )
            chapter_rows = [
                row
                for row in package.get("chapters") or []
                if isinstance(row, Mapping)
                and isinstance(row.get("chapter"), Mapping)
                and row["chapter"].get("chapter_id") == stage.chapter_id
            ]
            if len(chapter_rows) != 1:
                raise LiteraryChapterLoopExecutorError(
                    "B2 input does not select exactly one current chapter"
                )
            template_path = Path(resolved["canary_profile"]).resolve()
            template_profile = load_b2_canary_profile_v1(template_path)
            phase_profile = load_b2_phase_a_profile(
                template_profile.b2_profile_path
            )
            interaction_call_count = len(
                build_b2_windows_v1(
                    chapter_rows[0]["chapter"],
                    profile=phase_profile,
                )
            )
            resolved["canary_profile"] = materialize_b2_canary_profile_v1(
                template_path=template_path,
                output_path=(
                    self.run_root
                    / "control"
                    / "materialized_profiles"
                    / (
                        f"ch{stage.chapter_ordinal:03d}_b2_canary_profile_"
                        f"{file_sha256(template_path)[:12]}.json"
                    )
                ),
                chapter_id=stage.chapter_id,
                prior_frame_candidate_carry_required=stage.chapter_ordinal > 1,
                interaction_call_count=interaction_call_count,
            )
        if stage.stage_name == "b2_review_routing":
            identity_apply_root = self._receipt_selector(
                ordinal=stage.chapter_ordinal,
                stage_name="identity_apply",
                selector="root",
                strict=False,
            )
            decided_component_ids: list[str] = []
            if identity_apply_root is not None:
                ledger_path = Path(identity_apply_root) / "decision_ledger.json"
                ledger = verify_decision_ledger_v1(
                    _read_json(ledger_path, "cross-chapter decision ledger")
                )
                decided_component_ids = sorted(
                    str(row["component_id"]) for row in ledger["entries"]
                )
                resolved["decision_ledger"] = ledger_path
            resolved["decided_cross_component_ids"] = decided_component_ids
        return resolved

    def expected_calls(self, stage: ChapterCycleStage, resolved: Mapping[str, Any]) -> int:
        name = stage.stage_name
        if not stage.requires_api:
            return 0
        if name in {"b1_scan", "b1_enrich", "b0_summary"}:
            return 1
        if name == "b1_local_auditor":
            runtime = _read_json(
                Path(resolved["runtime_profile"]),
                "local-auditor runtime profile",
            )
            role = _single_role(runtime, "local-auditor runtime profile")
            generation = role["generation"]
            chapter = chapter_from_document_v1(self.document, stage.chapter_id)
            _, batches = plan_b1_enrich_local_audit_batches_v1(
                chapter=chapter,
                scan_artifact=_read_json(
                    Path(resolved["scan_artifact"]), "scan artifact"
                ),
                enrich_artifact=_read_json(
                    Path(resolved["enrich_artifact"]), "enrich artifact"
                ),
                design_doc=DESIGN_DOC,
                prompt_token_cap=int(generation["max_input_tokens"]),
                output_token_cap=int(generation["max_output_tokens"]),
                model_id=str(role["requested_model_id"]),
                reasoning_effort=str(generation["reasoning_effort"]),
                temperature=float(generation["temperature"]),
                seed=int(generation["seed"]),
            )
            return len(batches)
        if name == "xchapter_hearing":
            report = _read_json(
                self.stage_output_root_for(stage, "xchapter_prepare")
                / "dry_run_report.json",
                "cross-chapter prepare report",
            )
            ready_count = int(report["coverage"]["prepared_count"])
            recovered_count = _recoverable_xchapter_component_count_v1(
                self.stage_output_root(stage),
                ready_count=ready_count,
            )
            return ready_count - recovered_count
        if name == "b2_frame_interaction":
            package = _read_json(
                Path(resolved["source_run_root"]) / "b2_registry_input.json",
                "B2 registry input",
            )
            rows = [
                row
                for row in package.get("chapters") or []
                if isinstance(row, Mapping)
                and isinstance(row.get("chapter"), Mapping)
                and row["chapter"].get("chapter_id") == stage.chapter_id
            ]
            if len(rows) != 1:
                raise LiteraryChapterLoopExecutorError(
                    "B2 input does not select exactly one current chapter"
                )
            canary = load_b2_canary_profile_v1(
                Path(resolved["canary_profile"])
            )
            profile = load_b2_phase_a_profile(canary.b2_profile_path)
            return 1 + len(build_b2_windows_v1(rows[0]["chapter"], profile=profile))
        if name == "speaker_recovery":
            return _speaker_recovery_expected_calls_v1(
                b2_root=Path(resolved["b2_root"]),
                output_root=self.stage_output_root(stage),
            )
        if name == "b3_temporal":
            sealed_count = _sealed_b3_request_count_v1(
                output_root=self.stage_output_root(stage),
                chapter_id=stage.chapter_id,
            )
            if sealed_count is not None:
                return sealed_count
            profile = load_b3_temporal_profile_v1(Path(resolved["context_profile"]))
            hearing_root = (
                Path(resolved["identity_hearing_root"])
                if resolved.get("identity_hearing_root")
                else None
            )
            parked = (
                build_parked_identity_index_v2(hearing_root)
                if hearing_root is not None
                else empty_parked_identity_index_v2()
            )
            kwargs: dict[str, Any] = {}
            if resolved.get("speaker_recovery_root"):
                kwargs["speaker_recovery_root"] = Path(
                    resolved["speaker_recovery_root"]
                )
            if hearing_root is not None:
                kwargs["parked_identity_index"] = parked
            temporal_input = load_b2_temporal_input_v1(
                Path(resolved["b2_root"]),
                **kwargs,
            )
            prior_roots = [
                Path(value) for value in (resolved.get("prior_b3_roots") or [])
            ]
            prefix = build_b3_temporal_prefix_v1(prior_roots)
            bundle = build_b3_temporal_cross_chapter_bundle_v7(
                temporal_input=temporal_input,
                profile=profile,
                prior_states=prefix["effective_open_states"],
                prior_pending_cases=prefix["pending_cases"],
            )
            return int(bundle["plan"]["request_count"])
        if name == "b3_auditor":
            artifact = _read_json(
                Path(resolved["b3_root"]) / "chapter_temporal_artifact.json",
                "B3 artifact",
            )
            return len(_serviceable_cases(artifact))
        raise LiteraryChapterLoopExecutorError(
            f"no call-count rule for model stage {name}"
        )

    def build_command(
        self,
        stage: ChapterCycleStage,
        resolved: Mapping[str, Any],
        output_root: Path,
        expected_calls: int,
    ) -> list[str]:
        binding = self._binding(stage)
        if binding.script is None:
            raise LiteraryChapterLoopExecutorError("checkpoint has no subprocess")
        command = [sys.executable, str(RUNTIME_ROOT / binding.script)]
        global_credential_stages = {"b2_frame_interaction", "speaker_recovery"}
        subcommand_credential_stages = {
            "b1_scan",
            "b1_enrich",
            "b1_local_auditor",
            "xchapter_hearing",
            "b3_auditor",
            "b0_summary",
        }
        if stage.stage_name in global_credential_stages:
            command.extend(["--credential-file", str(self.credential_file)])
        if binding.command:
            command.append(binding.command)
        if stage.stage_name in subcommand_credential_stages:
            command.extend(["--credential-file", str(self.credential_file)])
        add = command.extend
        run_id = f"{self.run_id}_{stage.stage_id}"
        attempt_id = f"{run_id}_a1"
        name = stage.stage_name
        if name == "b1_scan":
            add(_pairs(
                output_root=output_root,
                capability_root=resolved["capability_root"],
                chapter=stage.chapter_id,
                document=resolved["document"],
                run_id=run_id,
                attempt_run_id=attempt_id,
                runtime_profile=self.runtime.stages[name].runtime_profile,
                scheduler_root=self.scheduler_root,
            ))
            if resolved.get("prior_cards"):
                add(["--prior-cards", str(resolved["prior_cards"])])
            if resolved.get("previous_summary_root"):
                add(["--previous-summary-root", str(resolved["previous_summary_root"])])
        elif name == "b1_enrich":
            add(_pairs(
                output_root=output_root,
                capability_root=resolved["capability_root"],
                chapter=stage.chapter_id,
                document=resolved["document"],
                scan_artifact=resolved["scan_artifact"],
                run_id=run_id,
                attempt_run_id=attempt_id,
                runtime_profile=self.runtime.stages[name].runtime_profile,
                scheduler_root=self.scheduler_root,
            ))
            if resolved.get("prior_cards"):
                add(["--injected-prior-cards", str(resolved["prior_cards"])])
            if resolved.get("previous_summary_root"):
                add(["--previous-summary-root", str(resolved["previous_summary_root"])])
        elif name == "b1_local_auditor":
            add(_pairs(
                output_root=output_root,
                capability_root=resolved["capability_root"],
                scan_artifact=resolved["scan_artifact"],
                enrich_artifact=resolved["enrich_artifact"],
                chapter=stage.chapter_id,
                document=resolved["document"],
                runtime_profile=resolved["runtime_profile"],
                run_id=run_id,
                attempt_run_id=attempt_id,
                scheduler_root=self.scheduler_root,
            ))
        elif name == "b1_registry_writer":
            add(_pairs(
                output_dir=output_root,
                scan_artifact=resolved["scan_artifact"],
                enrich_artifact=resolved["enrich_artifact"],
                audit_artifact=resolved["audit_artifact"],
                chapter=stage.chapter_id,
                document=resolved["document"],
            ))
            if resolved.get("prior_cards"):
                add(["--prior-cards", str(resolved["prior_cards"])])
            if resolved.get("reconciled_projection"):
                add(["--reconciled-projection", str(resolved["reconciled_projection"])])
        elif name == "xchapter_prepare":
            add(_pairs(
                queue=resolved["queue"],
                registry=resolved["registry"],
                out_dir=output_root,
                model_id=self.runtime.stages["xchapter_hearing"].model_id,
            ))
            for path in resolved["chapters"]:
                add(["--chapter", str(path)])
        elif name == "xchapter_hearing":
            add(_pairs(
                output_root=output_root,
                prepared_dir=resolved["prepared_dir"],
                queue=resolved["queue"],
                registry=resolved["registry"],
                identity_capability_root=resolved["identity_capability_root"],
                stable_claim_capability_root=resolved["stable_claim_capability_root"],
                runtime_profile=resolved["runtime_profile"],
                run_id=run_id,
                attempt_run_id=attempt_id,
                scheduler_root=self.scheduler_root,
            ))
            for path in resolved["chapters"]:
                add(["--chapter", str(path)])
        elif name == "identity_apply":
            add(["--book-id", _document_id(self.document)])
            for path in resolved["registries"]:
                add(["--registry", str(path)])
            add(_pairs(
                queue=resolved["queue"],
                decisions=resolved["decisions"],
                out_dir=output_root,
            ))
            if resolved.get("ledger"):
                add(["--ledger", str(resolved["ledger"])])
        elif name == "b1_to_b2_input":
            for root in resolved["registries"]:
                add(["--registry-root", str(root)])
            add(_pairs(output_root=output_root, document=resolved["document"]))
            if resolved.get("reconciled_projection"):
                add(["--reconciled-projection", str(resolved["reconciled_projection"])])
        elif name == "b2_frame_interaction":
            add(_pairs(
                source_run_root=resolved["source_run_root"],
                output_root=output_root,
                frame_capability_root=resolved["frame_capability_root"],
                interaction_capability_root=resolved["interaction_capability_root"],
                run_id=run_id,
                attempt_run_id=attempt_id,
                canary_profile=resolved["canary_profile"],
                runtime_profile=self.runtime.stages[name].runtime_profile,
                frozen_db=resolved["frozen_db"],
            ))
            if resolved.get("prior_b2_root"):
                add(["--prior-b2-root", str(resolved["prior_b2_root"])])
        elif name == "b2_review_routing":
            add(_pairs(
                b2_root=resolved["b2_root"],
                b2_input_root=resolved["b2_input_root"],
                registry_root=resolved["registry_root"],
                local_audit_root=resolved["local_audit_root"],
                hearing_queue_root=resolved["hearing_queue_root"],
                output_root=output_root,
            ))
            for component_id in resolved.get("decided_cross_component_ids") or []:
                add(["--decided-cross-component-id", str(component_id)])
        elif name == "speaker_recovery":
            add(_pairs(
                b2_root=resolved["b2_root"],
                output_root=output_root,
                capability_root=resolved["capability_root"],
                run_id=run_id,
                attempt_run_id=attempt_id,
                runtime_profile=resolved.get("runtime_profile"),
                frozen_db=resolved["frozen_db"],
            ))
        elif name == "b3_temporal":
            add(_pairs(
                b2_root=resolved["b2_root"],
                output_root=output_root,
                capability_root=resolved["capability_root"],
                context_profile=resolved["context_profile"],
                runtime_profile=resolved["runtime_profile"],
                run_id=run_id,
                attempt_run_id=attempt_id,
                scheduler_root=self.scheduler_root,
                max_calls=max(1, expected_calls),
            ))
            if resolved.get("speaker_recovery_root"):
                add(["--speaker-recovery-root", str(resolved["speaker_recovery_root"])])
            if resolved.get("identity_hearing_root"):
                add(["--identity-hearing-root", str(resolved["identity_hearing_root"])])
            for root in resolved.get("prior_b3_roots") or []:
                add(["--prior-b3-root", str(root)])
        elif name == "b3_auditor":
            # The executor expands this marker to one existing Auditor CLI call
            # per serviceable pending case.
            add(["--per-serviceable-case"])
        elif name == "b3_apply":
            add(_pairs(b3_root=resolved["b3_root"], out_dir=output_root))
            overlays = list(resolved.get("overlays") or [])
            for root in overlays:
                add(["--overlay", str(root)])
            for catalog in resolved.get("prior_component_catalogs") or []:
                add(["--component-catalog", str(catalog)])
            if resolved.get("reconciled_projection"):
                add(["--reconciled-projection", str(resolved["reconciled_projection"])])
            if not overlays and not resolved.get("reconciled_projection"):
                add(["--consolidate-only"])
        elif name == "b0_summary":
            add(_pairs(
                output_root=output_root,
                capability_root=resolved["capability_root"],
                chapter=stage.chapter_id,
                chapter_order=stage.chapter_ordinal,
                document=resolved["document"],
                runtime_profile=resolved["runtime_profile"],
                b1_root=resolved["b1_root"],
                b2_root=resolved["b2_root"],
                b3_root=resolved["b3_root"],
                run_id=run_id,
                attempt_run_id=attempt_id,
                scheduler_root=self.scheduler_root,
            ))
            if resolved.get("speaker_recovery_root"):
                add(["--b2-speaker-recovery-root", str(resolved["speaker_recovery_root"])])
            if resolved.get("prior_summary_root"):
                add(["--prior-summary-root", str(resolved["prior_summary_root"])])
        else:
            raise LiteraryChapterLoopExecutorError(f"unbound stage command: {name}")
        return [str(value) for value in command if value is not None]

    def dry_run_plan(self) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        total_min = 0
        total_max = 0
        completed_ids = {
            str(row.get("stage_id"))
            for row in _read_state(self.run_root).get("stage_receipts") or []
            if isinstance(row, Mapping)
        }
        for raw in self.plan["stage_plan"]:
            stage = _stage_from_plan_row(raw)
            binding = self._binding(stage)
            completed = stage.stage_id in completed_ids
            actual_inputs = (
                self.resolve_inputs(stage, strict=False) if completed else None
            )
            call_min, call_max, call_status = self._dry_call_range(
                stage, actual_inputs
            )
            total_min += call_min
            total_max += call_max
            runtime = self.runtime.stages.get(stage.stage_name)
            limits = (
                _stage_limits_payload(self.plan, stage.stage_role)
                if stage.requires_api
                else None
            )
            rows.append(
                {
                    "stage_id": stage.stage_id,
                    "chapter_id": stage.chapter_id,
                    "stage_name": stage.stage_name,
                    "role": stage.stage_role,
                    "api": stage.requires_api,
                    "conditional": binding.conditional,
                    "condition": binding.condition,
                    "condition_status": (
                        "completed" if completed else (
                            "pending_producer" if binding.conditional else "always"
                        )
                    ),
                    "resolved_inputs": (
                        _public_resolved_inputs(actual_inputs or {})
                        if completed
                        else {
                            name: {
                                "status": "planned",
                                "source": expression,
                            }
                            for name, expression in binding.inputs.items()
                        }
                    ),
                    "output_root": str(self.stage_output_root(stage)),
                    "runtime_binding": (
                        {
                            "source_id": runtime.source_id,
                            "model_id": runtime.model_id,
                            "runtime_profile": (
                                str(runtime.runtime_profile)
                                if runtime.runtime_profile
                                else None
                            ),
                            "context_profile": (
                                str(runtime.context_profile)
                                if runtime.context_profile
                                else None
                            ),
                            "capabilities": {
                                key: str(value)
                                for key, value in runtime.capabilities.items()
                            },
                            "source_match": True,
                        }
                        if runtime
                        else None
                    ),
                    "limits": limits,
                    "expected_calls": {
                        "minimum": call_min,
                        "maximum": call_max,
                        "status": call_status,
                    },
                }
            )
        return {
            "schema_version": "literary_chapter_loop_dry_run_v1",
            "run_id": self.run_id,
            "plan_hash": self.plan["plan_hash"],
            "binding_id": self.runtime.binding_id,
            "chapters": list(self.plan["ordered_chapter_ids"]),
            "stages": rows,
            "totals": {
                "stage_count": len(rows),
                "api_stage_count": sum(1 for row in rows if row["api"]),
                "expected_calls_minimum": total_min,
                "expected_calls_maximum": total_max,
                "exact": total_min == total_max,
            },
            "provider_calls": 0,
        }

    def stage_output_root_for(
        self, stage: ChapterCycleStage, source_stage_name: str
    ) -> Path:
        source_root = (
            self.run_root
            / "artifacts"
            / "chapters"
            / f"ch{stage.chapter_ordinal:03d}"
            / source_stage_name
        )
        root, _ = self._effective_stage_root(
            stage_id=f"ch{stage.chapter_ordinal:03d}_{source_stage_name}",
            source_root=source_root,
        )
        return root

    def bind_effective_stage_roots(
        self,
        effective_roots: Mapping[str, Path],
        *,
        replace_existing: bool = False,
    ) -> dict[str, Any]:
        payload = write_effective_stage_roots_manifest_v1(
            run_root=self.run_root,
            plan=self.plan,
            stage_bindings=self.bindings,
            effective_roots=effective_roots,
            replace_existing=replace_existing,
        )
        self._effective_stage_roots_manifest_sha256 = None
        self._refresh_effective_stage_roots()
        return payload

    def _refresh_effective_stage_roots(self) -> None:
        manifest_path = self.run_root / EFFECTIVE_STAGE_ROOTS_RELATIVE_PATH
        observed_sha256 = (
            file_sha256(manifest_path) if manifest_path.is_file() else None
        )
        if observed_sha256 == self._effective_stage_roots_manifest_sha256:
            return
        payload, roots = load_effective_stage_roots_manifest_v1(
            run_root=self.run_root,
            plan=self.plan,
            stage_bindings=self.bindings,
        )
        self._effective_stage_roots = roots
        self._effective_stage_root_rows = {
            str(row["stage_id"]): row
            for row in ((payload or {}).get("overrides") or [])
        }
        self._effective_stage_roots_manifest_sha256 = observed_sha256

    def _effective_stage_root(
        self, *, stage_id: str, source_root: Path
    ) -> tuple[Path, bool]:
        self._refresh_effective_stage_roots()
        root = self._effective_stage_roots.get(stage_id)
        if root is None:
            return Path(source_root), False
        manifest_path = self.run_root / EFFECTIVE_STAGE_ROOTS_RELATIVE_PATH
        if (
            self._effective_stage_roots_manifest_sha256 is None
            or not manifest_path.is_file()
            or file_sha256(manifest_path)
            != self._effective_stage_roots_manifest_sha256
        ):
            raise LiteraryChapterLoopExecutorError(
                "effective stage-root manifest changed during resolution"
            )
        row = self._effective_stage_root_rows[stage_id]
        stage_name = str(row["stage_name"])
        binding = self.bindings[stage_name]
        _verify_effective_stage_source_v1(
            run_root=self.run_root,
            stage_id=stage_id,
            stage_name=stage_name,
            source_receipt_sha256=row["source_receipt_sha256"],
            source_stage_result_sha256=row["source_stage_result_sha256"],
        )
        _validate_stage_outputs_at_root_v1(
            stage_id=stage_id,
            binding=binding,
            root=root,
        )
        if _directory_fingerprint(root) != row["root_fingerprint"]:
            raise LiteraryChapterLoopExecutorError(
                f"effective stage-root fingerprint differs: {stage_id}"
            )
        return root, True

    def _binding(self, stage: ChapterCycleStage) -> StageBindingV1:
        try:
            binding = self.bindings[stage.stage_name]
        except KeyError as exc:
            raise LiteraryChapterLoopExecutorError(
                f"stage has no binding: {stage.stage_name}"
            ) from exc
        if (
            binding.role != stage.stage_role
            or binding.api != stage.requires_api
        ):
            raise LiteraryChapterLoopExecutorError(
                f"stage binding classification differs: {stage.stage_id}"
            )
        return binding

    def _resolve_expression(
        self,
        *,
        stage: ChapterCycleStage,
        input_name: str,
        expression: str,
        strict: bool,
    ) -> Any:
        for arm in expression.split("|"):
            value = self._resolve_arm(stage=stage, arm=arm, strict=strict)
            if value is not None and value != []:
                return value
        return None

    def _resolve_arm(
        self, *, stage: ChapterCycleStage, arm: str, strict: bool
    ) -> Any:
        if arm == "run.document":
            return self.document_path
        if arm == "run.frozen_db":
            return self.frozen_db
        if arm == "run.chapter_bridges_through_current":
            return [
                self.run_root / "chapter_sources" / f"ch{ordinal:03d}.json"
                for ordinal in range(1, stage.chapter_ordinal + 1)
            ]
        if arm.startswith("runtime."):
            _, runtime_stage, kind, *rest = arm.split(".")
            row = self.runtime.stages[runtime_stage]
            if kind == "runtime_profile":
                return row.runtime_profile
            if kind == "context_profile":
                return row.context_profile
            if kind == "capability" and len(rest) == 1:
                return row.capabilities[rest[0]]
            raise LiteraryChapterLoopExecutorError(f"unknown runtime binding arm: {arm}")
        source, _, selector = arm.partition(":")
        scope, _, source_stage = source.partition(".")
        if not selector:
            raise LiteraryChapterLoopExecutorError(f"binding arm lacks selector: {arm}")
        if scope == "current":
            return self._receipt_selector(
                ordinal=stage.chapter_ordinal,
                stage_name=source_stage,
                selector=selector,
                strict=strict,
            )
        if scope == "previous":
            if stage.chapter_ordinal == 1:
                return None
            return self._receipt_selector(
                ordinal=stage.chapter_ordinal - 1,
                stage_name=source_stage,
                selector=selector,
                strict=strict,
            )
        if scope in {"all", "all_previous"}:
            maximum = (
                stage.chapter_ordinal
                if scope == "all"
                else stage.chapter_ordinal - 1
            )
            values = [
                self._receipt_selector(
                    ordinal=ordinal,
                    stage_name=source_stage,
                    selector=selector,
                    strict=False,
                )
                for ordinal in range(1, maximum + 1)
            ]
            return [value for value in values if value is not None]
        raise LiteraryChapterLoopExecutorError(f"unknown binding scope: {arm}")

    def _receipt_selector(
        self,
        *,
        ordinal: int,
        stage_name: str,
        selector: str,
        strict: bool,
    ) -> Any:
        receipt_path = (
            self.run_root / "receipts" / f"ch{ordinal:03d}_{stage_name}.json"
        )
        if not receipt_path.is_file():
            if strict:
                return None
            return None
        receipt = _read_json(receipt_path, "stage receipt")
        if receipt.get("status") == "skipped":
            return None
        artifact_path = self.run_root / str(receipt["artifact_path"])
        result = _read_json(artifact_path, "stage result")
        payload = result.get("payload")
        if not isinstance(payload, Mapping):
            raise LiteraryChapterLoopExecutorError("stage result payload is malformed")
        output_root = payload.get("output_root")
        if not isinstance(output_root, str) or not output_root:
            raise LiteraryChapterLoopExecutorError("stage result has no output root")
        stage_id = f"ch{ordinal:03d}_{stage_name}"
        root, has_generic_override = self._effective_stage_root(
            stage_id=stage_id,
            source_root=Path(output_root),
        )
        if (
            stage_name == "b1_registry_writer"
            and selector in {"root", "chapter_registry.json", "prior_cards.json"}
        ):
            legacy_roots = self._legacy_b1_correction_roots(ordinal)
            if has_generic_override and any(path.exists() for path in legacy_roots):
                raise LiteraryChapterLoopExecutorError(
                    f"chapter {ordinal} has generic and legacy B1 corrections"
                )
            if not has_generic_override:
                root = self._effective_b1_registry_root(
                    ordinal=ordinal,
                    source_root=root,
                )
        if selector == "root":
            return root
        if selector == "overlays":
            index = _read_json(root / "audit_index.json", "B3 audit index")
            return [
                Path(row["output_root"])
                for row in index.get("cases") or []
                if isinstance(row, Mapping)
                and isinstance(row.get("output_root"), str)
            ]
        return root / selector

    def _effective_b1_registry_root(
        self, *, ordinal: int, source_root: Path
    ) -> Path:
        identity_root, correction_root = self._legacy_b1_correction_roots(ordinal)
        if identity_root.exists() and correction_root.exists():
            raise LiteraryChapterLoopExecutorError(
                f"chapter {ordinal} has multiple effective B1 corrections"
            )
        if identity_root.exists():
            try:
                source_registry = _read_json(
                    source_root / "chapter_registry.json",
                    "source B1 registry",
                )
                corrected_registry = _read_json(
                    identity_root / "chapter_registry.json",
                    "identity-corrected B1 registry",
                )
                corrected_audit = _read_json(
                    identity_root / "corrected_local_audit_artifact.json",
                    "identity-corrected Local Auditor artifact",
                )
                prior_cards = _read_json(
                    identity_root / "prior_cards.json",
                    "identity-corrected prior cards",
                )
                queue = _read_json(
                    identity_root / "cross_chapter_hearing_queue.json",
                    "identity-corrected hearing queue",
                )
                writer_report = _read_json(
                    identity_root / "writer_report.json",
                    "identity-corrected writer report",
                )
                overlay = _read_json(
                    identity_root / "identity_split_correction_overlay.json",
                    "identity split correction overlay",
                )
                receipt = _read_json(
                    identity_root / "identity_split_correction_receipt.json",
                    "identity split correction receipt",
                )
                verify_identity_split_bundle_v1(
                    source_registry=source_registry,
                    corrected_registry=corrected_registry,
                    corrected_local_audit=corrected_audit,
                    prior_cards=prior_cards,
                    queue=queue,
                    writer_report=writer_report,
                    normalized_overlay=overlay,
                    receipt=receipt,
                )
            except (
                LiteraryIdentitySplitCorrectionError,
                OSError,
                ValueError,
            ) as exc:
                raise LiteraryChapterLoopExecutorError(
                    f"chapter {ordinal} identity split correction is invalid: {exc}"
                ) from exc
            return identity_root
        if not correction_root.exists():
            return source_root
        try:
            source_registry = _read_json(
                source_root / "chapter_registry.json",
                "source B1 registry",
            )
            corrected_registry = _read_json(
                correction_root / "chapter_registry.json",
                "corrected B1 registry",
            )
            prior_cards = _read_json(
                correction_root / "prior_cards.json",
                "corrected B1 prior cards",
            )
            overlay = _read_json(
                correction_root / "relation_correction_overlay.json",
                "relation correction overlay",
            )
            receipt = _read_json(
                correction_root / "relation_correction_receipt.json",
                "relation correction receipt",
            )
            verify_relation_correction_bundle_v1(
                source_registry=source_registry,
                corrected_registry=corrected_registry,
                prior_cards=prior_cards,
                normalized_overlay=overlay,
                receipt=receipt,
            )
            for name in (
                "cross_chapter_hearing_queue.json",
                "writer_report.json",
            ):
                source = source_root / name
                corrected = correction_root / name
                if source.is_file() and (
                    not corrected.is_file()
                    or file_sha256(source) != file_sha256(corrected)
                ):
                    raise LiteraryRelationCorrectionError(
                        f"relation correction passthrough differs: {name}"
                    )
        except (LiteraryRelationCorrectionError, OSError, ValueError) as exc:
            raise LiteraryChapterLoopExecutorError(
                f"chapter {ordinal} relation correction is invalid: {exc}"
            ) from exc
        return correction_root

    def _legacy_b1_correction_roots(self, ordinal: int) -> tuple[Path, Path]:
        chapter_root = (
            self.run_root
            / "corrections"
            / "chapters"
            / f"ch{ordinal:03d}"
        )
        return (
            chapter_root / "identity_split_correction",
            chapter_root / "relation_correction",
        )

    def _condition(
        self, stage: ChapterCycleStage, resolved: Mapping[str, Any]
    ) -> tuple[bool, str]:
        name = stage.stage_name
        if name == "xchapter_hearing":
            prepare_root = self._receipt_selector(
                ordinal=stage.chapter_ordinal,
                stage_name="xchapter_prepare",
                selector="root",
                strict=True,
            )
            if prepare_root is None:
                raise LiteraryChapterLoopExecutorError(
                    "cross-chapter prepare receipt is absent"
                )
            report = _read_json(
                Path(prepare_root) / "dry_run_report.json",
                "cross-chapter prepare report",
            )
            count = int(report["coverage"]["prepared_count"])
            return (count > 0, "no_ready_cross_chapter_component")
        if name == "identity_apply":
            receipt = self.run_root / "receipts" / f"ch{stage.chapter_ordinal:03d}_xchapter_hearing.json"
            if not receipt.is_file():
                return False, "cross_chapter_hearing_has_no_receipt"
            return (
                _read_json(receipt, "hearing receipt").get("status") != "skipped",
                "cross_chapter_hearing_skipped",
            )
        if name == "speaker_recovery":
            routing_root = self._receipt_selector(
                ordinal=stage.chapter_ordinal,
                stage_name="b2_review_routing",
                selector="root",
                strict=True,
            )
            if routing_root is None:
                raise LiteraryChapterLoopExecutorError(
                    "B2 review-routing receipt is absent"
                )
            report = _read_json(
                Path(routing_root) / "routing_report.json",
                "B2 routing report",
            )
            return (
                int((report.get("counts") or {}).get("A") or 0) > 0,
                "no_route_a_review",
            )
        if name == "b3_auditor":
            artifact = _read_json(
                Path(resolved["b3_root"]) / "chapter_temporal_artifact.json",
                "B3 artifact",
            )
            return (
                bool(_serviceable_cases(artifact)),
                "no_serviceable_b3_review_case",
            )
        return True, ""

    def _run_stage(
        self,
        stage: ChapterCycleStage,
        command: Sequence[str],
        output_root: Path,
    ) -> subprocess.CompletedProcess[str]:
        recovery_root: Path | None = None
        if output_root.exists() or _shared_output_root(output_root).exists():
            archive_root = self._archive_incomplete_stage_output(stage, output_root)
            if stage.stage_name == "b3_auditor":
                archived_output = archive_root / "output"
                if archived_output.is_dir():
                    recovery_root = archived_output
            elif stage.stage_name == "xchapter_hearing":
                archived_output = archive_root / "output"
                if archived_output.is_dir():
                    recovery_root = archived_output
        output_root.parent.mkdir(parents=True, exist_ok=True)
        if stage.stage_name == "b3_auditor":
            return self._run_b3_auditor(
                stage,
                output_root,
                recovery_root=recovery_root,
            )
        environment = _runtime_environment()
        if stage.stage_name == "b3_temporal":
            environment["OPENAI_API_KEY"] = _credential_value(self.credential_file)
        rerun_command = list(command)
        if stage.stage_name == "xchapter_hearing" and recovery_root is not None:
            rerun_command.extend(["--recovery-root", str(recovery_root)])
        completed = subprocess.run(
            rerun_command,
            cwd=RUNTIME_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self._write_process_logs(stage, completed)
        if completed.returncode != 0:
            self.history.emit(
                "validation_failed",
                stage=stage.stage_id,
                agent=_agent(stage.stage_name),
                script=self._binding(stage).script or "",
                severity="error",
                payload={
                    "exit_code": completed.returncode,
                    "stderr_tail": completed.stderr[-2000:],
                },
            )
            raise ChapterCycleStagePause(
                "whole_response_contract",
                f"{stage.stage_id} exited {completed.returncode}",
            )
        return completed

    def _archive_incomplete_stage_output(
        self,
        stage: ChapterCycleStage,
        output_root: Path,
    ) -> Path:
        root = Path(output_root).resolve()
        candidates = {
            "output": root,
            "shared_output": _shared_output_root(root),
        }
        existing_roots = {
            label: path for label, path in candidates.items() if path.exists()
        }
        if not existing_roots:
            raise ChapterCycleStagePause(
                "integrity_or_lineage",
                f"incomplete stage output is absent: {stage.stage_id}",
            )
        for path in existing_roots.values():
            if not path.is_dir() or path.is_symlink():
                raise ChapterCycleStagePause(
                    "integrity_or_lineage",
                    f"incomplete stage output is not a real directory: {path}",
                )
        receipt = self.run_root / "receipts" / f"{stage.stage_id}.json"
        result = self.run_root / "stages" / stage.stage_id / "stage_result.json"
        if receipt.exists() or result.exists():
            raise ChapterCycleStagePause(
                "integrity_or_lineage",
                f"finalized stage output cannot be archived: {stage.stage_id}",
            )
        root_fingerprints = {
            label: _directory_fingerprint(path)
            for label, path in existing_roots.items()
        }
        fingerprint = canonical_hash(root_fingerprints)
        archive_parent = (
            self.run_root / "operational_attempts" / stage.stage_id
        )
        archive_parent.mkdir(parents=True, exist_ok=True)
        attempt_index = (
            len(
                [
                    path
                    for path in archive_parent.iterdir()
                    if path.is_dir() and path.name.startswith("attempt_")
                ]
            )
            + 1
        )
        archive_root = archive_parent / (
            f"attempt_{attempt_index:03d}_{fingerprint[:12]}"
        )
        if archive_root.exists():
            raise ChapterCycleStagePause(
                "integrity_or_lineage",
                f"incomplete attempt archive already exists: {archive_root}",
            )
        archive_root.mkdir(parents=False, exist_ok=False)
        for label, path in existing_roots.items():
            os.replace(path, archive_root / label)
        metadata = {
            "schema_version": "literary_incomplete_stage_attempt_v1",
            "stage_id": stage.stage_id,
            "chapter_id": stage.chapter_id,
            "source_output_roots": {
                label: str(path) for label, path in existing_roots.items()
            },
            "root_fingerprints": root_fingerprints,
            "content_fingerprint": fingerprint,
            "replay_visible": False,
            "production_publish_performed": False,
        }
        (archive_root / "incomplete_attempt.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        return archive_root

    def _run_b3_auditor(
        self,
        stage: ChapterCycleStage,
        output_root: Path,
        *,
        recovery_root: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        b3_root = self.stage_output_root_for(stage, "b3_temporal")
        artifact = _read_json(
            b3_root / "chapter_temporal_artifact.json",
            "B3 artifact",
        )
        cases = _serviceable_cases(artifact)
        output_root.mkdir(parents=True, exist_ok=False)
        case_rows: list[dict[str, Any]] = []
        quarantined_rows: list[dict[str, Any]] = []
        all_stdout: list[str] = []
        all_stderr: list[str] = []
        recovered_case_count = 0
        recovered_quarantined_case_count = 0
        runtime = self.runtime.stages["b3_auditor"]
        for index, case in enumerate(cases, start=1):
            case_id = str(case["pending_case_id"])
            case_root = output_root / "overlays" / f"case_{index:03d}"
            if recovery_root is not None and self._recover_b3_auditor_case(
                recovery_root=recovery_root,
                output_root=output_root,
                case_index=index,
                case_id=case_id,
                source_b3_artifact_hash=str(artifact["artifact_hash"]),
            ):
                recovered_case_count += 1
                all_stdout.append(
                    json.dumps(
                        {
                            "status": "recovered",
                            "pending_case_id": case_id,
                        },
                        sort_keys=True,
                    )
                )
                case_rows.append(
                    {
                        "pending_case_id": case_id,
                        "review_route": case["review_route"],
                        "output_root": str(case_root),
                        "overlay": str(
                            case_root / "temporal_review_overlay.json"
                        ),
                        "audit_report": str(case_root / "audit_report.json"),
                    }
                )
                continue
            if recovery_root is not None and self._recover_b3_auditor_quarantine(
                recovery_root=recovery_root,
                output_root=output_root,
                case_index=index,
                case_id=case_id,
                source_b3_artifact_hash=str(artifact["artifact_hash"]),
            ):
                recovered_quarantined_case_count += 1
                quarantine = self._load_b3_auditor_quarantine(
                    case_root=case_root,
                    case_id=case_id,
                    source_b3_artifact_hash=str(artifact["artifact_hash"]),
                )
                if quarantine is None:
                    raise ChapterCycleStagePause(
                        "integrity_or_lineage",
                        f"recovered B3 quarantine disappeared: {case_id}",
                    )
                quarantined_rows.append(quarantine)
                all_stdout.append(
                    json.dumps(
                        {
                            "status": "recovered_quarantine",
                            "pending_case_id": case_id,
                        },
                        sort_keys=True,
                    )
                )
                continue
            command = [
                sys.executable,
                str(RUNTIME_ROOT / self.bindings["b3_auditor"].script),
                "audit",
                "--credential-file",
                str(self.credential_file),
                "--b3-root",
                str(b3_root),
                "--pending-case-id",
                case_id,
                "--output-root",
                str(case_root),
                "--capability-root",
                str(runtime.capabilities["default"]),
                "--run-id",
                f"{self.run_id}_{stage.stage_id}_{index:03d}",
                "--attempt-run-id",
                f"{self.run_id}_{stage.stage_id}_{index:03d}_a1",
                "--scheduler-root",
                str(self.scheduler_root),
            ]
            if runtime.runtime_profile:
                command.extend(["--runtime-profile", str(runtime.runtime_profile)])
            completed = subprocess.run(
                command,
                cwd=RUNTIME_ROOT,
                env=_runtime_environment(),
                capture_output=True,
                text=True,
                check=False,
            )
            all_stdout.append(completed.stdout)
            all_stderr.append(completed.stderr)
            if completed.returncode != 0:
                quarantine = self._load_b3_auditor_quarantine(
                    case_root=case_root,
                    case_id=case_id,
                    source_b3_artifact_hash=str(artifact["artifact_hash"]),
                )
                if quarantine is not None:
                    quarantined_rows.append(quarantine)
                    all_stdout.append(
                        json.dumps(
                            {
                                "status": "quarantined",
                                "pending_case_id": case_id,
                                "validator_error": quarantine["validator_error"],
                            },
                            sort_keys=True,
                        )
                    )
                    continue
                self._write_process_logs(
                    stage,
                    subprocess.CompletedProcess(
                        args=command,
                        returncode=completed.returncode,
                        stdout="\n".join(all_stdout),
                        stderr="\n".join(all_stderr),
                    ),
                )
                raise ChapterCycleStagePause(
                    "whole_response_contract",
                    f"{stage.stage_id} case {case_id} exited {completed.returncode}",
                )
            case_rows.append(
                {
                    "pending_case_id": case_id,
                    "review_route": case["review_route"],
                    "output_root": str(case_root),
                    "overlay": str(case_root / "temporal_review_overlay.json"),
                    "audit_report": str(case_root / "audit_report.json"),
                }
            )
        index_body = {
            "schema_version": "literary_b3_audit_index_v1",
            "chapter_id": stage.chapter_id,
            "case_count": len(case_rows),
            "serviceable_case_count": len(cases),
            "cases": case_rows,
            "quarantined_case_count": len(quarantined_rows),
            "quarantined_cases": quarantined_rows,
            "provider_calls": len(cases),
            "recovered_case_count": recovered_case_count,
            "recovered_quarantined_case_count": recovered_quarantined_case_count,
            "new_provider_calls": (
                len(cases)
                - recovered_case_count
                - recovered_quarantined_case_count
            ),
        }
        (output_root / "audit_index.json").write_text(
            json.dumps(
                {**index_body, "index_hash": canonical_hash(index_body)},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        aggregate = subprocess.CompletedProcess(
            args=["b3_auditor", *[row["pending_case_id"] for row in case_rows]],
            returncode=0,
            stdout="\n".join(all_stdout),
            stderr="\n".join(all_stderr),
        )
        self._write_process_logs(stage, aggregate)
        return aggregate

    def _load_b3_auditor_quarantine(
        self,
        *,
        case_root: Path,
        case_id: str,
        source_b3_artifact_hash: str,
    ) -> dict[str, Any] | None:
        diagnostic_path = case_root / "semantic_rejection.json"
        if not diagnostic_path.is_file():
            return None
        diagnostic = _read_json(
            diagnostic_path,
            "B3 auditor semantic rejection diagnostic",
        )
        if diagnostic.get("schema_version") != (
            "literary_semantic_rejection_diagnostic_v1"
        ):
            raise ChapterCycleStagePause(
                "integrity_or_lineage",
                f"B3 quarantine diagnostic schema differs: {case_id}",
            )
        if diagnostic.get("semantic_status") != "rejected":
            raise ChapterCycleStagePause(
                "integrity_or_lineage",
                f"B3 quarantine diagnostic is not a rejection: {case_id}",
            )
        if diagnostic.get("semantic_authority_granted") is True:
            raise ChapterCycleStagePause(
                "integrity_or_lineage",
                f"B3 rejected case claims authority: {case_id}",
            )
        if diagnostic.get("production_publish_performed") is True:
            raise ChapterCycleStagePause(
                "integrity_or_lineage",
                f"B3 rejected case claims publication: {case_id}",
            )
        validator_error = diagnostic.get("validator_error")
        if not isinstance(validator_error, Mapping):
            raise ChapterCycleStagePause(
                "integrity_or_lineage",
                f"B3 quarantine has no validator error: {case_id}",
            )
        packet_path = case_root / "review_packet.json"
        if not packet_path.is_file():
            raise ChapterCycleStagePause(
                "integrity_or_lineage",
                f"B3 quarantine packet is absent: {case_id}",
            )
        packet = verify_b3_temporal_review_packet_v1(
            _read_json(packet_path, "B3 quarantined review packet")
        )
        packet_case_ids = [
            str(row["pending_case_id"])
            for row in packet["pending_cases"]
        ]
        if packet_case_ids != [case_id]:
            raise ChapterCycleStagePause(
                "integrity_or_lineage",
                f"B3 quarantine case identity differs: {case_id}",
            )
        if packet.get("source_b3_artifact_hash") != source_b3_artifact_hash:
            raise ChapterCycleStagePause(
                "integrity_or_lineage",
                f"B3 quarantine base differs: {case_id}",
            )
        return {
            "pending_case_id": case_id,
            "review_route": str(
                packet["pending_cases"][0].get("review_route") or ""
            ),
            "output_root": str(case_root),
            "semantic_rejection": str(diagnostic_path),
            "validator_error": {
                "error_type": str(validator_error.get("error_type") or ""),
                "message": str(validator_error.get("message") or ""),
            },
        }

    def _recover_b3_auditor_quarantine(
        self,
        *,
        recovery_root: Path,
        output_root: Path,
        case_index: int,
        case_id: str,
        source_b3_artifact_hash: str,
    ) -> bool:
        source_case = (
            recovery_root / "overlays" / f"case_{case_index:03d}"
        )
        if self._load_b3_auditor_quarantine(
            case_root=source_case,
            case_id=case_id,
            source_b3_artifact_hash=source_b3_artifact_hash,
        ) is None:
            return False
        destination_case = (
            output_root / "overlays" / f"case_{case_index:03d}"
        )
        destination_case.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_case, destination_case)
        source_shared = source_case.with_name(f"{source_case.name}-shared")
        if source_shared.is_dir():
            shutil.copytree(
                source_shared,
                destination_case.with_name(f"{destination_case.name}-shared"),
            )
        return True

    def _recover_b3_auditor_case(
        self,
        *,
        recovery_root: Path,
        output_root: Path,
        case_index: int,
        case_id: str,
        source_b3_artifact_hash: str,
    ) -> bool:
        source_case = (
            recovery_root / "overlays" / f"case_{case_index:03d}"
        )
        overlay_path = source_case / "temporal_review_overlay.json"
        if not overlay_path.is_file():
            return False
        packet_path = source_case / "review_packet.json"
        report_path = source_case / "audit_report.json"
        if not packet_path.is_file() or not report_path.is_file():
            raise ChapterCycleStagePause(
                "integrity_or_lineage",
                f"recoverable B3 auditor case is incomplete: {case_id}",
            )
        packet = verify_b3_temporal_review_packet_v1(
            _read_json(packet_path, "recovered B3 review packet")
        )
        packet_case_ids = [
            str(row["pending_case_id"])
            for row in packet["pending_cases"]
        ]
        if packet_case_ids != [case_id]:
            raise ChapterCycleStagePause(
                "integrity_or_lineage",
                f"recovered B3 auditor case identity differs: {case_id}",
            )
        if packet.get("source_b3_artifact_hash") != source_b3_artifact_hash:
            raise ChapterCycleStagePause(
                "integrity_or_lineage",
                f"recovered B3 auditor base differs: {case_id}",
            )
        overlay = _read_json(overlay_path, "recovered B3 review overlay")
        verify_b3_temporal_review_overlay_v1(overlay, packet=packet)
        raw_decision = dict(overlay["decision"])
        raw_decision.pop("decision_hash", None)
        raw_decision.pop("packet_hash", None)
        raw_decision.pop("response_normalization_notes", None)
        normalized = validate_b3_temporal_audit_response_v1(
            packet=packet,
            response=raw_decision,
        )
        rebuilt = build_b3_temporal_review_overlay_v1(
            packet=packet,
            decision=normalized,
        )
        if rebuilt != overlay:
            raise ChapterCycleStagePause(
                "integrity_or_lineage",
                f"recovered B3 auditor overlay differs under current contract: {case_id}",
            )
        report = _read_json(report_path, "recovered B3 audit report")
        if (
            report.get("status") != "semantic_accepted"
            or report.get("overlay_hash") != overlay.get("overlay_hash")
            or report.get("production_publish_performed") is True
        ):
            raise ChapterCycleStagePause(
                "integrity_or_lineage",
                f"recovered B3 auditor report differs: {case_id}",
            )

        destination_case = (
            output_root / "overlays" / f"case_{case_index:03d}"
        )
        destination_case.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_case, destination_case)
        source_shared = source_case.with_name(f"{source_case.name}-shared")
        if source_shared.is_dir():
            shutil.copytree(
                source_shared,
                destination_case.with_name(f"{destination_case.name}-shared"),
            )
        return True

    def _validate_report(
        self, stage: ChapterCycleStage, report: Mapping[str, Any]
    ) -> None:
        if report.get("production_publish_performed") is True:
            raise ChapterCycleStagePause(
                "integrity_or_lineage",
                f"{stage.stage_id} claims production publication",
            )
        status = report.get("status")
        rejected = {
            "failed",
            "rejected",
            "preflight_rejected",
            "local_validator_rejected",
            "pending_contract_conflict",
        }
        if isinstance(status, str) and status in rejected:
            raise ChapterCycleStagePause(
                "whole_response_contract",
                f"{stage.stage_id} report status is {status}",
            )

    def _validate_required_outputs(
        self,
        stage: ChapterCycleStage,
        binding: StageBindingV1,
        output_root: Path,
    ) -> None:
        directory_outputs = {"prepared_requests", "overlays"}
        missing = []
        for name in binding.outputs:
            path = output_root / name
            if name in directory_outputs:
                if not path.is_dir():
                    missing.append(name)
            elif not path.is_file():
                missing.append(name)
        if missing:
            raise ChapterCycleStagePause(
                "integrity_or_lineage",
                f"{stage.stage_id} omitted outputs: {','.join(missing)}",
            )

    def _write_command_record(
        self,
        *,
        stage: ChapterCycleStage,
        command: Sequence[str],
        resolved: Mapping[str, Any],
        expected_calls: int,
    ) -> Path:
        command_root = self.run_root / "commands"
        command_root.mkdir(parents=True, exist_ok=True)
        path = command_root / f"{stage.stage_id}.json"
        safe_command = [
            "<credential-file>" if index > 0 and command[index - 1] == "--credential-file"
            else value
            for index, value in enumerate(command)
        ]
        body = {
            "schema_version": "literary_chapter_loop_command_v1",
            "stage_id": stage.stage_id,
            "chapter_id": stage.chapter_id,
            "command": safe_command,
            "resolved_inputs": _public_resolved_inputs(resolved),
            "expected_calls": expected_calls,
            "retry_allowed": False,
            "fallback_allowed": False,
        }
        payload = {**body, "command_hash": canonical_hash(body)}
        if path.exists():
            existing = _read_json(path, "command record")
            if existing != payload:
                receipt = (
                    self.run_root / "receipts" / f"{stage.stage_id}.json"
                )
                result = (
                    self.run_root
                    / "stages"
                    / stage.stage_id
                    / "stage_result.json"
                )
                if (
                    receipt.exists()
                    or result.exists()
                    or not _approved_retry_command_change(existing, payload)
                ):
                    raise ChapterCycleStagePause(
                        "integrity_or_lineage",
                        f"command record drifted: {stage.stage_id}",
                    )
                archive = (
                    self.run_root
                    / "operational_attempts"
                    / stage.stage_id
                    / "command_records"
                )
                archive.mkdir(parents=True, exist_ok=True)
                archived_path = archive / (
                    f"command_{existing['command_hash'][:16]}.json"
                )
                if archived_path.exists():
                    if _read_json(
                        archived_path, "archived command record"
                    ) != existing:
                        raise ChapterCycleStagePause(
                            "integrity_or_lineage",
                            f"archived command record drifted: {stage.stage_id}",
                        )
                else:
                    archived_path.write_text(
                        json.dumps(
                            existing,
                            ensure_ascii=False,
                            indent=2,
                            sort_keys=True,
                        )
                        + "\n",
                        encoding="utf-8",
                    )
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return path

    def _write_process_logs(
        self,
        stage: ChapterCycleStage,
        completed: subprocess.CompletedProcess[str],
    ) -> None:
        root = self.run_root / "logs"
        root.mkdir(parents=True, exist_ok=True)
        (root / f"{stage.stage_id}.stdout.log").write_text(
            completed.stdout or "", encoding="utf-8"
        )
        (root / f"{stage.stage_id}.stderr.log").write_text(
            completed.stderr or "", encoding="utf-8"
        )

    def _dry_call_range(
        self,
        stage: ChapterCycleStage,
        resolved: Mapping[str, Any] | None,
    ) -> tuple[int, int, str]:
        if not stage.requires_api:
            return 0, 0, "exact"
        if resolved is not None:
            try:
                value = self.expected_calls(stage, resolved)
                return value, value, "exact"
            except Exception:
                pass
        cap = int(self.plan["logical_call_caps_by_role"][stage.stage_role])
        if self.bindings[stage.stage_name].conditional:
            return 0, cap, "conditional_until_inputs_exist"
        if stage.stage_name in {"b1_scan", "b1_enrich", "b0_summary"}:
            return 1, 1, "exact"
        if stage.stage_name == "speaker_recovery":
            return 0, 1, "conditional_until_inputs_exist"
        return 1, cap, "bounded_until_inputs_exist"


def load_effective_stage_roots_manifest_v1(
    *,
    run_root: Path,
    plan: Mapping[str, Any],
    stage_bindings: Mapping[str, StageBindingV1],
) -> tuple[dict[str, Any] | None, dict[str, Path]]:
    root = Path(run_root).resolve()
    manifest_path = root / EFFECTIVE_STAGE_ROOTS_RELATIVE_PATH
    if not manifest_path.is_file():
        return None, {}
    payload = _read_json(manifest_path, "effective stage-root manifest")
    if set(payload) != {
        "schema_version",
        "plan_hash",
        "previous_manifest_hash",
        "overrides",
        "manifest_hash",
    }:
        raise LiteraryChapterLoopExecutorError(
            "effective stage-root manifest key set is not closed"
        )
    body = dict(payload)
    observed_hash = body.pop("manifest_hash", None)
    if (
        payload.get("schema_version") != EFFECTIVE_STAGE_ROOTS_SCHEMA
        or canonical_hash(body) != observed_hash
    ):
        raise LiteraryChapterLoopExecutorError(
            "effective stage-root manifest seal is invalid"
        )
    if payload.get("plan_hash") != plan.get("plan_hash"):
        raise LiteraryChapterLoopExecutorError(
            "effective stage-root manifest belongs to another plan"
        )
    previous_hash = payload.get("previous_manifest_hash")
    if previous_hash is not None:
        if not isinstance(previous_hash, str) or len(previous_hash) != 64:
            raise LiteraryChapterLoopExecutorError(
                "effective stage-root predecessor hash is malformed"
            )
        archive_path = (
            root
            / "corrections"
            / "effective_stage_root_manifests"
            / f"{previous_hash}.json"
        )
        archived = _read_json(archive_path, "effective stage-root predecessor")
        archived_body = dict(archived)
        archived_hash = archived_body.pop("manifest_hash", None)
        if (
            archived_hash != previous_hash
            or canonical_hash(archived_body) != previous_hash
        ):
            raise LiteraryChapterLoopExecutorError(
                "effective stage-root predecessor is invalid"
            )
    raw_rows = payload.get("overrides")
    if not isinstance(raw_rows, list):
        raise LiteraryChapterLoopExecutorError(
            "effective stage-root overrides must be a list"
        )
    stage_index = _plan_stage_index_v1(plan)
    resolved: dict[str, Path] = {}
    observed_order: list[str] = []
    for row in raw_rows:
        if not isinstance(row, Mapping) or set(row) != {
            "stage_id",
            "stage_name",
            "chapter_ordinal",
            "root_relative_path",
            "root_fingerprint",
            "required_outputs",
            "source_receipt_sha256",
            "source_stage_result_sha256",
        }:
            raise LiteraryChapterLoopExecutorError(
                "effective stage-root row key set is not closed"
            )
        stage_id = row.get("stage_id")
        if not isinstance(stage_id, str) or stage_id in resolved:
            raise LiteraryChapterLoopExecutorError(
                "effective stage-root rows repeat or omit a stage id"
            )
        observed_order.append(stage_id)
        plan_row = stage_index.get(stage_id)
        if plan_row is None:
            raise LiteraryChapterLoopExecutorError(
                f"effective stage-root cites an unknown stage: {stage_id}"
            )
        stage_name = plan_row["stage_name"]
        chapter_ordinal = plan_row["chapter_ordinal"]
        if (
            row.get("stage_name") != stage_name
            or row.get("chapter_ordinal") != chapter_ordinal
        ):
            raise LiteraryChapterLoopExecutorError(
                f"effective stage-root identity differs: {stage_id}"
            )
        binding = stage_bindings.get(str(stage_name))
        if binding is None:
            raise LiteraryChapterLoopExecutorError(
                f"effective stage-root has no stage binding: {stage_id}"
            )
        required_outputs = row.get("required_outputs")
        if required_outputs != list(binding.outputs):
            raise LiteraryChapterLoopExecutorError(
                f"effective stage-root output contract differs: {stage_id}"
            )
        _verify_effective_stage_source_v1(
            run_root=root,
            stage_id=stage_id,
            stage_name=str(stage_name),
            source_receipt_sha256=row.get("source_receipt_sha256"),
            source_stage_result_sha256=row.get("source_stage_result_sha256"),
        )
        effective_root = _component_relative_root_v1(
            root,
            row.get("root_relative_path"),
            label=f"effective stage-root {stage_id}",
        )
        _validate_stage_outputs_at_root_v1(
            stage_id=stage_id,
            binding=binding,
            root=effective_root,
        )
        if _directory_fingerprint(effective_root) != row.get("root_fingerprint"):
            raise LiteraryChapterLoopExecutorError(
                f"effective stage-root fingerprint differs: {stage_id}"
            )
        resolved[stage_id] = effective_root
    if observed_order != sorted(observed_order):
        raise LiteraryChapterLoopExecutorError(
            "effective stage-root rows are not deterministically ordered"
        )
    return payload, resolved


def write_effective_stage_roots_manifest_v1(
    *,
    run_root: Path,
    plan: Mapping[str, Any],
    stage_bindings: Mapping[str, StageBindingV1],
    effective_roots: Mapping[str, Path],
    replace_existing: bool = False,
) -> dict[str, Any]:
    if not effective_roots:
        raise LiteraryChapterLoopExecutorError(
            "effective stage-root update is empty"
        )
    root = Path(run_root).resolve()
    existing, _ = load_effective_stage_roots_manifest_v1(
        run_root=root,
        plan=plan,
        stage_bindings=stage_bindings,
    )
    rows_by_stage = {
        str(row["stage_id"]): dict(row)
        for row in ((existing or {}).get("overrides") or [])
    }
    stage_index = _plan_stage_index_v1(plan)
    changed = False
    for stage_id, supplied_root in effective_roots.items():
        plan_row = stage_index.get(stage_id)
        if plan_row is None:
            raise LiteraryChapterLoopExecutorError(
                f"effective stage-root cites an unknown stage: {stage_id}"
            )
        stage_name = str(plan_row["stage_name"])
        binding = stage_bindings.get(stage_name)
        if binding is None:
            raise LiteraryChapterLoopExecutorError(
                f"effective stage-root has no stage binding: {stage_id}"
            )
        effective_root = Path(supplied_root).resolve()
        try:
            relative_root = effective_root.relative_to(root).as_posix()
        except ValueError as exc:
            raise LiteraryChapterLoopExecutorError(
                f"effective stage-root is outside the component: {stage_id}"
            ) from exc
        _validate_stage_outputs_at_root_v1(
            stage_id=stage_id,
            binding=binding,
            root=effective_root,
        )
        source_receipt_sha256, source_result_sha256 = (
            _verify_effective_stage_source_v1(
                run_root=root,
                stage_id=stage_id,
                stage_name=stage_name,
            )
        )
        row = {
            "stage_id": stage_id,
            "stage_name": stage_name,
            "chapter_ordinal": int(plan_row["chapter_ordinal"]),
            "root_relative_path": relative_root,
            "root_fingerprint": _directory_fingerprint(effective_root),
            "required_outputs": list(binding.outputs),
            "source_receipt_sha256": source_receipt_sha256,
            "source_stage_result_sha256": source_result_sha256,
        }
        current = rows_by_stage.get(stage_id)
        if current == row:
            continue
        if current is not None and not replace_existing:
            raise LiteraryChapterLoopExecutorError(
                f"effective stage-root already exists: {stage_id}"
            )
        rows_by_stage[stage_id] = row
        changed = True
    if existing is not None and not changed:
        return existing
    previous_hash = existing.get("manifest_hash") if existing else None
    if existing is not None:
        archive_path = (
            root
            / "corrections"
            / "effective_stage_root_manifests"
            / f"{previous_hash}.json"
        )
        if archive_path.exists():
            if _read_json(archive_path, "effective stage-root archive") != existing:
                raise LiteraryChapterLoopExecutorError(
                    "effective stage-root archive differs"
                )
        else:
            _write_json_atomic_v1(archive_path, existing)
    body = {
        "schema_version": EFFECTIVE_STAGE_ROOTS_SCHEMA,
        "plan_hash": plan["plan_hash"],
        "previous_manifest_hash": previous_hash,
        "overrides": [rows_by_stage[key] for key in sorted(rows_by_stage)],
    }
    payload = {**body, "manifest_hash": canonical_hash(body)}
    _write_json_atomic_v1(root / EFFECTIVE_STAGE_ROOTS_RELATIVE_PATH, payload)
    load_effective_stage_roots_manifest_v1(
        run_root=root,
        plan=plan,
        stage_bindings=stage_bindings,
    )
    return payload


def _plan_stage_index_v1(plan: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    rows = plan.get("stage_plan")
    if not isinstance(rows, list):
        raise LiteraryChapterLoopExecutorError("chapter-loop stage plan is malformed")
    result: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("stage_id"), str):
            raise LiteraryChapterLoopExecutorError(
                "chapter-loop stage descriptor is malformed"
            )
        stage_id = str(row["stage_id"])
        if stage_id in result:
            raise LiteraryChapterLoopExecutorError(
                "chapter-loop stage plan repeats a stage"
            )
        result[stage_id] = row
    return result


def _verify_effective_stage_source_v1(
    *,
    run_root: Path,
    stage_id: str,
    stage_name: str,
    source_receipt_sha256: Any = None,
    source_stage_result_sha256: Any = None,
) -> tuple[str, str]:
    receipt_path = run_root / "receipts" / f"{stage_id}.json"
    receipt = _read_json(receipt_path, f"source receipt {stage_id}")
    receipt_sha256 = file_sha256(receipt_path)
    if source_receipt_sha256 is not None and source_receipt_sha256 != receipt_sha256:
        raise LiteraryChapterLoopExecutorError(
            f"effective stage-root source receipt differs: {stage_id}"
        )
    if receipt.get("stage_id") != stage_id or receipt.get("status") != "accepted":
        raise LiteraryChapterLoopExecutorError(
            f"effective stage-root source receipt is not accepted: {stage_id}"
        )
    result_path = _component_relative_root_v1(
        run_root,
        receipt.get("artifact_path"),
        label=f"source stage result {stage_id}",
        require_directory=False,
    )
    if not result_path.is_file():
        raise LiteraryChapterLoopExecutorError(
            f"effective stage-root source result is absent: {stage_id}"
        )
    result_sha256 = file_sha256(result_path)
    if (
        receipt.get("artifact_sha256") != result_sha256
        or (
            source_stage_result_sha256 is not None
            and source_stage_result_sha256 != result_sha256
        )
    ):
        raise LiteraryChapterLoopExecutorError(
            f"effective stage-root source result differs: {stage_id}"
        )
    result = _read_json(result_path, f"source stage result {stage_id}")
    descriptor = result.get("stage_descriptor")
    if (
        not isinstance(descriptor, Mapping)
        or descriptor.get("stage_id") != stage_id
        or descriptor.get("stage_name") != stage_name
    ):
        raise LiteraryChapterLoopExecutorError(
            f"effective stage-root source identity differs: {stage_id}"
        )
    return receipt_sha256, result_sha256


def _component_relative_root_v1(
    run_root: Path,
    raw_relative_path: Any,
    *,
    label: str,
    require_directory: bool = True,
) -> Path:
    if not isinstance(raw_relative_path, str) or not raw_relative_path:
        raise LiteraryChapterLoopExecutorError(f"{label} path is malformed")
    relative = Path(raw_relative_path)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or raw_relative_path != relative.as_posix()
    ):
        raise LiteraryChapterLoopExecutorError(f"{label} path is not canonical")
    target = (Path(run_root).resolve() / relative).resolve()
    try:
        target.relative_to(Path(run_root).resolve())
    except ValueError as exc:
        raise LiteraryChapterLoopExecutorError(
            f"{label} is outside the component"
        ) from exc
    if require_directory and (not target.is_dir() or target.is_symlink()):
        raise LiteraryChapterLoopExecutorError(f"{label} is not a real directory")
    return target


def _validate_stage_outputs_at_root_v1(
    *, stage_id: str, binding: StageBindingV1, root: Path
) -> None:
    if not Path(root).is_dir() or Path(root).is_symlink():
        raise LiteraryChapterLoopExecutorError(
            f"effective stage-root is not a real directory: {stage_id}"
        )
    directory_outputs = {"prepared_requests", "overlays"}
    missing = []
    for name in binding.outputs:
        path = Path(root) / name
        if name in directory_outputs:
            if not path.is_dir() or path.is_symlink():
                missing.append(name)
        elif not path.is_file() or path.is_symlink():
            missing.append(name)
    if missing:
        raise LiteraryChapterLoopExecutorError(
            f"effective stage-root omits outputs for {stage_id}: {','.join(missing)}"
        )


def _write_json_atomic_v1(path: Path, value: Mapping[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(f"{target.suffix}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, target)


def write_chapter_bridge_files_v1(
    *, run_root: Path, document: Mapping[str, Any], ordered_chapter_ids: Sequence[str]
) -> list[Path]:
    root = Path(run_root).resolve() / "chapter_sources"
    root.mkdir(parents=True, exist_ok=True)
    result: list[Path] = []
    for ordinal, chapter_id in enumerate(ordered_chapter_ids, start=1):
        chapter = chapter_from_document_v1(document, chapter_id)
        blocks = []
        for row in chapter["blocks"]:
            text = row.get("clean_text")
            if not isinstance(text, str):
                raise LiteraryChapterLoopExecutorError(
                    f"chapter block lacks clean_text: {chapter_id}"
                )
            blocks.append({"block_id": row["block_id"], "text": text})
        body = {"chapter_id": chapter_id, "blocks": blocks}
        payload = {**body, "chapter_bridge_hash": canonical_hash(body)}
        path = root / f"ch{ordinal:03d}.json"
        if path.exists() and _read_json(path, "chapter bridge") != payload:
            raise LiteraryChapterLoopExecutorError(
                f"chapter bridge differs: {chapter_id}"
            )
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        result.append(path)
    return result


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise LiteraryChapterLoopExecutorError(f"cannot load {label}: {path}") from exc
    if not isinstance(value, dict):
        raise LiteraryChapterLoopExecutorError(f"{label} must be an object")
    return value


def _directory_fingerprint(root: Path) -> str:
    target = Path(root).resolve()
    rows: list[dict[str, str]] = []
    for path in sorted(target.rglob("*")):
        if path.is_symlink():
            raise ChapterCycleStagePause(
                "integrity_or_lineage",
                f"incomplete stage output contains a symlink: {path}",
            )
        if path.is_file():
            rows.append(
                {
                    "relative_path": path.relative_to(target).as_posix(),
                    "sha256": file_sha256(path),
                }
            )
        elif not path.is_dir():
            raise ChapterCycleStagePause(
                "integrity_or_lineage",
                f"incomplete stage output contains an unsupported entry: {path}",
            )
    return canonical_hash(rows)


def _shared_output_root(output_root: Path) -> Path:
    root = Path(output_root).resolve()
    return root.with_name(f"{root.name}-shared")


def _recoverable_xchapter_component_count_v1(
    output_root: Path,
    *,
    ready_count: int,
) -> int:
    root = Path(output_root)
    report_path = root / "run_report.json"
    decisions_path = root / "validated_decisions.json"
    components_root = root / "components"
    if (
        not report_path.is_file()
        or not decisions_path.is_file()
        or not components_root.is_dir()
    ):
        return 0
    try:
        report = _read_json(report_path, "cross-chapter partial report")
        decisions = json.loads(decisions_path.read_text(encoding="utf-8"))
    except (LiteraryChapterLoopExecutorError, OSError, ValueError, TypeError):
        return 0
    accepted_ids = report.get("accepted_component_ids")
    if (
        report.get("schema_version")
        != "literary_b1_cross_chapter_auditor_report_v1"
        or not isinstance(accepted_ids, list)
        or len(set(accepted_ids)) != len(accepted_ids)
        or not isinstance(decisions, list)
        or len(accepted_ids) > ready_count
    ):
        return 0
    decision_ids = {
        row.get("component_id")
        for row in decisions
        if isinstance(row, Mapping)
        and isinstance(row.get("component_id"), str)
    }
    if decision_ids != set(accepted_ids) or len(decisions) != len(accepted_ids):
        return 0
    for component_id in accepted_ids:
        matches = [
            path
            for path in components_root.iterdir()
            if path.is_dir()
            and path.name.endswith(f"_{component_id}")
            and (path / "validated_decision.json").is_file()
            and (path / "component_report.json").is_file()
        ]
        if len(matches) != 1:
            return 0
    return len(accepted_ids)


def _existing_stage_output_is_complete_v1(
    *,
    stage_name: str,
    report_path: Path,
) -> bool:
    path = Path(report_path)
    if not path.is_file():
        return False
    if stage_name == "b3_auditor":
        try:
            report = _read_json(path, "B3 auditor existing index")
        except LiteraryChapterLoopExecutorError:
            return False
        expected = report.get("serviceable_case_count")
        observed = report.get("provider_calls")
        if isinstance(expected, int) and isinstance(observed, int):
            return observed == expected
        return True
    if stage_name != "xchapter_hearing":
        return True
    try:
        report = _read_json(path, "cross-chapter existing report")
    except LiteraryChapterLoopExecutorError:
        return False
    return (
        report.get("schema_version")
        == "literary_b1_cross_chapter_auditor_report_v1"
        and report.get("ready_hearings_complete") is True
        and report.get("chapter_loop_complete") is True
        and report.get("quarantined_component_ids") == []
    )


def materialize_b2_canary_profile_v1(
    *,
    template_path: Path,
    output_path: Path,
    chapter_id: str,
    prior_frame_candidate_carry_required: bool,
    interaction_call_count: int | None = None,
) -> Path:
    """Bind the existing B2 canary contract to one chapter mechanically."""

    template = Path(template_path).resolve()
    output = Path(output_path).resolve()
    profile = load_b2_canary_profile_v1(template)
    payload = _read_json(template, "B2 canary profile template")
    if payload.get("schema_version") != "literary_b2_ch1_canary_profile_v4":
        raise LiteraryChapterLoopExecutorError(
            "chapter-loop B2 template must support explicit prior-frame carry"
        )
    safety = dict(payload["safety"])
    safety["stop_after_chapter_id"] = chapter_id
    safety["prior_frame_candidate_carry_required"] = bool(
        prior_frame_candidate_carry_required
    )
    limits = dict(payload["limits"])
    if interaction_call_count is not None:
        if (
            not isinstance(interaction_call_count, int)
            or isinstance(interaction_call_count, bool)
            or not 1 <= interaction_call_count <= 6
        ):
            raise LiteraryChapterLoopExecutorError(
                "B2 interaction call count is outside the canary profile bounds"
            )
        limits["interaction_calls"] = interaction_call_count
        limits["max_total_calls"] = (
            int(limits["frame_calls"]) + interaction_call_count
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    sibling_sources = (
        profile.b2_profile_path,
        profile.provider_profile_path,
        profile.structured_output_policy_path,
    )
    sibling_targets: dict[Path, Path] = {}
    for sibling_source in sibling_sources:
        if sibling_source is None:
            continue
        sibling_target = output.parent / (
            f"{sibling_source.stem}_{file_sha256(sibling_source)[:12]}"
            f"{sibling_source.suffix}"
        )
        sibling_targets[sibling_source] = sibling_target
        sibling_bytes = sibling_source.read_bytes()
        if sibling_target.exists():
            if sibling_target.read_bytes() != sibling_bytes:
                raise ChapterCycleStagePause(
                    "integrity_or_lineage",
                    f"materialized B2 profile dependency drifted: {sibling_target}",
                )
        else:
            sibling_target.write_bytes(sibling_bytes)
    payload.update(
        {
            "profile_id": f"{profile.profile_id}__{chapter_id}",
            "b2_profile": sibling_targets[profile.b2_profile_path].name,
            "provider_profile": sibling_targets[profile.provider_profile_path].name,
            "structured_output_policy": (
                sibling_targets[profile.structured_output_policy_path].name
                if profile.structured_output_policy_path is not None
                else None
            ),
            "chapter_id": chapter_id,
            "limits": limits,
            "safety": safety,
        }
    )
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    if output.exists():
        if output.read_text(encoding="utf-8-sig") != encoded:
            existing = _read_json(output, "materialized B2 profile")
            existing_without_limits = dict(existing)
            expected_without_limits = dict(payload)
            existing_without_limits.pop("limits", None)
            expected_without_limits.pop("limits", None)
            if existing_without_limits != expected_without_limits:
                raise ChapterCycleStagePause(
                    "integrity_or_lineage",
                    f"materialized B2 profile drifted: {output}",
                )
            output.write_text(encoded, encoding="utf-8")
    else:
        output.write_text(encoded, encoding="utf-8")
    load_b2_canary_profile_v1(output)
    return output


def _read_state(run_root: Path) -> dict[str, Any]:
    return _read_json(Path(run_root) / "run_state.json", "chapter-cycle state")


def _runtime_environment() -> dict[str, str]:
    environment = os.environ.copy()
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        f"{RUNTIME_ROOT}{os.pathsep}{existing}" if existing else str(RUNTIME_ROOT)
    )
    return environment


def _stage_from_plan_row(row: Mapping[str, Any]) -> ChapterCycleStage:
    return ChapterCycleStage(
        stage_id=str(row["stage_id"]),
        chapter_id=str(row["chapter_id"]),
        chapter_ordinal=int(row["chapter_ordinal"]),
        stage_name=str(row["stage_name"]),
        stage_role=str(row["stage_role"]),
        requires_api=bool(row["requires_api"]),
        is_chapter_checkpoint=bool(row["is_chapter_checkpoint"]),
        stage_descriptor_hash=str(row["stage_descriptor_hash"]),
    )


def _pairs(**values: Any) -> list[str]:
    result: list[str] = []
    for key, value in values.items():
        if value is None:
            continue
        result.extend([f"--{key.replace('_', '-')}", str(value)])
    return result


def _single_role(runtime: Mapping[str, Any], label: str) -> Mapping[str, Any]:
    rows = runtime.get("roles")
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], Mapping):
        raise LiteraryChapterLoopExecutorError(f"{label} must bind exactly one role")
    return rows[0]


def _serviceable_cases(artifact: Mapping[str, Any]) -> list[dict[str, Any]]:
    chapter_id = artifact.get("chapter_id")
    if not isinstance(chapter_id, str) or not chapter_id:
        raise LiteraryChapterLoopExecutorError("B3 artifact chapter id is absent")
    result: list[dict[str, Any]] = []
    for row in artifact.get("pending_cases") or []:
        if not isinstance(row, Mapping) or row.get("review_route") not in STATE_REVIEW_ROUTES:
            continue
        case_chapter_id = row.get("chapter_id")
        if not isinstance(case_chapter_id, str) or not case_chapter_id:
            raise LiteraryChapterLoopExecutorError(
                "serviceable B3 pending case chapter id is absent"
            )
        if case_chapter_id != chapter_id:
            continue
        result.append(dict(row))
    return result


def _reported_calls(report: Mapping[str, Any]) -> int | None:
    for key in (
        "api_calls_performed",
        "provider_calls",
        "model_calls_performed",
        "call_count",
    ):
        value = report.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


def _speaker_recovery_expected_calls_v1(
    *,
    b2_root: Path,
    output_root: Path,
) -> int:
    report_path = Path(output_root) / "canary_report.json"
    if report_path.is_file():
        report = _read_json(report_path, "speaker-recovery existing report")
        observed = _reported_calls(report)
        batch_count = report.get("batch_count")
        if (
            report.get("schema_version")
            != "literary_b2_speaker_recovery_canary_report_v1"
            or report.get("status") != "semantic_accepted"
            or observed is None
            or not isinstance(batch_count, int)
            or isinstance(batch_count, bool)
            or batch_count < 1
            or observed != batch_count
        ):
            raise LiteraryChapterLoopExecutorError(
                "speaker-recovery existing report has inconsistent call coverage"
            )
        return observed

    chapter, interaction_requests = load_b2_slim_speaker_source_v1(b2_root)
    index = build_b2_slim_speaker_recovery_index_v1(
        chapter_artifact=chapter,
        interaction_requests=interaction_requests,
    )
    components = list(index.get("registry_components") or [])
    if any(row.get("overflow") for row in components):
        raise LiteraryChapterLoopExecutorError(
            "speaker-recovery component exceeds its sealed cap"
        )
    component_count = sum(not row.get("overflow") for row in components)
    if component_count < 1:
        raise LiteraryChapterLoopExecutorError(
            "speaker-recovery source has no callable component"
        )
    return (
        component_count + MAX_BATCH_COMPONENTS_V1 - 1
    ) // MAX_BATCH_COMPONENTS_V1


def _request_fingerprint(stage_root: Path) -> str:
    hashes: list[str] = []
    stack = [Path(stage_root)]
    while stack:
        root = stack.pop()
        for path in sorted(root.iterdir(), key=lambda value: value.name):
            if path.is_dir():
                stack.append(path)
            elif path.name in {"request.json", "transport_request.json"}:
                hashes.append(file_sha256(path))
    if not hashes:
        raise LiteraryChapterLoopExecutorError("model stage emitted no request artifact")
    return canonical_hash(hashes)


def _document_id(document: Mapping[str, Any]) -> str:
    for key in ("doc_id", "document_id", "id"):
        value = document.get(key)
        if isinstance(value, str) and value:
            return value
    raise LiteraryChapterLoopExecutorError("document has no stable id")


def _credential_value(path: Path) -> str:
    try:
        rows = [
            row.strip()
            for row in Path(path).read_text(encoding="utf-8").splitlines()
            if row.strip()
        ]
    except OSError as exc:
        raise LiteraryChapterLoopExecutorError("cannot load credential") from exc
    if not rows:
        raise LiteraryChapterLoopExecutorError("credential file is empty")
    return rows[0]


def _public_resolved_inputs(value: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, raw in value.items():
        if raw is None:
            result[key] = None
        elif isinstance(raw, (list, tuple)):
            result[key] = [str(item) for item in raw]
        else:
            result[key] = str(raw)
    return result


def _event_resolved_inputs(
    value: Mapping[str, Any], run_root: Path
) -> dict[str, Any]:
    root = Path(run_root).resolve()

    def project(raw: Any) -> Any:
        if raw is None:
            return None
        if isinstance(raw, (list, tuple)):
            return [project(item) for item in raw]
        if not isinstance(raw, (str, Path)):
            return str(raw)
        path = Path(raw)
        if not path.exists():
            return {"scope": "logical", "value": str(raw)}
        resolved = path.resolve()
        try:
            relative = resolved.relative_to(root).as_posix()
        except ValueError:
            if resolved.is_file():
                return {
                    "scope": "external_file",
                    "name": resolved.name,
                    "physical_sha256": file_sha256(resolved),
                }
            return {"scope": "external_directory", "name": resolved.name}
        return {"scope": "run", "relative_path": relative}

    return {key: project(raw) for key, raw in value.items()}


def _ensure_resolved_value_exists(
    value: Any, *, stage_id: str, input_name: str
) -> None:
    rows = value if isinstance(value, (list, tuple)) else [value]
    for raw in rows:
        if not isinstance(raw, (str, Path)):
            continue
        path = Path(raw)
        if not path.exists():
            raise ChapterCycleStagePause(
                "integrity_or_lineage",
                f"{stage_id} resolved absent input {input_name}: {path}",
            )


def _parent_artifact_refs(
    resolved: Mapping[str, Any], run_root: Path
) -> list[str]:
    refs: list[str] = []
    root = Path(run_root).resolve()
    index_path = root / "artifact_index.json"
    indexed: list[tuple[Path, str]] = []
    if index_path.is_file():
        index = _read_json(index_path, "artifact index")
        for row in index.get("artifacts") or []:
            if not isinstance(row, Mapping):
                continue
            relative = row.get("relative_path")
            artifact_ref = row.get("artifact_ref")
            if isinstance(relative, str) and isinstance(artifact_ref, str):
                indexed.append(((root / relative).resolve(), artifact_ref))
    for value in resolved.values():
        rows = value if isinstance(value, (list, tuple)) else [value]
        for raw in rows:
            if raw is None:
                continue
            path = Path(raw)
            if not path.exists():
                continue
            resolved_path = path.resolve()
            for artifact_path, artifact_ref in indexed:
                if resolved_path == artifact_path:
                    refs.append(artifact_ref)
                    continue
                if resolved_path.is_dir():
                    try:
                        artifact_path.relative_to(resolved_path)
                    except ValueError:
                        continue
                    refs.append(artifact_ref)
    return sorted(set(refs))


def _approved_retry_command_change(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> bool:
    def normalized(value: Mapping[str, Any]) -> dict[str, Any]:
        body = deepcopy(dict(value))
        body.pop("command_hash", None)
        if str(body.get("stage_id", "")).endswith("_b3_auditor"):
            body["expected_calls"] = "<current-chapter-serviceable-cases>"
        elif str(body.get("stage_id", "")).endswith("_xchapter_hearing"):
            body["expected_calls"] = "<remaining-unrecovered-components>"
        elif str(body.get("stage_id", "")).endswith("_b3_temporal"):
            body["expected_calls"] = "<sealed-batch-count>"
        elif str(body.get("stage_id", "")).endswith("_speaker_recovery"):
            body["expected_calls"] = "<component-batch-count>"
        command = list(body.get("command") or [])
        if str(body.get("stage_id", "")).endswith("_b2_review_routing"):
            stripped: list[Any] = []
            index = 0
            while index < len(command):
                if command[index] == "--decided-cross-component-id":
                    index += 2
                    continue
                stripped.append(command[index])
                index += 1
            command = stripped
        if str(body.get("stage_id", "")).endswith("_b3_apply"):
            stripped = []
            index = 0
            while index < len(command):
                if command[index] == "--component-catalog":
                    index += 2
                    continue
                stripped.append(command[index])
                index += 1
            command = stripped
        for index, argument in enumerate(command[:-1]):
            if isinstance(argument, str) and argument.endswith(
                "capability-root"
            ):
                command[index + 1] = "<capability-root>"
            elif argument == "--runtime-profile":
                command[index + 1] = "<runtime-profile>"
            elif (
                argument == "--canary-profile"
                and str(body.get("stage_id", "")).endswith(
                    "_b2_frame_interaction"
                )
            ):
                command[index + 1] = "<capacity-profile>"
            elif (
                argument == "--context-profile"
                and str(body.get("stage_id", "")).endswith("_b3_temporal")
            ):
                command[index + 1] = "<capacity-profile>"
            elif (
                argument == "--max-calls"
                and str(body.get("stage_id", "")).endswith("_b3_temporal")
            ):
                command[index + 1] = "<sealed-batch-count>"
        body["command"] = command
        resolved = dict(body.get("resolved_inputs") or {})
        if str(body.get("stage_id", "")).endswith("_b2_review_routing"):
            resolved.pop("decision_ledger", None)
            resolved.pop("decided_cross_component_ids", None)
        if str(body.get("stage_id", "")).endswith("_b3_apply"):
            resolved.pop("prior_component_catalogs", None)
        for key in list(resolved):
            if key.endswith("capability_root"):
                resolved[key] = "<capability-root>"
            elif key == "runtime_profile":
                resolved[key] = "<runtime-profile>"
            elif (
                key == "canary_profile"
                and str(body.get("stage_id", "")).endswith(
                    "_b2_frame_interaction"
                )
            ):
                resolved[key] = "<capacity-profile>"
            elif (
                key == "context_profile"
                and str(body.get("stage_id", "")).endswith("_b3_temporal")
            ):
                resolved[key] = "<capacity-profile>"
        body["resolved_inputs"] = resolved
        return body

    return normalized(before) == normalized(after)


def _sealed_b3_request_count_v1(
    *,
    output_root: Path,
    chapter_id: str,
) -> int | None:
    plan_path = Path(output_root) / "live_plan.json"
    seal_path = Path(output_root) / "run_seal.json"
    if not plan_path.exists() and not seal_path.exists():
        return None
    if not plan_path.is_file() or not seal_path.is_file():
        raise LiteraryChapterLoopExecutorError(
            "partial B3 resume plan is not admissible"
        )

    plan = _read_json(plan_path, "B3 live plan")
    plan_body = dict(plan)
    plan_hash = plan_body.pop("plan_hash", None)
    if not isinstance(plan_hash, str) or canonical_hash(plan_body) != plan_hash:
        raise LiteraryChapterLoopExecutorError("B3 live plan seal is invalid")

    seal = _read_json(seal_path, "B3 run seal")
    seal_body = dict(seal)
    seal_hash = seal_body.pop("seal_hash", None)
    if not isinstance(seal_hash, str) or canonical_hash(seal_body) != seal_hash:
        raise LiteraryChapterLoopExecutorError("B3 run seal is invalid")
    if seal.get("live_plan_hash") != plan_hash:
        raise LiteraryChapterLoopExecutorError("B3 live plan differs from run seal")
    if plan.get("chapter_id") != chapter_id or seal.get("chapter_id") != chapter_id:
        raise LiteraryChapterLoopExecutorError("B3 resume plan belongs to another chapter")

    rows = plan.get("batch_membership")
    count = plan.get("request_count")
    if (
        not isinstance(rows, list)
        or not isinstance(count, int)
        or isinstance(count, bool)
        or count < 1
        or len(rows) != count
        or [row.get("batch_ordinal") for row in rows if isinstance(row, Mapping)]
        != list(range(1, count + 1))
    ):
        raise LiteraryChapterLoopExecutorError("B3 sealed batch plan is malformed")
    return count


def _agent(stage_name: str) -> str:
    if "auditor" in stage_name or "hearing" in stage_name:
        return "auditor"
    if stage_name in {
        "b1_scan",
        "b1_enrich",
        "b2_frame_interaction",
        "b3_temporal",
        "b0_summary",
    }:
        return "builder"
    return "system"


def _stage_limits_payload(plan: Mapping[str, Any], role: str) -> dict[str, Any]:
    return {
        "max_calls_per_chapter": int(plan["logical_call_caps_by_role"][role]),
        "max_api_calls_per_chapter": int(plan["max_api_calls_per_chapter"]),
        "max_api_calls_per_run": int(plan["max_api_calls_per_run"]),
    }


__all__ = [
    "LiteraryChapterLoopExecutorError",
    "LiteraryChapterLoopExecutorV1",
    "write_chapter_bridge_files_v1",
]
