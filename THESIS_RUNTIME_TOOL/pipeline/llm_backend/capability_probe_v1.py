"""One-call bootstrap path for qualifying an untrusted LLM capability.

This executor never creates a normal run seal, response-cache entry, checkpoint
or pipeline payload. It performs one sealed transport call and publishes only a
terminal probe receipt plus exact CapabilityEvidenceV1.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from hashlib import sha256
from typing import Any, Callable, Mapping

from .artifact_store_v1 import ContentAddressedArtifactStore
from .capability_probe_contracts_v1 import (
    CAPABILITY_PROBE_RECEIPT_SCHEMA_VERSION,
    CAPABILITY_PROBE_SEAL_SCHEMA_VERSION,
    validate_capability_probe_bundle,
    validate_capability_probe_seal,
)
from .contracts_v1 import (
    ContractValidationError,
    canonical_json,
    canonical_sha256,
    validate_api_source,
)
from .credentials_v1 import CredentialProvider, resolve_source_credential
from .ledger_v1 import SharedLlmAttemptLedger
from .scheduler_v1 import PhysicalQuotaScheduler
from .transport_v1 import (
    RawTransportResponse,
    TransportCallError,
    TransportSender,
    normalize_provider_response,
    prepare_capability_probe_transport_request,
    validate_capability_probe_request_body,
)


class SharedLlmCapabilityProbe:
    """Execute at most one physical call for one reserved capability probe."""

    def __init__(
        self,
        *,
        credential_provider: CredentialProvider,
        scheduler: PhysicalQuotaScheduler,
        ledger: SharedLlmAttemptLedger,
        artifact_store: ContentAddressedArtifactStore,
        sender: TransportSender,
        implementation_binding: Mapping[str, Any],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.credential_provider = credential_provider
        self.scheduler = scheduler
        self.ledger = ledger
        self.artifact_store = artifact_store
        self.sender = sender
        self.implementation_binding = deepcopy(dict(implementation_binding))
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def execute_once(
        self,
        *,
        seal: Mapping[str, Any],
        request_body: Mapping[str, Any],
        local_validator: Callable[[Mapping[str, Any]], Any],
        local_validator_id: str,
        local_validator_sha256: str,
        cost_fact: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run one probe and return authority-free qualification evidence."""

        normalized = validate_capability_probe_seal(seal)
        if canonical_json(self.implementation_binding) != canonical_json(
            normalized["implementation_binding"]
        ):
            raise ContractValidationError(
                "runtime implementation identity differs from probe seal"
            )
        intent = normalized["capability_intent"]
        if (
            local_validator_id != intent["local_validator_id"]
            or local_validator_sha256 != intent["local_validator_sha256"]
        ):
            raise ContractValidationError(
                "runtime local validator identity differs from probe seal"
            )
        if not callable(local_validator):
            raise ContractValidationError("local_validator must be callable")
        validate_capability_probe_request_body(
            probe_seal=normalized, request_body=request_body
        )
        source = normalized["source_binding"]["record"]
        credential = resolve_source_credential(
            source=source, provider=self.credential_provider
        )
        request = prepare_capability_probe_transport_request(
            probe_seal=normalized,
            request_body=request_body,
            credential=credential,
            timeout_seconds=normalized["limits"]["request_timeout_ms"] / 1000.0,
        )
        observed_cost = _cost_fact(cost_fact)
        lease_id = canonical_sha256(
            {
                "probe_seal_sha256": normalized["seal_sha256"],
                "probe_run_id": normalized["probe_run_id"],
            }
        )
        lease_started = self.clock()
        with self.scheduler.acquire(
            physical_quota_bucket_id=source["physical_quota_bucket_id"],
            lease_id=lease_id,
            owner_id=normalized["probe_run_id"],
            acquired_at_utc=_timestamp(lease_started),
        ):
            self.ledger.reserve_capability_probe(normalized)
            started = self.clock()
            raw_response: RawTransportResponse | None = None
            facts: dict[str, Any] | None = None
            parsed_content: Mapping[str, Any] | None = None
            failure: dict[str, Any] | None = None
            try:
                raw_response = self.sender.send(request)
                if len(raw_response.body) > normalized["limits"][
                    "max_response_utf8_bytes"
                ]:
                    failure = _failure(
                        category="usage_limits",
                        code="response_bytes_exceeded",
                        safe_message="probe response exceeded its sealed byte cap",
                        details={
                            "observed_bytes": len(raw_response.body),
                            "maximum_bytes": normalized["limits"][
                                "max_response_utf8_bytes"
                            ],
                        },
                    )
                else:
                    facts = normalize_provider_response(
                        request=request, response=raw_response
                    )
            except TransportCallError as exc:
                diagnostic_response = exc.response
                if diagnostic_response is not None and not exc.response_body_truncated:
                    raw_response = diagnostic_response
                safe_message = exc.safe_message
                if exc.response_body_truncated:
                    safe_message = (
                        f"{safe_message}; diagnostic body exceeded sealed cap"
                    )
                details = {"code": exc.code, "status_code": exc.status_code}
                if diagnostic_response is not None:
                    details.update(
                        {
                            "response_body_sha256": sha256(
                                diagnostic_response.body
                            ).hexdigest(),
                            "response_body_truncated": exc.response_body_truncated,
                        }
                    )
                failure = _failure(
                    category=_transport_failure_category(exc),
                    code=_safe_error_code(exc.code),
                    safe_message=safe_message,
                    details=details,
                )

            if failure is None and facts is not None:
                failure = _validate_provider_facts(
                    facts=facts,
                    accepted_observed_model_ids=intent[
                        "accepted_observed_model_ids"
                    ],
                    limits=normalized["limits"],
                )
            if failure is None and facts is not None:
                try:
                    parsed_content = _structured_content(
                        protocol=request.protocol,
                        provider_payload=facts["payload"],
                    )
                except Exception as exc:
                    failure = _failure(
                        category="response_contract",
                        code="response_json_invalid",
                        safe_message="probe response did not contain a valid JSON object",
                        details={
                            "exception_type": type(exc).__name__,
                            "exception_sha256": sha256(
                                str(exc).encode("utf-8")
                            ).hexdigest(),
                        },
                    )
            if failure is None and parsed_content is not None:
                try:
                    local_validator(parsed_content)
                except Exception as exc:
                    failure = _failure(
                        category="response_contract",
                        code="local_validator_rejected",
                        safe_message="probe response failed its sealed local validator",
                        details={
                            "exception_type": type(exc).__name__,
                            "exception_sha256": sha256(
                                str(exc).encode("utf-8")
                            ).hexdigest(),
                        },
                    )

            finished = self.clock()
            raw_sha256 = None
            artifact_sha256 = None
            if raw_response is not None:
                raw_sha256 = sha256(raw_response.body).hexdigest()
                artifact_sha256 = self.artifact_store.put_bytes(raw_response.body)
                if artifact_sha256 != raw_sha256:
                    raise ContractValidationError(
                        "probe response artifact is not content addressed"
                    )
            outcome = "qualified" if failure is None else "failed"
            receipt = _receipt(
                seal=normalized,
                started=started,
                finished=finished,
                outcome=outcome,
                failure=failure,
                raw_response=raw_response,
                raw_response_sha256=raw_sha256,
                response_artifact_sha256=artifact_sha256,
                facts=facts,
                parsed_content=parsed_content,
                cost_fact=observed_cost,
            )
            evidence = _capability_evidence(
                seal=normalized, receipt=receipt
            )
            validated = self.ledger.append_capability_probe_result(
                seal=normalized,
                receipt=receipt,
                capability_evidence=evidence,
            )
        return {
            "status": outcome,
            "provider_called": True,
            "probe_seal_sha256": normalized["seal_sha256"],
            "receipt": validated["receipt"],
            "capability_evidence": validated["capability_evidence"],
        }


