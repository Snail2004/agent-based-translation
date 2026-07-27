"""Deterministic, resumable control plane for the literary chapter cycle.

This module owns ordering, call permits, receipts, and checkpoints.  It does
not perform literary judgment and cannot publish a production registry.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from pipeline.agents.provider_profile import (
    load_provider_profile,
)
from pipeline.literary.book_source_lineage import (
    build_book_source_manifest,
    state_lineage_id_for_manifest,
)
from pipeline.literary.chapter_cycle_profile_v1 import (
    LiteraryChapterCycleProfile,
    load_chapter_cycle_profile,
    verify_profile_roles,
)
from pipeline.literary.chapter_cycle_resilience_v1 import (
    IntegrityOrLineageFailure,
    ResilientStageHalt,
)
from pipeline.literary.literary_pipeline_profile_v1 import (
    load_literary_pipeline_profile,
)
from pipeline.literary.checkpoint import (
    CheckpointLock,
    canonical_hash,
    canonical_json,
    file_sha256,
    write_checkpoint_atomic,
)


PLAN_SCHEMA_VERSION = "literary_chapter_cycle_plan_v1"
STATE_SCHEMA_VERSION = "literary_chapter_cycle_state_v1"
STAGE_RESULT_SCHEMA_VERSION = "literary_chapter_cycle_stage_result_v1"
STAGE_RECEIPT_SCHEMA_VERSION = "literary_chapter_cycle_stage_receipt_v1"
CHAPTER_CHECKPOINT_SCHEMA_VERSION = "literary_chapter_cycle_checkpoint_v1"
CHECKPOINT_POINTER_SCHEMA_VERSION = "literary_chapter_cycle_checkpoint_pointer_v1"
PAUSE_SCHEMA_VERSION = "literary_chapter_cycle_pause_v1"

_STATE_STATUSES = {"running", "stopped", "paused", "complete"}
_RESULT_STATUSES = {"accepted", "semantic_pending", "skipped"}
_CALL_DISPOSITIONS = {"called", "cache_replay", "not_required", "code_only"}
_CUMULATIVE_HASH_KEYS = {
    "claim_ledger_hash",
    "identity_ledger_hash",
    "prefix_hash",
    "review_ledger_hash",
    "review_case_ledger_hash",
    "semantic_identity_occurrence_bridge_hash",
    "semantic_lead_index_hash",
}

_FIRST_CHAPTER_TEMPLATE = (
    ("b0", "b0", True),
    ("local_auditor", "local_auditor", True),
    ("prefix", "code", False),
    ("semantic_leads", "code", False),
    ("checkpoint", "code", False),
)

_LATER_CHAPTER_TEMPLATE = (
    ("b0_prior", "b0", True),
    ("local_auditor", "local_auditor", True),
    ("stable_claim_prepare", "code", False),
    ("stable_claim_components", "stable_claim_auditor", True),
    ("stable_claim_reconcile", "code", False),
    ("prefix_extend", "code", False),
    ("semantic_leads", "code", False),
    ("identity_prepare", "code", False),
    ("identity_components", "identity_auditor", True),
    ("identity_reconcile", "code", False),
    ("checkpoint", "code", False),
)

_CURRENT_CHAPTER_LOOP_TEMPLATE = (
    ("b1_scan", "b1_scan", True),
    ("b1_enrich", "b1_enrich", True),
    ("b1_local_auditor", "local_auditor", True),
    ("b1_registry_writer", "code", False),
    ("xchapter_prepare", "code", False),
    ("xchapter_hearing", "identity_auditor", True),
    ("identity_apply", "code", False),
    ("b1_to_b2_input", "code", False),
    ("b2_frame_interaction", "b2", True),
    ("b2_review_routing", "code", False),
    ("speaker_recovery", "speaker_recovery", True),
    ("b3_temporal", "b3_temporal", True),
    ("b3_auditor", "b3_auditor", True),
    ("b3_apply", "code", False),
    ("b0_summary", "b0_summary", True),
    ("checkpoint", "code", False),
)


class ChapterCycleOrchestratorError(RuntimeError):
    pass


class ChapterCycleIntegrityError(ChapterCycleOrchestratorError):
    pass


class ChapterCycleCallCapReached(ChapterCycleOrchestratorError):
    pass


class ChapterCycleStagePause(ChapterCycleOrchestratorError):
    def __init__(self, failure_class: str, reason: str) -> None:
        super().__init__(reason)
        self.failure_class = _required_string(failure_class, "failure_class")
        self.reason = _required_string(reason, "reason")


@dataclass(frozen=True)
class ChapterCycleStage:
    stage_id: str
    chapter_id: str
    chapter_ordinal: int
    stage_name: str
    stage_role: str
    requires_api: bool
    is_chapter_checkpoint: bool
    stage_descriptor_hash: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "stage_id": self.stage_id,
            "chapter_id": self.chapter_id,
            "chapter_ordinal": self.chapter_ordinal,
            "stage_name": self.stage_name,
            "stage_role": self.stage_role,
            "requires_api": self.requires_api,
            "is_chapter_checkpoint": self.is_chapter_checkpoint,
            "stage_descriptor_hash": self.stage_descriptor_hash,
        }


@dataclass(frozen=True)
class StageExecutionResult:
    status: str
    payload: Mapping[str, Any]
    call_disposition: str
    request_fingerprint: str | None = None
    model_actual: str | None = None
    resilience_report_hash: str | None = None
    attempt_count: int = 0
    retry_count: int = 0
    fallback_count: int = 0
    semantic_pending_count: int = 0
    cumulative_hash_updates: Mapping[str, str] = field(default_factory=dict)


StageExecutor = Callable[[ChapterCycleStage, "ApiCallPermit"], StageExecutionResult]


def _clone(value: Any) -> Any:
    return deepcopy(value)


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ChapterCycleOrchestratorError(f"{label} must be a non-empty string")
    return value


def _hash_string(value: Any, label: str) -> str:
    result = _required_string(value, label)
    if len(result) != 64 or any(ch not in "0123456789abcdef" for ch in result):
        raise ChapterCycleOrchestratorError(f"{label} must be a lowercase SHA-256")
    return result


def _safe_stage_token(value: Any, label: str) -> str:
    result = _required_string(value, label)
    if any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for ch in result):
        raise ChapterCycleOrchestratorError(f"{label} contains an unsafe character")
    return result


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError) as exc:
        raise ChapterCycleIntegrityError(f"cannot load {label}: {path}") from exc
    if not isinstance(value, dict):
        raise ChapterCycleIntegrityError(f"{label} must be a JSON object")
    return value


def _checked_file_sha256(path: Path, label: str) -> str:
    try:
        return file_sha256(path)
    except OSError as exc:
        raise ChapterCycleIntegrityError(f"cannot hash {label}: {path}") from exc


def _write_immutable_json(path: Path, payload: Mapping[str, Any]) -> None:
    target = Path(path)
    if target.exists():
        existing = _load_json(target, "immutable artifact")
        if canonical_json(existing) != canonical_json(payload):
            raise ChapterCycleIntegrityError(f"immutable artifact differs: {target}")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    write_checkpoint_atomic(target, dict(payload))


def _document_chapter_ids(document: Mapping[str, Any]) -> list[str]:
    chapters = document.get("chapters")
    if not isinstance(chapters, list) or not chapters:
        raise ChapterCycleOrchestratorError("document has no chapters")
    result = [
        _required_string(row.get("chapter_id"), "chapter_id")
        for row in chapters
        if isinstance(row, Mapping)
    ]
    if len(result) != len(chapters):
        raise ChapterCycleOrchestratorError("document contains a malformed chapter")
    if len(result) != len(set(result)):
        raise ChapterCycleOrchestratorError("document repeats a chapter id")
    return result


def _validated_selection(
    document: Mapping[str, Any], ordered_chapter_ids: Sequence[str]
) -> list[str]:
    document_ids = _document_chapter_ids(document)
    selected = [_required_string(value, "ordered chapter id") for value in ordered_chapter_ids]
    if not selected:
        raise ChapterCycleOrchestratorError("chapter selection cannot be empty")
    if len(selected) != len(set(selected)):
        raise ChapterCycleOrchestratorError("chapter selection repeats a chapter")
    try:
        positions = [document_ids.index(chapter_id) for chapter_id in selected]
    except ValueError as exc:
        raise ChapterCycleOrchestratorError(
            "chapter selection contains a foreign chapter"
        ) from exc
    expected = list(range(positions[0], positions[0] + len(positions)))
    if positions != expected:
        raise ChapterCycleOrchestratorError(
            "chapter selection must preserve one contiguous document range"
        )
    return selected


def _stage_row(
    *,
    chapter_id: str,
    chapter_ordinal: int,
    stage_name: str,
    stage_role: str,
    requires_api: bool,
) -> dict[str, Any]:
    body = {
        "stage_id": f"ch{chapter_ordinal:03d}_{stage_name}",
        "chapter_id": chapter_id,
        "chapter_ordinal": chapter_ordinal,
        "stage_name": stage_name,
        "stage_role": stage_role,
        "requires_api": requires_api,
        "is_chapter_checkpoint": stage_name == "checkpoint",
    }
    return {**body, "stage_descriptor_hash": canonical_hash(body)}


def build_dynamic_stage_plan_v1(
    *,
    document: Mapping[str, Any],
    ordered_chapter_ids: Sequence[str],
    stage_graph_id: str = "legacy_builder_v3",
) -> list[dict[str, Any]]:
    selected = _validated_selection(document, ordered_chapter_ids)
    rows: list[dict[str, Any]] = []
    for ordinal, chapter_id in enumerate(selected, start=1):
        if stage_graph_id == "literary_chapter_loop_v1":
            template = _CURRENT_CHAPTER_LOOP_TEMPLATE
        elif stage_graph_id == "legacy_builder_v3":
            template = (
                _FIRST_CHAPTER_TEMPLATE if ordinal == 1 else _LATER_CHAPTER_TEMPLATE
            )
        else:
            raise ChapterCycleOrchestratorError("unknown chapter-cycle stage graph")
        rows.extend(
            _stage_row(
                chapter_id=chapter_id,
                chapter_ordinal=ordinal,
                stage_name=stage_name,
                stage_role=stage_role,
                requires_api=requires_api,
            )
            for stage_name, stage_role, requires_api in template
        )
    return rows


def _stage_from_payload(value: Mapping[str, Any]) -> ChapterCycleStage:
    body = dict(value)
    observed = _hash_string(
        body.pop("stage_descriptor_hash", None), "stage_descriptor_hash"
    )
    if canonical_hash(body) != observed:
        raise ChapterCycleIntegrityError("stage descriptor hash mismatch")
    expected_keys = {
        "stage_id",
        "chapter_id",
        "chapter_ordinal",
        "stage_name",
        "stage_role",
        "requires_api",
        "is_chapter_checkpoint",
    }
    if set(body) != expected_keys:
        raise ChapterCycleIntegrityError("stage descriptor field set drifted")
    ordinal = body["chapter_ordinal"]
    if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 1:
        raise ChapterCycleIntegrityError("stage chapter ordinal is invalid")
    requires_api = body["requires_api"]
    is_checkpoint = body["is_chapter_checkpoint"]
    if not isinstance(requires_api, bool) or not isinstance(is_checkpoint, bool):
        raise ChapterCycleIntegrityError("stage booleans are invalid")
    stage = ChapterCycleStage(
        stage_id=_safe_stage_token(body["stage_id"], "stage_id"),
        chapter_id=_required_string(body["chapter_id"], "chapter_id"),
        chapter_ordinal=ordinal,
        stage_name=_safe_stage_token(body["stage_name"], "stage_name"),
        stage_role=_safe_stage_token(body["stage_role"], "stage_role"),
        requires_api=requires_api,
        is_chapter_checkpoint=is_checkpoint,
        stage_descriptor_hash=observed,
    )
    if stage.stage_id != f"ch{ordinal:03d}_{stage.stage_name}":
        raise ChapterCycleIntegrityError("stage id is not chapter-indexed")
    if stage.is_chapter_checkpoint != (stage.stage_name == "checkpoint"):
        raise ChapterCycleIntegrityError("checkpoint marker is stale")
    if stage.requires_api != (stage.stage_role != "code"):
        raise ChapterCycleIntegrityError("stage role/API classification drifted")
    return stage


def _profile_payload(profile: LiteraryChapterCycleProfile) -> dict[str, Any]:
    provider_profile = load_provider_profile(profile.provider_profile_path())
    verify_profile_roles(profile, provider_profile=provider_profile)
    payload = {
        "chapter_cycle_profile_path": str(profile.source_path),
        "chapter_cycle_profile_sha256": _checked_file_sha256(
            profile.source_path, "chapter-cycle profile"
        ),
        "chapter_cycle_profile_id": profile.profile_id,
        "provider_profile_path": str(provider_profile.source_path),
        "provider_profile_sha256": _checked_file_sha256(
            provider_profile.source_path, "provider profile"
        ),
        "provider_profile_id": provider_profile.profile_id,
        "provider_profile_hash": provider_profile.profile_hash,
        "role_bindings": dict(profile.role_bindings),
        "logical_call_caps_by_role": {
            stage_id: limits.max_calls_per_chapter
            for stage_id, limits in profile.stage_limits.items()
        },
        "max_api_calls_per_chapter": int(
            profile.orchestration["max_api_calls_per_chapter"]
        ),
        "max_api_calls_per_run": int(profile.orchestration["max_api_calls_per_run"]),
        "default_stop_after_chapter_count": int(
            profile.orchestration["default_stop_after_chapter_count"]
        ),
        "production_publish_enabled": False,
    }
    if profile.stage_graph_id != "legacy_builder_v3":
        payload["stage_graph_id"] = profile.stage_graph_id
    return payload


def initialize_chapter_cycle_run_v1(
    *,
    run_root: Path,
    document_path: Path,
    profile_path: Path,
    frozen_db_path: Path,
    ordered_chapter_ids: Sequence[str],
    stop_after_chapter_count: int | None = None,
    pipeline_profile_path: Path | None = None,
) -> dict[str, Any]:
    root = Path(run_root).resolve()
    if root.exists() and any(root.iterdir()):
        raise ChapterCycleOrchestratorError("run root must be absent or empty")
    root.mkdir(parents=True, exist_ok=True)
    document_source = Path(document_path).resolve()
    profile_source = Path(profile_path).resolve()
    frozen_source = Path(frozen_db_path).resolve()
    document = _load_json(document_source, "document")
    selected = _validated_selection(document, ordered_chapter_ids)
    profile = load_chapter_cycle_profile(profile_source)
    profile_body = _profile_payload(profile)
    pipeline_seal: dict[str, Any] = {}
    if pipeline_profile_path is not None:
        pipeline_profile = load_literary_pipeline_profile(pipeline_profile_path)
        if pipeline_profile.chapter_cycle_profile_path != profile_source:
            raise ChapterCycleOrchestratorError(
                "pipeline profile resolves a different chapter-cycle profile"
            )
        pipeline_seal = pipeline_profile.seal_payload()
    stage_plan = build_dynamic_stage_plan_v1(
        document=document,
        ordered_chapter_ids=selected,
        stage_graph_id=profile.stage_graph_id,
    )
    source_manifest = build_book_source_manifest(document)
    stop_after = (
        min(profile_body["default_stop_after_chapter_count"], len(selected))
        if stop_after_chapter_count is None
        else stop_after_chapter_count
    )
    if (
        not isinstance(stop_after, int)
        or isinstance(stop_after, bool)
        or not 1 <= stop_after <= len(selected)
    ):
        raise ChapterCycleOrchestratorError("stop-after count is outside the sealed run")
    if not frozen_source.is_file():
        raise ChapterCycleOrchestratorError("frozen database is absent")
    plan_body = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "document_path": str(document_source),
        "document_sha256": _checked_file_sha256(document_source, "document"),
        "frozen_db_path": str(frozen_source),
        "frozen_db_sha256": _checked_file_sha256(
            frozen_source, "frozen database"
        ),
        "book_source_manifest_hash": source_manifest["manifest_hash"],
        "state_lineage_id": state_lineage_id_for_manifest(source_manifest),
        "ordered_chapter_ids": selected,
        "sealed_end_chapter_id": selected[-1],
        "stage_plan": stage_plan,
        **profile_body,
        **pipeline_seal,
        "production_publish_performed": False,
    }
    plan = {**plan_body, "plan_hash": canonical_hash(plan_body)}
    _write_immutable_json(root / "run_plan.json", plan)
    initial_body = {
        "schema_version": STATE_SCHEMA_VERSION,
        "plan_hash": plan["plan_hash"],
        "generation": 0,
        "parent_state_hash": None,
        "status": "running",
        "halt_failure_class": None,
        "halt_reason": None,
        "next_stage_index": 0,
        "current_stage": stage_plan[0]["stage_id"],
        "stop_after_chapter_count": stop_after,
        "completed_chapter_ids": [],
        "stage_receipts": [],
        "stage_call_reservations": {},
        "chapter_api_call_counts": {chapter_id: 0 for chapter_id in selected},
        "run_api_call_count": 0,
        "semantic_pending_count": 0,
        "cumulative_hashes": {key: None for key in sorted(_CUMULATIVE_HASH_KEYS)},
        "production_publish_performed": False,
    }
    initial = _persist_state_generation(root, initial_body)
    _publish_state_pointer(root, initial)
    return _clone(initial)


def _plan_body(plan: Mapping[str, Any]) -> dict[str, Any]:
    body = dict(plan)
    body.pop("plan_hash", None)
    return body


def _load_plan_unlocked(run_root: Path) -> dict[str, Any]:
    root = Path(run_root).resolve()
    plan = _load_json(root / "run_plan.json", "run plan")
    if plan.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise ChapterCycleIntegrityError("foreign chapter-cycle plan schema")
    observed = _hash_string(plan.get("plan_hash"), "plan_hash")
    if canonical_hash(_plan_body(plan)) != observed:
        raise ChapterCycleIntegrityError("run plan hash mismatch")
    if plan.get("production_publish_enabled") is not False:
        raise ChapterCycleIntegrityError("run plan enables production publication")
    if plan.get("production_publish_performed") is not False:
        raise ChapterCycleIntegrityError("run plan claims production publication")
    document_path = Path(_required_string(plan.get("document_path"), "document_path"))
    if _checked_file_sha256(document_path, "sealed document") != plan.get(
        "document_sha256"
    ):
        raise ChapterCycleIntegrityError("sealed document changed")
    frozen_db_path = Path(_required_string(plan.get("frozen_db_path"), "frozen_db_path"))
    if _checked_file_sha256(frozen_db_path, "sealed frozen database") != plan.get(
        "frozen_db_sha256"
    ):
        raise ChapterCycleIntegrityError("sealed frozen database changed")
    profile_path = Path(
        _required_string(
            plan.get("chapter_cycle_profile_path"), "chapter_cycle_profile_path"
        )
    )
    if _checked_file_sha256(
        profile_path, "chapter-cycle profile"
    ) != plan.get("chapter_cycle_profile_sha256"):
        raise ChapterCycleIntegrityError("chapter-cycle profile changed")
    profile = load_chapter_cycle_profile(profile_path)
    if profile.profile_id != plan.get("chapter_cycle_profile_id"):
        raise ChapterCycleIntegrityError("chapter-cycle profile identity drifted")
    expected_profile_payload = _profile_payload(profile)
    for key, expected_value in expected_profile_payload.items():
        if plan.get(key) != expected_value:
            raise ChapterCycleIntegrityError(
                f"run plan differs from profile authority: {key}"
            )
    provider_path = Path(
        _required_string(plan.get("provider_profile_path"), "provider_profile_path")
    )
    if _checked_file_sha256(provider_path, "provider profile") != plan.get(
        "provider_profile_sha256"
    ):
        raise ChapterCycleIntegrityError("provider profile changed")
    provider = load_provider_profile(provider_path)
    if (
        provider.profile_id != plan.get("provider_profile_id")
        or provider.profile_hash != plan.get("provider_profile_hash")
    ):
        raise ChapterCycleIntegrityError("provider profile identity drifted")
    verify_profile_roles(profile, provider_profile=provider)
    pipeline_seal: dict[str, Any] = {}
    pipeline_profile_path = plan.get("pipeline_profile_path")
    if pipeline_profile_path is not None:
        pipeline_profile = load_literary_pipeline_profile(
            Path(_required_string(pipeline_profile_path, "pipeline_profile_path"))
        )
        if pipeline_profile.chapter_cycle_profile_path != profile_path:
            raise ChapterCycleIntegrityError(
                "pipeline profile chapter-cycle authority drifted"
            )
        pipeline_seal = pipeline_profile.seal_payload()
        for key, expected_value in pipeline_seal.items():
            if plan.get(key) != expected_value:
                raise ChapterCycleIntegrityError(
                    f"run plan differs from pipeline profile authority: {key}"
                )
    document = _load_json(document_path, "sealed document")
    source_manifest = build_book_source_manifest(document)
    if source_manifest["manifest_hash"] != plan.get("book_source_manifest_hash"):
        raise ChapterCycleIntegrityError("book source manifest changed")
    if state_lineage_id_for_manifest(source_manifest) != plan.get("state_lineage_id"):
        raise ChapterCycleIntegrityError("state lineage changed")
    selected = _validated_selection(document, plan.get("ordered_chapter_ids") or [])
    expected_plan = build_dynamic_stage_plan_v1(
        document=document,
        ordered_chapter_ids=selected,
        stage_graph_id=profile.stage_graph_id,
    )
    if canonical_json(expected_plan) != canonical_json(plan.get("stage_plan")):
        raise ChapterCycleIntegrityError("dynamic stage plan changed")
    if plan.get("sealed_end_chapter_id") != selected[-1]:
        raise ChapterCycleIntegrityError("sealed end chapter is stale")
    for raw_stage in expected_plan:
        _stage_from_payload(raw_stage)
    expected_body = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "document_path": str(document_path),
        "document_sha256": _checked_file_sha256(document_path, "sealed document"),
        "frozen_db_path": str(frozen_db_path),
        "frozen_db_sha256": _checked_file_sha256(
            frozen_db_path, "sealed frozen database"
        ),
        "book_source_manifest_hash": source_manifest["manifest_hash"],
        "state_lineage_id": state_lineage_id_for_manifest(source_manifest),
        "ordered_chapter_ids": selected,
        "sealed_end_chapter_id": selected[-1],
        "stage_plan": expected_plan,
        **expected_profile_payload,
        **pipeline_seal,
        "production_publish_performed": False,
    }
    if canonical_json(_plan_body(plan)) != canonical_json(expected_body):
        raise ChapterCycleIntegrityError("run plan field set or sealed value drifted")
    return _clone(plan)


def load_chapter_cycle_plan_v1(run_root: Path) -> dict[str, Any]:
    with CheckpointLock(Path(run_root).resolve()):
        return _load_plan_unlocked(run_root)


def _state_body(state: Mapping[str, Any]) -> dict[str, Any]:
    body = dict(state)
    body.pop("state_hash", None)
    return body


def _persist_state_generation(
    run_root: Path, state_body: Mapping[str, Any]
) -> dict[str, Any]:
    body = _clone(dict(state_body))
    body.pop("state_hash", None)
    result = {**body, "state_hash": canonical_hash(body)}
    generation = result.get("generation")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
        raise ChapterCycleOrchestratorError("state generation is invalid")
    _write_immutable_json(
        Path(run_root)
        / "state_generations"
        / f"{generation:04d}_{result['state_hash'][:12]}.json",
        result,
    )
    return result


def _publish_state_pointer(run_root: Path, state: Mapping[str, Any]) -> None:
    write_checkpoint_atomic(Path(run_root) / "run_state.json", dict(state))


def _save_state_unlocked(run_root: Path, state_body: Mapping[str, Any]) -> dict[str, Any]:
    result = _persist_state_generation(run_root, state_body)
    _publish_state_pointer(run_root, result)
    return result


def _transition_body(state: Mapping[str, Any]) -> dict[str, Any]:
    body = _state_body(state)
    body["parent_state_hash"] = state["state_hash"]
    body["generation"] = int(state["generation"]) + 1
    return body


def _verify_receipt_unlocked(
    run_root: Path, receipt: Mapping[str, Any], expected_stage: ChapterCycleStage
) -> None:
    body = dict(receipt)
    observed = _hash_string(body.pop("receipt_hash", None), "receipt_hash")
    if canonical_hash(body) != observed:
        raise ChapterCycleIntegrityError("stage receipt hash mismatch")
    if receipt.get("schema_version") != STAGE_RECEIPT_SCHEMA_VERSION:
        raise ChapterCycleIntegrityError("foreign stage receipt schema")
    if receipt.get("stage_id") != expected_stage.stage_id:
        raise ChapterCycleIntegrityError("stage receipt order drifted")
    if receipt.get("production_publish_performed") is not False:
        raise ChapterCycleIntegrityError("stage receipt claims production publication")
    receipt_path = Path(run_root) / "receipts" / f"{expected_stage.stage_id}.json"
    persisted_receipt = _load_json(receipt_path, "persisted stage receipt")
    if canonical_json(persisted_receipt) != canonical_json(receipt):
        raise ChapterCycleIntegrityError(
            "persisted stage receipt differs from checkpoint state"
        )
    relative = Path(_required_string(receipt.get("artifact_path"), "artifact_path"))
    if relative.is_absolute() or ".." in relative.parts:
        raise ChapterCycleIntegrityError("stage receipt path escapes run root")
    artifact = Path(run_root) / relative
    if file_sha256(artifact) != receipt.get("artifact_sha256"):
        raise ChapterCycleIntegrityError("stage receipt artifact changed")
    result = _verify_stage_result(artifact, expected_stage)
    if result.get("result_hash") != receipt.get("result_hash"):
        raise ChapterCycleIntegrityError("stage receipt result hash is stale")


def _verify_state_unlocked(
    run_root: Path, state: Mapping[str, Any], plan: Mapping[str, Any]
) -> dict[str, Any]:
    if state.get("schema_version") != STATE_SCHEMA_VERSION:
        raise ChapterCycleIntegrityError("foreign chapter-cycle state schema")
    observed = _hash_string(state.get("state_hash"), "state_hash")
    if canonical_hash(_state_body(state)) != observed:
        raise ChapterCycleIntegrityError("chapter-cycle state hash mismatch")
    if state.get("plan_hash") != plan.get("plan_hash"):
        raise ChapterCycleIntegrityError("state belongs to a different plan")
    if state.get("production_publish_performed") is not False:
        raise ChapterCycleIntegrityError("state claims production publication")
    status = state.get("status")
    if status not in _STATE_STATUSES:
        raise ChapterCycleIntegrityError("state status is invalid")
    stages = [_stage_from_payload(row) for row in plan["stage_plan"]]
    next_index = state.get("next_stage_index")
    if (
        not isinstance(next_index, int)
        or isinstance(next_index, bool)
        or not 0 <= next_index <= len(stages)
    ):
        raise ChapterCycleIntegrityError("next stage index is invalid")
    expected_current = stages[next_index].stage_id if next_index < len(stages) else None
    if state.get("current_stage") != expected_current:
        raise ChapterCycleIntegrityError("current stage does not match stage index")
    if status == "complete" and next_index != len(stages):
        raise ChapterCycleIntegrityError("complete state has remaining stages")
    if status != "complete" and next_index == len(stages):
        raise ChapterCycleIntegrityError("non-complete state has no remaining stage")
    receipts = state.get("stage_receipts")
    if not isinstance(receipts, list) or len(receipts) != next_index:
        raise ChapterCycleIntegrityError("stage receipts do not exact-cover progress")
    for stage, receipt in zip(stages[:next_index], receipts, strict=True):
        if not isinstance(receipt, Mapping):
            raise ChapterCycleIntegrityError("stage receipt is malformed")
        _verify_receipt_unlocked(run_root, receipt, stage)
    selected = list(plan["ordered_chapter_ids"])
    completed = state.get("completed_chapter_ids")
    if not isinstance(completed, list) or completed != selected[: len(completed)]:
        raise ChapterCycleIntegrityError("completed chapters are not a contiguous prefix")
    completed_from_stages = [
        stage.chapter_id for stage in stages[:next_index] if stage.is_chapter_checkpoint
    ]
    if completed != completed_from_stages:
        raise ChapterCycleIntegrityError("completed chapter list is stale")
    stop_after = state.get("stop_after_chapter_count")
    if (
        not isinstance(stop_after, int)
        or isinstance(stop_after, bool)
        or not 1 <= stop_after <= len(selected)
    ):
        raise ChapterCycleIntegrityError("state stop-after count is invalid")
    chapter_counts = state.get("chapter_api_call_counts")
    if not isinstance(chapter_counts, Mapping) or set(chapter_counts) != set(selected):
        raise ChapterCycleIntegrityError("chapter call counts are incomplete")
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in chapter_counts.values()
    ):
        raise ChapterCycleIntegrityError("chapter call count is invalid")
    run_count = state.get("run_api_call_count")
    if run_count != sum(chapter_counts.values()):
        raise ChapterCycleIntegrityError("run call count is stale")
    reservations = state.get("stage_call_reservations")
    if not isinstance(reservations, Mapping):
        raise ChapterCycleIntegrityError("stage call reservations are malformed")
    reservation_sum = 0
    for stage_id, value in reservations.items():
        if stage_id not in {stage.stage_id for stage in stages} or not isinstance(
            value, Mapping
        ):
            raise ChapterCycleIntegrityError("foreign stage call reservation")
        logical_ids = value.get("logical_call_ids")
        attempt_count = value.get("attempt_count")
        if (
            not isinstance(logical_ids, list)
            or len(logical_ids) != len(set(logical_ids))
            or not all(isinstance(item, str) and item for item in logical_ids)
            or not isinstance(attempt_count, int)
            or isinstance(attempt_count, bool)
            or attempt_count < len(logical_ids)
        ):
            raise ChapterCycleIntegrityError("stage call reservation is invalid")
        reservation_sum += attempt_count
    if reservation_sum != run_count:
        raise ChapterCycleIntegrityError("call reservations do not match run count")
    cumulative = state.get("cumulative_hashes")
    if not isinstance(cumulative, Mapping) or set(cumulative) != _CUMULATIVE_HASH_KEYS:
        raise ChapterCycleIntegrityError("cumulative hash field set drifted")
    for value in cumulative.values():
        if value is not None:
            _hash_string(value, "cumulative hash")
    pending_count = state.get("semantic_pending_count")
    if (
        not isinstance(pending_count, int)
        or isinstance(pending_count, bool)
        or pending_count < 0
    ):
        raise ChapterCycleIntegrityError("semantic pending count is invalid")
    return _clone(dict(state))


def _load_state_unlocked(
    run_root: Path, plan: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    verified_plan = dict(plan) if plan is not None else _load_plan_unlocked(run_root)
    state = _load_json(Path(run_root) / "run_state.json", "run state")
    verified_state = _verify_state_unlocked(run_root, state, verified_plan)
    _ensure_chapter_checkpoint_unlocked(
        run_root=run_root,
        plan=verified_plan,
        state=verified_state,
    )
    return verified_state


def load_chapter_cycle_state_v1(run_root: Path) -> dict[str, Any]:
    with CheckpointLock(Path(run_root).resolve()):
        plan = _load_plan_unlocked(run_root)
        return _load_state_unlocked(run_root, plan)


def current_chapter_cycle_stage_v1(
    run_root: Path,
) -> ChapterCycleStage | None:
    """Return the verified current stage, or ``None`` after completion."""

    root = Path(run_root).resolve()
    with CheckpointLock(root):
        plan = _load_plan_unlocked(root)
        state = _load_state_unlocked(root, plan)
        next_index = int(state["next_stage_index"])
        if next_index == len(plan["stage_plan"]):
            return None
        return _stage_from_payload(plan["stage_plan"][next_index])


def _pause_state_unlocked(
    run_root: Path,
    state: Mapping[str, Any],
    *,
    failure_class: str,
    reason: str,
) -> dict[str, Any]:
    body = _transition_body(state)
    body["status"] = "paused"
    body["halt_failure_class"] = _required_string(failure_class, "failure_class")
    body["halt_reason"] = _required_string(reason, "reason")
    return _save_state_unlocked(run_root, body)


def _write_unbound_integrity_pause(run_root: Path, reason: str) -> None:
    body = {
        "schema_version": PAUSE_SCHEMA_VERSION,
        "failure_class": "integrity_or_lineage",
        "reason": _required_string(reason, "reason"),
        "production_publish_performed": False,
    }
    payload = {**body, "pause_hash": canonical_hash(body)}
    write_checkpoint_atomic(Path(run_root) / "integrity_pause.json", payload)


def _validated_capacity_only_permit_plan(
    base_plan: Mapping[str, Any],
    candidate_plan: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Validate an in-memory capacity view without changing the sealed plan.

    The persisted run plan remains the authority for stage order, inputs, and
    lineage.  A caller may provide only upward call-capacity changes for the
    permit layer; request packing and every other plan field must remain
    byte-equivalent.
    """

    if candidate_plan is None:
        return deepcopy(dict(base_plan))
    candidate = deepcopy(dict(candidate_plan))
    if candidate.get("plan_hash") != base_plan.get("plan_hash"):
        raise ChapterCycleIntegrityError(
            "capacity permit plan belongs to a different run plan"
        )

    def without_capacity(value: Mapping[str, Any]) -> dict[str, Any]:
        body = deepcopy(dict(value))
        body.pop("logical_call_caps_by_role", None)
        body.pop("max_api_calls_per_chapter", None)
        body.pop("max_api_calls_per_run", None)
        return body

    if canonical_json(without_capacity(base_plan)) != canonical_json(
        without_capacity(candidate)
    ):
        raise ChapterCycleIntegrityError(
            "capacity permit plan changes non-capacity fields"
        )

    base_roles = base_plan.get("logical_call_caps_by_role")
    candidate_roles = candidate.get("logical_call_caps_by_role")
    if not isinstance(base_roles, Mapping) or not isinstance(candidate_roles, Mapping):
        raise ChapterCycleIntegrityError("capacity permit role caps are malformed")
    if set(base_roles) != set(candidate_roles):
        raise ChapterCycleIntegrityError("capacity permit role set changed")
    for role, base_value in base_roles.items():
        value = candidate_roles[role]
        if (
            not isinstance(base_value, int)
            or isinstance(base_value, bool)
            or not isinstance(value, int)
            or isinstance(value, bool)
            or value < base_value
            or value < 1
        ):
            raise ChapterCycleIntegrityError(
                f"capacity permit role cap is not an upward integer: {role}"
            )
    for key in ("max_api_calls_per_chapter", "max_api_calls_per_run"):
        base_value = base_plan.get(key)
        value = candidate.get(key)
        if (
            not isinstance(base_value, int)
            or isinstance(base_value, bool)
            or not isinstance(value, int)
            or isinstance(value, bool)
            or value < base_value
            or value < 1
        ):
            raise ChapterCycleIntegrityError(
                f"capacity permit global cap is not an upward integer: {key}"
            )
    return candidate


