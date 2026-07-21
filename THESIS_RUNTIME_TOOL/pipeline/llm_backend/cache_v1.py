"""Durable application-response cache backed by content-addressed artifacts."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping

from .artifact_store_v1 import ContentAddressedArtifactStore
from .contracts_v1 import ContractValidationError, canonical_json
from .resolver_v1 import (
    create_reusable_artifact_receipt,
    derive_cache_key_sha256,
    validate_resolved_llm_run_seal,
)


@dataclass(frozen=True)
class ApplicationResponseCacheHit:
    cache_key_sha256: str
    artifact_sha256: str
    artifact_bytes: bytes
    producer_seal: dict[str, Any]
    receipt: dict[str, Any]


class ApplicationResponseCache:
    def __init__(
        self,
        *,
        index_path: str | Path,
        artifact_store: ContentAddressedArtifactStore,
    ) -> None:
        self.index_path = Path(index_path).resolve()
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        self.artifact_store = artifact_store
        self._initialize()

    def store(
        self,
        *,
        producer_seal: Mapping[str, Any],
        logical_request_id: str,
        response_bytes: bytes,
        created_at_utc: str,
    ) -> ApplicationResponseCacheHit:
        seal = validate_resolved_llm_run_seal(producer_seal)
        artifact_sha256 = self.artifact_store.put_bytes(response_bytes)
        receipt = create_reusable_artifact_receipt(
            producer_seal=seal,
            logical_request_id=logical_request_id,
            artifact_kind="application_response",
            artifact_sha256=artifact_sha256,
            created_at_utc=created_at_utc,
        )
        cache_key = derive_cache_key_sha256(
            seal=seal,
            logical_request_id=logical_request_id,
            cache_kind="application_response_cache",
        )
        record = {
            "cache_key_sha256": cache_key,
            "cache_namespace": seal["cache_namespace"],
            "producer_seal": seal,
            "receipt": receipt,
            "artifact_sha256": artifact_sha256,
        }
        rendered = canonical_json(record)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT OR IGNORE INTO response_cache(cache_key_sha256, record_json)
                VALUES (?, ?)
                """,
                (cache_key, rendered),
            )
            observed = connection.execute(
                "SELECT record_json FROM response_cache WHERE cache_key_sha256 = ?",
                (cache_key,),
            ).fetchone()
            if observed != (rendered,):
                raise ContractValidationError(
                    "application response cache key already has different bytes"
                )
            connection.commit()
        return ApplicationResponseCacheHit(
            cache_key_sha256=cache_key,
            artifact_sha256=artifact_sha256,
            artifact_bytes=response_bytes,
            producer_seal=seal,
            receipt=receipt,
        )

    def lookup(
        self, *, consumer_seal: Mapping[str, Any], logical_request_id: str
    ) -> ApplicationResponseCacheHit | None:
        seal = validate_resolved_llm_run_seal(consumer_seal)
        cache_key = derive_cache_key_sha256(
            seal=seal,
            logical_request_id=logical_request_id,
            cache_kind="application_response_cache",
        )
        with self._connect() as connection:
            observed = connection.execute(
                "SELECT record_json FROM response_cache WHERE cache_key_sha256 = ?",
                (cache_key,),
            ).fetchone()
        if observed is None:
            return None
        record = json.loads(observed[0])
        artifact = self.artifact_store.get_bytes(record["artifact_sha256"])
        return ApplicationResponseCacheHit(
            cache_key_sha256=cache_key,
            artifact_sha256=record["artifact_sha256"],
            artifact_bytes=artifact,
            producer_seal=record["producer_seal"],
            receipt=record["receipt"],
        )

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS response_cache (
                    cache_key_sha256 TEXT PRIMARY KEY,
                    record_json TEXT NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.index_path, timeout=5.0)
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        return connection
