"""Append-only SQLite evidence ledger for shared LLM execution records."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping, Sequence

from .capability_probe_contracts_v1 import (
    validate_capability_probe_bundle,
    validate_capability_probe_seal,
)
from .contracts_v1 import ContractValidationError, canonical_json, canonical_sha256
from .resolver_v1 import (
    validate_llm_run_records,
    validate_resolved_llm_run_seal,
)


LEDGER_SCHEMA_VERSION = "shared_llm_attempt_ledger_v1"


class SharedLlmAttemptLedger:
    """Persist immutable execution evidence without touching pipeline memory DBs."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def append_bundle(
        self,
        *,
        seal: Mapping[str, Any],
        usage_rows: Sequence[Mapping[str, Any]] = (),
        error_rows: Sequence[Mapping[str, Any]] = (),
        cache_observations: Sequence[Mapping[str, Any]] = (),
        producer_seals: Sequence[Mapping[str, Any]] = (),
        reusable_artifact_receipts: Sequence[Mapping[str, Any]] = (),
        certify_limits: bool = True,
    ) -> dict[str, Any]:
        current = validate_resolved_llm_run_seal(seal)
        persisted_producers = self.list_records("seal")
        persisted_receipts = self.list_records("artifact_receipt")
        seal_sha256 = current["seal_sha256"]
        combined_usage = _merge_records(
            [
                row
                for row in self.list_records("usage")
                if row["seal_sha256"] == seal_sha256
            ],
            usage_rows,
            "attempt_usage_id",
        )
        combined_errors = _merge_records(
            [
                row
                for row in self.list_records("error")
                if row["seal_sha256"] == seal_sha256
            ],
            error_rows,
            "error_id",
        )
        combined_cache = _merge_records(
            [
                row
                for row in self.list_records("cache")
                if row["seal_sha256"] == seal_sha256
            ],
            cache_observations,
            "observation_id",
        )
        combined_producers = _merge_records(
            persisted_producers, producer_seals, "seal_sha256"
        )
        combined_receipts = _merge_records(
            persisted_receipts,
            reusable_artifact_receipts,
            "receipt_sha256",
        )
        validated = validate_llm_run_records(
            seal=current,
            usage_rows=combined_usage,
            error_rows=combined_errors,
            cache_observations=combined_cache,
            producer_seals=combined_producers,
            reusable_artifact_receipts=combined_receipts,
            certify_limits=certify_limits,
        )
        records: list[tuple[str, str, Mapping[str, Any]]] = [
            ("seal", current["seal_sha256"], current)
        ]
        records.extend(
            ("seal", row["seal_sha256"], row)
            for row in validated["producer_seals"]
        )
        records.extend(
            ("artifact_receipt", row["receipt_sha256"], row)
            for row in validated["reusable_artifact_receipts"]
        )
        records.extend(
            ("usage", row["attempt_usage_id"], row)
            for row in validated["usage_rows"]
        )
        records.extend(
            ("error", row["error_id"], row) for row in validated["error_rows"]
        )
        records.extend(
            ("cache", row["observation_id"], row)
            for row in validated["cache_observations"]
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for kind, record_id, row in records:
                self._insert_immutable(connection, kind, record_id, row)
            connection.commit()
        return validated

    def reserve_capability_probe(self, seal: Mapping[str, Any]) -> dict[str, Any]:
        """Persist probe authority before transport so a crash cannot permit replay."""

        normalized = validate_capability_probe_seal(seal)
        rendered = canonical_json(normalized)
        intent = normalized["capability_intent"]
        source = normalized["source_binding"]["record"]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT record_json FROM records WHERE kind = ?",
                ("capability_probe_seal",),
            ).fetchall()
            for (record_json,) in existing:
                prior = json.loads(record_json)
                if prior["probe_run_id"] == normalized["probe_run_id"]:
                    if canonical_json(prior) == rendered:
                        raise ContractValidationError(
                            "capability probe is already reserved and may not call again"
                        )
                    raise ContractValidationError(
                        "probe_run_id is already bound to different sealed bytes"
                    )
            prior_evidence = connection.execute(
                "SELECT record_json FROM records WHERE kind = ?",
                ("capability_evidence",),
            ).fetchall()
            for (record_json,) in prior_evidence:
                evidence = json.loads(record_json)
                if all(
                    evidence[field] == expected
                    for field, expected in {
                        "capability_id": intent["capability_id"],
                        "capability_revision": intent["capability_revision"],
                        "source_id": source["source_id"],
                        "source_revision": source["source_revision"],
                    }.items()
                ):
                    raise ContractValidationError(
                        "capability revision already has terminal probe evidence"
                    )
            self._insert_immutable(
                connection,
                "capability_probe_seal",
                normalized["seal_sha256"],
                normalized,
            )
            connection.commit()
        return normalized

    def append_capability_probe_result(
        self,
        *,
        seal: Mapping[str, Any],
        receipt: Mapping[str, Any],
        capability_evidence: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Append the only terminal result allowed for a reserved probe seal."""

        validated = validate_capability_probe_bundle(
            seal=seal,
            receipt=receipt,
            capability_evidence=capability_evidence,
        )
        normalized_seal = validated["seal"]
        normalized_receipt = validated["receipt"]
        evidence = validated["capability_evidence"]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            persisted = connection.execute(
                "SELECT record_json FROM records WHERE kind = ? AND record_id = ?",
                ("capability_probe_seal", normalized_seal["seal_sha256"]),
            ).fetchone()
            if persisted is None or persisted[0] != canonical_json(normalized_seal):
                raise ContractValidationError(
                    "capability probe result lacks its exact reserved seal"
                )
            prior_receipts = connection.execute(
                "SELECT record_json FROM records WHERE kind = ?",
                ("capability_probe_receipt",),
            ).fetchall()
            if any(
                json.loads(record_json)["probe_seal_sha256"]
                == normalized_seal["seal_sha256"]
                for (record_json,) in prior_receipts
            ):
                raise ContractValidationError(
                    "capability probe already has a terminal receipt"
                )
            self._insert_immutable(
                connection,
                "capability_probe_receipt",
                normalized_receipt["receipt_sha256"],
                normalized_receipt,
            )
            self._insert_immutable(
                connection,
                "capability_evidence",
                canonical_sha256(evidence),
                evidence,
            )
            connection.commit()
        return validated

    def get_record(self, kind: str, record_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT record_json FROM records WHERE kind = ? AND record_id = ?",
                (kind, record_id),
            ).fetchone()
        return None if row is None else json.loads(row[0])

    def list_records(self, kind: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT record_json FROM records WHERE kind = ? ORDER BY record_id",
                (kind,),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def count(self, kind: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM records WHERE kind = ?", (kind,)
            ).fetchone()
        return int(row[0])

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS ledger_meta (
                    schema_version TEXT PRIMARY KEY
                );
                CREATE TABLE IF NOT EXISTS records (
                    kind TEXT NOT NULL,
                    record_id TEXT NOT NULL,
                    record_sha256 TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    PRIMARY KEY (kind, record_id)
                );
                """
            )
            existing = connection.execute(
                "SELECT schema_version FROM ledger_meta"
            ).fetchall()
            if existing and existing != [(LEDGER_SCHEMA_VERSION,)]:
                raise ContractValidationError("foreign shared LLM ledger schema")
            connection.execute(
                "INSERT OR IGNORE INTO ledger_meta(schema_version) VALUES (?)",
                (LEDGER_SCHEMA_VERSION,),
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @staticmethod
    def _insert_immutable(
        connection: sqlite3.Connection,
        kind: str,
        record_id: str,
        record: Mapping[str, Any],
    ) -> None:
        rendered = canonical_json(record)
        digest = canonical_sha256(record)
        connection.execute(
            """
            INSERT OR IGNORE INTO records(kind, record_id, record_sha256, record_json)
            VALUES (?, ?, ?, ?)
            """,
            (kind, record_id, digest, rendered),
        )
        observed = connection.execute(
            """
            SELECT record_sha256, record_json FROM records
            WHERE kind = ? AND record_id = ?
            """,
            (kind, record_id),
        ).fetchone()
        if observed != (digest, rendered):
            raise ContractValidationError(
                f"append-only ledger identity {kind}:{record_id} already has different bytes"
            )


def _merge_records(
    existing: Sequence[Mapping[str, Any]],
    incoming: Sequence[Mapping[str, Any]],
    identity_field: str,
) -> list[Mapping[str, Any]]:
    merged: dict[str, Mapping[str, Any]] = {}
    for row in [*existing, *incoming]:
        identity = row.get(identity_field)
        if not isinstance(identity, str):
            raise ContractValidationError(
                f"record lacks string identity field {identity_field}"
            )
        prior = merged.get(identity)
        if prior is not None and canonical_json(prior) != canonical_json(row):
            raise ContractValidationError(
                f"record identity {identity} has conflicting bytes"
            )
        merged[identity] = row
    return list(merged.values())