class ApiCallPermit:
    """Persist one call reservation before the caller performs transport."""

    def __init__(
        self,
        *,
        run_root: Path,
        plan: Mapping[str, Any],
        stage: ChapterCycleStage,
    ) -> None:
        self.run_root = Path(run_root)
        self.plan = dict(plan)
        self.stage = stage

    def reserve(self, logical_call_id: str) -> dict[str, Any]:
        logical_id = _safe_stage_token(logical_call_id, "logical_call_id")
        if not self.stage.requires_api:
            raise ChapterCycleOrchestratorError(
                "code-only stage cannot reserve an API call"
            )
        state = _load_state_unlocked(self.run_root, self.plan)
        if state["status"] != "running" or state["current_stage"] != self.stage.stage_id:
            raise ChapterCycleOrchestratorError(
                "API call reservation does not own the current running stage"
            )
        existing = dict(
            state["stage_call_reservations"].get(
                self.stage.stage_id,
                {"logical_call_ids": [], "attempt_count": 0},
            )
        )
        logical_ids = list(existing["logical_call_ids"])
        role_cap = int(
            self.plan["logical_call_caps_by_role"][self.stage.stage_role]
        )
        if logical_id not in logical_ids and len(logical_ids) >= role_cap:
            _pause_state_unlocked(
                self.run_root,
                state,
                failure_class="api_call_cap",
                reason=f"logical_call_cap_reached:{self.stage.stage_role}",
            )
            raise ChapterCycleCallCapReached(
                f"logical call cap reached for {self.stage.stage_role}"
            )
        chapter_count = int(state["chapter_api_call_counts"][self.stage.chapter_id])
        run_count = int(state["run_api_call_count"])
        if chapter_count >= int(self.plan["max_api_calls_per_chapter"]):
            _pause_state_unlocked(
                self.run_root,
                state,
                failure_class="api_call_cap",
                reason=f"chapter_api_call_cap_reached:{self.stage.chapter_id}",
            )
            raise ChapterCycleCallCapReached("chapter API call cap reached")
        if run_count >= int(self.plan["max_api_calls_per_run"]):
            _pause_state_unlocked(
                self.run_root,
                state,
                failure_class="api_call_cap",
                reason="run_api_call_cap_reached",
            )
            raise ChapterCycleCallCapReached("run API call cap reached")
        if logical_id not in logical_ids:
            logical_ids.append(logical_id)
        body = _transition_body(state)
        reservations = _clone(dict(body["stage_call_reservations"]))
        reservations[self.stage.stage_id] = {
            "logical_call_ids": logical_ids,
            "attempt_count": int(existing["attempt_count"]) + 1,
        }
        body["stage_call_reservations"] = reservations
        chapter_counts = dict(body["chapter_api_call_counts"])
        chapter_counts[self.stage.chapter_id] = chapter_count + 1
        body["chapter_api_call_counts"] = chapter_counts
        body["run_api_call_count"] = run_count + 1
        updated = _save_state_unlocked(self.run_root, body)
        return {
            "stage_id": self.stage.stage_id,
            "logical_call_id": logical_id,
            "stage_attempt_number": reservations[self.stage.stage_id]["attempt_count"],
            "chapter_api_call_count": chapter_counts[self.stage.chapter_id],
            "run_api_call_count": updated["run_api_call_count"],
        }

    def attempt_count(self) -> int:
        """Return persisted attempts, including a call completed before a crash."""

        state = _load_state_unlocked(self.run_root, self.plan)
        reservation = state["stage_call_reservations"].get(
            self.stage.stage_id,
            {"attempt_count": 0},
        )
        return int(reservation["attempt_count"])


