from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
import uuid

from pipeline.literary.checkpoint import (
    CheckpointError,
    artifact_manifest,
    build_checkpoint,
    canonical_hash,
    canonical_json,
    file_sha256,
    read_checkpoint,
    validate_checkpoint,
    write_checkpoint_atomic,
)


BUILDER_SCHEMA_V3 = "v3"
M1_CHECKPOINT_SCHEMA_VERSION_V3 = "literary_m1_checkpoint_v3"
M2_CHECKPOINT_SCHEMA_VERSION_V3 = "literary_m2_checkpoint_v3"
VALIDATOR_CONTRACT_VERSION = "literary_builder_v3_validator_contract_v2"
SOURCE_ANCHOR_VERSION = "literary_source_anchor_v1"
CONTEXT_POLICY_VERSION = "literary_builder_v3_context_policy_v1"
REQUEST_CONTRACT_VERSION = "literary_builder_v3_request_contract_v1"
SYNTHETIC_EXECUTOR_VERSION = "literary_builder_v3_synthetic_executor_v1"

M1_GROUND_STATE_VERSION_V3 = "literary_m1_ground_state_v3"
M2_DIGEST_STATE_VERSION_V3 = "literary_m2_digest_state_v3"

STAGE_SCHEMA_VERSIONS = {
    "m1v3": M1_CHECKPOINT_SCHEMA_VERSION_V3,
    "m2v3": M2_CHECKPOINT_SCHEMA_VERSION_V3,
}


def contract_versions() -> dict[str, str]:
    return {
        "builder_schema": BUILDER_SCHEMA_V3,
        "validator": VALIDATOR_CONTRACT_VERSION,
        "source_anchor": SOURCE_ANCHOR_VERSION,
        "context_policy": CONTEXT_POLICY_VERSION,
        "request_contract": REQUEST_CONTRACT_VERSION,
    }


def builder_v3_root(out_dir: Path) -> Path:
    path = Path(out_dir)
    return path if path.name == "builder_v3" else path / "builder_v3"


def current_pointer_path(root: Path, stage: str, chapter_id: str) -> Path:
    _require_stage(stage)
    return builder_v3_root(root) / "current" / stage / f"{chapter_id}.json"


def _require_stage(stage: str) -> None:
    if stage not in STAGE_SCHEMA_VERSIONS:
        raise ValueError(f"unsupported Builder-v3 checkpoint stage: {stage}")


def _json_bytes(value: Any, *, pretty: bool = False) -> bytes:
    if pretty:
        text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    else:
        text = canonical_json(value)
    return text.encode("utf-8")


def write_bytes_exclusive(path: Path, payload: bytes) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    return path


def write_json_exclusive(path: Path, payload: Any) -> Path:
    return write_bytes_exclusive(path, _json_bytes(payload, pretty=True))


def write_json_atomic(path: Path, payload: Any) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    encoded = _json_bytes(payload, pretty=True)
    try:
        with temporary.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def _safe_relative(path: Path, root: Path) -> str:
    resolved_root = Path(root).resolve()
    resolved_path = Path(path).resolve()
    try:
        relative = resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise CheckpointError(f"Builder-v3 artifact escapes root: {resolved_path}") from exc
    return relative.as_posix()


def logical_content_manifest_hash(rows: Iterable[Mapping[str, Any]]) -> str:
    normalized = [
        {
            "role": str(row["role"]),
            "sha256": str(row["sha256"]),
            "size": int(row["size"]),
        }
        for row in rows
    ]
    normalized.sort(key=lambda row: (row["role"], row["sha256"], row["size"]))
    roles = [row["role"] for row in normalized]
    if len(roles) != len(set(roles)):
        raise CheckpointError("logical artifact roles must be unique")
    return canonical_hash(normalized)


