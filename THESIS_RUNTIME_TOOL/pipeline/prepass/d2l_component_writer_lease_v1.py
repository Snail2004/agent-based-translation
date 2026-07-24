"""Cross-process single-writer lease for one D2L component package."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import IO
from uuid import uuid4


LEASE_SCHEMA = "d2l_component_writer_lease_v1"
STAGE_LEASE_SCHEMA = "d2l_stage_writer_lease_v1"


class D2LComponentWriterLeaseError(RuntimeError):
    """Raised when a component package already has an active writer."""


def lease_path_for_component(component_root: str | Path) -> Path:
    root = Path(component_root).resolve()
    return root.parent / f".{root.name}.writer.lock"


def stage_lease_path_for_component(component_root: str | Path) -> Path:
    root = Path(component_root).resolve()
    return root.parent / f".{root.name}.stage-writer.lock"


class D2LComponentWriterLease:
    """Hold an OS-released exclusive byte lock for the runner lifetime."""

    schema = LEASE_SCHEMA

    def __init__(self, component_root: str | Path) -> None:
        self.component_root = Path(component_root).resolve()
        self.path = lease_path_for_component(self.component_root)
        self._handle: IO[bytes] | None = None

    def acquire(self) -> "D2LComponentWriterLease":
        if self._handle is not None:
            raise D2LComponentWriterLeaseError("component writer lease is already held")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b", buffering=0)
        try:
            if os.name == "nt":
                self._lock_windows(handle)
            else:
                self._lock_posix(handle)
        except (OSError, BlockingIOError) as exc:
            handle.close()
            raise D2LComponentWriterLeaseError(
                "component writer lease is held by another process"
            ) from exc
        self._handle = handle
        self._write_owner_record()
        return self

    @staticmethod
    def _lock_windows(handle: IO[bytes]) -> None:
        import msvcrt

        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    @staticmethod
    def _lock_posix(handle: IO[bytes]) -> None:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _write_owner_record(self) -> None:
        assert self._handle is not None
        record = {
            "schema": self.schema,
            "component_root": str(self.component_root),
            "owner_pid": os.getpid(),
            "lease_nonce": uuid4().hex,
        }
        payload = (
            json.dumps(record, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        self._handle.seek(0)
        self._handle.truncate()
        self._handle.write(payload)
        self._handle.flush()
        os.fsync(self._handle.fileno())
        self._handle.seek(0)

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._handle = None

    def __enter__(self) -> "D2LComponentWriterLease":
        return self.acquire()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()


class D2LStageWriterLease(D2LComponentWriterLease):
    """Lease retained by the stage guard that owns the actual writer tree."""

    schema = STAGE_LEASE_SCHEMA

    def __init__(self, component_root: str | Path) -> None:
        super().__init__(component_root)
        self.path = stage_lease_path_for_component(self.component_root)


def _lease_is_active(lease: D2LComponentWriterLease) -> bool:
    try:
        lease.acquire()
    except D2LComponentWriterLeaseError:
        return True
    else:
        lease.release()
        return False


def component_writer_is_active(component_root: str | Path) -> bool:
    """Probe the lease without trusting registry PID state."""

    return _lease_is_active(D2LComponentWriterLease(component_root))


def stage_writer_is_active(component_root: str | Path) -> bool:
    """Return true while a stage guard can still write journal/output bytes."""

    return _lease_is_active(D2LStageWriterLease(component_root))


__all__ = [
    "D2LComponentWriterLease",
    "D2LComponentWriterLeaseError",
    "D2LStageWriterLease",
    "LEASE_SCHEMA",
    "STAGE_LEASE_SCHEMA",
    "component_writer_is_active",
    "lease_path_for_component",
    "stage_lease_path_for_component",
    "stage_writer_is_active",
]
