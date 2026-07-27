from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import socket
import time
from typing import Any, Callable, Mapping
import uuid

from pipeline.literary.checkpoint import canonical_hash, canonical_json, pid_is_alive
from pipeline.literary.step5_boundary import AccessLedger, record_adapter_access
from pipeline.literary.step5_preregister import SealedHeldOutPayload
from pipeline.literary.step5_support import SupportReverseIndex, verify_support_reverse_index
from pipeline.literary.step5_types import (
    CanonicalRecord,
    FullScopeChangeSet,
    Generation,
    SafetySeedChangeSet,
    ScopeChangeSet,
    Step5ContractError,
    verify_content_address,
)


class StoreError(Step5ContractError):
    """Raised when an append-only Step-5 artifact is malformed or tampered."""


class StaleParentError(StoreError):
    """Raised when a semantic changeset loses the lineage CAS race."""


class StoreLockedError(StoreError):
    """Raised when a lineage lock cannot be acquired safely."""


class HeldOutAccessError(StoreError):
    """Raised when held-out payload is opened without its gate capability."""


_SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]+$")
_CONTENT_ARTIFACT_FAMILIES = frozenset(
    {"requests", "responses", "request_lineage", "authority", "overlay"}
)


def _safe_id(value: str, label: str) -> str:
    if not value or not _SAFE_ID.fullmatch(value):
        raise StoreError(f"unsafe {label}: {value!r}")
    return value


