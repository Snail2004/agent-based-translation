"""Durable, attempt-independent work results for long D2L stages."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from pipeline.prepass.d2l_console_replay_contract_v1 import canonical_sha256


SCHEMA_VERSION = "d2l_stage_work_journal_v1"
_FORBIDDEN_KEYS = {
    "api_key",
    "credential",
    "gold",
    "oracle",
    "raw_prompt",
    "raw_response",
    "reference_text",
    "secret",
}


class D2LStageWorkJournalError(ValueError):
    """Raised when durable stage work cannot be trusted."""


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise D2LStageWorkJournalError(f"{label} must be an object")
    return dict(value)


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise D2LStageWorkJournalError(f"{label} must be a non-empty string")
    return value


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise D2LStageWorkJournalError(f"{label} must be an integer >= {minimum}")
    return value


def _sha(value: Any, label: str) -> str:
    text = _string(value, label).upper()
    if len(text) != 64 or any(character not in "0123456789ABCDEF" for character in text):
        raise D2LStageWorkJournalError(f"{label} must be a SHA-256 digest")
    return text


def _reject_forbidden(value: Any, label: str = "work_journal") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).lower() in _FORBIDDEN_KEYS:
                raise D2LStageWorkJournalError(
                    f"{label} contains forbidden key: {key}"
                )
            _reject_forbidden(child, f"{label}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_forbidden(child, f"{label}[{index}]")


def _entry_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(value)
    payload.pop("entry_sha256", None)
    return payload


def validate_work_journal_entry(
    value: Mapping[str, Any],
    *,
    expected_seq: int,
    previous_entry_sha256: str | None,
) -> dict[str, Any]:
    row = _mapping(value, "work_journal_entry")
    required = {
        "schema_version",
        "workflow_run_id",
        "component_run_id",
        "component_attempt_id",
        "stage_id",
        "work_item_id",
        "work_contract_id",
        "input_sha256",
        "result",
        "result_sha256",
        "journal_seq",
        "previous_entry_sha256",
        "entry_sha256",
    }
    if set(row) != required:
        raise D2LStageWorkJournalError("work journal entry keys mismatch")
    _reject_forbidden(row)
    if row["schema_version"] != SCHEMA_VERSION:
        raise D2LStageWorkJournalError("work journal schema is invalid")
    for key in (
        "workflow_run_id",
        "component_run_id",
        "stage_id",
        "work_item_id",
        "work_contract_id",
    ):
        _string(row[key], f"work_journal_entry.{key}")
    _integer(
        row["component_attempt_id"],
        "work_journal_entry.component_attempt_id",
        minimum=1,
    )
    if (
        _integer(row["journal_seq"], "work_journal_entry.journal_seq", minimum=1)
        != expected_seq
    ):
        raise D2LStageWorkJournalError("work journal sequence is not contiguous")
    previous = row["previous_entry_sha256"]
    if previous is not None:
        previous = _sha(previous, "work_journal_entry.previous_entry_sha256")
    if previous != previous_entry_sha256:
        raise D2LStageWorkJournalError("work journal previous hash mismatch")
    result = _mapping(row["result"], "work_journal_entry.result")
    result_sha = _sha(row["result_sha256"], "work_journal_entry.result_sha256")
    if result_sha != canonical_sha256(result):
        raise D2LStageWorkJournalError("work journal result hash drift")
    input_sha = _sha(row["input_sha256"], "work_journal_entry.input_sha256")
    expected_hash = canonical_sha256(_entry_payload(row))
    if _sha(row["entry_sha256"], "work_journal_entry.entry_sha256") != expected_hash:
        raise D2LStageWorkJournalError("work journal entry hash drift")
    row["input_sha256"] = input_sha
    row["result"] = result
    row["result_sha256"] = result_sha
    row["previous_entry_sha256"] = previous
    row["entry_sha256"] = expected_hash
    return row


def read_work_journal(
    path: str | Path,
    *,
    allow_incomplete_tail: bool = False,
) -> list[dict[str, Any]]:
    journal_path = Path(path)
    if not journal_path.is_file():
        return []
    lines = journal_path.read_bytes().splitlines(keepends=True)
    rows: list[dict[str, Any]] = []
    previous: str | None = None
    for index, line in enumerate(lines, start=1):
        unterminated = index == len(lines) and not line.endswith((b"\n", b"\r"))
        if unterminated:
            if allow_incomplete_tail:
                break
            raise D2LStageWorkJournalError(
                "work journal has an unterminated final row"
            )
        if not line.strip():
            raise D2LStageWorkJournalError("work journal contains a blank row")
        try:
            decoded = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise D2LStageWorkJournalError(
                f"work journal row {index} is invalid"
            ) from exc
        row = validate_work_journal_entry(
            decoded,
            expected_seq=index,
            previous_entry_sha256=previous,
        )
        rows.append(row)
        previous = row["entry_sha256"]
    return rows


def work_journal_state(entries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "entry_count": len(entries),
        "last_entry_sha256": (
            None if not entries else str(entries[-1]["entry_sha256"]).upper()
        ),
    }


class D2LStageWorkJournal:
    """Append accepted work once and reuse it across component attempts."""

    def __init__(
        self,
        *,
        path: str | Path,
        workflow_run_id: str,
        component_run_id: str,
        component_attempt_id: int,
        stage_id: str,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.workflow_run_id = _string(workflow_run_id, "workflow_run_id")
        self.component_run_id = _string(component_run_id, "component_run_id")
        self.component_attempt_id = _integer(
            component_attempt_id, "component_attempt_id", minimum=1
        )
        self.stage_id = _string(stage_id, "stage_id")
        self.entries = read_work_journal(self.path)
        for row in self.entries:
            if (
                row["workflow_run_id"] != self.workflow_run_id
                or row["component_run_id"] != self.component_run_id
                or row["stage_id"] != self.stage_id
            ):
                raise D2LStageWorkJournalError(
                    "work journal contains a foreign run or stage"
                )
        state = work_journal_state(self.entries)
        self.next_seq = int(state["entry_count"]) + 1
        self.previous_entry_sha256 = state["last_entry_sha256"]

    def lookup(
        self,
        *,
        work_item_id: str,
        work_contract_id: str,
        input_sha256: str,
    ) -> dict[str, Any] | None:
        item_id = _string(work_item_id, "work_item_id")
        contract_id = _string(work_contract_id, "work_contract_id")
        input_digest = _sha(input_sha256, "input_sha256")
        item_matches = [
            row
            for row in self.entries
            if row["work_item_id"] == item_id
        ]
        matches = [
            row
            for row in item_matches
            if row["work_contract_id"] == contract_id
            and row["input_sha256"] == input_digest
        ]
        if item_matches and not matches:
            raise D2LStageWorkJournalError(
                "work item input or semantic contract drift"
            )
        if len(matches) > 1:
            raise D2LStageWorkJournalError(
                "work journal contains duplicate compatible results"
            )
        return None if not matches else dict(matches[0]["result"])

    def append(
        self,
        *,
        work_item_id: str,
        work_contract_id: str,
        input_sha256: str,
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        item_id = _string(work_item_id, "work_item_id")
        contract_id = _string(work_contract_id, "work_contract_id")
        input_digest = _sha(input_sha256, "input_sha256")
        result_row = _mapping(result, "result")
        existing = self.lookup(
            work_item_id=item_id,
            work_contract_id=contract_id,
            input_sha256=input_digest,
        )
        if existing is not None:
            if canonical_sha256(existing) != canonical_sha256(result_row):
                raise D2LStageWorkJournalError(
                    "compatible work item already has a different result"
                )
            return next(
                row
                for row in self.entries
                if row["work_item_id"] == item_id
                and row["work_contract_id"] == contract_id
                and row["input_sha256"] == input_digest
            )
        row = {
            "schema_version": SCHEMA_VERSION,
            "workflow_run_id": self.workflow_run_id,
            "component_run_id": self.component_run_id,
            "component_attempt_id": self.component_attempt_id,
            "stage_id": self.stage_id,
            "work_item_id": item_id,
            "work_contract_id": contract_id,
            "input_sha256": input_digest,
            "result": result_row,
            "result_sha256": canonical_sha256(result_row),
            "journal_seq": self.next_seq,
            "previous_entry_sha256": self.previous_entry_sha256,
        }
        row["entry_sha256"] = canonical_sha256(row)
        normalized = validate_work_journal_entry(
            row,
            expected_seq=self.next_seq,
            previous_entry_sha256=self.previous_entry_sha256,
        )
        encoded = (
            json.dumps(
                normalized,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        descriptor = os.open(
            self.path,
            os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_BINARY", 0),
        )
        try:
            offset = 0
            while offset < len(encoded):
                written = os.write(descriptor, encoded[offset:])
                if written <= 0:
                    raise D2LStageWorkJournalError(
                        "work journal write did not advance"
                    )
                offset += written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self.entries.append(normalized)
        self.next_seq += 1
        self.previous_entry_sha256 = normalized["entry_sha256"]
        return normalized


__all__ = [
    "D2LStageWorkJournal",
    "D2LStageWorkJournalError",
    "SCHEMA_VERSION",
    "read_work_journal",
    "validate_work_journal_entry",
    "work_journal_state",
]