def create_capability_probe_seal(
    *,
    source: Mapping[str, Any],
    consumer_workstream: str,
    role_id: str,
    probe_run_id: str,
    probe_profile_id: str,
    probe_profile_revision: str,
    implementation_binding: Mapping[str, Any],
    capability_intent: Mapping[str, Any],
    response_schema: Mapping[str, Any],
    request_body: Mapping[str, Any],
    limits: Mapping[str, Any],
    issued_at_utc: str,
) -> dict[str, Any]:
    """Create and fully preflight one immutable capability-only seal."""

    normalized_source = validate_api_source(source)
    body = {
        "schema_version": CAPABILITY_PROBE_SEAL_SCHEMA_VERSION,
        "probe_run_id": probe_run_id,
        "consumer_workstream": consumer_workstream,
        "role_id": role_id,
        "probe_profile_id": probe_profile_id,
        "probe_profile_revision": probe_profile_revision,
        "implementation_binding": deepcopy(dict(implementation_binding)),
        "authority": "capability_only",
        "source_binding": {
            "record_sha256": canonical_sha256(normalized_source),
            "record": normalized_source,
        },
        "capability_intent": deepcopy(dict(capability_intent)),
        "response_schema": deepcopy(dict(response_schema)),
        "request_body_sha256": canonical_sha256(request_body),
        "limits": deepcopy(dict(limits)),
        "issued_at_utc": issued_at_utc,
    }
    body["seal_sha256"] = canonical_sha256(body)
    normalized = validate_capability_probe_seal(body)
    validate_capability_probe_request_body(
        probe_seal=normalized, request_body=request_body
    )
    return normalized