def _encoded(payload: Mapping[str, Any]) -> bytes:
    return (canonical_json(dict(payload)) + "\n").encode("utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StoreError(f"invalid Step-5 artifact: {path}") from exc
    if not isinstance(payload, dict):
        raise StoreError(f"Step-5 artifact is not an object: {path}")
    return payload


def _write_exclusive_or_identical(path: Path, payload: Mapping[str, Any]) -> Path:
    encoded = _encoded(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        if path.read_bytes() != encoded:
            raise StoreError(f"content-address collision with different bytes: {path}")
    return path


def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(_encoded(payload))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True, slots=True, kw_only=True)
class AuditRecord(CanonicalRecord):
    audit_record_id: str = field(default="", metadata={"canonical_exclude": True})
    kind: str
    payload_hash: str
    request_fingerprint: str | None = None
    authority_route_id: str | None = None
    operational_timestamp: str | None = field(
        default=None, metadata={"canonical_exclude": True}
    )

    self_hash_field = "audit_record_id"


class _LineageLock:
    def __init__(
        self,
        *,
        path: Path,
        timeout_seconds: float = 10.0,
        alive_check: Callable[[int], bool] = pid_is_alive,
    ) -> None:
        self.path = path
        self.timeout_seconds = timeout_seconds
        self.alive_check = alive_check
        self.pid = os.getpid()
        self.host = socket.gethostname()
        self.token = uuid.uuid4().hex
        self.acquired = False

    def _owner(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def acquire(self) -> "_LineageLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout_seconds
        payload = {
            "pid": self.pid,
            "host": self.host,
            "token": self.token,
        }
        encoded = _encoded(payload)
        while time.monotonic() < deadline:
            try:
                descriptor = os.open(
                    self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY
                )
            except FileExistsError:
                owner = self._owner()
                owner_host = str(owner.get("host") or "")
                owner_pid = int(owner.get("pid") or 0)
                owner_token = str(owner.get("token") or "")
                if owner_host == self.host and owner_token and not self.alive_check(owner_pid):
                    if self._owner().get("token") == owner_token:
                        self.path.unlink(missing_ok=True)
                        continue
                time.sleep(0.01)
                continue
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            self.acquired = True
            return self
        raise StoreLockedError(f"timed out acquiring lineage lock: {self.path.name}")

    def release(self) -> None:
        if self.acquired and self._owner().get("token") == self.token:
            self.path.unlink(missing_ok=True)
        self.acquired = False

    def __enter__(self) -> "_LineageLock":
        return self.acquire()

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.release()


class Step5Store:
    def __init__(self, root: Path, *, access_ledger: AccessLedger | None = None) -> None:
        self.root = Path(root).resolve()
        self.ledger = access_ledger or AccessLedger()

    def _path(self, family: str, artifact_id: str) -> Path:
        return self.root / family / f"{_safe_id(artifact_id, family + ' id')}.json"

    def _write_record(self, family: str, artifact_id: str, payload: Mapping[str, Any]) -> Path:
        record_adapter_access(
            self.ledger,
            adapter="Step5Store",
            capability="write",
            target=str(self._path(family, artifact_id)),
        )
        return _write_exclusive_or_identical(self._path(family, artifact_id), payload)

    def _read_record(self, family: str, artifact_id: str) -> dict[str, Any]:
        path = self._path(family, artifact_id)
        record_adapter_access(
            self.ledger,
            adapter="Step5Store",
            capability="read",
            target=str(path),
        )
        if not path.is_file():
            raise StoreError(f"Step-5 content-addressed artifact is missing: {path}")
        return _read_json(path)

    def put_audit(self, record: AuditRecord) -> str:
        verify_content_address(record)
        payload = record.to_canonical_payload() | {
            "audit_record_id": record.audit_record_id,
            "operational_timestamp": record.operational_timestamp,
        }
        self._write_record("audit", record.audit_record_id, payload)
        return record.audit_record_id

    def put_content_artifact(
        self, family: str, payload: Mapping[str, Any]
    ) -> str:
        """Persist exact append-only bytes for a closed set of Step-5 artifacts."""

        if family not in _CONTENT_ARTIFACT_FAMILIES:
            raise StoreError(f"unsupported Step-5 content artifact family: {family}")
        body = dict(payload)
        if "artifact_hash" in body:
            raise StoreError("content artifact payload cannot declare its own hash")
        artifact_hash = canonical_hash(body)
        self._write_record(
            family,
            artifact_hash,
            body | {"artifact_hash": artifact_hash},
        )
        return artifact_hash

    def load_content_artifact(self, family: str, artifact_hash: str) -> dict[str, Any]:
        if family not in _CONTENT_ARTIFACT_FAMILIES:
            raise StoreError(f"unsupported Step-5 content artifact family: {family}")
        return self._verify_hashed_payload(family, artifact_hash, "artifact_hash")

    def put_changeset(self, changeset: ScopeChangeSet) -> str:
        verify_content_address(changeset)
        payload = changeset.to_canonical_payload() | {
            "changeset_hash": changeset.changeset_hash,
            "changeset_kind": "safety_seed"
            if isinstance(changeset, SafetySeedChangeSet)
            else "full",
        }
        if isinstance(changeset, SafetySeedChangeSet):
            validate_safety_seed_shape(
                {key: value for key, value in payload.items() if key != "changeset_kind"}
            )
        self._write_record("changesets", changeset.changeset_hash, payload)
        return changeset.changeset_hash

    def put_support_index(self, index: SupportReverseIndex) -> str:
        verify_support_reverse_index(index)
        payload = index.to_canonical_payload() | {
            "support_index_hash": index.support_index_hash
        }
        self._write_record("support", index.support_index_hash, payload)
        return index.support_index_hash

    def put_semantic_state(self, payload: Mapping[str, Any]) -> str:
        artifact_hash = canonical_hash(dict(payload))
        self._write_record(
            "semantic", artifact_hash, dict(payload) | {"semantic_state_hash": artifact_hash}
        )
        return artifact_hash

    def load_semantic_state(self, artifact_hash: str) -> dict[str, Any]:
        return self._verify_hashed_payload(
            "semantic", artifact_hash, "semantic_state_hash"
        )

    def put_materialized_view(self, payload: Mapping[str, Any]) -> str:
        artifact_hash = canonical_hash(dict(payload))
        self._write_record(
            "views", artifact_hash, dict(payload) | {"materialized_view_hash": artifact_hash}
        )
        return artifact_hash

    def load_materialized_view(self, artifact_hash: str) -> dict[str, Any]:
        return self._verify_hashed_payload(
            "views", artifact_hash, "materialized_view_hash"
        )

    def put_generation(self, generation: Generation) -> str:
        verify_content_address(generation)
        self._verify_generation_dependencies(generation)
        payload = generation.to_canonical_payload() | {
            "generation_hash": generation.generation_hash,
            "created_at_audit": generation.created_at_audit,
        }
        self._write_record("generations", generation.generation_hash, payload)
        return generation.generation_hash

    def _verify_hashed_payload(
        self, family: str, artifact_hash: str, own_hash_field: str
    ) -> dict[str, Any]:
        payload = self._read_record(family, artifact_hash)
        own = str(payload.pop(own_hash_field, ""))
        payload.pop("changeset_kind", None)
        if own != artifact_hash or canonical_hash(payload) != artifact_hash:
            raise StoreError(f"tampered {family} artifact: {artifact_hash}")
        return payload

    def _verify_generation_dependencies(self, generation: Generation) -> None:
        changeset_payload = self._read_record("changesets", generation.changeset_hash)
        kind = str(changeset_payload.pop("changeset_kind", ""))
        own_changeset = str(changeset_payload.pop("changeset_hash", ""))
        if own_changeset != generation.changeset_hash or canonical_hash(changeset_payload) != own_changeset:
            raise StoreError("generation references a tampered changeset")
        expected_kind = "safety_seed" if generation.kind == "safety_seed" else "full"
        if kind != expected_kind:
            raise StoreError("generation and changeset kinds disagree")
        if changeset_payload.get("state_lineage_id") != generation.state_lineage_id:
            raise StoreError("generation and changeset lineage disagree")
        if changeset_payload.get("parent_generation_hash") != generation.parent_generation_hash:
            raise StoreError("generation and changeset parents disagree")
        if changeset_payload.get("validator_contract_hash") != generation.validator_contract_hash:
            raise StoreError("generation and changeset validator contracts disagree")
        if changeset_payload.get("materialized_view_hash") != generation.materialized_view_hash:
            raise StoreError("generation and changeset materialized views disagree")
        if kind == "full":
            for overlay_ref in changeset_payload.get("overlay_record_refs") or []:
                self._verify_hashed_payload("overlay", str(overlay_ref), "artifact_hash")
        self._verify_hashed_payload("support", generation.support_index_hash, "support_index_hash")
        self._verify_hashed_payload("semantic", generation.semantic_state_hash, "semantic_state_hash")
        self._verify_hashed_payload("views", generation.materialized_view_hash, "materialized_view_hash")

    def load_generation(self, generation_hash: str) -> dict[str, Any]:
        payload = self._read_record("generations", generation_hash)
        own = str(payload.pop("generation_hash", ""))
        payload.pop("created_at_audit", None)
        if own != generation_hash or canonical_hash(payload) != generation_hash:
            raise StoreError("generation content hash mismatch")
        generation = Generation(generation_hash=own, **payload)
        self._verify_generation_dependencies(generation)
        return payload | {"generation_hash": own}

    def _pointer_path(self, state_lineage_id: str) -> Path:
        return self._path("current", state_lineage_id)

    def current_generation_hash(self, state_lineage_id: str) -> str | None:
        path = self._pointer_path(state_lineage_id)
        if not path.is_file():
            return None
        pointer = _read_json(path)
        if pointer.get("state_lineage_id") != state_lineage_id:
            raise StoreError("pointer lineage mismatch")
        generation_hash = str(pointer.get("generation_hash") or "")
        self.load_generation(generation_hash)
        return generation_hash

    def cas_switch(
        self,
        *,
        state_lineage_id: str,
        expected_current: str | None,
        new_generation_hash: str,
        before_pointer_switch: Callable[[], None] | None = None,
    ) -> None:
        _safe_id(state_lineage_id, "state lineage id")
        lock_name = canonical_hash({"state_lineage_id": state_lineage_id})
        lock_path = self.root / "locks" / f"{lock_name}.lock"
        with _LineageLock(path=lock_path):
            current = self.current_generation_hash(state_lineage_id)
            if current != expected_current:
                raise StaleParentError(
                    f"stale parent for {state_lineage_id}: expected {expected_current}, current {current}"
                )
            loaded = self.load_generation(new_generation_hash)
            if loaded.get("state_lineage_id") != state_lineage_id:
                raise StoreError("new generation belongs to another lineage")
            if loaded.get("parent_generation_hash") != expected_current:
                raise StaleParentError("new generation parent does not match CAS expectation")
            if before_pointer_switch is not None:
                before_pointer_switch()
            _write_atomic(
                self._pointer_path(state_lineage_id),
                {
                    "state_lineage_id": state_lineage_id,
                    "generation_hash": new_generation_hash,
                },
            )

    def publish_generation(
        self,
        *,
        changeset: ScopeChangeSet,
        support_index: SupportReverseIndex,
        semantic_state: Mapping[str, Any],
        materialized_view: Mapping[str, Any],
        generation_schema_version: str,
        authority_policy_hash: str,
        qualification_policy_hash: str,
        expected_current: str | None,
        created_at_audit: str | None = None,
        before_pointer_switch: Callable[[], None] | None = None,
    ) -> Generation:
        if changeset.parent_generation_hash != expected_current:
            raise StaleParentError("changeset parent differs from expected current pointer")
        changeset_hash = self.put_changeset(changeset)
        support_hash = self.put_support_index(support_index)
        semantic_hash = self.put_semantic_state(semantic_state)
        view_hash = self.put_materialized_view(materialized_view)
        if changeset.materialized_view_hash != view_hash:
            raise StoreError("changeset materialized-view hash mismatch")
        draft = Generation(
            parent_generation_hash=expected_current,
            state_lineage_id=changeset.state_lineage_id,
            kind="safety_seed" if isinstance(changeset, SafetySeedChangeSet) else "full",
            generation_schema_version=generation_schema_version,
            changeset_hash=changeset_hash,
            support_index_hash=support_hash,
            authority_policy_hash=authority_policy_hash,
            qualification_policy_hash=qualification_policy_hash,
            validator_contract_hash=changeset.validator_contract_hash,
            semantic_state_hash=semantic_hash,
            materialized_view_hash=view_hash,
            created_at_audit=created_at_audit,
        )
        generation = Generation(
            **draft.to_canonical_payload(),
            generation_hash=draft.canonical_hash(),
            created_at_audit=created_at_audit,
        )
        self.put_generation(generation)
        self.cas_switch(
            state_lineage_id=changeset.state_lineage_id,
            expected_current=expected_current,
            new_generation_hash=generation.generation_hash,
            before_pointer_switch=before_pointer_switch,
        )
        return generation


def validate_safety_seed_shape(payload: Mapping[str, Any]) -> None:
    allowed = {
        "changeset_hash",
        "state_lineage_id",
        "source_scope_id",
        "parent_generation_hash",
        "bundle_manifest_hash",
        "validator_contract_hash",
        "quarantine_records",
        "materialized_view_hash",
        "estimated_apply_cost",
    }
    if set(payload) != allowed:
        raise StoreError("safety-seed changeset contains forbidden fields")
    records = payload.get("quarantine_records")
    if not isinstance(records, (list, tuple)):
        raise StoreError("safety-seed quarantine records must be a collection")
    for record in records:
        required_record_fields = {
            "record_id",
            "proposal_record_id",
            "seed_occurrence_ids",
            "evidence_refs",
            "state",
            "decided_at_scope",
        }
        if (
            not isinstance(record, Mapping)
            or set(record) != required_record_fields
            or record.get("state") != "quarantine_proposed"
            or not record.get("record_id")
            or not record.get("proposal_record_id")
            or not record.get("seed_occurrence_ids")
            or not record.get("evidence_refs")
            or not record.get("decided_at_scope")
        ):
            raise StoreError("safety-seed contains a non-local quarantine record")


class HeldOutGateCapability:
    __slots__ = ("_token",)

    def __init__(self, token: str) -> None:
        self._token = token


class HeldOutVault:
    def __init__(self, root: Path, *, access_ledger: AccessLedger | None = None) -> None:
        self.root = Path(root).resolve()
        self.ledger = access_ledger or AccessLedger()
        self._gate_token = uuid.uuid4().hex

    def create_gate_capability(self) -> HeldOutGateCapability:
        return HeldOutGateCapability(self._gate_token)

    def seal(self, payload: SealedHeldOutPayload) -> str:
        path = self.root / f"{_safe_id(payload.commitment_hash, 'held-out commitment')}.json"
        record_adapter_access(
            self.ledger,
            adapter="HeldOutVault",
            capability="write",
            target=str(path),
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as handle:
                handle.write(payload.canonical_payload + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError:
            if path.read_bytes().rstrip(b"\n") != payload.canonical_payload:
                raise StoreError("held-out commitment collision")
        return payload.commitment_hash

    def open(
        self, commitment_hash: str, *, capability: HeldOutGateCapability
    ) -> tuple[dict[str, Any], ...]:
        allowed = (
            isinstance(capability, HeldOutGateCapability)
            and capability._token == self._gate_token
        )
        path = self.root / f"{_safe_id(commitment_hash, 'held-out commitment')}.json"
        self.ledger.record(
            adapter="HeldOutVault",
            capability="read",
            target=str(path),
            allowed=allowed,
        )
        if not allowed:
            raise HeldOutAccessError("invalid held-out gate capability")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StoreError("held-out payload is missing or invalid") from exc
        if not isinstance(payload, list) or canonical_hash(payload) != commitment_hash:
            raise StoreError("held-out payload commitment mismatch")
        if not all(isinstance(row, dict) for row in payload):
            raise StoreError("held-out payload rows are malformed")
        return tuple(dict(row) for row in payload)


__all__ = [
    "AuditRecord",
    "HeldOutAccessError",
    "HeldOutGateCapability",
    "HeldOutVault",
    "StaleParentError",
    "Step5Store",
    "StoreError",
    "StoreLockedError",
    "validate_safety_seed_shape",
]
