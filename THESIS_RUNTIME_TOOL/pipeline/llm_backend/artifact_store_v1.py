"""Content-addressed artifact bytes for shared response/checkpoint reuse."""

from __future__ import annotations

from hashlib import sha256
import os
from pathlib import Path
from typing import Any

from .contracts_v1 import ContractValidationError, _sha256, canonical_json


class ContentAddressedArtifactStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def put_bytes(self, payload: bytes) -> str:
        if not isinstance(payload, bytes):
            raise TypeError("artifact payload must be bytes")
        digest = sha256(payload).hexdigest()
        path = self.path_for(digest)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if self.get_bytes(digest) != payload:
                raise ContractValidationError(
                    "content-addressed artifact path contains different bytes"
                )
            return digest
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return digest

    def put_json(self, payload: Any) -> str:
        return self.put_bytes(canonical_json(payload).encode("utf-8"))

    def get_bytes(self, artifact_sha256: str) -> bytes:
        digest = _sha256(artifact_sha256, "artifact_sha256")
        path = self.path_for(digest)
        try:
            payload = path.read_bytes()
        except FileNotFoundError as exc:
            raise ContractValidationError("content-addressed artifact is absent") from exc
        if sha256(payload).hexdigest() != digest:
            raise ContractValidationError("content-addressed artifact hash mismatch")
        return payload

    def path_for(self, artifact_sha256: str) -> Path:
        digest = _sha256(artifact_sha256, "artifact_sha256")
        path = (self.root / digest[:2] / digest).resolve()
        if self.root not in path.parents:
            raise ContractValidationError("artifact path escapes store root")
        return path