def _audit_logical_rows(
    audit_artifacts: Iterable[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[Path]]:
    logical: list[dict[str, Any]] = []
    paths: list[Path] = []
    for row in audit_artifacts:
        path = Path(str(row["path"]))
        if not path.is_file():
            raise CheckpointError(f"audit artifact is missing before publish: {path}")
        logical.append(
            {
                "role": str(row["role"]),
                "sha256": file_sha256(path),
                "size": path.stat().st_size,
            }
        )
        paths.append(path)
    return logical, paths


def publish_generation(
    *,
    out_dir: Path,
    stage: str,
    chapter_id: str,
    state: Mapping[str, Any],
    semantic_projection: Mapping[str, Any],
    identity_base: Mapping[str, Any],
    operational_fields: Mapping[str, Any],
    audit_artifacts: Iterable[Mapping[str, Any]],
    before_pointer_switch: Callable[[Path, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Publish one immutable generation, then atomically switch its pointer."""

    _require_stage(stage)
    root = builder_v3_root(out_dir)
    generation_id = uuid.uuid4().hex
    generation_dir = root / "generations" / stage / chapter_id / generation_id
    generation_dir.mkdir(parents=True, exist_ok=False)

    state_path = generation_dir / "state.json"
    write_json_exclusive(state_path, dict(state))

    semantic_json = canonical_json(semantic_projection)
    semantic_hash = canonical_hash(semantic_projection)
    audit_logical, audit_paths = _audit_logical_rows(audit_artifacts)
    logical_rows = [
        {
            "role": "state_semantic",
            "sha256": semantic_hash,
            "size": len(semantic_json.encode("utf-8")),
        },
        *audit_logical,
    ]
    content_manifest_hash = logical_content_manifest_hash(logical_rows)

    identity = {
        **dict(identity_base),
        "stage": stage,
        "chapter_id": chapter_id,
        "schema_version": STAGE_SCHEMA_VERSIONS[stage],
        "builder_schema": BUILDER_SCHEMA_V3,
        "semantic_state_hash": semantic_hash,
        "artifact_content_manifest_hash": content_manifest_hash,
    }
    identity_hash = canonical_hash(identity)

    manifested_paths = [state_path, *audit_paths]
    checkpoint_base = {
        **identity,
        **dict(operational_fields),
        "checkpoint_identity": identity,
        "checkpoint_identity_hash": identity_hash,
        "generation_id": generation_id,
        "generation_path": _safe_relative(generation_dir, root),
        "state_path": _safe_relative(state_path, root),
        "artifact_manifest": artifact_manifest(manifested_paths, root=root),
    }
    checkpoint = build_checkpoint(checkpoint_base)
    checkpoint_path = generation_dir / "checkpoint.json"
    write_checkpoint_atomic(checkpoint_path, checkpoint)

    errors = validate_v3_checkpoint(checkpoint, root=root)
    if errors:
        raise CheckpointError(f"new Builder-v3 checkpoint failed self-validation: {errors}")

    pointer = {
        "stage": stage,
        "chapter_id": chapter_id,
        "schema_version": STAGE_SCHEMA_VERSIONS[stage],
        "checkpoint_path": _safe_relative(checkpoint_path, root),
        "checkpoint_hash": str(checkpoint["checkpoint_hash"]),
        "checkpoint_identity_hash": identity_hash,
    }
    pointer_path = current_pointer_path(root, stage, chapter_id)
    if before_pointer_switch is not None:
        before_pointer_switch(pointer_path, pointer)
    write_json_atomic(pointer_path, pointer)
    return checkpoint


def validate_v3_checkpoint(
    checkpoint: Mapping[str, Any],
    *,
    root: Path,
    expected: Mapping[str, Any] | None = None,
) -> list[str]:
    payload = dict(checkpoint)
    errors = validate_checkpoint(payload, root=builder_v3_root(root), expected=dict(expected or {}))

    identity = payload.get("checkpoint_identity")
    identity_hash = str(payload.get("checkpoint_identity_hash") or "")
    if not isinstance(identity, dict):
        errors.append("checkpoint_identity")
    else:
        if canonical_hash(identity) != identity_hash:
            errors.append("checkpoint_identity_hash")
        for key, value in identity.items():
            if payload.get(key) != value:
                errors.append(f"checkpoint_identity_field:{key}")

    stage = str(payload.get("stage") or "")
    if stage not in STAGE_SCHEMA_VERSIONS:
        errors.append("stage")
    elif payload.get("schema_version") != STAGE_SCHEMA_VERSIONS[stage]:
        errors.append("schema_version")
    if payload.get("builder_schema") != BUILDER_SCHEMA_V3:
        errors.append("builder_schema")

    state_path_value = str(payload.get("state_path") or "")
    if not state_path_value:
        errors.append("state_path")
    else:
        state_path = (builder_v3_root(root) / state_path_value).resolve()
        try:
            state_path.relative_to(builder_v3_root(root).resolve())
        except ValueError:
            errors.append("state_path_escape")
        else:
            if not state_path.is_file():
                errors.append("state_path_missing")
    return sorted(set(errors))


def read_current_checkpoint(
    *,
    out_dir: Path,
    stage: str,
    chapter_id: str,
    expected: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    root = builder_v3_root(out_dir)
    pointer_path = current_pointer_path(root, stage, chapter_id)
    if not pointer_path.is_file():
        return None
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    if not isinstance(pointer, dict):
        raise CheckpointError(f"Builder-v3 pointer is not an object: {pointer_path}")
    if pointer.get("stage") != stage or pointer.get("chapter_id") != chapter_id:
        raise CheckpointError(f"Builder-v3 pointer identity mismatch: {pointer_path}")

    relative = Path(str(pointer.get("checkpoint_path") or ""))
    checkpoint_path = (root / relative).resolve()
    try:
        checkpoint_path.relative_to(root.resolve())
    except ValueError as exc:
        raise CheckpointError(f"Builder-v3 pointer escapes root: {pointer_path}") from exc
    if not checkpoint_path.is_file():
        raise CheckpointError(f"Builder-v3 pointer target is missing: {checkpoint_path}")
    checkpoint = read_checkpoint(checkpoint_path)
    if checkpoint.get("checkpoint_hash") != pointer.get("checkpoint_hash"):
        raise CheckpointError(f"Builder-v3 pointer checkpoint hash mismatch: {pointer_path}")
    if checkpoint.get("checkpoint_identity_hash") != pointer.get("checkpoint_identity_hash"):
        raise CheckpointError(f"Builder-v3 pointer identity hash mismatch: {pointer_path}")
    errors = validate_v3_checkpoint(checkpoint, root=root, expected=expected)
    if errors:
        raise CheckpointError(f"Invalid Builder-v3 checkpoint {chapter_id}: {errors}")
    return checkpoint


def read_state_from_checkpoint(checkpoint: Mapping[str, Any], *, out_dir: Path) -> dict[str, Any]:
    root = builder_v3_root(out_dir)
    relative = Path(str(checkpoint.get("state_path") or ""))
    path = (root / relative).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise CheckpointError(f"Builder-v3 state path escapes root: {relative}") from exc
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise CheckpointError(f"Builder-v3 state is not an object: {path}")
    return payload


def write_report(out_dir: Path, name: str, report: Mapping[str, Any]) -> Path:
    root = builder_v3_root(out_dir)
    return write_json_atomic(root / name, dict(report))


__all__ = [
    "BUILDER_SCHEMA_V3",
    "CONTEXT_POLICY_VERSION",
    "M1_CHECKPOINT_SCHEMA_VERSION_V3",
    "M1_GROUND_STATE_VERSION_V3",
    "M2_CHECKPOINT_SCHEMA_VERSION_V3",
    "M2_DIGEST_STATE_VERSION_V3",
    "REQUEST_CONTRACT_VERSION",
    "SOURCE_ANCHOR_VERSION",
    "SYNTHETIC_EXECUTOR_VERSION",
    "VALIDATOR_CONTRACT_VERSION",
    "builder_v3_root",
    "contract_versions",
    "current_pointer_path",
    "logical_content_manifest_hash",
    "publish_generation",
    "read_current_checkpoint",
    "read_state_from_checkpoint",
    "validate_v3_checkpoint",
    "write_bytes_exclusive",
    "write_json_atomic",
    "write_json_exclusive",
    "write_report",
]
