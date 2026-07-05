from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def read_jsonl_events(path: Path, *, offset: int = 0, max_bytes: int = 256 * 1024) -> dict[str, Any]:
    """Read complete JSONL event rows from a byte offset without consuming partial rows."""
    safe_offset = max(int(offset or 0), 0)
    safe_max_bytes = max(64, min(int(max_bytes or 256 * 1024), 1024 * 1024))
    events: list[dict[str, Any]] = []
    new_offset = safe_offset
    truncated = False
    partial_line = False

    if not path.exists():
        return {
            "events": events,
            "offset": new_offset,
            "truncated": truncated,
            "partial_line": partial_line,
            "max_bytes": safe_max_bytes,
        }

    with path.open("rb") as fh:
        fh.seek(safe_offset)
        raw = fh.read(safe_max_bytes + 1)
    truncated = len(raw) > safe_max_bytes
    if truncated:
        raw = raw[:safe_max_bytes]
    last_newline = raw.rfind(b"\n")
    if last_newline < 0:
        partial_line = bool(raw)
        complete = b""
    else:
        complete = raw[: last_newline + 1]
        tail = raw[last_newline + 1 :]
        partial_line = bool(tail)
    if complete:
        new_offset = safe_offset + len(complete)
        for line in complete.splitlines():
            if not line.strip():
                continue
            try:
                parsed = json.loads(line.decode("utf-8"))
                if isinstance(parsed, dict):
                    events.append(parsed)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                events.append(
                    {
                        "v": 1,
                        "event": "parse_error",
                        "payload": {"error": f"{type(exc).__name__}: {exc}"},
                    }
                )

    return {
        "events": events,
        "offset": new_offset,
        "truncated": truncated,
        "partial_line": partial_line,
        "max_bytes": safe_max_bytes,
    }
