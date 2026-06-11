import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from config import SCHEMA_DIR, VALIDATOR_SCRIPT


def _extract_report(stdout: str) -> dict[str, Any] | None:
    """Parse the validator's JSON report, tolerating stray noise on stdout.

    The validator emits exactly one JSON object on stdout (``--json``). On Windows
    a UTF-8 BOM or a stray warning line can prefix that object and break a naive
    ``json.loads``. We strip a BOM/whitespace, try a direct parse, then fall back
    to the first ``{`` .. last ``}`` slice. Returns ``None`` if nothing parses.
    """
    if not stdout:
        return None
    cleaned = stdout.lstrip("﻿ \t\r\n").strip()
    if not cleaned:
        return None
    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end > start:
        try:
            parsed = json.loads(cleaned[start:end + 1])
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def run_validator(dataset_dir: Path, schema_dir: Path = SCHEMA_DIR) -> dict[str, Any]:
    cmd = [
        sys.executable,
        str(VALIDATOR_SCRIPT),
        "--dataset",
        str(dataset_dir),
        "--schema",
        str(schema_dir),
        "--json",
    ]
    # Force the child process to use UTF-8 for stdout/stderr regardless of the
    # parent's locale. The validator prints JSON with ensure_ascii=False, so a
    # non-UTF-8 child stream (e.g. a stale Windows code page) could raise
    # UnicodeEncodeError, crash, and leave stdout empty -> opaque failure.
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )

    report = _extract_report(proc.stdout)
    if report is None:
        # Surface the real reason instead of hiding it behind a generic message:
        # include the exit code and a tail of stderr (the validator's traceback or
        # setup error) so the UI/log shows WHY the validator produced no JSON.
        stderr_tail = (proc.stderr or "").strip()
        stdout_tail = (proc.stdout or "").strip()
        detail = stderr_tail[-1200:] or stdout_tail[-600:] or "validator produced no output"
        report = {
            "ok": False,
            "dataset": str(dataset_dir),
            "counts": {},
            "errors": [{
                "file": "-",
                "location": f"exit_code={proc.returncode}",
                "message": f"Validator did not return valid JSON. Detail: {detail}",
                "severity": "error",
            }],
            "warnings": [],
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }

    report["exit_code"] = proc.returncode
    if proc.stderr and "stderr" not in report:
        report["stderr"] = proc.stderr
    return report
