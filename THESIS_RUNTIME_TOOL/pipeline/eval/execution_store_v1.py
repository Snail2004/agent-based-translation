from __future__ import annotations

import copy
from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any, Mapping
import uuid

from pipeline.eval.contracts_v1 import (
    CanonicalPolicy,
    ContractValidationError,
    canonicalize,
    require_exact_keys,
    require_mapping,
    require_relative_path,
    require_rfc3339,
    require_sha256,
    require_string,
    seal_payload,
    validate_producer,
    verify_payload_hash,
)
from pipeline.eval.execution_runner_v1 import validate_evaluation_execution_artifact
from pipeline.eval.offline_orchestrator_v1 import validate_evaluation_run_config


__all__ = [
    "EXECUTION_BUNDLE_SCHEMA_ID",
    "EXECUTION_BUNDLE_SCHEMA_VERSION",
    "EvaluationExecutionBundleV1",
    "load_evaluation_execution_bundle_v1",
    "persist_evaluation_execution_bundle_v1",
    "seal_evaluation_execution_bundle_manifest",
    "validate_evaluation_execution_bundle_manifest",
]


EXECUTION_BUNDLE_SCHEMA_ID = "EvaluationExecutionBundleManifestV1"
EXECUTION_BUNDLE_SCHEMA_VERSION = "1.0.0"
_SELF_HASH_PATH = ("integrity", "manifest_sha256")
_POLICY = CanonicalPolicy(set_like_paths=frozenset(), semantic_sequence_paths=frozenset())


@dataclass(frozen=True, slots=True)
class EvaluationExecutionBundleV1:
    root: Path
    manifest_path: Path
    config_path: Path
    execution_path: Path
    manifest: dict[str, Any]
    config: dict[str, Any]
    execution: dict[str, Any]
    reused: bool


def persist_evaluation_execution_bundle_v1(
    *,
    output_root: Path,
    config_payload: Mapping[str, Any],
    execution_payload: Mapping[str, Any],
    created_at: str,
    producer_code_commit: str,
) -> EvaluationExecutionBundleV1:
    config = validate_evaluation_run_config(config_payload)
    execution = validate_evaluation_execution_artifact(execution_payload)
    _validate_config_execution_binding(config, execution)
    manifest = _build_manifest(
        config,
        execution,
        created_at=created_at,
        producer_code_commit=producer_code_commit,
    )
    root = _prepare_root(output_root)
    config_path = _contained_path(root, manifest["artifacts"]["config"]["relative_path"])
    execution_path = _contained_path(
        root, manifest["artifacts"]["execution"]["relative_path"]
    )
    manifest_path = _contained_path(root, "manifest.json")

    if manifest_path.exists():
        loaded = load_evaluation_execution_bundle_v1(output_root=root)
        if loaded.manifest != manifest:
            raise ContractValidationError(
                "immutable_conflict",
                str(manifest_path),
                "committed bundle manifest differs from requested content",
            )
        return loaded

    config_reused = _ensure_immutable_json(config_path, config)
    execution_reused = _ensure_immutable_json(execution_path, execution)
    manifest_reused = _ensure_immutable_json(manifest_path, manifest)
    loaded = load_evaluation_execution_bundle_v1(output_root=root)
    return EvaluationExecutionBundleV1(
        root=loaded.root,
        manifest_path=loaded.manifest_path,
        config_path=loaded.config_path,
        execution_path=loaded.execution_path,
        manifest=loaded.manifest,
        config=loaded.config,
        execution=loaded.execution,
        reused=config_reused and execution_reused and manifest_reused,
    )


def load_evaluation_execution_bundle_v1(
    *, output_root: Path
) -> EvaluationExecutionBundleV1:
    root = _prepare_root(output_root, create=False)
    manifest_path = _contained_path(root, "manifest.json")
    manifest = validate_evaluation_execution_bundle_manifest(
        _load_json_object(manifest_path)
    )
    config_path = _contained_path(root, manifest["artifacts"]["config"]["relative_path"])
    execution_path = _contained_path(
        root, manifest["artifacts"]["execution"]["relative_path"]
    )
    config = validate_evaluation_run_config(_load_json_object(config_path))
    execution = validate_evaluation_execution_artifact(
        _load_json_object(execution_path)
    )
    if config["integrity"]["config_sha256"] != manifest["artifacts"]["config"]["sha256"]:
        raise ContractValidationError(
            "config_hash", str(config_path), "config bytes do not match manifest identity"
        )
    if execution["integrity"]["artifact_sha256"] != manifest["artifacts"]["execution"]["sha256"]:
        raise ContractValidationError(
            "execution_hash",
            str(execution_path),
            "execution bytes do not match manifest identity",
        )
    _validate_config_execution_binding(config, execution)
    _validate_manifest_binding(manifest, config, execution)
    return EvaluationExecutionBundleV1(
        root=root,
        manifest_path=manifest_path,
        config_path=config_path,
        execution_path=execution_path,
        manifest=manifest,
        config=config,
        execution=execution,
        reused=True,
    )