def _validate_provider_facts(
    *,
    facts: Mapping[str, Any],
    accepted_observed_model_ids: list[str],
    limits: Mapping[str, Any],
) -> dict[str, Any] | None:
    if facts["observed_model_id"] not in accepted_observed_model_ids:
        return _failure(
            category="model_identity",
            code="observed_model_mismatch",
            safe_message="provider observed model differs from sealed identity",
            details={
                "observed_model_id": facts["observed_model_id"],
                "accepted_observed_model_ids": accepted_observed_model_ids,
            },
        )
    if facts["finish_reason"] != "stop":
        return _failure(
            category="response_contract",
            code="incomplete_response",
            safe_message="probe response did not finish normally",
            details={"finish_reason": facts["finish_reason"]},
        )
    comparisons = (
        ("prompt_tokens", "max_prompt_tokens"),
        ("completion_tokens", "max_completion_tokens"),
        ("total_tokens", "max_total_tokens"),
    )
    for usage_field, limit_field in comparisons:
        observed = facts[usage_field]
        if observed is None:
            return _failure(
                category="usage_limits",
                code="usage_uncertified",
                safe_message="provider omitted usage needed to certify probe limits",
                details={"missing_usage_field": usage_field},
            )
        if observed > limits[limit_field]:
            return _failure(
                category="usage_limits",
                code="token_cap_exceeded",
                safe_message="probe usage exceeded a sealed token cap",
                details={
                    "usage_field": usage_field,
                    "observed": observed,
                    "limit_field": limit_field,
                    "maximum": limits[limit_field],
                },
            )
    if facts["total_tokens"] != facts["prompt_tokens"] + facts["completion_tokens"]:
        return _failure(
            category="usage_limits",
            code="usage_inconsistent",
            safe_message="provider token usage is internally inconsistent",
            details={
                "prompt_tokens": facts["prompt_tokens"],
                "completion_tokens": facts["completion_tokens"],
                "total_tokens": facts["total_tokens"],
            },
        )
    if (
        facts["cached_input_tokens"] is not None
        and facts["cached_input_tokens"] > facts["prompt_tokens"]
    ):
        return _failure(
            category="usage_limits",
            code="usage_inconsistent",
            safe_message="provider cached-token usage is internally inconsistent",
            details={
                "cached_input_tokens": facts["cached_input_tokens"],
                "prompt_tokens": facts["prompt_tokens"],
            },
        )
    if (
        facts["reasoning_tokens"] is not None
        and facts["reasoning_tokens"] > facts["completion_tokens"]
    ):
        return _failure(
            category="usage_limits",
            code="usage_inconsistent",
            safe_message="provider reasoning-token usage is internally inconsistent",
            details={
                "reasoning_tokens": facts["reasoning_tokens"],
                "completion_tokens": facts["completion_tokens"],
            },
        )
    return None


