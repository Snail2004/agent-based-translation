"""Owning validator and App handoff for Literary chapter-loop history."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from pipeline.literary.checkpoint import canonical_hash, file_sha256
from pipeline.literary.project_source_bridge_v1 import (
    validate_literary_project_binding_v1,
)


VALIDATOR_ID = "literary.chapter_loop.component_validator_v1"
VALIDATOR_REVISION = "v1"
HANDOFF_SCHEMA = "literary_workflow_replay_handoff_v1"
APP_REGISTRATION_SCHEMA = "literary_app_run_registration_handoff_v1"
LITERARY_APP_SCRIPT_ID = "run_literary_project_chapter_loop_v1"

_STATUSES = {"running", "paused", "failed", "succeeded"}
_SEVERITIES = {"info", "warning", "error"}
_FORBIDDEN_PUBLIC_KEYS = {
    "api_key",
    "authorization",
    "bearer",
    "bearer_token",
    "credential",
    "credentials",
    "gold",
    "gold_translation",
    "human_reference",
    "human_translation",
    "prompt",
    "raw_prompt",
    "raw_request",
    "raw_response",
    "reference_translation",
    "request_body",
    "response",
    "response_body",
    "secret",
}
_MANDATORY_FILES = {
    "component_manifest.json",
    "events.jsonl",
    "artifact_index.json",
    "run_plan.json",
    "chapter_loop_session.json",
}


class LiteraryChapterLoopComponentError(ValueError):
    pass


def validate_literary_chapter_loop_component_v1(
    component_root: Path,
    *,
    require_terminal: bool = False,
) -> dict[str, Any]:
    root = Path(component_root).resolve()
    if not root.is_dir() or root.is_symlink():
        raise LiteraryChapterLoopComponentError(
            "component root must be a real directory"
        )
    for relative in sorted(_MANDATORY_FILES):
        _component_file(root, relative)

    manifest = _read_object(root / "component_manifest.json", "component manifest")
    required_manifest = {
        "schema_version",
        "workflow_run_id",
        "component_id",
        "component_run_id",
        "component_attempt_id",
        "component_attempt_index",
        "flow_kind",
        "status",
        "plan_hash",
        "event_log_ref",
        "artifact_index_ref",
    }
    if not required_manifest.issubset(manifest):
        raise LiteraryChapterLoopComponentError(
            "component manifest lacks required replay fields"
        )
    if manifest["schema_version"] != "literary_chapter_loop_component_manifest_v2":
        raise LiteraryChapterLoopComponentError("component manifest schema differs")
    workflow_run_id = _identifier(
        manifest["workflow_run_id"], "workflow_run_id"
    )
    if manifest["component_id"] != "translation":
        raise LiteraryChapterLoopComponentError(
            "Literary history must occupy the translation component"
        )
    if manifest["component_run_id"] != workflow_run_id:
        raise LiteraryChapterLoopComponentError("component run identity drifted")
    if (
        manifest["component_attempt_id"] != 1
        or manifest["component_attempt_index"] != 1
    ):
        raise LiteraryChapterLoopComponentError(
            "Literary component attempt identity is invalid"
        )
    if manifest["flow_kind"] != "literary_chapter_loop":
        raise LiteraryChapterLoopComponentError("component flow kind differs")
    status = manifest["status"]
    if status not in _STATUSES:
        raise LiteraryChapterLoopComponentError("component status is invalid")
    if require_terminal and status not in {"failed", "succeeded"}:
        raise LiteraryChapterLoopComponentError("component is not terminal")

    plan = _read_object(root / "run_plan.json", "chapter-loop plan")
    plan_body = dict(plan)
    plan_hash = plan_body.pop("plan_hash", None)
    if not isinstance(plan_hash, str) or canonical_hash(plan_body) != plan_hash:
        raise LiteraryChapterLoopComponentError("chapter-loop plan hash drifted")
    if manifest["plan_hash"] != plan_hash:
        raise LiteraryChapterLoopComponentError(
            "component manifest belongs to another plan"
        )
    stage_rows = plan.get("stage_plan")
    if not isinstance(stage_rows, list) or not stage_rows:
        raise LiteraryChapterLoopComponentError("chapter-loop plan has no stages")
    stage_ids = {
        _identifier(row.get("stage_id"), "stage_id")
        for row in stage_rows
        if isinstance(row, Mapping)
    }
    if len(stage_ids) != len(stage_rows):
        raise LiteraryChapterLoopComponentError("chapter-loop stage IDs repeat")

    session = _read_object(
        root / "chapter_loop_session.json", "chapter-loop session"
    )
    session_body = dict(session)
    session_hash = session_body.pop("session_hash", None)
    if (
        not isinstance(session_hash, str)
        or canonical_hash(session_body) != session_hash
    ):
        raise LiteraryChapterLoopComponentError("chapter-loop session hash drifted")
    if (
        session.get("run_id") != workflow_run_id
        or session.get("plan_hash") != plan_hash
    ):
        raise LiteraryChapterLoopComponentError(
            "chapter-loop session identity drifted"
        )

    project_binding = None
    project_ref = manifest.get("project_binding_ref")
    if project_ref is not None:
        project_path = _component_file(root, _relative_ref(project_ref))
        project_binding = validate_literary_project_binding_v1(
            _read_object(project_path, "literary project binding")
        )
        if (
            manifest.get("project_binding_hash")
            != project_binding["binding_hash"]
            or session.get("project_binding_hash")
            != project_binding["binding_hash"]
        ):
            raise LiteraryChapterLoopComponentError(
                "project binding identity drifted"
            )
        if file_sha256(project_path) != session.get("project_binding_sha256"):
            raise LiteraryChapterLoopComponentError(
                "project binding physical hash drifted"
            )
        for key in ("project_id", "job_id", "source_identity_sha256"):
            if manifest.get(key) != project_binding[key]:
                raise LiteraryChapterLoopComponentError(
                    f"component project identity drifted at {key}"
                )

    event_rows, event_bytes = _load_event_rows(root / "events.jsonl")
    if not event_rows:
        raise LiteraryChapterLoopComponentError("component event stream is empty")
    event_ids: set[str] = set()
    for index, row in enumerate(event_rows, start=1):
        required = {
            "event_id",
            "workflow_run_id",
            "component_id",
            "component_run_id",
            "component_attempt_id",
            "component_attempt_index",
            "component_seq",
            "ts",
            "stage_id",
            "agent",
            "event",
            "severity",
            "payload",
        }
        if not required.issubset(row):
            raise LiteraryChapterLoopComponentError(
                f"event {index} lacks replay fields"
            )
        if "seq" in row:
            raise LiteraryChapterLoopComponentError(
                "component event cannot assign parent seq"
            )
        event_id = _identifier(row["event_id"], "event_id")
        if event_id in event_ids:
            raise LiteraryChapterLoopComponentError("component event ID repeats")
        event_ids.add(event_id)
        if (
            row["workflow_run_id"] != workflow_run_id
            or row["component_id"] != "translation"
            or row["component_run_id"] != workflow_run_id
            or row["component_attempt_id"] != 1
            or row["component_attempt_index"] != 1
        ):
            raise LiteraryChapterLoopComponentError(
                "component event identity drifted"
            )
        if row["component_seq"] != index:
            raise LiteraryChapterLoopComponentError(
                "component event sequence is not contiguous"
            )
        stage_id = row["stage_id"]
        if stage_id != "__component__" and stage_id not in stage_ids:
            raise LiteraryChapterLoopComponentError(
                f"component event cites unknown stage {stage_id}"
            )
        _identifier(row["agent"], "event agent")
        _identifier(row["event"], "event name")
        if row["severity"] not in _SEVERITIES:
            raise LiteraryChapterLoopComponentError("event severity is invalid")
        _assert_public_payload(row["payload"], path="payload")
        if len(event_bytes[index - 1]) > 65536:
            raise LiteraryChapterLoopComponentError("event exceeds 64 KiB")
    if event_rows[0]["event"] != "component_started":
        raise LiteraryChapterLoopComponentError(
            "component stream must begin with component_started"
        )
    last_event = event_rows[-1]["event"]
    if status == "succeeded" and last_event != "component_done":
        raise LiteraryChapterLoopComponentError(
            "succeeded component lacks component_done"
        )
    if status == "failed" and last_event != "component_failed":
        raise LiteraryChapterLoopComponentError(
            "failed component lacks component_failed"
        )
    if status not in {"failed", "succeeded"} and last_event in {
        "component_done",
        "component_failed",
    }:
        raise LiteraryChapterLoopComponentError(
            "nonterminal component carries a terminal event"
        )

    index = _read_object(root / "artifact_index.json", "artifact index")
    index_body = dict(index)
    index_hash = index_body.pop("index_hash", None)
    if not isinstance(index_hash, str) or canonical_hash(index_body) != index_hash:
        raise LiteraryChapterLoopComponentError("artifact index hash drifted")
    if (
        index.get("schema_version")
        != "literary_chapter_loop_artifact_index_v2"
        or index.get("workflow_run_id") != workflow_run_id
    ):
        raise LiteraryChapterLoopComponentError(
            "artifact index identity drifted"
        )
    artifacts = index.get("artifacts")
    if not isinstance(artifacts, list):
        raise LiteraryChapterLoopComponentError("artifact rows must be a list")
    artifact_refs: set[str] = set()
    relative_paths: set[str] = set()
    for row in artifacts:
        if not isinstance(row, Mapping):
            raise LiteraryChapterLoopComponentError("artifact row must be an object")
        required = {
            "artifact_ref",
            "artifact_kind",
            "schema_version",
            "sha256",
            "sha256_kind",
            "relative_path",
            "producer_stage_id",
            "parent_artifact_refs",
            "created_event_id",
        }
        if not required.issubset(row):
            raise LiteraryChapterLoopComponentError(
                "artifact row lacks replay fields"
            )
        artifact_ref = _relative_ref(row["artifact_ref"])
        if artifact_ref in artifact_refs:
            raise LiteraryChapterLoopComponentError("artifact ref repeats")
        artifact_refs.add(artifact_ref)
        relative = _relative_ref(row["relative_path"])
        if relative in relative_paths:
            raise LiteraryChapterLoopComponentError(
                "artifact source path repeats"
            )
        relative_paths.add(relative)
        artifact_path = _component_file(root, relative)
        if row["sha256_kind"] != "physical":
            raise LiteraryChapterLoopComponentError(
                "Literary component artifacts must bind physical bytes"
            )
        if file_sha256(artifact_path) != row["sha256"]:
            raise LiteraryChapterLoopComponentError(
                f"artifact physical hash drifted: {artifact_ref}"
            )
        if row["producer_stage_id"] not in stage_ids:
            raise LiteraryChapterLoopComponentError(
                "artifact producer stage is unknown"
            )
        if row["created_event_id"] not in event_ids:
            raise LiteraryChapterLoopComponentError(
                "artifact creator event is unknown"
            )
        parents = row["parent_artifact_refs"]
        if not isinstance(parents, list) or any(
            not isinstance(parent, str) for parent in parents
        ):
            raise LiteraryChapterLoopComponentError(
                "artifact parents must be a string list"
            )
    for row in artifacts:
        unknown = set(row["parent_artifact_refs"]) - artifact_refs
        if unknown:
            raise LiteraryChapterLoopComponentError(
                "artifact cites unknown parents: " + ", ".join(sorted(unknown))
            )
    semantic_deltas = {
        path.relative_to(root).as_posix()
        for path in root.rglob("semantic_delta.json")
        if path.is_file()
    }
    if not semantic_deltas.issubset(relative_paths):
        raise LiteraryChapterLoopComponentError(
            "semantic delta exists outside the artifact index"
        )

    receipt_body = {
        "schema_version": "literary_component_validation_receipt_v1",
        "validator_id": VALIDATOR_ID,
        "validator_revision": VALIDATOR_REVISION,
        "workflow_run_id": workflow_run_id,
        "component_run_id": workflow_run_id,
        "status": status,
        "plan_hash": plan_hash,
        "project_binding_hash": (
            project_binding["binding_hash"] if project_binding else None
        ),
        "event_count": len(event_rows),
        "event_log_sha256": file_sha256(root / "events.jsonl"),
        "artifact_count": len(artifacts),
        "artifact_index_sha256": file_sha256(root / "artifact_index.json"),
    }
    receipt = {**receipt_body, "receipt_hash": canonical_hash(receipt_body)}
    return {
        "manifest": manifest,
        "plan": plan,
        "session": session,
        "project_binding": project_binding,
        "events": event_rows,
        "event_bytes": event_bytes,
        "artifact_index": index,
        "validation_receipt": receipt,
    }


def build_literary_workflow_handoff_v1(
    *,
    component_root: Path,
    component_root_ref: str,
) -> dict[str, Any]:
    validation = validate_literary_chapter_loop_component_v1(component_root)
    manifest = validation["manifest"]
    project = validation["project_binding"]
    if project is None:
        raise LiteraryChapterLoopComponentError(
            "App handoff requires a project-bound Literary run"
        )
    titles = _chapter_titles(component_root, validation["plan"])
    stage_definitions = []
    for order, row in enumerate(validation["plan"]["stage_plan"], start=1):
        local_stage_id = row["stage_id"]
        stage_definitions.append(
            {
                "stage_id": f"translation.{local_stage_id}",
                "component_id": "translation",
                "local_stage_id": local_stage_id,
                "order": order,
                "label": (
                    f"{titles.get(row['chapter_id'], row['chapter_id'])} - "
                    f"{_stage_label(row['stage_name'])}"
                ),
                "producer": _stage_producer(row["stage_name"]),
            }
        )
    body = {
        "schema_version": HANDOFF_SCHEMA,
        "workflow_run_id": manifest["workflow_run_id"],
        "component_id": "translation",
        "component_flow_kind": manifest["flow_kind"],
        "component_run_id": manifest["component_run_id"],
        "component_root_ref": _relative_ref(component_root_ref),
        "project_id": project["project_id"],
        "job_id": project["job_id"],
        "source_identity_sha256": project["source_identity_sha256"],
        "project_binding_hash": project["binding_hash"],
        "plan_hash": manifest["plan_hash"],
        "adapter_id": VALIDATOR_ID,
        "adapter_revision": VALIDATOR_REVISION,
        "stage_definitions": stage_definitions,
        "production_publish_performed": False,
    }
    return {**body, "handoff_hash": canonical_hash(body)}


def validate_literary_workflow_handoff_v1(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    row = dict(value)
    if row.get("schema_version") != HANDOFF_SCHEMA:
        raise LiteraryChapterLoopComponentError("workflow handoff schema differs")
    observed = row.pop("handoff_hash", None)
    if not isinstance(observed, str) or canonical_hash(row) != observed:
        raise LiteraryChapterLoopComponentError("workflow handoff hash drifted")
    if row.get("component_id") != "translation":
        raise LiteraryChapterLoopComponentError("workflow handoff component differs")
    stages = row.get("stage_definitions")
    if not isinstance(stages, list) or not stages:
        raise LiteraryChapterLoopComponentError("workflow handoff has no stages")
    local_ids: set[str] = set()
    global_ids: set[str] = set()
    for index, stage in enumerate(stages, start=1):
        if not isinstance(stage, Mapping):
            raise LiteraryChapterLoopComponentError("handoff stage must be an object")
        local = _identifier(stage.get("local_stage_id"), "local_stage_id")
        global_id = _identifier(stage.get("stage_id"), "stage_id")
        if (
            stage.get("component_id") != "translation"
            or stage.get("order") != index
            or global_id != f"translation.{local}"
        ):
            raise LiteraryChapterLoopComponentError(
                "workflow handoff stage identity drifted"
            )
        if local in local_ids or global_id in global_ids:
            raise LiteraryChapterLoopComponentError("workflow handoff stage repeats")
        local_ids.add(local)
        global_ids.add(global_id)
    return {**row, "handoff_hash": observed}


def build_literary_app_run_registration_v1(
    *,
    component_root: Path,
    component_root_ref: str,
    workflow_replay_root_ref: str,
) -> dict[str, Any]:
    """Build a non-launching handoff for the App's existing RunRegistry."""

    validation = validate_literary_chapter_loop_component_v1(component_root)
    manifest = validation["manifest"]
    project = validation["project_binding"]
    if project is None:
        raise LiteraryChapterLoopComponentError(
            "App run registration requires a project-bound Literary run"
        )
    component_ref = _relative_ref(component_root_ref)
    replay_ref = _relative_ref(workflow_replay_root_ref)
    body = {
        "schema_version": APP_REGISTRATION_SCHEMA,
        "registration_mode": "app_run_registry_import",
        "script": LITERARY_APP_SCRIPT_ID,
        "run_id": manifest["workflow_run_id"],
        "project_id": project["project_id"],
        "job_id": project["job_id"],
        "workflow_run_id": manifest["workflow_run_id"],
        "component_id": "translation",
        "component_run_id": manifest["component_run_id"],
        "component_attempt_id": manifest["component_attempt_id"],
        "selected_chapter_ids": list(project["selected_chapter_ids"]),
        "profile_id": "literary_v1",
        "registry_status": manifest["status"],
        "component_root_ref": component_ref,
        "component_manifest_ref": (
            f"{component_ref}/component_manifest.json"
        ),
        "component_event_log_ref": f"{component_ref}/events.jsonl",
        "workflow_replay_root_ref": replay_ref,
        "source_identity_sha256": project["source_identity_sha256"],
        "project_binding_hash": project["binding_hash"],
        "plan_hash": manifest["plan_hash"],
        "launch_authority": "none",
        "canonical_project_mutated": False,
        "production_publish_performed": False,
    }
    return {**body, "registration_hash": canonical_hash(body)}


