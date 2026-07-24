from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from pipeline.prepass.d2l_component_writer_lease_v1 import (
    D2LComponentWriterLease,
    D2LComponentWriterLeaseError,
    component_writer_is_active,
    lease_path_for_component,
)


TOOL_ROOT = Path(__file__).resolve().parents[2]


def _wait_until(predicate, *, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition did not become true before timeout")


def test_component_writer_lease_is_exclusive_and_released(tmp_path: Path) -> None:
    root = tmp_path / "component"
    with D2LComponentWriterLease(root):
        assert component_writer_is_active(root) is True
        with pytest.raises(
            D2LComponentWriterLeaseError,
            match="another process",
        ):
            D2LComponentWriterLease(root).acquire()

    assert component_writer_is_active(root) is False
    record = json.loads(lease_path_for_component(root).read_text(encoding="utf-8"))
    assert record["component_root"] == str(root.resolve())


def test_wrapper_exit_does_not_hide_live_component_writer(
    tmp_path: Path,
) -> None:
    root = tmp_path / "component"
    ready = tmp_path / "child_ready"
    stop = tmp_path / "child_stop"
    pid_file = tmp_path / "child_pid"
    child_code = """
import os
from pathlib import Path
import sys
import time
from pipeline.prepass.d2l_component_writer_lease_v1 import D2LComponentWriterLease

root, ready, stop, pid_file = map(Path, sys.argv[1:5])
with D2LComponentWriterLease(root):
    pid_file.write_text(str(os.getpid()), encoding="ascii")
    ready.write_text("ready", encoding="ascii")
    while not stop.exists():
        time.sleep(0.05)
"""
    wrapper_code = """
import subprocess
import sys

subprocess.Popen(
    [sys.executable, "-c", sys.argv[1], *sys.argv[2:6]],
    cwd=sys.argv[6],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    close_fds=True,
)
"""
    wrapper = subprocess.run(
        [
            sys.executable,
            "-c",
            wrapper_code,
            child_code,
            str(root),
            str(ready),
            str(stop),
            str(pid_file),
            str(TOOL_ROOT),
        ],
        cwd=TOOL_ROOT,
        check=True,
        timeout=10,
    )
    assert wrapper.returncode == 0
    _wait_until(ready.is_file)
    child_pid = int(pid_file.read_text(encoding="ascii"))

    try:
        assert component_writer_is_active(root) is True
        with pytest.raises(
            D2LComponentWriterLeaseError,
            match="another process",
        ):
            D2LComponentWriterLease(root).acquire()
    finally:
        stop.write_text("stop", encoding="ascii")
        try:
            _wait_until(lambda: not component_writer_is_active(root))
        except AssertionError:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(child_pid)],
                    check=False,
                    capture_output=True,
                )
            else:
                os.kill(child_pid, signal.SIGTERM)
            raise