def _stage_result_path(run_root: Path, stage: ChapterCycleStage) -> Path:
    return Path(run_root) / "stages" / stage.stage_id / "stage_result.json"


def _validate_result_against_calls(
    *,
    result: StageExecutionResult,
    stage: ChapterCycleStage,
    state: Mapping[str, Any],
) -> None:
    if result.status not in _RESULT_STATUSES:
        raise ChapterCycleOrchestratorError("stage result status is invalid")
    if result.call_disposition not in _CALL_DISPOSITIONS:
        raise ChapterCycleOrchestratorError("stage call disposition is invalid")
    reservation = state["stage_call_reservations"].get(
        stage.stage_id, {"logical_call_ids": [], "attempt_count": 0}
    )
    actual_calls = int(reservation["attempt_count"])
    if (
        not isinstance(result.attempt_count, int)
        or isinstance(result.attempt_count, bool)
        or result.attempt_count < 0
    ):
        raise ChapterCycleOrchestratorError("stage attempt count is invalid")
    if result.attempt_count != actual_calls:
        raise ChapterCycleOrchestratorError(
            "stage result attempt count differs from persisted call permits"
        )
    if stage.requires_api:
        if result.call_disposition == "code_only":
            raise ChapterCycleOrchestratorError("model stage claims code-only execution")
        if result.call_disposition == "called" and actual_calls < 1:
            raise ChapterCycleOrchestratorError("called stage lacks a call permit")
        if result.call_disposition in {"cache_replay", "not_required"} and actual_calls:
            raise ChapterCycleOrchestratorError(
                "zero-call stage has persisted call permits"
            )
    else:
        if result.call_disposition != "code_only" or actual_calls:
            raise ChapterCycleOrchestratorError("code stage used model-call authority")
    if (
        not isinstance(result.retry_count, int)
        or isinstance(result.retry_count, bool)
        or not 0 <= result.retry_count <= max(0, actual_calls - 1)
    ):
        raise ChapterCycleOrchestratorError("stage retry count is invalid")
    if (
        not isinstance(result.fallback_count, int)
        or isinstance(result.fallback_count, bool)
        or not 0 <= result.fallback_count <= min(1, actual_calls)
    ):
        raise ChapterCycleOrchestratorError("stage fallback count is invalid")
    if (
        not isinstance(result.semantic_pending_count, int)
        or isinstance(result.semantic_pending_count, bool)
        or result.semantic_pending_count < 0
    ):
        raise ChapterCycleOrchestratorError("semantic pending count is invalid")
    if result.status == "semantic_pending":
        if result.semantic_pending_count < 1:
            raise ChapterCycleOrchestratorError(
                "semantic-pending result lacks a pending row count"
            )
    elif result.semantic_pending_count != 0:
        raise ChapterCycleOrchestratorError(
            "accepted result carries an unreported pending row"
        )
    if result.status == "skipped":
        if result.call_disposition not in {"not_required", "code_only"}:
            raise ChapterCycleOrchestratorError(
                "skipped stage carries a model call disposition"
            )
        reason = result.payload.get("skip_reason")
        if not isinstance(reason, str) or not reason:
            raise ChapterCycleOrchestratorError("skipped stage lacks skip_reason")
    if result.call_disposition in {"called", "cache_replay"}:
        _hash_string(result.request_fingerprint, "request_fingerprint")
        _required_string(result.model_actual, "model_actual")
        _hash_string(result.resilience_report_hash, "resilience_report_hash")
    elif any(
        value is not None
        for value in (
            result.request_fingerprint,
            result.model_actual,
            result.resilience_report_hash,
        )
    ):
        raise ChapterCycleOrchestratorError(
            "zero-model result carries model execution metadata"
        )
    if not isinstance(result.payload, Mapping):
        raise ChapterCycleOrchestratorError("stage result payload must be an object")
    if set(result.cumulative_hash_updates) - _CUMULATIVE_HASH_KEYS:
        raise ChapterCycleOrchestratorError("stage updates a foreign cumulative hash")
    for value in result.cumulative_hash_updates.values():
        _hash_string(value, "cumulative hash update")


