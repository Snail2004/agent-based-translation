from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping


LEDGER_POLICY = "CONTENT_ADDRESSED_V1"


class ProviderResponseLedger:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.response_root = self.root / "provider_responses"
        self.attempt_path = self.root / "provider_attempts.jsonl"
        self.response_root.mkdir(parents=True, exist_ok=True)

    def capture(self, text: str) -> dict[str, str]:
        raw = text.encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        relative = f"provider_responses/{digest}.txt"
        target = self.root / Path(relative)
        if target.exists():
            if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise RuntimeError("provider response ledger hash collision")
        else:
            self._atomic_write(target, raw)
        self._append(
            {
                "record_kind": "RAW_RESPONSE_CAPTURED",
                "raw_response_ref": relative,
                "raw_response_sha256": digest,
                "raw_response_bytes": len(raw),
            }
        )
        return {
            "raw_response_ref": relative,
            "raw_response_sha256": digest,
            "raw_response_storage_status": "STORED",
        }

    def record_attempt(self, attempt: Mapping[str, Any]) -> None:
        self._append({"record_kind": "PROVIDER_ATTEMPT", **dict(attempt)})

    def _append(self, row: Mapping[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = (
            json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        with self.attempt_path.open("ab") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