def validate_literary_app_run_registration_v1(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    row = dict(value)
    if row.get("schema_version") != APP_REGISTRATION_SCHEMA:
        raise LiteraryChapterLoopComponentError(
            "App run registration schema differs"
        )
    observed = row.pop("registration_hash", None)
    if not isinstance(observed, str) or canonical_hash(row) != observed:
        raise LiteraryChapterLoopComponentError(
            "App run registration hash drifted"
        )
    if (
        row.get("registration_mode") != "app_run_registry_import"
        or row.get("script") != LITERARY_APP_SCRIPT_ID
        or row.get("component_id") != "translation"
        or row.get("component_attempt_id") != 1
        or row.get("launch_authority") != "none"
    ):
        raise LiteraryChapterLoopComponentError(
            "App run registration contract differs"
        )
    if (
        row.get("run_id") != row.get("workflow_run_id")
        or row.get("component_run_id") != row.get("workflow_run_id")
    ):
        raise LiteraryChapterLoopComponentError(
            "App run registration identity drifted"
        )
    _identifier(row.get("project_id"), "project_id")
    _identifier(row.get("job_id"), "job_id")
    _identifier(row.get("workflow_run_id"), "workflow_run_id")
    selected = row.get("selected_chapter_ids")
    if (
        not isinstance(selected, list)
        or not selected
        or len(selected) != len(set(selected))
        or any(not isinstance(item, str) or not item for item in selected)
    ):
        raise LiteraryChapterLoopComponentError(
            "App run registration chapter scope is invalid"
        )
    for key in (
        "component_root_ref",
        "component_manifest_ref",
        "component_event_log_ref",
        "workflow_replay_root_ref",
    ):
        _relative_ref(row.get(key))
    if row.get("registry_status") not in _STATUSES:
        raise LiteraryChapterLoopComponentError(
            "App run registration status is invalid"
        )
    return {**row, "registration_hash": observed}


def _chapter_titles(root: Path, plan: Mapping[str, Any]) -> dict[str, str]:
    source = Path(str(plan["document_path"]))
    if not source.is_file():
        return {}
    document = _read_object(source, "projected literary document")
    return {
        str(row["chapter_id"]): str(row.get("title") or row["chapter_id"])
        for row in document.get("chapters") or []
        if isinstance(row, Mapping) and isinstance(row.get("chapter_id"), str)
    }


def _stage_label(stage_name: str) -> str:
    labels = {
        "b1_scan": "B1 Scan",
        "b1_enrich": "B1 Enrich",
        "b1_local_auditor": "B1 Local Auditor",
        "b1_registry_writer": "B1 Registry Writer",
        "xchapter_prepare": "Cross-chapter Prepare",
        "xchapter_hearing": "Cross-chapter Hearing",
        "identity_apply": "Identity Apply",
        "b1_to_b2_input": "B2 Input",
        "b2_frame_interaction": "B2 Frame + Interaction",
        "b2_review_routing": "B2 Review Routing",
        "speaker_recovery": "Speaker Recovery",
        "b3_temporal": "B3 Temporal",
        "b3_auditor": "B3 Auditor",
        "b3_apply": "B3 Apply",
        "b0_summary": "B0 Summary",
        "checkpoint": "Checkpoint",
    }
    return labels.get(stage_name, stage_name.replace("_", " ").title())


def _stage_producer(stage_name: str) -> str:
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


def _load_event_rows(path: Path) -> tuple[list[dict[str, Any]], list[bytes]]:
    rows: list[dict[str, Any]] = []
    encoded: list[bytes] = []
    for raw in path.read_bytes().splitlines():
        if not raw.strip():
            continue
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise LiteraryChapterLoopComponentError(
                "event stream contains invalid JSON"
            ) from exc
        if not isinstance(value, dict):
            raise LiteraryChapterLoopComponentError("event row must be an object")
        rows.append(value)
        encoded.append(raw)
    return rows, encoded


def _assert_public_payload(value: Any, *, path: str) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).lower()
            if (
                normalized in _FORBIDDEN_PUBLIC_KEYS
                or normalized.startswith("raw_prompt")
                or normalized.startswith("raw_response")
            ):
                raise LiteraryChapterLoopComponentError(
                    f"private field is forbidden in public event payload: {path}.{key}"
                )
            _assert_public_payload(child, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_public_payload(child, path=f"{path}[{index}]")


def _relative_ref(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise LiteraryChapterLoopComponentError("relative reference is invalid")
    ref = PurePosixPath(value.replace("\\", "/"))
    if ref.is_absolute() or ".." in ref.parts or "." in ref.parts:
        raise LiteraryChapterLoopComponentError("relative reference is unsafe")
    return ref.as_posix()


def _component_file(root: Path, relative: str) -> Path:
    ref = _relative_ref(relative)
    path = (root / Path(*PurePosixPath(ref).parts)).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise LiteraryChapterLoopComponentError(
            "component file escapes the root"
        ) from exc
    if not path.is_file() or path.is_symlink():
        raise LiteraryChapterLoopComponentError(
            f"component file is absent or unsafe: {relative}"
        )
    return path


def _identifier(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 192
        or any(char.isspace() for char in value)
    ):
        raise LiteraryChapterLoopComponentError(f"{label} is invalid")
    return value


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, TypeError, ValueError) as exc:
        raise LiteraryChapterLoopComponentError(f"cannot load {label}") from exc
    if not isinstance(value, dict):
        raise LiteraryChapterLoopComponentError(f"{label} must be an object")
    return value


__all__ = [
    "HANDOFF_SCHEMA",
    "LiteraryChapterLoopComponentError",
    "VALIDATOR_ID",
    "VALIDATOR_REVISION",
    "build_literary_workflow_handoff_v1",
    "validate_literary_chapter_loop_component_v1",
    "validate_literary_workflow_handoff_v1",
]
