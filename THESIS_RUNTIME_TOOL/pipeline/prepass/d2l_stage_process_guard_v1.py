"""Stage subprocess guard that retains the stage-writer lease."""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import signal
import subprocess
import time

from pipeline.prepass.d2l_component_writer_lease_v1 import (
    D2LStageWriterLease,
)


GUARD_VERSION = "d2l_stage_process_guard_v1"
BARRIER_TIMEOUT_SECONDS = 30.0
PARENT_POLL_SECONDS = 0.05
_SYNCHRONIZE = 0x00100000
_WAIT_TIMEOUT = 0x00000102
_child: subprocess.Popen | None = None


def _terminate_child() -> None:
    global _child
    process = _child
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _handle_termination(_signum, _frame) -> None:
    _terminate_child()
    raise SystemExit(143)


def _wait_for_barrier(path: Path, token: str) -> None:
    deadline = time.monotonic() + BARRIER_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if path.is_file():
            try:
                observed = path.read_text(encoding="ascii")
            except PermissionError:
                time.sleep(0.02)
                continue
            if observed != token:
                raise RuntimeError("stage start barrier token drift")
            return
        time.sleep(0.02)
    raise RuntimeError("stage start barrier timed out")


def _write_token_atomically(path: Path, token: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(token, encoding="ascii")
    deadline = time.monotonic() + 2.0
    while True:
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.02)


def _parent_is_alive(parent_pid: int) -> bool:
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.WaitForSingleObject.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
        ]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(_SYNCHRONIZE, False, parent_pid)
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == _WAIT_TIMEOUT
        finally:
            kernel32.CloseHandle(handle)
    if os.getppid() != parent_pid:
        return False
    try:
        os.kill(parent_pid, 0)
    except OSError:
        return False
    return True


def run_guarded_stage(
    *,
    component_root: str | Path,
    parent_pid: int,
    ready_file: str | Path,
    barrier_file: str | Path,
    barrier_token: str,
    cwd: str | Path,
    command: list[str],
) -> int:
    if not command:
        raise RuntimeError("guarded stage command is empty")
    ready = Path(ready_file).resolve()
    barrier = Path(barrier_file).resolve()
    ready.parent.mkdir(parents=True, exist_ok=True)
    signal.signal(signal.SIGTERM, _handle_termination)
    signal.signal(signal.SIGINT, _handle_termination)
    with D2LStageWriterLease(component_root):
        _write_token_atomically(ready, barrier_token)
        _wait_for_barrier(barrier, barrier_token)
        global _child
        _child = subprocess.Popen(
            command,
            cwd=Path(cwd).resolve(),
            shell=False,
            env=dict(os.environ),
        )
        try:
            while _child.poll() is None:
                if not _parent_is_alive(parent_pid):
                    _terminate_child()
                    return 143
                time.sleep(PARENT_POLL_SECONDS)
            return int(_child.returncode)
        finally:
            _terminate_child()
            _child = None


def main() -> int:
    parser = argparse.ArgumentParser(description="Guard one D2L stage writer")
    parser.add_argument("--component-root", required=True)
    parser.add_argument("--parent-pid", required=True, type=int)
    parser.add_argument("--ready-file", required=True)
    parser.add_argument("--barrier-file", required=True)
    parser.add_argument("--barrier-token", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    return run_guarded_stage(
        component_root=args.component_root,
        parent_pid=args.parent_pid,
        ready_file=args.ready_file,
        barrier_file=args.barrier_file,
        barrier_token=args.barrier_token,
        cwd=args.cwd,
        command=command,
    )


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BARRIER_TIMEOUT_SECONDS",
    "GUARD_VERSION",
    "PARENT_POLL_SECONDS",
    "run_guarded_stage",
]
