from __future__ import annotations

import json
import os
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


EVENT_SCHEMA_VERSION = 1
EVENT_SCHEMA_NAME = "one_button_event_v1"

FORCE_FLUSH_EVENTS = {
    "run_start",
    "run_done",
    "run_failed",
    "run_cancelled",
    "run_resumed",
    "stage_start",
    "stage_done",
    "stage_skipped",
    "gate_pause",
    "warning",
    "error",
    "cost_snapshot",
}

EVENT_SEVERITY = {
    "warning": "warning",
    "error": "error",
    "run_failed": "error",
    "gate_pause": "warning",
}


class NullEventEmitter:
    enabled = False

    def emit(self, *args: Any, **kwargs: Any) -> None:
        return None

    def flush(self) -> None:
        return None

    def close(self) -> None:
        return None


class EventEmitter:
    """Buffered, best-effort JSONL emitter for one-button UI events.

    This class is intentionally single-writer.  Multi-process merging belongs
    to the future orchestrator, not to this file writer.
    """

    enabled = True

    def __init__(
        self,
        path: str | Path,
        *,
        run_id: str,
        attempt_id: int = 1,
        resume_from: dict[str, Any] | None = None,
        flush_every: int = 20,
        flush_interval_s: float = 0.5,
        chunk_bytes: int = 64 * 1024,
    ) -> None:
        self.path = Path(path)
        self.run_id = str(run_id)
        self.attempt_id = int(attempt_id)
        self.resume_from = resume_from
        self.flush_every = max(1, int(flush_every))
        self.flush_interval_s = max(0.0, float(flush_interval_s))
        self.chunk_bytes = max(1024, int(chunk_bytes))
        self._seq = 0
        self._buffer: list[bytes] = []
        self._last_flush = time.monotonic()
        self._writer_id = uuid.uuid4().hex[:12]

    @property
    def seq(self) -> int:
        return self._seq

    def emit(
        self,
        event_type: str,
        *,
        stage: str,
        agent: str,
        script: str,
        severity: str | None = None,
        payload: dict[str, Any] | None = None,
        **extra_payload: Any,
    ) -> None:
        try:
            self._seq += 1
            clean_event_type = str(event_type)
            clean_severity = str(severity or EVENT_SEVERITY.get(clean_event_type, "info"))
            if clean_event_type in {"warning", "error"}:
                clean_severity = clean_event_type
            merged_payload = dict(payload or {})
            merged_payload.update(extra_payload)
            row = {
                "v": EVENT_SCHEMA_VERSION,
                "schema": EVENT_SCHEMA_NAME,
                "event_id": f"{self.run_id}:{self.attempt_id}:{self._writer_id}:{self._seq}",
                "run_id": self.run_id,
                "attempt_id": self.attempt_id,
                "seq": self._seq,
                "resume_from": self.resume_from,
                "ts": datetime.now(UTC).isoformat(),
                "stage": str(stage),
                "script": str(script),
                "agent": str(agent),
                "event": clean_event_type,
                "event_type": clean_event_type,
                "severity": clean_severity,
                "payload": _json_safe(merged_payload),
            }
            self._buffer.append(
                (json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
            )
            now = time.monotonic()
            if (
                clean_event_type in FORCE_FLUSH_EVENTS
                or len(self._buffer) >= self.flush_every
                or (self.flush_interval_s and now - self._last_flush >= self.flush_interval_s)
            ):
                self.flush()
        except Exception:
            return None

    def flush(self) -> None:
        try:
            if not self._buffer:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = b"".join(self._buffer)
            self._buffer.clear()
            self._last_flush = time.monotonic()
            binary_flag = getattr(os, "O_BINARY", 0)
            fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_APPEND | binary_flag)
            try:
                for chunk in _split_on_line_boundaries(payload, self.chunk_bytes):
                    if chunk:
                        os.write(fd, chunk)
            finally:
                os.close(fd)
        except Exception:
            return None

    def close(self) -> None:
        self.flush()

    def __enter__(self) -> "EventEmitter":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()


def _split_on_line_boundaries(payload: bytes, limit: int) -> list[bytes]:
    if len(payload) <= limit:
        return [payload]
    chunks: list[bytes] = []
    cursor = 0
    while cursor < len(payload):
        end = min(cursor + limit, len(payload))
        if end < len(payload):
            newline = payload.rfind(b"\n", cursor, end)
            if newline >= cursor:
                end = newline + 1
        if end <= cursor:
            end = min(cursor + limit, len(payload))
        chunks.append(payload[cursor:end])
        cursor = end
    return chunks


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if hasattr(value, "to_dict"):
        try:
            return _json_safe(value.to_dict())
        except Exception:
            return repr(value)
    return repr(value)
