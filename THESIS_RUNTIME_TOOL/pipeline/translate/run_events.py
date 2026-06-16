from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


EVENT_SCHEMA_VERSION = "run_event_v1"


class NullEventSink:
    """No-op sink used when live event logging is disabled."""

    enabled = False

    def emit(self, event: str, **payload: Any) -> None:
        return None


class EventSink:
    """Best-effort JSONL sidecar event sink.

    The sink is observability-only.  I/O failures are swallowed so enabling the
    sink cannot change translation success/failure behavior.
    """

    enabled = True

    def __init__(
        self,
        path: str | Path,
        *,
        run_id: str | None = None,
        attempt_id: str | None = None,
    ) -> None:
        self.path = Path(path)
        self.run_id = run_id or attempt_id or self.path.stem
        self.attempt_id = attempt_id or self.run_id
        self._seq = 0

    def emit(self, event: str, **payload: Any) -> None:
        try:
            self._seq += 1
            row = {
                "schema": EVENT_SCHEMA_VERSION,
                "seq": self._seq,
                "ts": datetime.now(UTC).isoformat(),
                "event": str(event),
                "run_id": self.run_id,
                "attempt_id": self.attempt_id,
                **_json_safe(payload),
            }
            self._write_line(row)
        except Exception:
            return None

    def _write_line(self, row: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def emit_event(sink: Any | None, event: str, **payload: Any) -> None:
    """Emit through any sink-like object without letting observability fail runs."""

    if sink is None:
        return None
    try:
        sink.emit(event, **payload)
    except Exception:
        return None


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
