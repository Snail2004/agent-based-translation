"""Validated child-stage observations for the D2L component replay stream."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from pipeline.prepass.d2l_console_replay_contract_v1 import (
    COMPONENT_EVENT_SCHEMA,
    COMPONENT_ID,
    FLOW_KIND,
    canonical_sha256,
    validate_component_event,
    validate_component_manifest,
)


STAGE_RECEIPT_SCHEMA = "d2l_component_stage_receipt_v1"
OBSERVATION_JOURNAL_SCHEMA = "d2l_component_observation_journal_v1"
ALLOWED_OBSERVATIONS = {
    "work_started",
    "work_progress",
    "request_sent",
    "response_received",
    "validation_passed",
    "validation_failed",
    "retry",
    "cost_snapshot",
    "usage_snapshot",
}
_FORBIDDEN_KEYS = {
    "raw_prompt",
    "raw_response",
    "prompt_text",
    "response_text",
    "api_key",
    "secret",
    "gold",
    "oracle",
    "reference_text",
}


class D2LStageReceiptError(ValueError):
    """Raised when a child receipt cannot safely enter the event stream."""


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise D2LStageReceiptError(f"{label} must be an object")
    return dict(value)


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise D2LStageReceiptError(f"{label} must be a non-empty string")
    return value


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise D2LStageReceiptError(f"{label} must be an integer >= {minimum}")
    return value


def _reject_forbidden(value: Any, label: str = "stage_receipt") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).lower() in _FORBIDDEN_KEYS:
                raise D2LStageReceiptError(f"{label} contains forbidden key: {key}")
            _reject_forbidden(child, f"{label}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_forbidden(child, f"{label}[{index}]")


def _receipt_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(value)
    payload.pop("receipt_sha256", None)
    return payload


def _journal_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(value)
    payload.pop("entry_sha256", None)
    return payload


def validate_observation_journal_entry(
    value: Mapping[str, Any],
    *,
    expected_seq: int,
    previous_entry_sha256: str | None,
) -> dict[str, Any]:
    row = _mapping(value, "observation_journal_entry")
    expected_keys = {
        "schema",
        "workflow_run_id",
        "component_run_id",
        "component_attempt_id",
        "stage_id",
        "producer",
        "work_id",
        "journal_seq",
        "previous_entry_sha256",
        "observation",
        "entry_sha256",
    }
    if set(row) != expected_keys:
        raise D2LStageReceiptError("observation journal entry keys mismatch")
    _reject_forbidden(row, "observation_journal_entry")
    if row["schema"] != OBSERVATION_JOURNAL_SCHEMA:
        raise D2LStageReceiptError("observation journal schema is invalid")
    for key in (
        "workflow_run_id",
        "component_run_id",
        "stage_id",
        "producer",
        "work_id",
    ):
        _string(row[key], f"observation_journal_entry.{key}")
    _integer(
        row["component_attempt_id"],
        "observation_journal_entry.component_attempt_id",
        minimum=1,
    )
    if _integer(row["journal_seq"], "observation_journal_entry.journal_seq", minimum=1) != expected_seq:
        raise D2LStageReceiptError("observation journal sequence is not contiguous")
    observed_previous = row["previous_entry_sha256"]
    if observed_previous is not None:
        observed_previous = _string(
            observed_previous,
            "observation_journal_entry.previous_entry_sha256",
        ).upper()
    if observed_previous != previous_entry_sha256:
        raise D2LStageReceiptError("observation journal previous hash mismatch")
    observation = _mapping(row["observation"], "observation_journal_entry.observation")
    if set(observation) != {"event", "agent", "severity", "ts", "payload"}:
        raise D2LStageReceiptError("observation journal payload keys mismatch")
    if observation["event"] not in ALLOWED_OBSERVATIONS:
        raise D2LStageReceiptError(
            f"observation journal event is not allowed: {observation['event']}"
        )
    _string(observation["agent"], "observation_journal_entry.observation.agent")
    _string(observation["severity"], "observation_journal_entry.observation.severity")
    _string(observation["ts"], "observation_journal_entry.observation.ts")
    _mapping(observation["payload"], "observation_journal_entry.observation.payload")
    expected_hash = canonical_sha256(_journal_payload(row))
    if _string(row["entry_sha256"], "observation_journal_entry.entry_sha256").upper() != expected_hash:
        raise D2LStageReceiptError("observation journal entry hash drift")
    row["previous_entry_sha256"] = observed_previous
    row["observation"] = observation
    row["entry_sha256"] = expected_hash
    return row


def read_observation_journal(
    path: str | Path,
    *,
    allow_incomplete_tail: bool = False,
) -> list[dict[str, Any]]:
    journal_path = Path(path)
    if not journal_path.is_file():
        return []
    raw = journal_path.read_bytes()
    lines = raw.splitlines(keepends=True)
    rows: list[dict[str, Any]] = []
    previous_sha: str | None = None
    for index, line in enumerate(lines, start=1):
        is_unterminated_tail = index == len(lines) and not line.endswith(
            (b"\n", b"\r")
        )
        if is_unterminated_tail:
            if allow_incomplete_tail:
                break
            raise D2LStageReceiptError(
                "observation journal has an unterminated final row"
            )
        if not line.strip():
            raise D2LStageReceiptError("observation journal contains a blank row")
        try:
            decoded = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise D2LStageReceiptError(
                f"observation journal row {index} is invalid"
            ) from exc
        row = validate_observation_journal_entry(
            decoded,
            expected_seq=index,
            previous_entry_sha256=previous_sha,
        )
        rows.append(row)
        previous_sha = row["entry_sha256"]
    return rows


def observation_journal_state(
    entries: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "entry_count": len(entries),
        "last_entry_sha256": (
            None if not entries else str(entries[-1]["entry_sha256"]).upper()
        ),
    }


class D2LStageObservationJournalWriter:
    """Append complete, hash-chained child observations for live replay."""

    def __init__(
        self,
        *,
        path: str | Path,
        workflow_run_id: str,
        component_run_id: str,
        component_attempt_id: int,
        stage_id: str,
        producer: str,
        work_id: str,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.workflow_run_id = _string(workflow_run_id, "workflow_run_id")
        self.component_run_id = _string(component_run_id, "component_run_id")
        self.component_attempt_id = _integer(
            component_attempt_id, "component_attempt_id", minimum=1
        )
        self.stage_id = _string(stage_id, "stage_id")
        self.producer = _string(producer, "producer")
        self.work_id = _string(work_id, "work_id")
        entries = read_observation_journal(self.path)
        state = observation_journal_state(entries)
        self.next_seq = int(state["entry_count"]) + 1
        self.previous_entry_sha256 = state["last_entry_sha256"]

    def append(self, observation: Mapping[str, Any]) -> dict[str, Any]:
        row = {
            "schema": OBSERVATION_JOURNAL_SCHEMA,
            "workflow_run_id": self.workflow_run_id,
            "component_run_id": self.component_run_id,
            "component_attempt_id": self.component_attempt_id,
            "stage_id": self.stage_id,
            "producer": self.producer,
            "work_id": self.work_id,
            "journal_seq": self.next_seq,
            "previous_entry_sha256": self.previous_entry_sha256,
            "observation": dict(observation),
        }
        row["entry_sha256"] = canonical_sha256(row)
        normalized = validate_observation_journal_entry(
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
                    raise D2LStageReceiptError(
                        "observation journal write did not advance"
                    )
                offset += written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self.next_seq += 1
        self.previous_entry_sha256 = normalized["entry_sha256"]
        return normalized


def build_stage_receipt(
    *,
    workflow_run_id: str,
    component_run_id: str,
    component_attempt_id: int,
    stage_id: str,
    producer: str,
    work_id: str,
    observations: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    receipt = {
        "schema": STAGE_RECEIPT_SCHEMA,
        "workflow_run_id": workflow_run_id,
        "component_run_id": component_run_id,
        "component_attempt_id": component_attempt_id,
        "stage_id": stage_id,
        "producer": producer,
        "work_id": work_id,
        "observations": [dict(row) for row in observations],
    }
    receipt["receipt_sha256"] = canonical_sha256(receipt)
    return receipt


def validate_stage_receipt(
    value: Mapping[str, Any],
    *,
    manifest: Mapping[str, Any],
    stage_id: str,
    producer: str,
    work_id: str,
    start_component_seq: int,
) -> dict[str, Any]:
    row = _mapping(value, "stage_receipt")
    expected_keys = {
        "schema",
        "workflow_run_id",
        "component_run_id",
        "component_attempt_id",
        "stage_id",
        "producer",
        "work_id",
        "observations",
        "receipt_sha256",
    }
    if set(row) != expected_keys:
        raise D2LStageReceiptError("stage_receipt keys mismatch")
    _reject_forbidden(row)
    if row["schema"] != STAGE_RECEIPT_SCHEMA:
        raise D2LStageReceiptError("stage_receipt.schema is invalid")
    manifest_row = validate_component_manifest(manifest)
    for key in ("workflow_run_id", "component_run_id"):
        if row[key] != manifest_row[key]:
            raise D2LStageReceiptError(f"stage_receipt.{key} does not match manifest")
    if _integer(row["component_attempt_id"], "component_attempt_id", minimum=1) != manifest_row[
        "component_attempt_id"
    ]:
        raise D2LStageReceiptError("stage receipt attempt does not match manifest")
    if row["stage_id"] != stage_id:
        raise D2LStageReceiptError("stage receipt stage_id mismatch")
    if row["producer"] != producer:
        raise D2LStageReceiptError("stage receipt producer mismatch")
    if row["work_id"] != work_id:
        raise D2LStageReceiptError("stage receipt work_id mismatch")
    _string(row["stage_id"], "stage_id")
    _string(row["producer"], "producer")
    _string(row["work_id"], "work_id")
    _integer(start_component_seq, "start_component_seq")
    observations = row["observations"]
    if not isinstance(observations, list):
        raise D2LStageReceiptError("stage_receipt.observations must be an array")
    if canonical_sha256(_receipt_payload(row)) != _string(
        row["receipt_sha256"], "receipt_sha256"
    ).upper():
        raise D2LStageReceiptError("stage receipt hash drift")

    sent: set[tuple[str, int]] = set()
    received: set[tuple[str, int]] = set()
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(observations):
        observation = _mapping(raw, f"observations[{index}]")
        if set(observation) != {"event", "agent", "severity", "ts", "payload"}:
            raise D2LStageReceiptError(f"observations[{index}] keys mismatch")
        event_name = _string(observation["event"], f"observations[{index}].event")
        if event_name not in ALLOWED_OBSERVATIONS:
            raise D2LStageReceiptError(f"observation event is not allowed: {event_name}")
        agent = _string(observation["agent"], f"observations[{index}].agent")
        severity = _string(observation["severity"], f"observations[{index}].severity")
        payload = _mapping(observation["payload"], f"observations[{index}].payload")
        component_seq = start_component_seq + index + 1
        prospective = {
            "schema": COMPONENT_EVENT_SCHEMA,
            "event_id": f"evt_{manifest_row['component_run_id']}_{component_seq:08d}",
            "workflow_run_id": manifest_row["workflow_run_id"],
            "flow_kind": FLOW_KIND,
            "component_id": COMPONENT_ID,
            "component_run_id": manifest_row["component_run_id"],
            "component_attempt_id": manifest_row["component_attempt_id"],
            "component_seq": component_seq,
            "ts": observation["ts"],
            "stage_id": (
                None
                if event_name in {"cost_snapshot", "usage_snapshot"}
                else stage_id
            ),
            "agent": agent,
            "event": event_name,
            "severity": severity,
            "payload": payload,
        }
        validate_component_event(
            prospective,
            manifest=manifest_row,
            expected_component_seq=component_seq,
        )
        if event_name == "request_sent":
            key = (str(payload["logical_request_id"]), int(payload["physical_attempt_index"]))
            if key in sent:
                raise D2LStageReceiptError("duplicate request_sent identity")
            sent.add(key)
        elif event_name == "response_received":
            usage = _mapping(payload["usage"], "response_received.usage")
            key = (str(usage["logical_request_id"]), int(usage["physical_attempt_index"]))
            if key not in sent:
                raise D2LStageReceiptError("response_received has no preceding request_sent")
            if key in received:
                raise D2LStageReceiptError("duplicate response_received identity")
            received.add(key)
        elif event_name == "retry":
            logical_request_id = str(payload["logical_request_id"])
            if logical_request_id not in {item[0] for item in sent}:
                raise D2LStageReceiptError("retry has no preceding request_sent")
        normalized.append(observation)
    row["observations"] = normalized
    row["receipt_sha256"] = row["receipt_sha256"].upper()
    return row


def validate_stage_receipt_against_journal(
    receipt: Mapping[str, Any],
    *,
    journal_entries: Sequence[Mapping[str, Any]],
) -> None:
    matching = [
        dict(entry["observation"])
        for entry in journal_entries
        if entry["workflow_run_id"] == receipt["workflow_run_id"]
        and entry["component_run_id"] == receipt["component_run_id"]
        and entry["component_attempt_id"] == receipt["component_attempt_id"]
        and entry["stage_id"] == receipt["stage_id"]
        and entry["producer"] == receipt["producer"]
        and entry["work_id"] == receipt["work_id"]
    ]
    if matching != list(receipt["observations"]):
        raise D2LStageReceiptError(
            "stage receipt observations do not exactly match the durable journal"
        )


__all__ = [
    "ALLOWED_OBSERVATIONS",
    "D2LStageReceiptError",
    "D2LStageObservationJournalWriter",
    "OBSERVATION_JOURNAL_SCHEMA",
    "STAGE_RECEIPT_SCHEMA",
    "build_stage_receipt",
    "observation_journal_state",
    "read_observation_journal",
    "validate_observation_journal_entry",
    "validate_stage_receipt",
    "validate_stage_receipt_against_journal",
]
