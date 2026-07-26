"""Fast, local Resume receipt for a paused D2L component package.

The owning package validator remains the fallback authority.  This receipt is
an operational cache: it binds the small authoritative files cryptographically
and binds append-only journals by their exact filesystem identity and sealed
tail.  Any drift rejects the fast path and forces full validation.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

from pipeline.prepass.d2l_console_replay_contract_v1 import (
    canonical_sha256,
    file_sha256,
    validate_artifact_index,
    validate_checkpoint,
    validate_component_manifest,
    validate_component_usage_snapshot,
    write_json,
)


SCHEMA_VERSION = "d2l_resume_checkpoint_receipt_v1"
VALIDATION_MODE = "sealed_checkpoint_receipt"
RECEIPT_REF = "runtime/resume_checkpoint_receipt_v1.json"
_INTEGRITY_PLACEHOLDER = "0" * 64


class D2LResumeCheckpointReceiptError(ValueError):
    pass


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise D2LResumeCheckpointReceiptError(f"{label} must be an object")
    return dict(value)


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise D2LResumeCheckpointReceiptError(f"{label} is missing") from exc
    except json.JSONDecodeError as exc:
        raise D2LResumeCheckpointReceiptError(
            f"{label} is not valid JSON"
        ) from exc
    return _mapping(value, label)


def _relative_path(root: Path, value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise D2LResumeCheckpointReceiptError(
            f"{label} must be a non-empty relative path"
        )
    if "\\" in value:
        raise D2LResumeCheckpointReceiptError(
            f"{label} must use canonical forward slashes"
        )
    relative = Path(value)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise D2LResumeCheckpointReceiptError(
            f"{label} must be a confined relative path"
        )
    candidate = (root / relative).resolve()
    resolved_root = root.resolve()
    if candidate != resolved_root and resolved_root not in candidate.parents:
        raise D2LResumeCheckpointReceiptError(f"{label} escapes component root")
    return candidate


def _last_nonempty_line(path: Path) -> bytes:
    try:
        size = path.stat().st_size
    except FileNotFoundError as exc:
        raise D2LResumeCheckpointReceiptError(
            f"sealed file is missing: {path.name}"
        ) from exc
    if size <= 0:
        raise D2LResumeCheckpointReceiptError(
            f"sealed file is empty: {path.name}"
        )
    with path.open("rb") as handle:
        cursor = size
        suffix = b""
        while cursor:
            amount = min(cursor, 8192)
            cursor -= amount
            handle.seek(cursor)
            suffix = handle.read(amount) + suffix
            lines = suffix.rstrip(b"\r\n").splitlines()
            if len(lines) >= 2 or cursor == 0:
                if not lines:
                    break
                return lines[-1]
    raise D2LResumeCheckpointReceiptError(
        f"sealed file has no non-empty line: {path.name}"
    )


def _line_json(path: Path, label: str) -> tuple[bytes, dict[str, Any]]:
    raw = _last_nonempty_line(path)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise D2LResumeCheckpointReceiptError(
            f"{label} final line is invalid"
        ) from exc
    return raw, _mapping(value, f"{label} final line")


def _stat_binding(path: Path) -> dict[str, int]:
    stat = path.stat()
    return {
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
    }


def _require_stat(path: Path, binding: Mapping[str, Any], label: str) -> None:
    expected = _mapping(binding, label)
    observed = _stat_binding(path)
    if expected != observed:
        raise D2LResumeCheckpointReceiptError(
            f"{label} filesystem identity drift"
        )


def _receipt_sha256(receipt: Mapping[str, Any]) -> str:
    payload = dict(receipt)
    payload["integrity"] = {"receipt_sha256": _INTEGRITY_PLACEHOLDER}
    return canonical_sha256(payload)


def _journal_binding(
    root: Path,
    *,
    relative_ref: str,
    entry_count: int,
    last_entry_sha256: str | None,
) -> dict[str, Any]:
    path = _relative_path(root, relative_ref, "journal_ref")
    if entry_count == 0:
        if path.exists() and path.stat().st_size:
            raise D2LResumeCheckpointReceiptError(
                f"empty journal state disagrees with {relative_ref}"
            )
        return {
            "journal_ref": relative_ref,
            "entry_count": 0,
            "last_entry_sha256": None,
            "file": None,
        }
    raw, row = _line_json(path, relative_ref)
    if row.get("entry_sha256") != last_entry_sha256:
        raise D2LResumeCheckpointReceiptError(
            f"{relative_ref} tail hash disagrees with checkpoint"
        )
    return {
        "journal_ref": relative_ref,
        "entry_count": int(entry_count),
        "last_entry_sha256": str(last_entry_sha256),
        "tail_line_sha256": canonical_sha256(
            {"raw_utf8": raw.decode("utf-8")}
        ),
        "file": _stat_binding(path),
    }


def _artifact_file_bindings(
    root: Path,
    index: Mapping[str, Any],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    artifacts = index.get("artifacts")
    if not isinstance(artifacts, list):
        raise D2LResumeCheckpointReceiptError(
            "artifact index artifacts must be an array"
        )
    for artifact in artifacts:
        row = _mapping(artifact, "artifact")
        if row.get("availability") != "available":
            continue
        relative_ref = str(row.get("relative_path") or "")
        path = _relative_path(root, relative_ref, "artifact.relative_path")
        rows.append(
            {
                "artifact_ref": str(row.get("artifact_ref") or ""),
                "relative_path": relative_ref,
                "sha256": str(row.get("sha256") or ""),
                "file": _stat_binding(path),
            }
        )
    rows.sort(key=lambda row: (row["relative_path"], row["artifact_ref"]))
    return rows


def can_reuse_term_lifecycle_projection(
    component_root: str | Path,
    *,
    workflow_run_id: str,
    component_run_id: str,
    maximum_component_attempt_id: int,
    work_journals: Mapping[str, Mapping[str, Any]],
) -> bool:
    """Return whether the last sealed projection still covers current journals."""

    root = Path(component_root).resolve()
    try:
        receipt = _load_json(
            root / RECEIPT_REF,
            "Resume checkpoint receipt",
        )
        if receipt.get("schema_version") != SCHEMA_VERSION:
            return False
        integrity = _mapping(receipt.get("integrity"), "receipt integrity")
        if integrity.get("receipt_sha256") != _receipt_sha256(receipt):
            return False
        if (
            receipt.get("workflow_run_id") != workflow_run_id
            or receipt.get("component_run_id") != component_run_id
            or not receipt.get("term_lifecycle_projection_complete")
        ):
            return False
        receipt_attempt = receipt.get("component_attempt_id")
        if (
            isinstance(receipt_attempt, bool)
            or not isinstance(receipt_attempt, int)
            or receipt_attempt > maximum_component_attempt_id
        ):
            return False

        expected = {
            str(stage_id): {
                "journal_ref": str(binding["journal_ref"]),
                "journal_sha256": str(binding["journal_sha256"]),
                "entry_count": int(binding["entry_count"]),
                "last_entry_sha256": binding["last_entry_sha256"],
            }
            for stage_id, binding in work_journals.items()
        }
        observed: dict[str, dict[str, Any]] = {}
        for raw in receipt.get("work_journals") or []:
            binding = _mapping(raw, "receipt work journal")
            stage_id = str(binding.get("stage_id") or "")
            if not stage_id or stage_id in observed:
                return False
            observed[stage_id] = {
                "journal_ref": str(binding.get("journal_ref") or ""),
                "journal_sha256": str(binding.get("journal_sha256") or ""),
                "entry_count": int(binding.get("entry_count") or 0),
                "last_entry_sha256": binding.get("last_entry_sha256"),
            }
        return observed == expected
    except (
        D2LResumeCheckpointReceiptError,
        KeyError,
        TypeError,
        ValueError,
    ):
        return False


def write_resume_checkpoint_receipt(
    component_root: str | Path,
    *,
    latest_usage_snapshot: Mapping[str, Any] | None = None,
    term_lifecycle_projection_complete: bool,
) -> dict[str, Any]:
    root = Path(component_root).resolve()
    manifest_path = root / "component_manifest.json"
    index_path = root / "artifact_index.json"
    events_path = root / "events.jsonl"
    manifest = validate_component_manifest(
        _load_json(manifest_path, "component manifest")
    )
    if manifest["status"] != "paused":
        raise D2LResumeCheckpointReceiptError(
            "fast Resume receipt requires a paused component"
        )
    resume = _mapping(manifest.get("resume"), "component manifest resume")
    if not resume.get("resume_available"):
        raise D2LResumeCheckpointReceiptError(
            "fast Resume receipt requires resume_available"
        )
    checkpoint_ref = str(resume.get("checkpoint_ref") or "")
    checkpoint_path = _relative_path(root, checkpoint_ref, "checkpoint_ref")
    checkpoint = validate_checkpoint(
        _load_json(checkpoint_path, "checkpoint"),
        manifest=manifest,
    )
    checkpoint_state = _mapping(checkpoint.get("state"), "checkpoint state")
    checkpoint_sha256 = file_sha256(checkpoint_path)
    if checkpoint_sha256 != resume.get("checkpoint_sha256"):
        raise D2LResumeCheckpointReceiptError("checkpoint hash drift")
    sealed_usage_sha256 = checkpoint_state.get(
        "latest_usage_snapshot_sha256"
    )
    if latest_usage_snapshot is None:
        normalized_usage_snapshot = None
        if sealed_usage_sha256 is not None:
            raise D2LResumeCheckpointReceiptError(
                "latest usage snapshot is missing from receipt input"
            )
    else:
        normalized_usage_snapshot = validate_component_usage_snapshot(
            latest_usage_snapshot
        )
        if (
            normalized_usage_snapshot["snapshot_sha256"]
            != sealed_usage_sha256
            or normalized_usage_snapshot["workflow_run_id"]
            != manifest["workflow_run_id"]
            or normalized_usage_snapshot["component_run_id"]
            != manifest["component_run_id"]
        ):
            raise D2LResumeCheckpointReceiptError(
                "latest usage snapshot binding drift"
            )

    raw_event, last_event = _line_json(events_path, "events")
    if (
        last_event.get("event") != "checkpoint"
        or last_event.get("component_run_id") != manifest["component_run_id"]
        or last_event.get("component_attempt_id")
        != manifest["component_attempt_id"]
        or (last_event.get("payload") or {}).get("checkpoint_sha256")
        != checkpoint_sha256
    ):
        raise D2LResumeCheckpointReceiptError(
            "event tail is not the owning checkpoint"
        )

    observation_binding = _journal_binding(
        root,
        relative_ref="runtime/component_observations.jsonl",
        entry_count=int(
            checkpoint_state.get("observation_journal_entry_count") or 0
        ),
        last_entry_sha256=checkpoint_state.get(
            "observation_journal_last_entry_sha256"
        ),
    )
    work_bindings: list[dict[str, Any]] = []
    work_journals = _mapping(
        checkpoint_state.get("work_journals") or {},
        "checkpoint work_journals",
    )
    for stage_id, binding_value in sorted(work_journals.items()):
        binding = _mapping(binding_value, f"work_journals.{stage_id}")
        journal = _journal_binding(
            root,
            relative_ref=str(binding.get("journal_ref") or ""),
            entry_count=int(binding.get("entry_count") or 0),
            last_entry_sha256=binding.get("last_entry_sha256"),
        )
        journal["stage_id"] = str(stage_id)
        journal["journal_sha256"] = str(binding.get("journal_sha256") or "")
        work_bindings.append(journal)

    index = validate_artifact_index(
        _load_json(index_path, "artifact index"),
        manifest=manifest,
    )
    manifest_sha256 = file_sha256(manifest_path)
    manifest_revision_ref = (
        f"manifest_revisions/{manifest_sha256}.json"
    )
    manifest_revision_path = _relative_path(
        root,
        manifest_revision_ref,
        "manifest_revision_ref",
    )
    if (
        file_sha256(manifest_revision_path) != manifest_sha256
        or manifest_revision_path.read_bytes() != manifest_path.read_bytes()
    ):
        raise D2LResumeCheckpointReceiptError(
            "current manifest revision binding drift"
        )
    receipt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "validation_mode": VALIDATION_MODE,
        "workflow_run_id": manifest["workflow_run_id"],
        "component_run_id": manifest["component_run_id"],
        "component_attempt_id": manifest["component_attempt_id"],
        "active_stage_id": manifest["active_stage_id"],
        "runner_plan_schema": checkpoint_state.get("runner_plan_schema"),
        "runner_plan_sha256": checkpoint_state.get("runner_plan_sha256"),
        "manifest": {
            "sha256": manifest_sha256,
            "file": _stat_binding(manifest_path),
        },
        "manifest_revision": {
            "relative_ref": manifest_revision_ref,
            "sha256": manifest_sha256,
            "file": _stat_binding(manifest_revision_path),
        },
        "artifact_index": {
            "sha256": file_sha256(index_path),
            "artifact_count": len(index["artifacts"]),
            "file": _stat_binding(index_path),
        },
        "checkpoint": {
            "checkpoint_ref": checkpoint_ref,
            "checkpoint_sha256": checkpoint_sha256,
            "file": _stat_binding(checkpoint_path),
        },
        "events": {
            "event_count": int(last_event.get("component_seq") or 0),
            "last_component_seq": int(last_event.get("component_seq") or 0),
            "last_component_attempt_id": int(
                last_event.get("component_attempt_id") or 0
            ),
            "last_event_id": str(last_event.get("event_id") or ""),
            "last_event": str(last_event.get("event") or ""),
            "tail_line_sha256": canonical_sha256(
                {"raw_utf8": raw_event.decode("utf-8")}
            ),
            "file": _stat_binding(events_path),
        },
        "observation_journal": observation_binding,
        "work_journals": work_bindings,
        "artifact_files": _artifact_file_bindings(root, index),
        "latest_usage_snapshot_sha256": checkpoint_state.get(
            "latest_usage_snapshot_sha256"
        ),
        "latest_usage_snapshot": normalized_usage_snapshot,
        "term_lifecycle_projection_complete": bool(
            term_lifecycle_projection_complete
        ),
        "integrity": {"receipt_sha256": _INTEGRITY_PLACEHOLDER},
    }
    receipt["integrity"]["receipt_sha256"] = _receipt_sha256(receipt)
    write_json(root / RECEIPT_REF, receipt)
    return receipt


def validate_resume_checkpoint_receipt(
    component_root: str | Path,
) -> dict[str, Any]:
    root = Path(component_root).resolve()
    receipt = _load_json(root / RECEIPT_REF, "Resume checkpoint receipt")
    if receipt.get("schema_version") != SCHEMA_VERSION:
        raise D2LResumeCheckpointReceiptError(
            "Resume checkpoint receipt schema is unsupported"
        )
    integrity = _mapping(receipt.get("integrity"), "receipt integrity")
    if integrity.get("receipt_sha256") != _receipt_sha256(receipt):
        raise D2LResumeCheckpointReceiptError(
            "Resume checkpoint receipt self-hash drift"
        )

    manifest_path = root / "component_manifest.json"
    index_path = root / "artifact_index.json"
    events_path = root / "events.jsonl"
    manifest = validate_component_manifest(
        _load_json(manifest_path, "component manifest")
    )
    if (
        manifest["status"] != "paused"
        or not manifest["resume"]["resume_available"]
        or manifest["workflow_run_id"] != receipt.get("workflow_run_id")
        or manifest["component_run_id"] != receipt.get("component_run_id")
        or manifest["component_attempt_id"]
        != receipt.get("component_attempt_id")
        or manifest["active_stage_id"] != receipt.get("active_stage_id")
    ):
        raise D2LResumeCheckpointReceiptError(
            "Resume checkpoint receipt identity drift"
        )

    manifest_binding = _mapping(receipt.get("manifest"), "receipt manifest")
    revision_binding = _mapping(
        receipt.get("manifest_revision"),
        "receipt manifest_revision",
    )
    index_binding = _mapping(
        receipt.get("artifact_index"), "receipt artifact_index"
    )
    _require_stat(manifest_path, manifest_binding.get("file"), "manifest")
    _require_stat(index_path, index_binding.get("file"), "artifact index")
    if file_sha256(manifest_path) != manifest_binding.get("sha256"):
        raise D2LResumeCheckpointReceiptError("manifest hash drift")
    if file_sha256(index_path) != index_binding.get("sha256"):
        raise D2LResumeCheckpointReceiptError("artifact index hash drift")
    manifest_revision_path = _relative_path(
        root,
        revision_binding.get("relative_ref"),
        "manifest_revision.relative_ref",
    )
    _require_stat(
        manifest_revision_path,
        revision_binding.get("file"),
        "manifest revision",
    )
    if (
        revision_binding.get("sha256") != manifest_binding.get("sha256")
        or file_sha256(manifest_revision_path)
        != manifest_binding.get("sha256")
        or manifest_revision_path.read_bytes() != manifest_path.read_bytes()
    ):
        raise D2LResumeCheckpointReceiptError(
            "manifest revision binding drift"
        )
    index = validate_artifact_index(
        _load_json(index_path, "artifact index"),
        manifest=manifest,
    )
    if len(index["artifacts"]) != index_binding.get("artifact_count"):
        raise D2LResumeCheckpointReceiptError(
            "artifact index count drift"
        )

    checkpoint_binding = _mapping(
        receipt.get("checkpoint"), "receipt checkpoint"
    )
    checkpoint_ref = str(checkpoint_binding.get("checkpoint_ref") or "")
    checkpoint_path = _relative_path(root, checkpoint_ref, "checkpoint_ref")
    _require_stat(
        checkpoint_path, checkpoint_binding.get("file"), "checkpoint"
    )
    checkpoint_sha256 = file_sha256(checkpoint_path)
    if (
        checkpoint_sha256 != checkpoint_binding.get("checkpoint_sha256")
        or checkpoint_sha256 != manifest["resume"]["checkpoint_sha256"]
        or checkpoint_ref != manifest["resume"]["checkpoint_ref"]
    ):
        raise D2LResumeCheckpointReceiptError("checkpoint binding drift")
    checkpoint = validate_checkpoint(
        _load_json(checkpoint_path, "checkpoint"),
        manifest=manifest,
    )
    checkpoint_state = _mapping(checkpoint.get("state"), "checkpoint state")
    if (
        checkpoint_state.get("runner_plan_schema")
        != receipt.get("runner_plan_schema")
        or checkpoint_state.get("runner_plan_sha256")
        != receipt.get("runner_plan_sha256")
    ):
        raise D2LResumeCheckpointReceiptError("runner plan seal drift")
    latest_usage_snapshot_value = receipt.get("latest_usage_snapshot")
    if latest_usage_snapshot_value is None:
        latest_usage_snapshot = None
        if receipt.get("latest_usage_snapshot_sha256") is not None:
            raise D2LResumeCheckpointReceiptError(
                "receipt latest usage snapshot is missing"
            )
    else:
        latest_usage_snapshot = validate_component_usage_snapshot(
            _mapping(
                latest_usage_snapshot_value,
                "receipt latest_usage_snapshot",
            )
        )
        if (
            latest_usage_snapshot["snapshot_sha256"]
            != receipt.get("latest_usage_snapshot_sha256")
            or latest_usage_snapshot["snapshot_sha256"]
            != checkpoint_state.get("latest_usage_snapshot_sha256")
            or latest_usage_snapshot["workflow_run_id"]
            != manifest["workflow_run_id"]
            or latest_usage_snapshot["component_run_id"]
            != manifest["component_run_id"]
        ):
            raise D2LResumeCheckpointReceiptError(
                "receipt latest usage snapshot binding drift"
            )

    events_binding = _mapping(receipt.get("events"), "receipt events")
    _require_stat(events_path, events_binding.get("file"), "events")
    raw_event, last_event = _line_json(events_path, "events")
    if (
        canonical_sha256({"raw_utf8": raw_event.decode("utf-8")})
        != events_binding.get("tail_line_sha256")
        or last_event.get("event_id") != events_binding.get("last_event_id")
        or last_event.get("component_seq")
        != events_binding.get("last_component_seq")
        or last_event.get("component_attempt_id")
        != events_binding.get("last_component_attempt_id")
        or last_event.get("event") != "checkpoint"
        or (last_event.get("payload") or {}).get("checkpoint_sha256")
        != checkpoint_sha256
    ):
        raise D2LResumeCheckpointReceiptError("event tail drift")

    journal_bindings = [
        _mapping(
            receipt.get("observation_journal"),
            "receipt observation_journal",
        ),
        *[
            _mapping(row, "receipt work journal")
            for row in receipt.get("work_journals") or []
        ],
    ]
    for binding in journal_bindings:
        relative_ref = str(binding.get("journal_ref") or "")
        if int(binding.get("entry_count") or 0) == 0:
            continue
        path = _relative_path(root, relative_ref, "journal_ref")
        _require_stat(path, binding.get("file"), relative_ref)
        raw, row = _line_json(path, relative_ref)
        if (
            row.get("entry_sha256") != binding.get("last_entry_sha256")
            or canonical_sha256({"raw_utf8": raw.decode("utf-8")})
            != binding.get("tail_line_sha256")
        ):
            raise D2LResumeCheckpointReceiptError(
                f"{relative_ref} tail drift"
            )

    for artifact in receipt.get("artifact_files") or []:
        binding = _mapping(artifact, "receipt artifact file")
        path = _relative_path(
            root, binding.get("relative_path"), "artifact.relative_path"
        )
        _require_stat(path, binding.get("file"), "artifact file")

    observation_binding = _mapping(
        receipt.get("observation_journal"),
        "receipt observation_journal",
    )
    work_journal_state = {
        str(binding["stage_id"]): {
            "journal_ref": str(binding["journal_ref"]),
            "journal_sha256": str(binding["journal_sha256"]),
            "entry_count": int(binding["entry_count"]),
            "last_entry_sha256": binding["last_entry_sha256"],
        }
        for binding in (
            _mapping(row, "receipt work journal")
            for row in receipt.get("work_journals") or []
        )
    }
    result = {
        "schema": "d2l_translation_component_package_validation_v1",
        "validation_mode": VALIDATION_MODE,
        "workflow_run_id": manifest["workflow_run_id"],
        "component_run_id": manifest["component_run_id"],
        "component_attempt_id": manifest["component_attempt_id"],
        "manifest": manifest,
        "component_manifest_sha256": manifest_binding["sha256"],
        "artifact_index_sha256": index_binding["sha256"],
        "event_count": int(events_binding["event_count"]),
        "artifact_count": int(index_binding["artifact_count"]),
        "checkpoint_reference_count": 1,
        "terminal_event": None,
        "latest_usage_snapshot_sha256": receipt.get(
            "latest_usage_snapshot_sha256"
        ),
        "term_lifecycle_projection_complete": bool(
            receipt.get("term_lifecycle_projection_complete")
        ),
        "event_writer_summary": {
            "last_component_seq": int(
                events_binding["last_component_seq"]
            ),
            "last_component_attempt_id": int(
                events_binding["last_component_attempt_id"]
            ),
            "terminal_event": None,
        },
        "receipt_sha256": integrity["receipt_sha256"],
        "checkpoint_ref": checkpoint_ref,
        "checkpoint_sha256": checkpoint_sha256,
        "observation_journal_state": {
            "entry_count": int(observation_binding["entry_count"]),
            "last_entry_sha256": observation_binding[
                "last_entry_sha256"
            ],
        },
        "work_journal_checkpoint_state": work_journal_state,
    }
    if latest_usage_snapshot is not None:
        result["component_usage"] = latest_usage_snapshot[
            "component_cumulative"
        ]
    return result


__all__ = [
    "can_reuse_term_lifecycle_projection",
    "D2LResumeCheckpointReceiptError",
    "RECEIPT_REF",
    "SCHEMA_VERSION",
    "VALIDATION_MODE",
    "validate_resume_checkpoint_receipt",
    "write_resume_checkpoint_receipt",
]