def _structured_content(
    *, protocol: str, provider_payload: Mapping[str, Any]
) -> Mapping[str, Any]:
    content: Any
    if protocol == "openai_chat_completions":
        choices = provider_payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ContractValidationError("OpenAI probe response lacks choices")
        message = choices[0].get("message") if isinstance(choices[0], Mapping) else None
        content = message.get("content") if isinstance(message, Mapping) else None
    elif protocol == "openai_responses":
        content = provider_payload.get("output_text")
        if content is None:
            output = provider_payload.get("output")
            item = output[0] if isinstance(output, list) and output else None
            pieces = item.get("content") if isinstance(item, Mapping) else None
            piece = pieces[0] if isinstance(pieces, list) and pieces else None
            content = piece.get("text") if isinstance(piece, Mapping) else None
    elif protocol == "google_genai_generate_content":
        candidates = provider_payload.get("candidates")
        candidate = candidates[0] if isinstance(candidates, list) and candidates else None
        candidate_content = candidate.get("content") if isinstance(candidate, Mapping) else None
        parts = candidate_content.get("parts") if isinstance(candidate_content, Mapping) else None
        part = parts[0] if isinstance(parts, list) and parts else None
        content = part.get("text") if isinstance(part, Mapping) else None
    elif protocol == "local_in_process":
        content = provider_payload.get("content")
    else:
        raise ContractValidationError("probe response protocol is unsupported")
    if not isinstance(content, str) or not content:
        raise ContractValidationError("probe response lacks structured content")
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ContractValidationError("probe content is not JSON") from exc
    if not isinstance(parsed, Mapping):
        raise ContractValidationError("probe JSON content must be an object")
    return dict(parsed)


def _receipt(
    *,
    seal: Mapping[str, Any],
    started: datetime,
    finished: datetime,
    outcome: str,
    failure: Mapping[str, Any] | None,
    raw_response: RawTransportResponse | None,
    raw_response_sha256: str | None,
    response_artifact_sha256: str | None,
    facts: Mapping[str, Any] | None,
    parsed_content: Mapping[str, Any] | None,
    cost_fact: Mapping[str, Any],
) -> dict[str, Any]:
    source = seal["source_binding"]["record"]
    intent = seal["capability_intent"]
    row = {
        "schema_version": CAPABILITY_PROBE_RECEIPT_SCHEMA_VERSION,
        "probe_seal_sha256": seal["seal_sha256"],
        "probe_run_id": seal["probe_run_id"],
        "authority": "capability_only",
        "source_record_sha256": seal["source_binding"]["record_sha256"],
        "source_id": source["source_id"],
        "source_revision": source["source_revision"],
        "physical_quota_bucket_id": source["physical_quota_bucket_id"],
        "capability_id": intent["capability_id"],
        "capability_revision": intent["capability_revision"],
        "request_body_sha256": seal["request_body_sha256"],
        "requested_model_id": intent["requested_model_id"],
        "observed_model_id": facts["observed_model_id"] if facts else None,
        "provider_called": True,
        "physical_attempt_index": 1,
        "request_id": facts["request_id"]
        if facts
        else raw_response.request_id
        if raw_response
        else None,
        "started_at_utc": _timestamp(started),
        "finished_at_utc": _timestamp(finished),
        "latency_ms": _latency_ms(started, finished),
        "outcome": outcome,
        "finish_reason": facts["finish_reason"] if facts else "error",
        "prompt_tokens": facts["prompt_tokens"] if facts else None,
        "cached_input_tokens": facts["cached_input_tokens"] if facts else None,
        "completion_tokens": facts["completion_tokens"] if facts else None,
        "reasoning_tokens": facts["reasoning_tokens"] if facts else None,
        "total_tokens": facts["total_tokens"] if facts else None,
        "cost_usd": cost_fact["cost_usd"],
        "cost_status": cost_fact["cost_status"],
        "cost_provenance": cost_fact["cost_provenance"],
        "raw_response_sha256": raw_response_sha256,
        "response_artifact_sha256": response_artifact_sha256,
        "parsed_content_sha256": canonical_sha256(parsed_content)
        if parsed_content is not None
        else None,
        "response_contract_validated": outcome == "qualified",
        "failure": deepcopy(dict(failure)) if failure is not None else None,
    }
    row["receipt_sha256"] = canonical_sha256(row)
    return row