def seal_evaluation_execution_bundle_manifest(
    payload: Mapping[str, Any]
) -> dict[str, Any]:
    return seal_payload(payload, policy=_POLICY, hash_path=_SELF_HASH_PATH)


def validate_evaluation_execution_bundle_manifest(
    payload: Mapping[str, Any]
) -> dict[str, Any]:
    root = require_mapping(payload, path="$")
    require_exact_keys(
        root,
        required={
            "schema_id",
            "schema_version",
            "bundle_id",
            "created_at",
            "producer",
            "binding",
            "artifacts",
            "integrity",
        },
        path="$",
    )
    if root["schema_id"] != EXECUTION_BUNDLE_SCHEMA_ID:
        raise ContractValidationError("schema_id", "$.schema_id", "foreign bundle schema")
    if root["schema_version"] != EXECUTION_BUNDLE_SCHEMA_VERSION:
        raise ContractValidationError(
            "schema_version", "$.schema_version", "foreign bundle schema version"
        )
    normalized = {
        "schema_id": EXECUTION_BUNDLE_SCHEMA_ID,
        "schema_version": EXECUTION_BUNDLE_SCHEMA_VERSION,
        "bundle_id": require_string(root["bundle_id"], path="$.bundle_id"),
        "created_at": require_rfc3339(root["created_at"], path="$.created_at"),
        "producer": validate_producer(
            root["producer"], path="$.producer", workstream="evaluation"
        ),
        "binding": _validate_binding(root["binding"]),
        "artifacts": _validate_artifacts(root["artifacts"]),
        "integrity": _validate_integrity(root["integrity"]),
    }
    _validate_manifest_paths(normalized)
    if not verify_payload_hash(normalized, policy=_POLICY, hash_path=_SELF_HASH_PATH):
        raise ContractValidationError(
            "manifest_hash",
            "$.integrity.manifest_sha256",
            "bundle manifest self-hash does not match content",
        )
    canonical = canonicalize(normalized, policy=_POLICY)
    if not isinstance(canonical, dict):
        raise AssertionError("canonical bundle manifest must remain an object")
    return canonical


def _build_manifest(
    config: Mapping[str, Any],
    execution: Mapping[str, Any],
    *,
    created_at: str,
    producer_code_commit: str,
) -> dict[str, Any]:
    config_sha256 = config["integrity"]["config_sha256"]
    execution_sha256 = execution["integrity"]["artifact_sha256"]
    bundle_id = f"evaluation-bundle-{execution_sha256[:24]}"
    return validate_evaluation_execution_bundle_manifest(
        seal_evaluation_execution_bundle_manifest(
            {
                "schema_id": EXECUTION_BUNDLE_SCHEMA_ID,
                "schema_version": EXECUTION_BUNDLE_SCHEMA_VERSION,
                "bundle_id": bundle_id,
                "created_at": created_at,
                "producer": {
                    "workstream": "evaluation",
                    "component": "execution_store_v1",
                    "component_version": "1.0.0",
                    "code_commit": producer_code_commit,
                },
                "binding": {
                    "project_id": execution["binding"]["project_id"],
                    "document_id": execution["binding"]["document_id"],
                    "config_id": execution["binding"]["config_id"],
                    "config_sha256": config_sha256,
                    "input_set_sha256": execution["binding"]["input_set_sha256"],
                    "plan_id": execution["binding"]["plan_id"],
                    "plan_sha256": execution["binding"]["plan_sha256"],
                    "execution_id": execution["execution_id"],
                    "execution_sha256": execution_sha256,
                },
                "artifacts": {
                    "config": {
                        "relative_path": f"config/{config_sha256}.json",
                        "sha256": config_sha256,
                    },
                    "execution": {
                        "relative_path": f"execution/{execution_sha256}.json",
                        "sha256": execution_sha256,
                    },
                },
                "integrity": {"manifest_sha256": "0" * 64},
            }
        )
    )


def _validate_binding(value: Any) -> dict[str, str]:
    path = "$.binding"
    row = require_mapping(value, path=path)
    fields = {
        "project_id",
        "document_id",
        "config_id",
        "config_sha256",
        "input_set_sha256",
        "plan_id",
        "plan_sha256",
        "execution_id",
        "execution_sha256",
    }
    require_exact_keys(row, required=fields, path=path)
    result = {
        field: require_string(row[field], path=f"{path}.{field}") for field in fields
    }
    for field in (
        "config_sha256",
        "input_set_sha256",
        "plan_sha256",
        "execution_sha256",
    ):
        result[field] = require_sha256(row[field], path=f"{path}.{field}")
    return result


def _validate_artifacts(value: Any) -> dict[str, dict[str, str]]:
    path = "$.artifacts"
    row = require_mapping(value, path=path)
    require_exact_keys(row, required={"config", "execution"}, path=path)
    result: dict[str, dict[str, str]] = {}
    for name in ("config", "execution"):
        item_path = f"{path}.{name}"
        item = require_mapping(row[name], path=item_path)
        require_exact_keys(item, required={"relative_path", "sha256"}, path=item_path)
        result[name] = {
            "relative_path": require_relative_path(
                item["relative_path"], path=f"{item_path}.relative_path"
            ),
            "sha256": require_sha256(item["sha256"], path=f"{item_path}.sha256"),
        }
    return result