def _build_stage_result(
    *,
    result: StageExecutionResult,
    stage: ChapterCycleStage,
    state: Mapping[str, Any],
) -> dict[str, Any]:
    _validate_result_against_calls(result=result, stage=stage, state=state)
    body = {
        "schema_version": STAGE_RESULT_SCHEMA_VERSION,
        "stage_descriptor": stage.to_payload(),
        "status": result.status,
        "call_disposition": result.call_disposition,
        "request_fingerprint": result.request_fingerprint,
        "model_actual": result.model_actual,
        "resilience_report_hash": result.resilience_report_hash,
        "attempt_count": result.attempt_count,
        "retry_count": result.retry_count,
        "fallback_count": result.fallback_count,
        "semantic_pending_count": result.semantic_pending_count,
        "payload": _clone(dict(result.payload)),
        "payload_hash": canonical_hash(result.payload),
        "cumulative_hash_updates": dict(result.cumulative_hash_updates),
        "production_publish_performed": False,
    }
    return {**body, "result_hash": canonical_hash(body)}


def _verify_stage_result(path: Path, stage: ChapterCycleStage) -> dict[str, Any]:
    result = _load_json(path, "stage result")
    if set(result) != {
        "schema_version",
        "stage_descriptor",
        "status",
        "call_disposition",
        "request_fingerprint",
        "model_actual",
        "resilience_report_hash",
        "attempt_count",
        "retry_count",
        "fallback_count",
        "semantic_pending_count",
        "payload",
        "payload_hash",
        "cumulative_hash_updates",
        "production_publish_performed",
        "result_hash",
    }:
        raise ChapterCycleIntegrityError("stage result field set drifted")
    if result.get("schema_version") != STAGE_RESULT_SCHEMA_VERSION:
        raise ChapterCycleIntegrityError("foreign stage result schema")
    body = dict(result)
    observed = _hash_string(body.pop("result_hash", None), "result_hash")
    if canonical_hash(body) != observed:
        raise ChapterCycleIntegrityError("stage result hash mismatch")
    descriptor = result.get("stage_descriptor")
    if not isinstance(descriptor, Mapping):
        raise ChapterCycleIntegrityError("stage result descriptor is missing")
    observed_stage = _stage_from_payload(descriptor)
    if observed_stage != stage:
        raise ChapterCycleIntegrityError("stage result belongs to a different stage")
    if result.get("production_publish_performed") is not False:
        raise ChapterCycleIntegrityError("stage result claims production publication")
    payload = result.get("payload")
    if not isinstance(payload, Mapping) or canonical_hash(payload) != result.get(
        "payload_hash"
    ):
        raise ChapterCycleIntegrityError("stage result payload hash mismatch")
    return result