def _capability_evidence(
    *, seal: Mapping[str, Any], receipt: Mapping[str, Any]
) -> dict[str, Any]:
    source = seal["source_binding"]["record"]
    intent = seal["capability_intent"]
    return {
        "schema_version": "capability_evidence_v1",
        "capability_id": intent["capability_id"],
        "capability_revision": intent["capability_revision"],
        "source_id": source["source_id"],
        "source_revision": source["source_revision"],
        "adapter_id": source["adapter_id"],
        "protocol": source["protocol"],
        "route_id": source["route_id"],
        "base_url": source["base_url"],
        "requested_model_id": intent["requested_model_id"],
        "observed_model_id": receipt["observed_model_id"],
        "capability_kind": intent["capability_kind"],
        "schema_dialect": intent["schema_dialect"],
        "schema_sha256": intent["schema_sha256"],
        "local_validator_id": intent["local_validator_id"],
        "local_validator_sha256": intent["local_validator_sha256"],
        "probe_id": seal["probe_run_id"],
        "evidence_sha256": receipt["receipt_sha256"],
        "observed_at_utc": receipt["finished_at_utc"],
        "verdict": receipt["outcome"],
    }


def _failure(
    *, category: str, code: str, safe_message: str, details: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "category": category,
        "code": code,
        "safe_message": safe_message,
        "details_sha256": canonical_sha256(details),
    }


def _transport_failure_category(error: TransportCallError) -> str:
    if error.code in {"invalid_json", "invalid_json_shape", "invalid_response", "invalid_usage", "missing_usage"}:
        return "response_contract"
    return "transport"


def _safe_error_code(value: str) -> str:
    normalized = value.casefold().replace("-", "_")
    if not normalized or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789._" for character in normalized):
        return "transport_error"
    return normalized[:191]


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
        raise ContractValidationError("probe cost_fact has missing or extra fields")
    result = deepcopy(dict(value))
    status = result["cost_status"]
    provenance = result["cost_provenance"]
    if status not in {"reported", "calculated", "unknown"}:
        raise ContractValidationError("probe cost_status is unsupported")
    if not isinstance(provenance, Mapping) or set(provenance) != {
        "kind",
        "reference_id",
        "reference_sha256",
    }:
        raise ContractValidationError("probe cost provenance has invalid fields")
    if status == "unknown":
        if result["cost_usd"] is not None or dict(provenance) != {
            "kind": "unavailable",
            "reference_id": None,
            "reference_sha256": None,
        }:
            raise ContractValidationError(
                "unknown probe cost requires unavailable provenance"
            )
    else:
        amount = result["cost_usd"]
        if (
            not isinstance(amount, (int, float))
            or isinstance(amount, bool)
            or amount < 0
        ):
            raise ContractValidationError("known probe cost must be nonnegative")
        expected_kind = (
            "provider_reported" if status == "reported" else "pricing_manifest"
        )
        if provenance["kind"] != expected_kind:
            raise ContractValidationError(
                "probe cost status and provenance kind differ"
            )
        if not isinstance(provenance["reference_id"], str) or not provenance[
            "reference_id"
        ]:
            raise ContractValidationError("probe cost provenance lacks reference_id")
        reference_sha = provenance["reference_sha256"]
        if (
            not isinstance(reference_sha, str)
            or len(reference_sha) != 64
            or any(character not in "0123456789abcdef" for character in reference_sha)
        ):
            raise ContractValidationError(
                "probe cost provenance lacks lowercase SHA-256"
            )
    canonical_json(result)
    return result


def _timestamp(value: datetime) -> str:
    normalized = _utc_datetime(value)
    return normalized.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _latency_ms(started: datetime, finished: datetime) -> int:
    started_utc = _utc_datetime(started).replace(
        microsecond=(_utc_datetime(started).microsecond // 1_000) * 1_000
    )
    finished_utc = _utc_datetime(finished).replace(
        microsecond=(_utc_datetime(finished).microsecond // 1_000) * 1_000
    )
    if finished_utc < started_utc:
        raise ContractValidationError("probe clock moved backwards")
    return int((finished_utc - started_utc).total_seconds() * 1_000)


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ContractValidationError("probe clock returned a naive datetime")
    return value.astimezone(timezone.utc)
