"""Launch a D2L stage behind a barrier and a kill-on-close process tree."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Mapping, Sequence
from uuid import uuid4

from pipeline.prepass.d2l_component_writer_lease_v1 import (
    stage_writer_is_active,
)


READY_TIMEOUT_SECONDS = 15.0
LEASE_RELEASE_TIMEOUT_SECONDS = 3.0
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_TOOL_ROOT = Path(__file__).resolve().parents[2]


class D2LStageProcessTreeError(RuntimeError):
    """Raised when a stage writer tree cannot be made fail-closed."""


class _JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobObjectBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _WindowsKillOnCloseJob:
    def __init__(self, process: subprocess.Popen) -> None:
        self._handle = None
        if os.name != "nt":
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
        ]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self._kernel32 = kernel32
        self._handle = handle
        info = _JobObjectExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = (
            _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        if not kernel32.SetInformationJobObject(
            handle,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            self.close()
            raise ctypes.WinError(ctypes.get_last_error())
        if not kernel32.AssignProcessToJobObject(
            handle,
            wintypes.HANDLE(process._handle),
        ):
            self.close()
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        self._kernel32.CloseHandle(handle)


class D2LGuardedStageProcess:
    """Popen facade whose writer descendants cannot outlive the runner."""

    def __init__(
        self,
        *,
        component_root: str | Path,
        stage_id: str,
        command: Sequence[str],
        cwd: str | Path,
        env: Mapping[str, str] | None,
        stdout,
        stderr,
    ) -> None:
        root = Path(component_root).resolve()
        self.component_root = root
        guard_root = root / "runtime" / "stage_process_guards"
        guard_root.mkdir(parents=True, exist_ok=True)
        nonce = uuid4().hex
        self.ready_path = guard_root / f"{stage_id}_{nonce}.ready"
        self.barrier_path = guard_root / f"{stage_id}_{nonce}.barrier"
        self._token = nonce
        guard_command = [
            sys.executable,
            "-S",
            "-m",
            "pipeline.prepass.d2l_stage_process_guard_v1",
            "--component-root",
            str(root),
            "--parent-pid",
            str(os.getpid()),
            "--ready-file",
            str(self.ready_path),
            "--barrier-file",
            str(self.barrier_path),
            "--barrier-token",
            nonce,
            "--cwd",
            str(Path(cwd).resolve()),
            "--",
            *[str(value) for value in command],
        ]
        guard_environment = dict(os.environ)
        if env is not None:
            guard_environment.update(env)
        self.process = subprocess.Popen(
            guard_command,
            cwd=_TOOL_ROOT,
            shell=False,
            stdout=stdout,
            stderr=stderr,
            env=guard_environment,
        )
        self._job: _WindowsKillOnCloseJob | None = None
        try:
            self._job = _WindowsKillOnCloseJob(self.process)
            self._await_ready()
            temporary_barrier = self.barrier_path.with_name(
                f".{self.barrier_path.name}.tmp"
            )
            temporary_barrier.write_text(nonce, encoding="ascii")
            deadline = time.monotonic() + 2.0
            while True:
                try:
                    os.replace(temporary_barrier, self.barrier_path)
                    break
                except PermissionError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.02)
        except Exception:
            self.terminate()
            try:
                self.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.kill()
                self.wait(timeout=5)
            self.close()
            raise

    @property
    def returncode(self) -> int | None:
        return self.process.returncode

    def _await_ready(self) -> None:
        deadline = time.monotonic() + READY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self.ready_path.is_file():
                try:
                    observed = self.ready_path.read_text(encoding="ascii")
                except PermissionError:
                    time.sleep(0.02)
                    continue
                if observed != self._token:
                    raise D2LStageProcessTreeError(
                        "stage guard ready token drift"
                    )
                return
            returncode = self.process.poll()
            if returncode is not None:
                raise D2LStageProcessTreeError(
                    f"stage guard exited before ready: {returncode}"
                )
            time.sleep(0.02)
        raise D2LStageProcessTreeError("stage guard did not become ready")

    def poll(self) -> int | None:
        return self.process.poll()

    def wait(self, timeout: float | None = None) -> int:
        return self.process.wait(timeout=timeout)

    def terminate(self) -> None:
        if self._job is not None:
            self._job.close()
            self._job = None
        if self.process.poll() is None:
            self.process.terminate()

    def kill(self) -> None:
        if self._job is not None:
            self._job.close()
            self._job = None
        if self.process.poll() is None:
            self.process.kill()

    def close(self) -> None:
        if self.process.poll() is None:
            self.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.kill()
                self.process.wait(timeout=5)
        if self._job is not None:
            self._job.close()
            self._job = None
        paths = (
            self.ready_path,
            self.barrier_path,
            self.ready_path.with_name(f".{self.ready_path.name}.tmp"),
            self.barrier_path.with_name(f".{self.barrier_path.name}.tmp"),
        )
        for path in paths:
            deadline = time.monotonic() + 2.0
            while True:
                try:
                    path.unlink(missing_ok=True)
                    break
                except PermissionError:
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(0.02)
        deadline = time.monotonic() + LEASE_RELEASE_TIMEOUT_SECONDS
        while stage_writer_is_active(self.component_root):
            if time.monotonic() >= deadline:
                raise D2LStageProcessTreeError(
                    "stage writer lease remained active after process-tree close"
                )
            time.sleep(0.02)


__all__ = [
    "D2LGuardedStageProcess",
    "D2LStageProcessTreeError",
    "LEASE_RELEASE_TIMEOUT_SECONDS",
    "READY_TIMEOUT_SECONDS",
]