def _execution_result_from_persisted(
    result: Mapping[str, Any],
) -> StageExecutionResult:
    payload = result.get("payload")
    cumulative = result.get("cumulative_hash_updates")
    if not isinstance(payload, Mapping) or not isinstance(cumulative, Mapping):
        raise ChapterCycleIntegrityError("persisted stage result payload is malformed")
    return StageExecutionResult(
        status=result.get("status"),
        payload=payload,
        call_disposition=result.get("call_disposition"),
        request_fingerprint=result.get("request_fingerprint"),
        model_actual=result.get("model_actual"),
        resilience_report_hash=result.get("resilience_report_hash"),
        attempt_count=result.get("attempt_count"),
        retry_count=result.get("retry_count"),
        fallback_count=result.get("fallback_count"),
        semantic_pending_count=result.get("semantic_pending_count"),
        cumulative_hash_updates=cumulative,
    )


def _persist_stage_result(
    *,
    run_root: Path,
    result: StageExecutionResult,
    stage: ChapterCycleStage,
    state: Mapping[str, Any],
) -> dict[str, Any]:
    path = _stage_result_path(run_root, stage)
    if path.is_file():
        return _verify_stage_result(path, stage)
    payload = _build_stage_result(result=result, stage=stage, state=state)
    _write_immutable_json(path, payload)
    return _verify_stage_result(path, stage)


