from __future__ import annotations

import ctypes
import hashlib
import json
import os
import socket
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterable


class CheckpointError(RuntimeError):
    """Base error for literary checkpoint operations."""


class CheckpointLockedError(CheckpointError):
    """Raised when another live process owns the checkpoint directory."""


def _normalize(value: Any) -> Any:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, dict):
        return {str(key): _normalize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    if isinstance(value, tuple):
        return [_normalize(item) for item in value]
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(
        _normalize(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_existing_canonical_path(path: str | Path) -> Path:
    """Resolve a canonical-JSON path without losing filesystem Unicode spelling."""

    candidate = Path(path)
    if candidate.exists():
        return candidate.resolve()
    if candidate.is_absolute():
        candidates = [Path(candidate.anchor)]
        remaining = candidate.parts[1:]
    else:
        candidates = [Path.cwd()]
        remaining = candidate.parts
    for part in remaining:
        normalized = unicodedata.normalize("NFC", part).casefold()
        matches: dict[str, Path] = {}
        for current in candidates:
            if not current.is_dir():
                continue
            for child in current.iterdir():
                if (
                    unicodedata.normalize("NFC", child.name).casefold()
                    == normalized
                ):
                    matches[str(child)] = child
        if not matches:
            raise CheckpointError(
                f"Canonical path has no filesystem equivalent: {candidate}"
            )
        candidates = [matches[key] for key in sorted(matches)]
    if len(candidates) != 1 or not candidates[0].exists():
        raise CheckpointError(
            f"Canonical path has no unique filesystem equivalent: {candidate}"
        )
    return candidates[0].resolve()


def chapter_source_hash(chapter: dict[str, Any]) -> str:
    rows = [
        {
            "block_id": str(block.get("block_id") or ""),
            "clean_text": str(block.get("clean_text") or block.get("source_text") or ""),
        }
        for block in chapter.get("blocks") or []
    ]
    return canonical_hash(rows)


def config_hash(payload: dict[str, Any]) -> str:
    return canonical_hash(payload)


def artifact_manifest(paths: Iterable[Path], *, root: Path) -> list[dict[str, Any]]:
    root_resolved = Path(root).resolve()
    rows: list[dict[str, Any]] = []
    for raw_path in paths:
        path = Path(raw_path).resolve()
        try:
            relative = path.relative_to(root_resolved)
        except ValueError as exc:
            raise CheckpointError(f"Artifact escapes checkpoint root: {path}") from exc
        if not path.is_file():
            raise CheckpointError(f"Artifact missing before checkpoint publish: {path}")
        rows.append(
            {
                "path": relative.as_posix(),
                "sha256": file_sha256(path),
                "size": path.stat().st_size,
            }
        )
    return sorted(rows, key=lambda item: str(item["path"]))


def build_checkpoint(payload: dict[str, Any]) -> dict[str, Any]:
    checkpoint = _normalize(dict(payload))
    checkpoint.pop("checkpoint_hash", None)
    checkpoint.setdefault("created_at", datetime.now(UTC).isoformat())
    checkpoint["checkpoint_hash"] = canonical_hash(checkpoint)
    return checkpoint


def verify_checkpoint_hash(checkpoint: dict[str, Any]) -> bool:
    expected = str(checkpoint.get("checkpoint_hash") or "")
    unhashed = dict(checkpoint)
    unhashed.pop("checkpoint_hash", None)
    return bool(expected) and canonical_hash(unhashed) == expected


def write_checkpoint_atomic(path: Path, checkpoint: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    encoded = (json.dumps(checkpoint, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    try:
        with temporary.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def read_checkpoint(path: Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise CheckpointError(f"Checkpoint is not a JSON object: {path}")
    return payload


def verify_artifact_manifest(
    manifest: list[dict[str, Any]],
    *,
    root: Path,
) -> list[str]:
    errors: list[str] = []
    root_resolved = Path(root).resolve()
    for row in manifest:
        relative = Path(str(row.get("path") or ""))
        path = (root_resolved / relative).resolve()
        try:
            path.relative_to(root_resolved)
        except ValueError:
            errors.append(f"artifact_path_escape:{relative.as_posix()}")
            continue
        if not path.is_file():
            errors.append(f"artifact_missing:{relative.as_posix()}")
            continue
        if path.stat().st_size != int(row.get("size") or -1):
            errors.append(f"artifact_size:{relative.as_posix()}")
            continue
        if file_sha256(path) != str(row.get("sha256") or ""):
            errors.append(f"artifact_sha256:{relative.as_posix()}")
    return errors


def validate_checkpoint(
    checkpoint: dict[str, Any],
    *,
    root: Path,
    expected: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    if not verify_checkpoint_hash(checkpoint):
        errors.append("checkpoint_hash")
    for field, expected_value in expected.items():
        if checkpoint.get(field) != expected_value:
            errors.append(field)
    manifest = checkpoint.get("artifact_manifest")
    if not isinstance(manifest, list):
        errors.append("artifact_manifest")
    else:
        errors.extend(verify_artifact_manifest(manifest, root=root))
    return errors


def semantic_state_hash(value: Any) -> str:
    """Hash deterministic state while excluding operational metadata."""

    if isinstance(value, dict):
        cleaned = {
            key: semantic_state_hash_value(item)
            for key, item in value.items()
            if key not in {"accounting", "created_at", "path", "checkpoint_hash"}
        }
        return canonical_hash(cleaned)
    return canonical_hash(value)


def semantic_state_hash_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: semantic_state_hash_value(item)
            for key, item in value.items()
            if key not in {"accounting", "created_at", "path", "checkpoint_hash"}
        }
    if isinstance(value, list):
        return [semantic_state_hash_value(item) for item in value]
    return value


def pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        process_query_limited_information = 0x1000
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@dataclass
class CheckpointLock:
    root: Path
    alive_check: Callable[[int], bool] = pid_is_alive

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.path = self.root / "checkpoints" / "lock.json"
        self.host = socket.gethostname()
        self.pid = os.getpid()
        self.token = uuid.uuid4().hex
        self.acquired = False
        self.took_over_stale = False

    def acquire(self) -> "CheckpointLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "pid": self.pid,
            "host": self.host,
            "start_time": datetime.now(UTC).isoformat(),
            "token": self.token,
        }
        encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        for _attempt in range(3):
            try:
                descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                owner = self._read_owner()
                owner_host = str(owner.get("host") or "")
                owner_pid = int(owner.get("pid") or 0)
                if owner_host != self.host:
                    raise CheckpointLockedError(
                        f"Checkpoint lock belongs to another host: {owner_host or 'unknown'}"
                    )
                if self.alive_check(owner_pid):
                    raise CheckpointLockedError(
                        f"Checkpoint directory is active under pid {owner_pid}"
                    )
                current = self._read_owner()
                if current.get("token") == owner.get("token"):
                    self.path.unlink(missing_ok=True)
                    self.took_over_stale = True
                continue
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            self.acquired = True
            return self
        raise CheckpointLockedError("Could not acquire checkpoint lock after stale takeover")

    def _read_owner(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def release(self) -> None:
        if not self.acquired:
            return
        owner = self._read_owner()
        if owner.get("token") == self.token:
            self.path.unlink(missing_ok=True)
        self.acquired = False

    def __enter__(self) -> "CheckpointLock":
        return self.acquire()

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.release()
