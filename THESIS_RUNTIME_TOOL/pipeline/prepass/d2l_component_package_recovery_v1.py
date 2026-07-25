"""Fail-closed recovery transaction for one paused D2L component package."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any, Mapping
from uuid import uuid4

from .d2l_component_writer_lease_v1 import (
    D2LComponentWriterLease,
    D2LComponentWriterLeaseError,
    stage_writer_is_active,
)
from .d2l_console_replay_contract_v1 import (
    canonical_json_bytes,
    canonical_sha256,
    file_sha256,
    validate_artifact_index,
    validate_component_event_stream,
    validate_component_manifest,
    validate_translation_component_package,
)


RECOVERY_REQUEST_SCHEMA = "d2l_component_package_recovery_request_v1"
RECOVERY_RECEIPT_SCHEMA = "d2l_component_package_recovery_receipt_v1"
_SHA256_RE = re.compile(r"^[0-9A-F]{64}$")


class D2LComponentPackageRecoveryError(RuntimeError):
    """Raised when recovery cannot be certified without inference."""


def _sha256_bytes(value: bytes) -> str:
    return sha256(value).hexdigest().upper()


def _require_sha256(value: str, label: str) -> str:
    normalized = str(value or "").strip().upper()
    if not _SHA256_RE.fullmatch(normalized):
        raise D2LComponentPackageRecoveryError(
            f"{label} must be an uppercase SHA-256"
        )
    return normalized


def _require_regular_file(path: Path, label: str) -> Path:
    if path.is_symlink() or not path.is_file():
        raise D2LComponentPackageRecoveryError(
            f"{label} must be a regular file"
        )
    return path


def _load_mapping_bytes(value: bytes, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise D2LComponentPackageRecoveryError(
            f"{label} must be UTF-8 JSON"
        ) from exc
    if not isinstance(parsed, dict):
        raise D2LComponentPackageRecoveryError(
            f"{label} must be a JSON object"
        )
    return parsed


def _normalize_snapshot_ref(
    value: str,
    *,
    snapshot_sha256: str,
) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    if not raw or len(raw) > 4096:
        raise D2LComponentPackageRecoveryError(
            "parent_snapshot_ref must be a bounded relative path"
        )
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts:
        raise D2LComponentPackageRecoveryError(
            "parent_snapshot_ref must be confined"
        )
    suffix = f"/snapshots/{snapshot_sha256.lower()}/artifact_index.json"
    if not raw.lower().endswith(suffix):
        raise D2LComponentPackageRecoveryError(
            "parent_snapshot_ref does not bind the supplied snapshot SHA"
        )
    return raw


def _assert_safe_transaction_root(
    component_root: Path,
    transaction_root: Path,
) -> None:
    if transaction_root == component_root or component_root in transaction_root.parents:
        raise D2LComponentPackageRecoveryError(
            "transaction_root must be outside the component package"
        )
    if transaction_root.exists() and transaction_root.is_symlink():
        raise D2LComponentPackageRecoveryError(
            "transaction_root must not be a symlink"
        )


def _write_absent_or_equal(path: Path, value: bytes) -> None:
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != value:
            raise D2LComponentPackageRecoveryError(
                f"recovery transaction file drift: {path.name}"
            )
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _replace_bytes(path: Path, value: bytes, *, transaction_id: str) -> None:
    temporary = path.with_name(
        f".{path.name}.{transaction_id}.{uuid4().hex}.tmp"
    )
    try:
        with temporary.open("xb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


@dataclass(frozen=True)
class D2LComponentPackageRecoveryRequestV1:
    component_root: Path
    transaction_root: Path
    authoritative_index_file: Path
    authoritative_index_sha256: str
    parent_snapshot_ref: str
    parent_snapshot_sha256: str
    parent_import_ordinal: int | None
    expected_manifest_sha256: str
    expected_events_sha256: str
    expected_broken_index_sha256: str
    expected_manifest_temp_sha256: str | None

    def normalized(self) -> "D2LComponentPackageRecoveryRequestV1":
        if self.component_root.is_symlink():
            raise D2LComponentPackageRecoveryError(
                "component_root must not be a symlink"
            )
        if self.transaction_root.exists() and self.transaction_root.is_symlink():
            raise D2LComponentPackageRecoveryError(
                "transaction_root must not be a symlink"
            )
        if self.authoritative_index_file.is_symlink():
            raise D2LComponentPackageRecoveryError(
                "authoritative_index_file must not be a symlink"
            )
        component_root = self.component_root.resolve()
        transaction_root = self.transaction_root.resolve()
        authoritative = self.authoritative_index_file.resolve()
        _assert_safe_transaction_root(component_root, transaction_root)
        if component_root.is_symlink() or not component_root.is_dir():
            raise D2LComponentPackageRecoveryError(
                "component_root must be an existing regular directory"
            )
        _require_regular_file(authoritative, "authoritative_index_file")
        snapshot_sha = _require_sha256(
            self.parent_snapshot_sha256,
            "parent_snapshot_sha256",
        )
        parent_snapshot_ref = _normalize_snapshot_ref(
            self.parent_snapshot_ref,
            snapshot_sha256=snapshot_sha,
        )
        if authoritative.parent.name.upper() != snapshot_sha:
            raise D2LComponentPackageRecoveryError(
                "authoritative index path is outside the supplied snapshot"
            )
        if authoritative.name != "artifact_index.json":
            raise D2LComponentPackageRecoveryError(
                "authoritative index filename must be artifact_index.json"
            )
        reference_parts = PurePosixPath(parent_snapshot_ref).parts
        authoritative_tail = authoritative.parts[-len(reference_parts) :]
        if tuple(os.path.normcase(part) for part in authoritative_tail) != tuple(
            os.path.normcase(part) for part in reference_parts
        ):
            raise D2LComponentPackageRecoveryError(
                "authoritative index path does not match parent_snapshot_ref"
            )
        ordinal = self.parent_import_ordinal
        if ordinal is not None and (
            isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 1
        ):
            raise D2LComponentPackageRecoveryError(
                "parent_import_ordinal must be a positive integer or null"
            )
        return D2LComponentPackageRecoveryRequestV1(
            component_root=component_root,
            transaction_root=transaction_root,
            authoritative_index_file=authoritative,
            authoritative_index_sha256=_require_sha256(
                self.authoritative_index_sha256,
                "authoritative_index_sha256",
            ),
            parent_snapshot_ref=parent_snapshot_ref,
            parent_snapshot_sha256=snapshot_sha,
            parent_import_ordinal=ordinal,
            expected_manifest_sha256=_require_sha256(
                self.expected_manifest_sha256,
                "expected_manifest_sha256",
            ),
            expected_events_sha256=_require_sha256(
                self.expected_events_sha256,
                "expected_events_sha256",
            ),
            expected_broken_index_sha256=_require_sha256(
                self.expected_broken_index_sha256,
                "expected_broken_index_sha256",
            ),
            expected_manifest_temp_sha256=(
                None
                if self.expected_manifest_temp_sha256 is None
                else _require_sha256(
                    self.expected_manifest_temp_sha256,
                    "expected_manifest_temp_sha256",
                )
            ),
        )

    def identity_payload(self) -> dict[str, Any]:
        return {
            "schema": RECOVERY_REQUEST_SCHEMA,
            "authoritative_index_sha256": self.authoritative_index_sha256,
            "parent_snapshot_ref": self.parent_snapshot_ref,
            "parent_snapshot_sha256": self.parent_snapshot_sha256,
            "parent_import_ordinal": self.parent_import_ordinal,
            "expected_manifest_sha256": self.expected_manifest_sha256,
            "expected_events_sha256": self.expected_events_sha256,
            "expected_broken_index_sha256": self.expected_broken_index_sha256,
            "expected_manifest_temp_sha256": (
                self.expected_manifest_temp_sha256
            ),
        }


def _receipt_with_hash(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    result["receipt_sha256"] = canonical_sha256(result)
    return result


def _expected_recovery_receipt(
    *,
    req: D2LComponentPackageRecoveryRequestV1,
    transaction_id: str,
    transaction_sha: str,
    manifest: Mapping[str, Any],
    resume: Mapping[str, Any],
    broken_copy_ref: str,
    temp_copy_ref: str | None,
    authoritative_copy_ref: str,
    manifest_path: Path,
    events_path: Path,
    index_path: Path,
    manifest_temp_path: Path,
    validation: Mapping[str, Any],
) -> dict[str, Any]:
    return _receipt_with_hash(
        {
            "schema": RECOVERY_RECEIPT_SCHEMA,
            "transaction_id": transaction_id,
            "transaction_sha256": transaction_sha,
            "workflow_run_id": manifest["workflow_run_id"],
            "component_run_id": manifest["component_run_id"],
            "component_attempt_id": manifest["component_attempt_id"],
            "checkpoint_ref": resume["checkpoint_ref"],
            "checkpoint_sha256": resume["checkpoint_sha256"],
            "parent_snapshot_ref": req.parent_snapshot_ref,
            "parent_snapshot_sha256": req.parent_snapshot_sha256,
            "parent_import_ordinal": req.parent_import_ordinal,
            "prestate": {
                "component_manifest_sha256": req.expected_manifest_sha256,
                "events_sha256": req.expected_events_sha256,
                "broken_artifact_index_sha256": (
                    req.expected_broken_index_sha256
                ),
                "orphan_manifest_temp_sha256": (
                    req.expected_manifest_temp_sha256
                ),
            },
            "quarantine": {
                "broken_artifact_index_ref": broken_copy_ref,
                "orphan_manifest_temp_ref": temp_copy_ref,
                "authoritative_artifact_index_ref": authoritative_copy_ref,
            },
            "poststate": {
                "component_manifest_sha256": file_sha256(manifest_path),
                "events_sha256": file_sha256(events_path),
                "artifact_index_sha256": file_sha256(index_path),
                "orphan_manifest_temp_present": manifest_temp_path.exists(),
            },
            "post_package_validation": validation,
            "post_package_validation_sha256": canonical_sha256(validation),
            "semantic_replay_count": 0,
            "provider_call_count": 0,
        }
    )


def validate_recovery_receipt_v1(value: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(value)
    if row.get("schema") != RECOVERY_RECEIPT_SCHEMA:
        raise D2LComponentPackageRecoveryError(
            "recovery receipt schema is invalid"
        )
    observed = _require_sha256(
        str(row.pop("receipt_sha256", "")),
        "receipt_sha256",
    )
    if canonical_sha256(row) != observed:
        raise D2LComponentPackageRecoveryError(
            "recovery receipt hash drift"
        )
    row["receipt_sha256"] = observed
    return row


def recover_d2l_component_package_v1(
    request: D2LComponentPackageRecoveryRequestV1,
) -> dict[str, Any]:
    """Recover exactly one torn paused package without replaying semantic work."""

    req = request.normalized()
    root = req.component_root
    manifest_path = root / "component_manifest.json"
    events_path = root / "events.jsonl"
    index_path = root / "artifact_index.json"
    manifest_temp_path = root / "component_manifest.json.tmp"

    try:
        lease = D2LComponentWriterLease(root)
        with lease:
            if stage_writer_is_active(root):
                raise D2LComponentPackageRecoveryError(
                    "stage writer lease is active"
                )

            manifest_path = _require_regular_file(
                manifest_path,
                "component manifest",
            )
            events_path = _require_regular_file(events_path, "component events")
            index_path = _require_regular_file(index_path, "artifact index")
            manifest_bytes = manifest_path.read_bytes()
            events_bytes = events_path.read_bytes()
            current_index_bytes = index_path.read_bytes()
            authoritative_bytes = req.authoritative_index_file.read_bytes()

            if _sha256_bytes(manifest_bytes) != req.expected_manifest_sha256:
                raise D2LComponentPackageRecoveryError(
                    "component manifest prestate hash drift"
                )
            if _sha256_bytes(events_bytes) != req.expected_events_sha256:
                raise D2LComponentPackageRecoveryError(
                    "component events prestate hash drift"
                )
            if (
                _sha256_bytes(authoritative_bytes)
                != req.authoritative_index_sha256
            ):
                raise D2LComponentPackageRecoveryError(
                    "authoritative index hash drift"
                )

            current_index_sha = _sha256_bytes(current_index_bytes)
            if current_index_sha not in {
                req.expected_broken_index_sha256,
                req.authoritative_index_sha256,
            }:
                raise D2LComponentPackageRecoveryError(
                    "component artifact index is neither broken nor recovered state"
                )

            stale_temp_bytes: bytes | None = None
            if manifest_temp_path.exists():
                _require_regular_file(
                    manifest_temp_path,
                    "orphan component manifest temporary",
                )
                stale_temp_bytes = manifest_temp_path.read_bytes()
                if req.expected_manifest_temp_sha256 is None:
                    raise D2LComponentPackageRecoveryError(
                        "unexpected orphan component manifest temporary"
                    )
                if (
                    _sha256_bytes(stale_temp_bytes)
                    != req.expected_manifest_temp_sha256
                ):
                    raise D2LComponentPackageRecoveryError(
                        "orphan component manifest temporary hash drift"
                    )
            elif req.expected_manifest_temp_sha256 is not None:
                # An already committed transaction may have quarantined it.
                stale_temp_bytes = None

            manifest = validate_component_manifest(
                _load_mapping_bytes(manifest_bytes, "component manifest")
            )
            if manifest["status"] != "paused":
                raise D2LComponentPackageRecoveryError(
                    "recovery requires a paused component manifest"
                )
            resume = manifest["resume"]
            if not resume["resume_available"]:
                raise D2LComponentPackageRecoveryError(
                    "paused component has no resumable checkpoint"
                )
            validate_component_event_stream(
                events_path,
                manifest=manifest,
                require_terminal=False,
            )

            authoritative_index = _load_mapping_bytes(
                authoritative_bytes,
                "authoritative artifact index",
            )
            if canonical_json_bytes(authoritative_index) != authoritative_bytes:
                raise D2LComponentPackageRecoveryError(
                    "authoritative artifact index is not canonical JSON"
                )
            validate_artifact_index(
                authoritative_index,
                manifest=manifest,
                artifact_root=root,
            )

            identity = {
                **req.identity_payload(),
                "workflow_run_id": manifest["workflow_run_id"],
                "component_run_id": manifest["component_run_id"],
                "component_attempt_id": manifest["component_attempt_id"],
                "checkpoint_ref": resume["checkpoint_ref"],
                "checkpoint_sha256": resume["checkpoint_sha256"],
            }
            transaction_sha = canonical_sha256(identity)
            transaction_id = f"d2l_recovery_{transaction_sha[:32].lower()}"
            transaction_dir = req.transaction_root / transaction_id
            if transaction_dir.exists() and transaction_dir.is_symlink():
                raise D2LComponentPackageRecoveryError(
                    "recovery transaction directory is a symlink"
                )
            transaction_dir.mkdir(parents=True, exist_ok=True)

            broken_copy_ref = (
                "quarantine/"
                f"artifact_index.{req.expected_broken_index_sha256}.json"
            )
            temp_copy_ref = (
                None
                if req.expected_manifest_temp_sha256 is None
                else "quarantine/"
                f"component_manifest_tmp."
                f"{req.expected_manifest_temp_sha256}.json"
            )
            authoritative_copy_ref = (
                "authority/"
                f"artifact_index.{req.authoritative_index_sha256}.json"
            )
            request_ref = "request.json"
            if current_index_sha == req.expected_broken_index_sha256:
                broken_bytes = current_index_bytes
            else:
                broken_bytes = _require_regular_file(
                    transaction_dir / broken_copy_ref,
                    "quarantined broken artifact index",
                ).read_bytes()
                if (
                    _sha256_bytes(broken_bytes)
                    != req.expected_broken_index_sha256
                ):
                    raise D2LComponentPackageRecoveryError(
                        "quarantined broken artifact index hash drift"
                    )
            _write_absent_or_equal(
                transaction_dir / broken_copy_ref,
                broken_bytes,
            )
            if temp_copy_ref is not None:
                if stale_temp_bytes is not None:
                    _write_absent_or_equal(
                        transaction_dir / temp_copy_ref,
                        stale_temp_bytes,
                    )
                else:
                    _require_regular_file(
                        transaction_dir / temp_copy_ref,
                        "quarantined manifest temporary",
                    )
                    if (
                        file_sha256(transaction_dir / temp_copy_ref)
                        != req.expected_manifest_temp_sha256
                    ):
                        raise D2LComponentPackageRecoveryError(
                            "quarantined manifest temporary hash drift"
                        )
            _write_absent_or_equal(
                transaction_dir / authoritative_copy_ref,
                authoritative_bytes,
            )
            request_payload = {
                **identity,
                "schema": RECOVERY_REQUEST_SCHEMA,
                "transaction_id": transaction_id,
                "transaction_sha256": transaction_sha,
            }
            _write_absent_or_equal(
                transaction_dir / request_ref,
                canonical_json_bytes(request_payload),
            )

            receipt_path = transaction_dir / "receipt.json"
            if receipt_path.exists():
                receipt_bytes = _require_regular_file(
                    receipt_path,
                    "recovery receipt",
                ).read_bytes()
                receipt = validate_recovery_receipt_v1(
                    _load_mapping_bytes(receipt_bytes, "recovery receipt")
                )
                validation = validate_translation_component_package(
                    root,
                    require_terminal=False,
                )
                expected_receipt = _expected_recovery_receipt(
                    req=req,
                    transaction_id=transaction_id,
                    transaction_sha=transaction_sha,
                    manifest=manifest,
                    resume=resume,
                    broken_copy_ref=broken_copy_ref,
                    temp_copy_ref=temp_copy_ref,
                    authoritative_copy_ref=authoritative_copy_ref,
                    manifest_path=manifest_path,
                    events_path=events_path,
                    index_path=index_path,
                    manifest_temp_path=manifest_temp_path,
                    validation=validation,
                )
                if receipt_bytes != canonical_json_bytes(expected_receipt):
                    raise D2LComponentPackageRecoveryError(
                        "committed recovery receipt does not match "
                        "the deterministic transaction"
                    )
                return expected_receipt

            index_was_broken = (
                current_index_sha == req.expected_broken_index_sha256
            )
            try:
                if index_was_broken:
                    _replace_bytes(
                        index_path,
                        authoritative_bytes,
                        transaction_id=transaction_id,
                    )
                validate_translation_component_package(
                    root,
                    require_terminal=False,
                )
                if manifest_temp_path.exists():
                    if (
                        req.expected_manifest_temp_sha256 is None
                        or file_sha256(manifest_temp_path)
                        != req.expected_manifest_temp_sha256
                    ):
                        raise D2LComponentPackageRecoveryError(
                            "manifest temporary changed before quarantine"
                        )
                    manifest_temp_path.unlink()
                validation = validate_translation_component_package(
                    root,
                    require_terminal=False,
                )
            except Exception:
                if index_was_broken:
                    broken_bytes = (
                        transaction_dir / broken_copy_ref
                    ).read_bytes()
                    _replace_bytes(
                        index_path,
                        broken_bytes,
                        transaction_id=transaction_id,
                    )
                if (
                    temp_copy_ref is not None
                    and stale_temp_bytes is not None
                    and not manifest_temp_path.exists()
                ):
                    _replace_bytes(
                        manifest_temp_path,
                        (transaction_dir / temp_copy_ref).read_bytes(),
                        transaction_id=transaction_id,
                    )
                raise

            receipt = _expected_recovery_receipt(
                req=req,
                transaction_id=transaction_id,
                transaction_sha=transaction_sha,
                manifest=manifest,
                resume=resume,
                broken_copy_ref=broken_copy_ref,
                temp_copy_ref=temp_copy_ref,
                authoritative_copy_ref=authoritative_copy_ref,
                manifest_path=manifest_path,
                events_path=events_path,
                index_path=index_path,
                manifest_temp_path=manifest_temp_path,
                validation=validation,
            )
            _write_absent_or_equal(
                receipt_path,
                canonical_json_bytes(receipt),
            )
            return receipt
    except D2LComponentWriterLeaseError as exc:
        raise D2LComponentPackageRecoveryError(str(exc)) from exc


__all__ = [
    "D2LComponentPackageRecoveryError",
    "D2LComponentPackageRecoveryRequestV1",
    "RECOVERY_RECEIPT_SCHEMA",
    "RECOVERY_REQUEST_SCHEMA",
    "recover_d2l_component_package_v1",
    "validate_recovery_receipt_v1",
]