def _validate_integrity(value: Any) -> dict[str, str]:
    row = require_mapping(value, path="$.integrity")
    require_exact_keys(row, required={"manifest_sha256"}, path="$.integrity")
    return {
        "manifest_sha256": require_sha256(
            row["manifest_sha256"], path="$.integrity.manifest_sha256"
        )
    }


def _validate_manifest_paths(manifest: Mapping[str, Any]) -> None:
    for name in ("config", "execution"):
        artifact = manifest["artifacts"][name]
        expected = f"{name}/{artifact['sha256']}.json"
        if artifact["relative_path"] != expected:
            raise ContractValidationError(
                "artifact_path",
                f"$.artifacts.{name}.relative_path",
                "bundle artifact path is not content-addressed",
            )
    expected_id = f"evaluation-bundle-{manifest['binding']['execution_sha256'][:24]}"
    if manifest["bundle_id"] != expected_id:
        raise ContractValidationError(
            "bundle_id", "$.bundle_id", "bundle ID differs from execution identity"
        )


def _validate_config_execution_binding(
    config: Mapping[str, Any], execution: Mapping[str, Any]
) -> None:
    if config["integrity"]["config_sha256"] != execution["binding"]["config_sha256"]:
        raise ContractValidationError(
            "config_binding", "$", "execution references another evaluation config"
        )
    if config["config_id"] != execution["binding"]["config_id"]:
        raise ContractValidationError(
            "config_binding", "$", "execution config ID differs from supplied config"
        )


def _validate_manifest_binding(
    manifest: Mapping[str, Any],
    config: Mapping[str, Any],
    execution: Mapping[str, Any],
) -> None:
    expected = {
        "project_id": execution["binding"]["project_id"],
        "document_id": execution["binding"]["document_id"],
        "config_id": execution["binding"]["config_id"],
        "config_sha256": config["integrity"]["config_sha256"],
        "input_set_sha256": execution["binding"]["input_set_sha256"],
        "plan_id": execution["binding"]["plan_id"],
        "plan_sha256": execution["binding"]["plan_sha256"],
        "execution_id": execution["execution_id"],
        "execution_sha256": execution["integrity"]["artifact_sha256"],
    }
    if manifest["binding"] != expected:
        raise ContractValidationError(
            "manifest_binding", "$.binding", "manifest references foreign bundle content"
        )


def _prepare_root(path: Path, *, create: bool = True) -> Path:
    candidate = Path(path)
    if create:
        candidate.mkdir(parents=True, exist_ok=True)
    if not candidate.exists() or not candidate.is_dir():
        raise ContractValidationError(
            "output_root", str(candidate), "evaluation output root is not a directory"
        )
    return candidate.resolve()


def _contained_path(root: Path, relative_path: str) -> Path:
    normalized = require_relative_path(relative_path, path="$.relative_path")
    candidate = (root / Path(*normalized.split("/"))).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ContractValidationError(
            "path_escape", str(candidate), "bundle path escapes output root"
        ) from exc
    return candidate


def _ensure_immutable_json(path: Path, payload: Mapping[str, Any]) -> bool:
    detached = copy.deepcopy(dict(payload))
    encoded = _canonical_json_bytes(detached)
    if path.exists():
        if path.read_bytes() != encoded:
            raise ContractValidationError(
                "immutable_conflict",
                str(path),
                "existing immutable artifact differs from canonical requested bytes",
            )
        return True
    return not _write_json_atomic(path, encoded)


def _canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    try:
        return (
            json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ContractValidationError(
            "json_encoding", "$", "bundle artifact must be finite JSON"
        ) from exc


def _write_json_atomic(path: Path, encoded: bytes) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() == encoded:
            return False
        raise ContractValidationError(
            "immutable_conflict", str(path), "refusing to overwrite immutable artifact"
        )
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() == encoded:
                return False
            raise ContractValidationError(
                "immutable_conflict",
                str(path),
                "concurrent writer published different immutable content",
            )
        except OSError as exc:
            raise ContractValidationError(
                "atomic_publish",
                str(path),
                "filesystem cannot publish immutable artifact atomically",
            ) from exc
        return True
    finally:
        if temporary.exists():
            temporary.unlink()


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            value = json.load(
                handle,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_non_finite,
            )
    except FileNotFoundError as exc:
        raise ContractValidationError(
            "missing_artifact", str(path), "bundle artifact is missing"
        ) from exc
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ContractValidationError(
            "artifact_json", str(path), "bundle artifact is not strict UTF-8 JSON"
        ) from exc
    if not isinstance(value, dict):
        raise ContractValidationError(
            "artifact_shape", str(path), "bundle artifact root must be an object"
        )
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_non_finite(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")
