"""Single-attempt shared LLM backend. It never retries or selects a fallback."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Callable, Mapping

from .cache_v1 import ApplicationResponseCache
from .contracts_v1 import ContractValidationError, canonical_sha256
from .credentials_v1 import CredentialProvider, resolve_source_credential
from .ledger_v1 import SharedLlmAttemptLedger
from .resolver_v1 import (
    create_reusable_artifact_receipt,
    derive_cache_key_sha256,
    derive_llm_attempt_identity,
    usage_limits_are_certifiable,
    validate_resolved_llm_run_seal,
)
from .scheduler_v1 import PhysicalQuotaScheduler
from .transport_v1 import (
    TransportCallError,
    TransportSender,
    normalize_provider_response,
    prepare_transport_request,
    validate_transport_request_body,
)


class UncertifiedAttemptError(RuntimeError):
    """Raised after honest evidence persistence when finite limits cannot be certified."""


class SharedLlmBackend:
    def __init__(
        self,
        *,
        credential_provider: CredentialProvider,
        scheduler: PhysicalQuotaScheduler,
        ledger: SharedLlmAttemptLedger,
        response_cache: ApplicationResponseCache,
        sender: TransportSender,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.credential_provider = credential_provider
        self.scheduler = scheduler
        self.ledger = ledger
        self.response_cache = response_cache
        self.sender = sender
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def execute_one_attempt(
        self,
        *,
        seal: Mapping[str, Any],
        logical_request_id: str,
        semantic_attempt_index: int,
        transport_retry_ordinal: int,
        request_body: Mapping[str, Any],
        target_index: int = 0,
        allow_response_cache_read: bool = True,
        allow_response_cache_write: bool = True,
        cost_fact: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute zero calls on a trusted cache hit, otherwise exactly one call."""

        normalized = validate_resolved_llm_run_seal(seal)
        targets = [normalized["primary"], *normalized["fallback_plan"]["steps"]]
        if target_index < 0 or target_index >= len(targets):
            raise ContractValidationError("target_index is outside the sealed target plan")
        if target_index > 0 and not normalized["fallback_plan"]["enabled"]:
            raise ContractValidationError("fallback target is not enabled")
        target = targets[target_index]
        role = normalized["role_binding"]["record"]
        lineage = derive_llm_attempt_identity(
            seal=normalized,
            logical_request_id=logical_request_id,
            semantic_attempt_index=semantic_attempt_index,
            transport_retry_ordinal=transport_retry_ordinal,
        )
        validate_transport_request_body(
            seal=normalized,
            request_body=request_body,
            target_index=target_index,
        )

        if allow_response_cache_read and semantic_attempt_index == 1 and transport_retry_ordinal == 0:
            hit = self.response_cache.lookup(
                consumer_seal=normalized, logical_request_id=logical_request_id
            )
            if hit is not None:
                return self._record_cache_hit(
                    seal=normalized,
                    lineage=lineage,
                    hit=hit,
                )

        observed_cost = _cost_fact(cost_fact)
        lease_started = self.clock()
        lease_id = canonical_sha256(
            {
                "seal_sha256": normalized["seal_sha256"],
                "attempt_usage_id": lineage["attempt_usage_id"],
            }
        )
        with self.scheduler.acquire(
            physical_quota_bucket_id=target["source"][
                "physical_quota_bucket_id"
            ],
            lease_id=lease_id,
            owner_id=normalized["attempt_run_id"],
            acquired_at_utc=_timestamp(lease_started),
        ):
            if (
                allow_response_cache_read
                and semantic_attempt_index == 1
                and transport_retry_ordinal == 0
            ):
                hit = self.response_cache.lookup(
                    consumer_seal=normalized,
                    logical_request_id=logical_request_id,
                )
                if hit is not None:
                    return self._record_cache_hit(
                        seal=normalized,
                        lineage=lineage,
                        hit=hit,
                    )

            if (
                self.ledger.get_record("usage", lineage["attempt_usage_id"])
                is not None
            ):
                raise ContractValidationError(
                    "sealed physical attempt already exists; use its trusted "
                    "artifact or advance the sealed retry lineage"
                )
            existing_usage = [
                row
                for row in self.ledger.list_records("usage")
                if row["seal_sha256"] == normalized["seal_sha256"]
            ]
            if len(existing_usage) >= role["limits"]["max_calls"]:
                raise ContractValidationError(
                    "sealed physical call cap is already exhausted"
                )
            credential = resolve_source_credential(
                source=target["source"], provider=self.credential_provider
            )
            request = prepare_transport_request(
                seal=normalized,
                request_body=request_body,
                credential=credential,
                timeout_seconds=role["limits"]["request_timeout_ms"] / 1000.0,
                target_index=target_index,
            )
            physical_attempt_index = 1 + len(existing_usage)
            started = self.clock()
            try:
                raw_response = self.sender.send(request)
                facts = normalize_provider_response(
                    request=request, response=raw_response
                )
            except TransportCallError as exc:
                finished = self.clock()
                usage, error = self._failed_attempt_records(
                    seal=normalized,
                    lineage=lineage,
                    target=target,
                    physical_attempt_index=physical_attempt_index,
                    started=started,
                    finished=finished,
                    error=exc,
                )
                self.ledger.append_bundle(
                    seal=normalized,
                    usage_rows=[usage],
                    error_rows=[error],
                    certify_limits=False,
                )
                raise

            finished = self.clock()
            usage = _success_usage(
                seal=normalized,
                lineage=lineage,
                target=target,
                physical_attempt_index=physical_attempt_index,
                started=started,
                finished=finished,
                facts=facts,
                cost_fact=observed_cost,
            )
            limits_certifiable = usage_limits_are_certifiable(
                usage_rows=[*existing_usage, usage],
                role=role,
            )
            artifact_sha256 = self.response_cache.artifact_store.put_bytes(
                raw_response.body
            )
            receipt = create_reusable_artifact_receipt(
                producer_seal=normalized,
                logical_request_id=logical_request_id,
                artifact_kind="application_response",
                artifact_sha256=artifact_sha256,
                created_at_utc=_timestamp(finished),
            )
            cache_observations = []
            if allow_response_cache_read:
                cache_observations.append(
                    _cache_observation(
                        seal=normalized,
                        lineage=lineage,
                        lookup_status="miss",
                        attempt_usage_id=usage["attempt_usage_id"],
                        artifact_sha256=None,
                        producer_seal_sha256=None,
                        producer_input_bindings_sha256=None,
                        receipt_sha256=None,
                        observed_at_utc=_timestamp(finished),
                    )
                )
            self.ledger.append_bundle(
                seal=normalized,
                usage_rows=[usage],
                cache_observations=cache_observations,
                reusable_artifact_receipts=[receipt],
                certify_limits=limits_certifiable,
            )
            if not limits_certifiable:
                raise UncertifiedAttemptError(
                    "attempt evidence was persisted, but finite token/cost "
                    "limits cannot be certified as satisfied"
                )
            if allow_response_cache_write:
                stored = self.response_cache.store(
                    producer_seal=normalized,
                    logical_request_id=logical_request_id,
                    response_bytes=raw_response.body,
                    created_at_utc=_timestamp(finished),
                )
                if (
                    stored.artifact_sha256 != artifact_sha256
                    or stored.receipt != receipt
                ):
                    raise ContractValidationError(
                        "published cache record differs from persisted artifact receipt"
                    )
            return {
                "status": "provider_succeeded",
                "provider_called": True,
                "response_bytes": raw_response.body,
                "artifact_sha256": artifact_sha256,
                "receipt": receipt,
                "usage": usage,
                "cache_observation": cache_observations[0]
                if cache_observations
                else None,
            }

    def _record_cache_hit(
        self,
        *,
        seal: Mapping[str, Any],
        lineage: Mapping[str, Any],
        hit,
    ) -> dict[str, Any]:
        self._require_trusted_cache_hit(hit)
        observation = _cache_observation(
            seal=seal,
            lineage=lineage,
            lookup_status="hit",
            attempt_usage_id=None,
            artifact_sha256=hit.artifact_sha256,
            producer_seal_sha256=hit.producer_seal["seal_sha256"],
            producer_input_bindings_sha256=hit.producer_seal[
                "input_bindings_sha256"
            ],
            receipt_sha256=hit.receipt["receipt_sha256"],
            observed_at_utc=_timestamp(self.clock()),
        )
        self.ledger.append_bundle(
            seal=seal,
            cache_observations=[observation],
            producer_seals=[hit.producer_seal],
            reusable_artifact_receipts=[hit.receipt],
        )
        return {
            "status": "cache_hit",
            "provider_called": False,
            "response_bytes": hit.artifact_bytes,
            "artifact_sha256": hit.artifact_sha256,
            "cache_observation": observation,
            "usage": None,
        }

    def _require_trusted_cache_hit(self, hit) -> None:
        persisted_seal = self.ledger.get_record(
            "seal", hit.producer_seal["seal_sha256"]
        )
        persisted_receipt = self.ledger.get_record(
            "artifact_receipt", hit.receipt["receipt_sha256"]
        )
        if persisted_seal != hit.producer_seal or persisted_receipt != hit.receipt:
            raise ContractValidationError(
                "cache index lacks a trusted producer seal and artifact receipt"
            )

    @staticmethod
    def _failed_attempt_records(
        *,
        seal: Mapping[str, Any],
        lineage: Mapping[str, Any],
        target: Mapping[str, Any],
        physical_attempt_index: int,
        started: datetime,
        finished: datetime,
        error: TransportCallError,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        retry_class = _transport_retry_class(error)
        policy = seal["role_binding"]["record"]["transport_retry"]
        retry_allowed = (
            retry_class in policy["retryable_codes"]
            and lineage["transport_retry_ordinal"] < policy["max_retries"]
        )
        error_id = canonical_sha256(
            {
                "seal_sha256": seal["seal_sha256"],
                "attempt_usage_id": lineage["attempt_usage_id"],
                "code": error.code,
            }
        )
        usage = {
            "schema_version": "llm_attempt_usage_v1",
            **lineage,
            "seal_sha256": seal["seal_sha256"],
            "physical_attempt_index": physical_attempt_index,
            "request_id": None,
            "source_id": target["source"]["source_id"],
            "source_revision": target["source"]["source_revision"],
            "physical_quota_bucket_id": target["source"][
                "physical_quota_bucket_id"
            ],
            "requested_model_id": target["target"]["requested_model_id"],
            "observed_model_id": None,
            "started_at_utc": _timestamp(started),
            "finished_at_utc": _timestamp(finished),
            "latency_ms": _latency_ms(started, finished),
            "outcome": "failed_after_request",
            "finish_reason": "error",
            "prompt_tokens": None,
            "cached_input_tokens": None,
            "completion_tokens": None,
            "reasoning_tokens": None,
            "total_tokens": None,
            "cost_usd": None,
            "cost_status": "unknown",
            "cost_provenance": {
                "kind": "unavailable",
                "reference_id": None,
                "reference_sha256": None,
            },
            "provider_usage_sha256": None,
            "error_id": error_id,
        }
        error_row = {
            "schema_version": "llm_error_v1",
            "error_id": error_id,
            "seal_sha256": seal["seal_sha256"],
            "attempt_usage_id": lineage["attempt_usage_id"],
            "category": "transport",
            "code": error.code,
            "retry_class": retry_class,
            "safe_message": error.safe_message,
            "details_sha256": canonical_sha256(
                {"code": error.code, "status_code": error.status_code}
            ),
            "source_health_effect": "temporary_unavailable"
            if retry_class in {
                "connection",
                "timeout",
                "rate_limit",
                "server_unavailable",
            }
            else "none",
            "retry_disposition": "transport_retry_allowed"
            if retry_allowed
            else "do_not_retry",
            "occurred_at_utc": _timestamp(finished),
        }
        return usage, error_row


def _success_usage(
    *,
    seal: Mapping[str, Any],
    lineage: Mapping[str, Any],
    target: Mapping[str, Any],
    physical_attempt_index: int,
    started: datetime,
    finished: datetime,
    facts: Mapping[str, Any],
    cost_fact: Mapping[str, Any],
) -> dict[str, Any]:
    provider_usage_sha256 = canonical_sha256(
        {
            key: facts[key]
            for key in (
                "prompt_tokens",
                "cached_input_tokens",
                "completion_tokens",
                "reasoning_tokens",
                "total_tokens",
                "raw_response_sha256",
            )
        }
    )
    return {
        "schema_version": "llm_attempt_usage_v1",
        **lineage,
        "seal_sha256": seal["seal_sha256"],
        "physical_attempt_index": physical_attempt_index,
        "request_id": facts["request_id"],
        "source_id": target["source"]["source_id"],
        "source_revision": target["source"]["source_revision"],
        "physical_quota_bucket_id": target["source"][
            "physical_quota_bucket_id"
        ],
        "requested_model_id": target["target"]["requested_model_id"],
        "observed_model_id": facts["observed_model_id"],
        "started_at_utc": _timestamp(started),
        "finished_at_utc": _timestamp(finished),
        "latency_ms": _latency_ms(started, finished),
        "outcome": "succeeded",
        "finish_reason": facts["finish_reason"],
        "prompt_tokens": facts["prompt_tokens"],
        "cached_input_tokens": facts["cached_input_tokens"],
        "completion_tokens": facts["completion_tokens"],
        "reasoning_tokens": facts["reasoning_tokens"],
        "total_tokens": facts["total_tokens"],
        "cost_usd": cost_fact["cost_usd"],
        "cost_status": cost_fact["cost_status"],
        "cost_provenance": cost_fact["cost_provenance"],
        "provider_usage_sha256": provider_usage_sha256,
        "error_id": None,
    }


def _cache_observation(
    *,
    seal: Mapping[str, Any],
    lineage: Mapping[str, Any],
    lookup_status: str,
    attempt_usage_id: str | None,
    artifact_sha256: str | None,
    producer_seal_sha256: str | None,
    producer_input_bindings_sha256: str | None,
    receipt_sha256: str | None,
    observed_at_utc: str,
) -> dict[str, Any]:
    body = {
        "seal_sha256": seal["seal_sha256"],
        "logical_request_sha256": lineage["logical_request_sha256"],
        "cache_kind": "application_response_cache",
        "lookup_status": lookup_status,
        "observed_at_utc": observed_at_utc,
    }
    return {
        "schema_version": "cache_observation_v1",
        "observation_id": canonical_sha256(body),
        "seal_sha256": seal["seal_sha256"],
        "logical_request_id": lineage["logical_request_id"],
        "logical_request_sha256": lineage["logical_request_sha256"],
        "attempt_usage_id": attempt_usage_id,
        "cache_kind": "application_response_cache",
        "cache_namespace": seal["cache_namespace"],
        "cache_key_sha256": derive_cache_key_sha256(
            seal=seal,
            logical_request_id=lineage["logical_request_id"],
            cache_kind="application_response_cache",
        ),
        "lookup_status": lookup_status,
        "provider_call_avoided": lookup_status == "hit",
        "provider_cached_input_tokens": None,
        "reused_artifact_sha256": artifact_sha256,
        "producer_seal_sha256": producer_seal_sha256,
        "producer_input_bindings_sha256": producer_input_bindings_sha256,
        "producer_artifact_receipt_sha256": receipt_sha256,
        "observed_at_utc": observed_at_utc,
    }


def _cost_fact(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {
            "cost_usd": None,
            "cost_status": "unknown",
            "cost_provenance": {
                "kind": "unavailable",
                "reference_id": None,
                "reference_sha256": None,
            },
        }
    required = {"cost_usd", "cost_status", "cost_provenance"}
    if set(value) != required:
        raise ContractValidationError("cost_fact has missing or extra fields")
    return dict(value)


def _transport_retry_class(error: TransportCallError) -> str:
    if error.code in {"connection", "timeout"}:
        return error.code
    if error.status_code == 401:
        return "authentication"
    if error.status_code in {402, 403}:
        return "authorization"
    if error.status_code == 408:
        return "timeout"
    if error.status_code == 429:
        return "rate_limit"
    if error.status_code is not None and error.status_code >= 500:
        return "server_unavailable"
    return "invalid_request"


def _timestamp(value: datetime) -> str:
    normalized = _utc_datetime(value)
    return normalized.isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _latency_ms(started: datetime, finished: datetime) -> int:
    started_utc = _utc_datetime(started)
    finished_utc = _utc_datetime(finished)
    if finished_utc < started_utc:
        raise ContractValidationError("backend clock moved backwards")
    started_ms = started_utc.replace(
        microsecond=(started_utc.microsecond // 1_000) * 1_000
    )
    finished_ms = finished_utc.replace(
        microsecond=(finished_utc.microsecond // 1_000) * 1_000
    )
    return int((finished_ms - started_ms).total_seconds() * 1_000)


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ContractValidationError("backend clock returned a naive datetime")
    return value.astimezone(timezone.utc)
