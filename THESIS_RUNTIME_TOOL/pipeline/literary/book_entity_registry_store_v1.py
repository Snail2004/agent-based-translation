"""Content-addressed atomic store for Global Entity Registry generations."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
from typing import Any, Callable, Mapping, Sequence

from pipeline.literary.book_entity_registry_v1 import (
    BOOK_REGISTRY_SCHEMA_VERSION,
    BOOK_VALIDATOR_VERSION,
    BookEntityContractError,
    verify_cross_chapter_decision_v1,
    verify_book_entity_index_v1,
    verify_global_entity_registry_v1,
)
from pipeline.literary.checkpoint import (
    CheckpointLock,
    canonical_hash,
    canonical_json,
    write_checkpoint_atomic,
)


GENERATION_SCHEMA_VERSION = "book_entity_registry_generation_v1"


class BookEntityStoreError(RuntimeError):
    """Raised when a persisted whole-book generation fails integrity checks."""


class BookEntityStaleParentError(BookEntityStoreError):
    """Raised when a whole-book writer loses the compare-and-swap race."""


@dataclass(frozen=True)
class PreparedBookEntityGenerationV1:
    state_lineage_id: str
    generation_id: str
    parent_generation_id: str | None
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return json.loads(canonical_json(self.payload))


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BookEntityStoreError(f"{label} must be a non-empty string")
    return value


def _verify_decision_hashes(
    decisions: Sequence[Mapping[str, Any]], *, index: Mapping[str, Any]
) -> list[str]:
    hashes: list[str] = []
    for raw in decisions:
        try:
            decision = verify_cross_chapter_decision_v1(raw, index=index)
        except BookEntityContractError as exc:
            raise BookEntityStoreError("cross-chapter decision failed verification") from exc
        hashes.append(str(decision["decision_hash"]))
    if len(hashes) != len(set(hashes)):
        raise BookEntityStoreError("duplicate cross-chapter decision hash")
    return sorted(hashes)


def prepare_book_entity_generation_v1(
    *,
    snapshot: Mapping[str, Any],
    index: Mapping[str, Any],
    decisions: Sequence[Mapping[str, Any]],
    prompt_manifest: Mapping[str, Any],
    parent_generation_id: str | None,
) -> PreparedBookEntityGenerationV1:
    verified_index = verify_book_entity_index_v1(index)
    verified_snapshot = verify_global_entity_registry_v1(snapshot)
    if verified_snapshot["state_lineage_id"] != verified_index["state_lineage_id"]:
        raise BookEntityStoreError("snapshot and index cross state lineages")
    if verified_snapshot["book_index_hash"] != verified_index["book_index_hash"]:
        raise BookEntityStoreError("snapshot targets a foreign book index")
    decision_hashes = _verify_decision_hashes(decisions, index=verified_index)
    if decision_hashes != sorted(verified_snapshot["cross_chapter_decision_hashes"]):
        raise BookEntityStoreError("snapshot decision manifest mismatch")
    prompt_body = dict(prompt_manifest)
    prompt_hash = _required_string(prompt_body.pop("manifest_hash", None), "prompt manifest hash")
    if canonical_hash(prompt_body) != prompt_hash:
        raise BookEntityStoreError("prompt manifest hash mismatch")
    body = {
        "schema_version": GENERATION_SCHEMA_VERSION,
        "registry_schema_version": BOOK_REGISTRY_SCHEMA_VERSION,
        "validator_version": BOOK_VALIDATOR_VERSION,
        "state_lineage_id": verified_index["state_lineage_id"],
        "parent_generation_id": parent_generation_id,
        "book_source_manifest_hash": verified_index["book_source_manifest_hash"],
        "book_index_hash": verified_index["book_index_hash"],
        "cross_chapter_decision_hashes": decision_hashes,
        "prompt_manifest": json.loads(canonical_json(prompt_manifest)),
        "snapshot": verified_snapshot,
        "snapshot_hash": verified_snapshot["snapshot_hash"],
    }
    commit_payload_hash = canonical_hash(body)
    generation_id = "bookreggen1_" + canonical_hash(
        {
            "state_lineage_id": verified_index["state_lineage_id"],
            "parent_generation_id": parent_generation_id,
            "commit_payload_hash": commit_payload_hash,
        }
    )[:20]
    payload = {
        **body,
        "generation_id": generation_id,
        "commit_payload_hash": commit_payload_hash,
    }
    return PreparedBookEntityGenerationV1(
        state_lineage_id=verified_index["state_lineage_id"],
        generation_id=generation_id,
        parent_generation_id=parent_generation_id,
        payload=payload,
    )


class BookEntityRegistryStoreV1:
    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()

    def _generation_path(self, generation_id: str) -> Path:
        if not re.fullmatch(r"bookreggen1_[0-9a-f]{20}", generation_id):
            raise BookEntityStoreError("unsafe whole-book generation id")
        return self.root / "generations" / f"{generation_id}.json"

    def _pointer_path(self, state_lineage_id: str) -> Path:
        return self.root / "current" / (
            canonical_hash({"state_lineage_id": state_lineage_id}) + ".json"
        )

    def load_generation(self, generation_id: str) -> dict[str, Any]:
        path = self._generation_path(generation_id)
        if not path.is_file():
            raise BookEntityStoreError(f"missing whole-book generation: {generation_id}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise BookEntityStoreError("whole-book generation is not an object")
        if payload.get("schema_version") != GENERATION_SCHEMA_VERSION:
            raise BookEntityStoreError("foreign whole-book generation schema")
        if payload.get("registry_schema_version") != BOOK_REGISTRY_SCHEMA_VERSION:
            raise BookEntityStoreError("whole-book registry schema mismatch")
        if payload.get("validator_version") != BOOK_VALIDATOR_VERSION:
            raise BookEntityStoreError("whole-book validator mismatch")
        own_hash = _required_string(payload.get("commit_payload_hash"), "commit payload hash")
        body = {
            key: value
            for key, value in payload.items()
            if key not in {"generation_id", "commit_payload_hash"}
        }
        if canonical_hash(body) != own_hash:
            raise BookEntityStoreError("whole-book commit payload hash mismatch")
        expected = "bookreggen1_" + canonical_hash(
            {
                "state_lineage_id": payload["state_lineage_id"],
                "parent_generation_id": payload["parent_generation_id"],
                "commit_payload_hash": own_hash,
            }
        )[:20]
        if payload.get("generation_id") != generation_id or expected != generation_id:
            raise BookEntityStoreError("whole-book generation identity/path mismatch")
        if payload.get("snapshot_hash") != payload.get("snapshot", {}).get("snapshot_hash"):
            raise BookEntityStoreError("whole-book generation snapshot hash mismatch")
        try:
            verify_global_entity_registry_v1(payload["snapshot"])
        except BookEntityContractError as exc:
            raise BookEntityStoreError("whole-book snapshot failed verification") from exc
        return payload

    def current_generation_id(self, state_lineage_id: str) -> str | None:
        path = self._pointer_path(state_lineage_id)
        if not path.is_file():
            return None
        pointer = json.loads(path.read_text(encoding="utf-8"))
        pointer_body = dict(pointer)
        pointer_hash = _required_string(pointer_body.pop("pointer_hash", None), "pointer hash")
        if canonical_hash(pointer_body) != pointer_hash:
            raise BookEntityStoreError("whole-book pointer hash mismatch")
        if pointer.get("schema_version") != GENERATION_SCHEMA_VERSION:
            raise BookEntityStoreError("foreign whole-book pointer schema")
        if pointer.get("state_lineage_id") != state_lineage_id:
            raise BookEntityStoreError("whole-book pointer crosses state lineage")
        generation_id = _required_string(pointer.get("generation_id"), "generation_id")
        generation = self.load_generation(generation_id)
        if generation.get("state_lineage_id") != state_lineage_id:
            raise BookEntityStoreError("whole-book pointer targets foreign lineage")
        return generation_id

    def load_b2_ready_snapshot(self, state_lineage_id: str) -> dict[str, Any]:
        generation_id = self.current_generation_id(state_lineage_id)
        if generation_id is None:
            raise BookEntityStoreError("B2 is blocked until a global registry is published")
        generation = self.load_generation(generation_id)
        snapshot = verify_global_entity_registry_v1(generation["snapshot"])
        if snapshot["state_lineage_id"] != state_lineage_id:
            raise BookEntityStoreError("B2 snapshot crosses state lineage")
        return snapshot

    def commit(
        self,
        generation: PreparedBookEntityGenerationV1,
        *,
        expected_parent: str | None,
        before_pointer_switch: Callable[[], None] | None = None,
    ) -> None:
        if generation.parent_generation_id != expected_parent:
            raise BookEntityStaleParentError("generation parent differs from CAS expectation")
        path = self._generation_path(generation.generation_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = (canonical_json(generation.to_dict()) + "\n").encode("utf-8")
        try:
            with path.open("xb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError:
            if path.read_bytes() != encoded:
                raise BookEntityStoreError("generation id collision with unequal bytes")
        lock_root = self.root / "lineage_locks" / canonical_hash(
            {"state_lineage_id": generation.state_lineage_id}
        )
        with CheckpointLock(lock_root):
            current = self.current_generation_id(generation.state_lineage_id)
            if current != expected_parent:
                raise BookEntityStaleParentError(
                    f"stale whole-book parent: expected {expected_parent}, current {current}"
                )
            if before_pointer_switch is not None:
                before_pointer_switch()
            pointer_body = {
                "schema_version": GENERATION_SCHEMA_VERSION,
                "state_lineage_id": generation.state_lineage_id,
                "generation_id": generation.generation_id,
            }
            write_checkpoint_atomic(
                self._pointer_path(generation.state_lineage_id),
                {**pointer_body, "pointer_hash": canonical_hash(pointer_body)},
            )


__all__ = [
    "BookEntityRegistryStoreV1",
    "BookEntityStaleParentError",
    "BookEntityStoreError",
    "GENERATION_SCHEMA_VERSION",
    "PreparedBookEntityGenerationV1",
    "prepare_book_entity_generation_v1",
]