def _build_receipt(
    *, run_root: Path, stage: ChapterCycleStage, result: Mapping[str, Any]
) -> dict[str, Any]:
    artifact_path = _stage_result_path(run_root, stage)
    relative = artifact_path.relative_to(Path(run_root)).as_posix()
    payload = result.get("payload")
    resolved_inputs = (
        dict(payload.get("resolved_inputs") or {})
        if isinstance(payload, Mapping)
        else {}
    )
    body = {
        "schema_version": STAGE_RECEIPT_SCHEMA_VERSION,
        "stage_id": stage.stage_id,
        "chapter_id": stage.chapter_id,
        "chapter_ordinal": stage.chapter_ordinal,
        "stage_descriptor_hash": stage.stage_descriptor_hash,
        "status": result["status"],
        "result_hash": result["result_hash"],
        "artifact_path": relative,
        "artifact_sha256": file_sha256(artifact_path),
        "request_fingerprint": result["request_fingerprint"],
        "model_actual": result["model_actual"],
        "api_calls": result["attempt_count"],
        "retry_count": result["retry_count"],
        "fallback_count": result["fallback_count"],
        "semantic_pending_count": result["semantic_pending_count"],
        "resolved_inputs": resolved_inputs,
        "production_publish_performed": False,
    }
    receipt = {**body, "receipt_hash": canonical_hash(body)}
    receipt_path = Path(run_root) / "receipts" / f"{stage.stage_id}.json"
    _write_immutable_json(receipt_path, receipt)
    return receipt


