"""Atomic physical-quota leases with no stale-lock inference or stealing."""

from __future__ import annotations

from hashlib import sha256
import os
from pathlib import Path
from typing import Any

from .contracts_v1 import (
    ContractValidationError,
    _identifier,
    _utc_timestamp,
    canonical_json,
    canonical_sha256,
)


class QuotaBusyError(RuntimeError):
    pass


class QuotaLease:
    def __init__(self, *, path: Path, payload: dict[str, Any]) -> None:
        self.path = path
        self.payload = payload
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        try:
            observed = self.path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise ContractValidationError("physical quota lease disappeared") from exc
        if observed != canonical_json(self.payload):
            raise ContractValidationError("physical quota lease ownership changed")
        self.path.unlink()
        self._released = True

    def __enter__(self) -> "QuotaLease":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()


class PhysicalQuotaScheduler:
    """Acquire one process-independent file lease per physical quota bucket."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def acquire(
        self,
        *,
        physical_quota_bucket_id: str,
        lease_id: str,
        owner_id: str,
        acquired_at_utc: str,
    ) -> QuotaLease:
        bucket = _identifier(
            physical_quota_bucket_id, "physical_quota_bucket_id"
        )
        lease = _identifier(lease_id, "lease_id")
        owner = _identifier(owner_id, "owner_id")
        acquired = _utc_timestamp(acquired_at_utc, "acquired_at_utc")
        digest = sha256(bucket.encode("utf-8")).hexdigest()
        path = self.root / f"{digest}.lock"
        payload_body = {
            "schema_version": "physical_quota_lease_v1",
            "physical_quota_bucket_id": bucket,
            "lease_id": lease,
            "owner_id": owner,
            "acquired_at_utc": acquired,
        }
        payload = {
            **payload_body,
            "lease_sha256": canonical_sha256(payload_body),
        }
        encoded = canonical_json(payload).encode("utf-8")
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError as exc:
            raise QuotaBusyError(
                f"physical quota bucket {bucket!r} already has an active lease"
            ) from exc
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return QuotaLease(path=path, payload=payload)
