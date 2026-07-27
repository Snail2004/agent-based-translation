from __future__ import annotations

import ast
import builtins
from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import socket
import sqlite3
from typing import Any, Callable, Iterator, Mapping, Protocol, Sequence


class BoundaryError(RuntimeError):
    """Raised when S5A crosses a declared capability boundary."""


class PromptProvider(Protocol):
    def load_prompt(self, prompt_id: str) -> str: ...


class RequestExecutor(Protocol):
    def execute(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...


class OperationalClock(Protocol):
    def now_audit(self) -> str: ...


class DisclosureFilteredContext(Protocol):
    @property
    def disclosure_view_hash(self) -> str: ...

    def iter_items(self) -> Sequence[Mapping[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class AccessEvent:
    capability: str
    target: str
    allowed: bool
    adapter: str | None = None


class AccessLedger:
    def __init__(self) -> None:
        self._events: list[AccessEvent] = []

    def record(
        self, *, capability: str, target: str, allowed: bool, adapter: str | None = None
    ) -> None:
        self._events.append(
            AccessEvent(
                capability=capability,
                target=target,
                allowed=allowed,
                adapter=adapter,
            )
        )

    @property
    def events(self) -> tuple[AccessEvent, ...]:
        return tuple(self._events)

    def assert_clean(self) -> None:
        denied = [event for event in self._events if not event.allowed]
        if denied:
            raise BoundaryError(f"forbidden semantic accesses were attempted: {denied}")


CORE_IMPORT_DENYLIST = frozenset(
    {
        "datetime",
        "http",
        "io",
        "os",
        "pathlib",
        "requests",
        "sqlite3",
        "socket",
        "subprocess",
        "time",
        "urllib",
        "pipeline.literary.builder_v3_pipeline",
        "pipeline.literary.checkpoint_v3",
        "pipeline.literary.builder_pilot",
        "pipeline.literary." + "b4_handoff" + "_v3",
    }
)


def assert_core_import_boundary(paths: Sequence[Path]) -> None:
    for path in paths:
        tree = ast.parse(Path(path).read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if any(name == denied or name.startswith(f"{denied}.") for denied in CORE_IMPORT_DENYLIST):
                    raise BoundaryError(f"core module imports denied capability {name}: {path}")


class RuntimeSpy:
    """Narrow runtime capability audit for disposable S5 fixtures."""

    def __init__(
        self,
        *,
        allowed_roots: Sequence[Path],
        denied_paths: Sequence[Path] = (),
        ledger: AccessLedger | None = None,
        allow_network: bool = False,
    ) -> None:
        self.allowed_roots = tuple(Path(path).resolve() for path in allowed_roots)
        self.denied_paths = tuple(Path(path).resolve() for path in denied_paths)
        self.ledger = ledger or AccessLedger()
        self.allow_network = allow_network
        self._restore: list[tuple[object, str, object]] = []

    def _path_allowed(self, raw_path: object) -> bool:
        try:
            path = Path(raw_path).resolve()
        except (TypeError, OSError):
            return False
        if any(path == denied or denied in path.parents for denied in self.denied_paths):
            return False
        return any(path == root or root in path.parents for root in self.allowed_roots)

    def _check_file(self, raw_path: object, capability: str) -> None:
        allowed = self._path_allowed(raw_path)
        self.ledger.record(capability=capability, target=str(raw_path), allowed=allowed)
        if not allowed:
            raise BoundaryError(f"forbidden {capability} access: {raw_path}")

    def _patch(self, owner: object, name: str, replacement: object) -> None:
        original = getattr(owner, name)
        self._restore.append((owner, name, original))
        setattr(owner, name, replacement)

    def __enter__(self) -> "RuntimeSpy":
        original_open = builtins.open
        original_path_open = Path.open
        original_os_open = os.open
        original_connect = sqlite3.connect
        original_create_connection = socket.create_connection
        original_socket = socket.socket

        def audited_open(file: object, *args: object, **kwargs: object) -> Any:
            self._check_file(file, "builtins.open")
            return original_open(file, *args, **kwargs)

        def audited_path_open(path: Path, *args: object, **kwargs: object) -> Any:
            self._check_file(path, "Path.open")
            return original_path_open(path, *args, **kwargs)

        def audited_os_open(path: object, *args: object, **kwargs: object) -> int:
            self._check_file(path, "os.open")
            return original_os_open(path, *args, **kwargs)

        def audited_connect(database: object, *args: object, **kwargs: object) -> Any:
            self._check_file(database, "sqlite3.connect")
            return original_connect(database, *args, **kwargs)

        def audited_create_connection(address: object, *args: object, **kwargs: object) -> Any:
            self.ledger.record(
                capability="network", target=str(address), allowed=self.allow_network
            )
            if not self.allow_network:
                raise BoundaryError(f"network unavailable outside RequestExecutor: {address}")
            return original_create_connection(address, *args, **kwargs)

        spy = self

        class AuditedSocket(original_socket):
            def connect(self, address: object) -> Any:
                spy.ledger.record(
                    capability="network",
                    target=str(address),
                    allowed=spy.allow_network,
                )
                if not spy.allow_network:
                    raise BoundaryError(
                        f"network unavailable outside RequestExecutor: {address}"
                    )
                return super().connect(address)

        self._patch(builtins, "open", audited_open)
        self._patch(Path, "open", audited_path_open)
        self._patch(os, "open", audited_os_open)
        self._patch(sqlite3, "connect", audited_connect)
        self._patch(socket, "create_connection", audited_create_connection)
        self._patch(socket, "socket", AuditedSocket)
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        while self._restore:
            owner, name, original = self._restore.pop()
            setattr(owner, name, original)


@contextmanager
def deny_original_inputs(
    *, allowed_roots: Sequence[Path], original_input_paths: Sequence[Path]
) -> Iterator[AccessLedger]:
    ledger = AccessLedger()
    with RuntimeSpy(
        allowed_roots=allowed_roots,
        denied_paths=original_input_paths,
        ledger=ledger,
    ):
        yield ledger


def record_adapter_access(
    ledger: AccessLedger,
    *,
    adapter: str,
    capability: str,
    target: str,
    allowed: bool = True,
) -> None:
    ledger.record(
        adapter=adapter, capability=capability, target=target, allowed=allowed
    )
    if not allowed:
        raise BoundaryError(f"adapter {adapter} attempted forbidden access: {target}")


__all__ = [
    "AccessEvent",
    "AccessLedger",
    "BoundaryError",
    "CORE_IMPORT_DENYLIST",
    "DisclosureFilteredContext",
    "OperationalClock",
    "PromptProvider",
    "RequestExecutor",
    "RuntimeSpy",
    "assert_core_import_boundary",
    "deny_original_inputs",
    "record_adapter_access",
]