def _write_chapter_checkpoint_unlocked(
    *,
    run_root: Path,
    plan: Mapping[str, Any],
    state: Mapping[str, Any],
    stage: ChapterCycleStage,
    receipt: Mapping[str, Any],
) -> dict[str, Any]:
    body = {
        "schema_version": CHAPTER_CHECKPOINT_SCHEMA_VERSION,
        "plan_hash": plan["plan_hash"],
        "state_hash": state["state_hash"],
        "state_generation": state["generation"],
        "completed_chapter_id": stage.chapter_id,
        "completed_chapter_count": len(state["completed_chapter_ids"]),
        "next_stage_id": state["current_stage"],
        "checkpoint_stage_receipt_hash": receipt["receipt_hash"],
        "cumulative_hashes": _clone(dict(state["cumulative_hashes"])),
        "run_api_call_count": state["run_api_call_count"],
        "chapter_api_call_counts": _clone(dict(state["chapter_api_call_counts"])),
        "semantic_pending_count": state["semantic_pending_count"],
        "production_publish_performed": False,
    }
    checkpoint = {**body, "checkpoint_hash": canonical_hash(body)}
    checkpoint_path = (
        Path(run_root)
        / "chapter_checkpoints"
        / f"ch{stage.chapter_ordinal:03d}_{checkpoint['checkpoint_hash'][:12]}.json"
    )
    _write_immutable_json(checkpoint_path, checkpoint)
    pointer_body = {
        "schema_version": CHECKPOINT_POINTER_SCHEMA_VERSION,
        "checkpoint_path": checkpoint_path.relative_to(Path(run_root)).as_posix(),
        "checkpoint_sha256": file_sha256(checkpoint_path),
        "checkpoint_hash": checkpoint["checkpoint_hash"],
        "completed_chapter_id": stage.chapter_id,
        "completed_chapter_count": len(state["completed_chapter_ids"]),
        "state_hash": state["state_hash"],
    }
    pointer = {**pointer_body, "pointer_hash": canonical_hash(pointer_body)}
    write_checkpoint_atomic(Path(run_root) / "chapter_checkpoint.json", pointer)
    return checkpoint


def _checkpoint_stage_and_receipt(
    *, plan: Mapping[str, Any], state: Mapping[str, Any]
) -> tuple[ChapterCycleStage, Mapping[str, Any]]:
    completed_count = len(state["completed_chapter_ids"])
    if completed_count < 1:
        raise ChapterCycleIntegrityError("checkpoint requested before a chapter completed")
    stages = [_stage_from_payload(row) for row in plan["stage_plan"]]
    checkpoint_stages = [
        stage
        for stage in stages[: int(state["next_stage_index"])]
        if stage.is_chapter_checkpoint
    ]
    if len(checkpoint_stages) != completed_count:
        raise ChapterCycleIntegrityError("checkpoint stage coverage is stale")
    stage = checkpoint_stages[-1]
    receipts = {
        row["stage_id"]: row
        for row in state["stage_receipts"]
        if isinstance(row, Mapping) and isinstance(row.get("stage_id"), str)
    }
    receipt = receipts.get(stage.stage_id)
    if not isinstance(receipt, Mapping):
        raise ChapterCycleIntegrityError("checkpoint stage receipt is absent")
    return stage, receipt


def _verify_checkpoint_pointer_unlocked(
    *,
    run_root: Path,
    plan: Mapping[str, Any],
    state: Mapping[str, Any],
    pointer: Mapping[str, Any],
) -> None:
    if pointer.get("schema_version") != CHECKPOINT_POINTER_SCHEMA_VERSION:
        raise ChapterCycleIntegrityError("foreign chapter checkpoint pointer schema")
    pointer_body = dict(pointer)
    observed_pointer_hash = _hash_string(
        pointer_body.pop("pointer_hash", None), "pointer_hash"
    )
    if canonical_hash(pointer_body) != observed_pointer_hash:
        raise ChapterCycleIntegrityError("chapter checkpoint pointer hash mismatch")
    stage, receipt = _checkpoint_stage_and_receipt(plan=plan, state=state)
    completed_count = len(state["completed_chapter_ids"])
    if (
        pointer.get("completed_chapter_count") != completed_count
        or pointer.get("completed_chapter_id") != stage.chapter_id
    ):
        raise ChapterCycleIntegrityError("chapter checkpoint pointer is stale")
    relative = Path(
        _required_string(pointer.get("checkpoint_path"), "checkpoint_path")
    )
    if relative.is_absolute() or ".." in relative.parts:
        raise ChapterCycleIntegrityError("chapter checkpoint path escapes run root")
    checkpoint_path = Path(run_root) / relative
    if _checked_file_sha256(
        checkpoint_path, "chapter checkpoint"
    ) != pointer.get("checkpoint_sha256"):
        raise ChapterCycleIntegrityError("chapter checkpoint bytes changed")
    checkpoint = _load_json(checkpoint_path, "chapter checkpoint")
    if checkpoint.get("schema_version") != CHAPTER_CHECKPOINT_SCHEMA_VERSION:
        raise ChapterCycleIntegrityError("foreign chapter checkpoint schema")
    checkpoint_body = dict(checkpoint)
    observed_checkpoint_hash = _hash_string(
        checkpoint_body.pop("checkpoint_hash", None), "checkpoint_hash"
    )
    if canonical_hash(checkpoint_body) != observed_checkpoint_hash:
        raise ChapterCycleIntegrityError("chapter checkpoint hash mismatch")
    if observed_checkpoint_hash != pointer.get("checkpoint_hash"):
        raise ChapterCycleIntegrityError("checkpoint pointer targets a foreign hash")
    if checkpoint.get("plan_hash") != plan.get("plan_hash"):
        raise ChapterCycleIntegrityError("chapter checkpoint belongs to another plan")
    if (
        checkpoint.get("completed_chapter_id") != stage.chapter_id
        or checkpoint.get("completed_chapter_count") != completed_count
        or checkpoint.get("checkpoint_stage_receipt_hash")
        != receipt.get("receipt_hash")
    ):
        raise ChapterCycleIntegrityError("chapter checkpoint coverage is stale")
    if checkpoint.get("state_hash") != pointer.get("state_hash"):
        raise ChapterCycleIntegrityError("checkpoint pointer state hash drifted")
    if state["status"] in {"stopped", "complete"} and checkpoint.get(
        "state_hash"
    ) != state.get("state_hash"):
        raise ChapterCycleIntegrityError("boundary state differs from checkpoint")
    if checkpoint.get("production_publish_performed") is not False:
        raise ChapterCycleIntegrityError("chapter checkpoint claims publication")


def _ensure_chapter_checkpoint_unlocked(
    *,
    run_root: Path,
    plan: Mapping[str, Any],
    state: Mapping[str, Any],
) -> None:
    pointer_path = Path(run_root) / "chapter_checkpoint.json"
    completed_count = len(state["completed_chapter_ids"])
    if completed_count == 0:
        if pointer_path.exists():
            raise ChapterCycleIntegrityError(
                "chapter checkpoint exists before any chapter completed"
            )
        return
    if pointer_path.is_file():
        pointer = _load_json(pointer_path, "chapter checkpoint pointer")
        pointer_count = pointer.get("completed_chapter_count")
        if pointer_count == completed_count:
            _verify_checkpoint_pointer_unlocked(
                run_root=run_root,
                plan=plan,
                state=state,
                pointer=pointer,
            )
            return
        if not isinstance(pointer_count, int) or pointer_count > completed_count:
            raise ChapterCycleIntegrityError("chapter checkpoint pointer moved ahead")
    stage, receipt = _checkpoint_stage_and_receipt(plan=plan, state=state)
    if state["stage_receipts"][-1].get("stage_id") != stage.stage_id:
        raise ChapterCycleIntegrityError(
            "missing checkpoint pointer cannot be repaired after later stage progress"
        )
    _write_chapter_checkpoint_unlocked(
        run_root=run_root,
        plan=plan,
        state=state,
        stage=stage,
        receipt=receipt,
    )


