import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def log_event(project_path: Path, event_type: str, payload: dict[str, Any] | None = None, user: str = "local") -> None:
    logs_dir = project_path / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    path = logs_dir / "app_events.jsonl"
    event = {
        "ts": _now_iso(),
        "user": user,
        "event": event_type,
        "payload": payload or {},
    }
    line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())