def _complete_stage_unlocked(
    *,
    run_root: Path,
    plan: Mapping[str, Any],
    state: Mapping[str, Any],
    stage: ChapterCycleStage,
    result: Mapping[str, Any],
) -> dict[str, Any]:
    _validate_result_against_calls(
        result=_execution_result_from_persisted(result),
        stage=stage,
        state=state,
    )
    receipt = _build_receipt(run_root=run_root, stage=stage, result=result)
    body = _transition_body(state)
    body["stage_receipts"] = [*body["stage_receipts"], receipt]
    body["next_stage_index"] = int(body["next_stage_index"]) + 1
    stages = plan["stage_plan"]
    next_index = body["next_stage_index"]
    body["current_stage"] = (
        stages[next_index]["stage_id"] if next_index < len(stages) else None
    )
    body["semantic_pending_count"] = int(body["semantic_pending_count"]) + int(
        result["semantic_pending_count"]
    )
    cumulative = dict(body["cumulative_hashes"])
    cumulative.update(dict(result["cumulative_hash_updates"]))
    body["cumulative_hashes"] = cumulative
    body["halt_failure_class"] = None
    body["halt_reason"] = None
    if stage.is_chapter_checkpoint:
        body["completed_chapter_ids"] = [
            *body["completed_chapter_ids"],
            stage.chapter_id,
        ]
        completed_count = len(body["completed_chapter_ids"])
        if completed_count == len(plan["ordered_chapter_ids"]):
            body["status"] = "complete"
        elif completed_count >= int(body["stop_after_chapter_count"]):
            body["status"] = "stopped"
        else:
            body["status"] = "running"
    else:
        body["status"] = "running"
    next_state = _persist_state_generation(run_root, body)
    if stage.is_chapter_checkpoint:
        _publish_state_pointer(run_root, next_state)
        _write_chapter_checkpoint_unlocked(
            run_root=run_root,
            plan=plan,
            state=next_state,
            stage=stage,
            receipt=receipt,
        )
    else:
        _publish_state_pointer(run_root, next_state)
    return next_state


def advance_chapter_cycle_stage_v1(
    *,
    run_root: Path,
    executor: StageExecutor,
    permit_plan: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    root = Path(run_root).resolve()
    with CheckpointLock(root):
        try:
            plan = _load_plan_unlocked(root)
            state = _load_state_unlocked(root, plan)
        except ChapterCycleIntegrityError as exc:
            _write_unbound_integrity_pause(root, str(exc))
            raise
        if state["status"] != "running":
            return state
        stage = _stage_from_payload(plan["stage_plan"][state["next_stage_index"]])
        result_path = _stage_result_path(root, stage)
        if result_path.is_file():
            persisted = _verify_stage_result(result_path, stage)
            state = _load_state_unlocked(root, plan)
            return _complete_stage_unlocked(
                run_root=root,
                plan=plan,
                state=state,
                stage=stage,
                result=persisted,
            )
        effective_permit_plan = _validated_capacity_only_permit_plan(
            plan,
            permit_plan,
        )
        permit = ApiCallPermit(
            run_root=root,
            plan=effective_permit_plan,
            stage=stage,
        )
        try:
            execution_result = executor(stage, permit)
        except ChapterCycleCallCapReached:
            return _load_state_unlocked(root, plan)
        except ResilientStageHalt as exc:
            current = _load_state_unlocked(root, plan)
            return _pause_state_unlocked(
                root,
                current,
                failure_class=str(exc.report.get("failure_class") or "unknown"),
                reason=str(exc.report.get("halt_reason") or "resilient_stage_halt"),
            )
        except (IntegrityOrLineageFailure, ChapterCycleIntegrityError) as exc:
            current = _load_state_unlocked(root, plan)
            paused = _pause_state_unlocked(
                root,
                current,
                failure_class="integrity_or_lineage",
                reason=str(exc),
            )
            _write_unbound_integrity_pause(root, str(exc))
            return paused
        except ChapterCycleStagePause as exc:
            current = _load_state_unlocked(root, plan)
            return _pause_state_unlocked(
                root,
                current,
                failure_class=exc.failure_class,
                reason=exc.reason,
            )
        except Exception as exc:
            current = _load_state_unlocked(root, plan)
            _pause_state_unlocked(
                root,
                current,
                failure_class="unknown",
                reason=f"{type(exc).__name__}:{exc}",
            )
            raise
        state = _load_state_unlocked(root, plan)
        persisted = _persist_stage_result(
            run_root=root,
            result=execution_result,
            stage=stage,
            state=state,
        )
        return _complete_stage_unlocked(
            run_root=root,
            plan=plan,
            state=state,
            stage=stage,
            result=persisted,
        )


def run_chapter_cycle_until_boundary_v1(
    *,
    run_root: Path,
    executor: StageExecutor,
    permit_plan: Mapping[str, Any] | None = None,
    production_writer: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Advance until stop, pause, or completion.

    ``production_writer`` is accepted only as a test spy.  The MVP control
    plane never calls it because publication is outside this contract.
    """

    _ = production_writer
    while True:
        state = load_chapter_cycle_state_v1(run_root)
        if state["status"] != "running":
            return state
        state = advance_chapter_cycle_stage_v1(
            run_root=run_root,
            executor=executor,
            permit_plan=permit_plan,
        )
        if state["status"] != "running":
            return state


def resume_chapter_cycle_run_v1(
    *,
    run_root: Path,
    stop_after_chapter_count: int | None = None,
) -> dict[str, Any]:
    root = Path(run_root).resolve()
    with CheckpointLock(root):
        plan = _load_plan_unlocked(root)
        state = _load_state_unlocked(root, plan)
        if state["status"] == "complete":
            raise ChapterCycleOrchestratorError("completed run cannot be resumed")
        completed_count = len(state["completed_chapter_ids"])
        requested = (
            int(state["stop_after_chapter_count"])
            if stop_after_chapter_count is None
            else stop_after_chapter_count
        )
        if (
            not isinstance(requested, int)
            or isinstance(requested, bool)
            or not completed_count < requested <= len(plan["ordered_chapter_ids"])
        ):
            raise ChapterCycleOrchestratorError(
                "resume stop-after count must extend beyond completed chapters"
            )
        body = _transition_body(state)
        body["status"] = "running"
        body["halt_failure_class"] = None
        body["halt_reason"] = None
        body["stop_after_chapter_count"] = requested
        return _save_state_unlocked(root, body)


def stage_result_from_resilience_report_v1(
    *,
    report: Mapping[str, Any],
    payload: Mapping[str, Any],
    call_disposition: str,
    retry_count: int,
    fallback_count: int,
    semantic_pending_count: int = 0,
    cumulative_hash_updates: Mapping[str, str] | None = None,
) -> StageExecutionResult:
    """Adapt a verified R-lite report without interpreting literary content."""

    status = report.get("status")
    semantic_status = report.get("semantic_status")
    if status not in {"accepted", "accepted_semantic_pending"}:
        raise ChapterCycleOrchestratorError("R-lite report is not accepted")
    result_status = (
        "semantic_pending" if semantic_status == "pending" else "accepted"
    )
    return StageExecutionResult(
        status=result_status,
        payload=payload,
        call_disposition=call_disposition,
        request_fingerprint=_hash_string(
            report.get("request_fingerprint"), "request_fingerprint"
        ),
        model_actual=_required_string(report.get("model_actual"), "model_actual"),
        resilience_report_hash=_hash_string(
            report.get("resilience_report_hash"), "resilience_report_hash"
        ),
        attempt_count=int(report.get("attempt_count") or 0),
        retry_count=retry_count,
        fallback_count=fallback_count,
        semantic_pending_count=semantic_pending_count,
        cumulative_hash_updates=dict(cumulative_hash_updates or {}),
    )


__all__ = [
    "ApiCallPermit",
    "CHAPTER_CHECKPOINT_SCHEMA_VERSION",
    "ChapterCycleCallCapReached",
    "ChapterCycleIntegrityError",
    "ChapterCycleOrchestratorError",
    "ChapterCycleStage",
    "ChapterCycleStagePause",
    "StageExecutionResult",
    "advance_chapter_cycle_stage_v1",
    "build_dynamic_stage_plan_v1",
    "current_chapter_cycle_stage_v1",
    "initialize_chapter_cycle_run_v1",
    "load_chapter_cycle_plan_v1",
    "load_chapter_cycle_state_v1",
    "resume_chapter_cycle_run_v1",
    "run_chapter_cycle_until_boundary_v1",
    "stage_result_from_resilience_report_v1",
]
