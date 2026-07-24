"""Thin D2L adapter over one shared-backend physical attempt."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import time
from typing import Any, Callable, Mapping

from pipeline.agents.llm_client import LLMUsage
from pipeline.llm_backend import (
    ApplicationResponseCache,
    ContentAddressedArtifactStore,
    PhysicalQuotaScheduler,
    SharedLlmAttemptLedger,
    SharedLlmBackend,
    UrllibTransportSender,
    canonical_json,
    resolve_llm_run_seal,
)
from pipeline.llm_backend.credentials_v1 import CredentialProvider
from pipeline.llm_backend.transport_v1 import TransportCallError, TransportSender
from pipeline.prepass.d2l_shared_llm_profiles_v1 import (
    D2LRolePreset,
    build_pipeline_profile,
)


ADAPTER_VERSION = "d2l_shared_llm_adapter_v2_transport_retry_resume_identity"
TRANSPORT_RETRY_EXHAUSTED_EXIT_CODE = 75

ProfileBuilder = Callable[..., dict[str, Any]]
TransportObserver = Callable[[str, Mapping[str, Any]], None]


class D2LSharedLlmAdapterError(RuntimeError):
    pass


class D2LTransportRetriesExhausted(RuntimeError):
    """Raised after every sealed retryable transport attempt is persisted."""

    def __init__(
        self,
        *,
        logical_request_id: str,
        retry_summary: Mapping[str, Any],
    ) -> None:
        super().__init__(
            f"transport retries exhausted for logical request {logical_request_id}"
        )
        self.logical_request_id = logical_request_id
        self.retry_summary = dict(retry_summary)


@dataclass(frozen=True)
class D2LSharedAttemptResult:
    status: str
    provider_called: bool
    response_bytes: bytes
    response_text: str
    response_payload: Mapping[str, Any]
    observed_model_id: str | None
    finish_reason: str | None
    usage: Mapping[str, Any] | None
    artifact_sha256: str
    seal: Mapping[str, Any]
    cache_observation: Mapping[str, Any] | None


@dataclass(frozen=True)
class D2LSharedClientResult:
    """Compatibility projection for existing D2L semantic runners.

    The shared attempt ledger remains authoritative for nullable cost and usage
    facts. This projection keeps the legacy client surface while exposing the
    cost status explicitly.
    """

    text: str
    parsed_json: Any | None
    json_error: str | None
    model: str
    system_fingerprint: str | None
    usage: LLMUsage
    cost_usd: float | None
    cost_status: str
    latency_ms: int
    from_cache: bool
    cache_key: str
    seal_sha256: str
    artifact_sha256: str
    response_payload: Mapping[str, Any]
    logical_request_id: str
    physical_attempt_index: int
    provider_id: str
    source_id: str
    masked_quota_bucket: str
    finish_reason: str | None
    cache_status: str
    cache_mechanism: str
    attempt_usage_id: str | None = None
    semantic_attempt_index: int = 1
    transport_retry_ordinal: int = 0
    cache_observation_id: str | None = None
    provider_called: bool | None = None
    source_revision: str | None = None
    transport_retry_summary: Mapping[str, Any] | None = None


class D2LSharedLlmAttemptAdapter:
    """Resolve one D2L role and execute at most one physical request."""

    def __init__(
        self,
        *,
        runtime_root: str | Path,
        credential_provider: CredentialProvider,
        sender: TransportSender | None = None,
        profile_builder: ProfileBuilder = build_pipeline_profile,
        clock=None,
    ) -> None:
        root = Path(runtime_root).resolve()
        artifacts = ContentAddressedArtifactStore(root / "objects")
        self.ledger = SharedLlmAttemptLedger(root / "attempt_ledger.sqlite3")
        self.cache = ApplicationResponseCache(
            index_path=root / "response_cache.sqlite3",
            artifact_store=artifacts,
        )
        self.backend = SharedLlmBackend(
            credential_provider=credential_provider,
            scheduler=PhysicalQuotaScheduler(root / "quota_locks"),
            ledger=self.ledger,
            response_cache=self.cache,
            sender=sender or UrllibTransportSender(),
            clock=clock,
        )
        self.profile_builder = profile_builder

    def execute(
        self,
        *,
        preset: D2LRolePreset,
        api_source: Mapping[str, Any],
        capability: Mapping[str, Any],
        prompt_ref: Mapping[str, Any],
        response_schema_ref: Mapping[str, Any] | None,
        validator_ref: Mapping[str, Any],
        semantic_extension_ref: Mapping[str, Any],
        structured_output: Mapping[str, Any],
        limits: Mapping[str, Any],
        run_id: str,
        attempt_run_id: str,
        stage_id: str,
        logical_request_id: str,
        semantic_attempt_index: int,
        transport_retry_ordinal: int,
        request_body: Mapping[str, Any],
        additional_input_bindings: list[Mapping[str, str]] | None = None,
        allow_response_cache_read: bool = True,
        allow_response_cache_write: bool = True,
        cost_fact: Mapping[str, Any] | None = None,
    ) -> D2LSharedAttemptResult:
        profile = self.profile_builder(
            preset=preset,
            api_source=api_source,
            capability=capability,
            prompt_ref=prompt_ref,
            response_schema_ref=response_schema_ref,
            validator_ref=validator_ref,
            semantic_extension_ref=semantic_extension_ref,
            structured_output=structured_output,
            limits=limits,
        )
        request_sha256 = sha256(
            canonical_json(request_body).encode("utf-8")
        ).hexdigest()
        bindings = [
            {"name": "transport_request_body", "sha256": request_sha256},
            *(additional_input_bindings or []),
        ]
        seal = resolve_llm_run_seal(
            profile=profile,
            api_sources=[api_source],
            capability_evidence=[capability],
            role_id=preset.role_id,
            run_id=run_id,
            attempt_run_id=attempt_run_id,
            stage_id=stage_id,
            input_bindings=bindings,
        )
        result = self.backend.execute_one_attempt(
            seal=seal,
            logical_request_id=logical_request_id,
            semantic_attempt_index=semantic_attempt_index,
            transport_retry_ordinal=transport_retry_ordinal,
            request_body=request_body,
            allow_response_cache_read=allow_response_cache_read,
            allow_response_cache_write=allow_response_cache_write,
            cost_fact=cost_fact,
        )
        payload = _decode_response_payload(result["response_bytes"])
        protocol = seal["primary"]["source"]["protocol"]
        response_text = _extract_response_text(payload, protocol=protocol)
        usage = result.get("usage")
        return D2LSharedAttemptResult(
            status=str(result["status"]),
            provider_called=bool(result["provider_called"]),
            response_bytes=result["response_bytes"],
            response_text=response_text,
            response_payload=payload,
            observed_model_id=(
                None if usage is None else usage.get("observed_model_id")
            ),
            finish_reason=None if usage is None else usage.get("finish_reason"),
            usage=usage,
            artifact_sha256=str(result["artifact_sha256"]),
            seal=seal,
            cache_observation=result.get("cache_observation"),
        )


class D2LSharedLlmClient:
    """LLMClient-compatible facade backed by one shared physical attempt."""

    uses_shared_backend = True

    def __init__(
        self,
        *,
        adapter: D2LSharedLlmAttemptAdapter,
        config: Any,
        preset: D2LRolePreset,
        api_source: Mapping[str, Any],
        capability: Mapping[str, Any],
        prompt_ref: Mapping[str, Any],
        response_schema_ref: Mapping[str, Any] | None,
        validator_ref: Mapping[str, Any],
        semantic_extension_ref: Mapping[str, Any],
        structured_output: Mapping[str, Any],
        limits: Mapping[str, Any],
        run_id: str,
        attempt_run_id: str,
        stage_id: str,
        google_response_json_schema: Mapping[str, Any] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.adapter = adapter
        self.config = config
        self.preset = preset
        self.api_source = dict(api_source)
        self.capability = dict(capability)
        self.prompt_ref = dict(prompt_ref)
        self.response_schema_ref = (
            None if response_schema_ref is None else dict(response_schema_ref)
        )
        self.validator_ref = dict(validator_ref)
        self.semantic_extension_ref = dict(semantic_extension_ref)
        self.structured_output = dict(structured_output)
        self.limits = dict(limits)
        self.run_id = run_id
        self.attempt_run_id = attempt_run_id
        self.stage_id = stage_id
        self.google_response_json_schema = (
            None
            if google_response_json_schema is None
            else dict(google_response_json_schema)
        )
        self._sleeper = sleeper

    @property
    def resume_transport_identity(self) -> str:
        """Stable semantic transport identity shared by component attempts."""

        material = {
            "adapter_version": ADAPTER_VERSION,
            "role_id": self.preset.role_id,
            "preset_id": self.preset.preset_id,
            "preset_revision": self.preset.preset_revision,
            "requested_model_id": self.preset.requested_model_id,
            "generation": dict(self.preset.generation),
            "transport_retry": dict(self.preset.transport_retry),
            "semantic_retry": dict(self.preset.semantic_retry),
            "namespaces": dict(self.preset.namespaces),
            "api_source": self.api_source,
            "capability": self.capability,
            "prompt_ref": self.prompt_ref,
            "response_schema_ref": self.response_schema_ref,
            "validator_ref": self.validator_ref,
            "semantic_extension_ref": self.semantic_extension_ref,
            "structured_output": self.structured_output,
            "limits": self.limits,
            "run_id": self.run_id,
            "stage_id": self.stage_id,
        }
        return sha256(canonical_json(material).encode("utf-8")).hexdigest()

    @property
    def transport_identity(self) -> str:
        """Physical-attempt identity retained for sealed call evidence."""

        material = {
            "resume_transport_identity": self.resume_transport_identity,
            "attempt_run_id": self.attempt_run_id,
        }
        return sha256(canonical_json(material).encode("utf-8")).hexdigest()

    def call(
        self,
        messages: list[dict[str, Any]],
        *,
        response_format: dict[str, Any] | None = None,
        tag: str = "",
        bypass_cache: bool = False,
        semantic_attempt_index: int = 1,
        transport_observer: TransportObserver | None = None,
    ) -> D2LSharedClientResult:
        if (
            isinstance(semantic_attempt_index, bool)
            or not isinstance(semantic_attempt_index, int)
            or semantic_attempt_index < 1
        ):
            raise D2LSharedLlmAdapterError(
                "semantic_attempt_index must be a positive integer"
            )
        protocol = str(self.api_source["protocol"])
        if protocol == "google_genai_generate_content":
            output_mode = str(self.structured_output["mode"])
            if output_mode in {"required", "preferred"} and (
                self.google_response_json_schema is None
            ):
                raise D2LSharedLlmAdapterError(
                    "Google shared client lacks its exact response JSON schema"
                )
            if output_mode in {"prompt_validated", "disabled"} and (
                self.google_response_json_schema is not None
            ):
                raise D2LSharedLlmAdapterError(
                    "Non-native Google output mode may not send a response JSON schema"
                )
            request_body = render_google_generate_content_request(
                preset=self.preset,
                messages=messages,
                response_json_schema=self.google_response_json_schema,
                structured_output_mode=output_mode,
            )
        elif protocol == "openai_chat_completions":
            output_mode = str(self.structured_output["mode"])
            if output_mode == "disabled":
                wire_response_format = None
            elif output_mode == "prompt_validated":
                wire_response_format = {"type": "json_object"}
            else:
                wire_response_format = response_format
            request_body = render_openai_chat_request(
                preset=self.preset,
                messages=messages,
                response_format=wire_response_format,
            )
        else:
            raise D2LSharedLlmAdapterError(
                f"LLMClient compatibility does not support {protocol}"
            )
        logical_request_id = _logical_request_id(
            tag=tag,
            request_body=request_body,
            run_id=self.run_id,
            attempt_run_id=self.attempt_run_id,
            stage_id=self.stage_id,
        )
        retry_reason_codes: list[str] = []
        retry_count = 0
        retry_summary: dict[str, Any] | None = None
        while True:
            transport_retry_ordinal = self._transport_retry_ordinal(
                logical_request_id=logical_request_id
            )
            started = time.perf_counter()
            try:
                result = self.adapter.execute(
                    preset=self.preset,
                    api_source=self.api_source,
                    capability=self.capability,
                    prompt_ref=self.prompt_ref,
                    response_schema_ref=self.response_schema_ref,
                    validator_ref=self.validator_ref,
                    semantic_extension_ref=self.semantic_extension_ref,
                    structured_output=self.structured_output,
                    limits=self.limits,
                    run_id=self.run_id,
                    attempt_run_id=self.attempt_run_id,
                    stage_id=self.stage_id,
                    logical_request_id=logical_request_id,
                    semantic_attempt_index=semantic_attempt_index,
                    transport_retry_ordinal=transport_retry_ordinal,
                    request_body=request_body,
                    additional_input_bindings=[
                        {
                            "name": "semantic_call_tag",
                            "sha256": sha256(tag.encode("utf-8")).hexdigest(),
                        }
                    ],
                    allow_response_cache_read=not bypass_cache,
                    allow_response_cache_write=True,
                )
            except TransportCallError as exc:
                failure = self._transport_failure(
                    logical_request_id=logical_request_id,
                    semantic_attempt_index=semantic_attempt_index,
                    transport_retry_ordinal=transport_retry_ordinal,
                )
                retry_reason_codes.append(str(failure["retry_class"]))
                if transport_observer is not None:
                    transport_observer("attempt_failed", failure)
                if failure["retry_disposition"] == "transport_retry_allowed":
                    retry_count = transport_retry_ordinal + 1
                    if transport_observer is not None:
                        transport_observer(
                            "retry_scheduled",
                            {
                                "logical_request_id": logical_request_id,
                                "retry_index": retry_count,
                                "retry_max": int(
                                    self.preset.transport_retry["max_retries"]
                                ),
                                "reason_code": str(failure["retry_class"]),
                            },
                        )
                    self._sleep_before_retry(retry_count)
                    continue
                retryable_codes = set(
                    str(value)
                    for value in self.preset.transport_retry["retryable_codes"]
                )
                exhausted = (
                    str(failure["retry_class"]) in retryable_codes
                    and transport_retry_ordinal
                    >= int(self.preset.transport_retry["max_retries"])
                )
                if exhausted:
                    summary = {
                        "logical_request_id": logical_request_id,
                        "retry_count": int(
                            self.preset.transport_retry["max_retries"]
                        ),
                        "outcome": "exhausted",
                        "reason_codes": sorted(set(retry_reason_codes)),
                    }
                    if transport_observer is not None:
                        transport_observer("retry_summary", summary)
                    raise D2LTransportRetriesExhausted(
                        logical_request_id=logical_request_id,
                        retry_summary=summary,
                    ) from exc
                raise
            latency_ms = int((time.perf_counter() - started) * 1000)
            if retry_count:
                retry_summary = {
                    "logical_request_id": logical_request_id,
                    "retry_count": retry_count,
                    "outcome": "recovered",
                    "reason_codes": sorted(set(retry_reason_codes)),
                }
            break
        parsed, json_error = _parse_json_if_requested(
            result.response_text, response_format=response_format
        )
        usage_row = result.usage or {}
        usage = LLMUsage(
            prompt_tokens=int(usage_row.get("prompt_tokens") or 0),
            cached_tokens=int(usage_row.get("cached_input_tokens") or 0),
            completion_tokens=int(usage_row.get("completion_tokens") or 0),
            reasoning_tokens=int(usage_row.get("reasoning_tokens") or 0),
        )
        raw_cost = usage_row.get("cost_usd")
        cost_status = str(usage_row.get("cost_status") or "cache_reuse")
        observation = result.cache_observation or {}
        cache_key = str(
            observation.get("cache_key_sha256") or result.artifact_sha256
        )
        physical_attempt_index = int(usage_row.get("physical_attempt_index") or 1)
        attempt_usage_id = usage_row.get("attempt_usage_id")
        cache_observation_id = observation.get("observation_id")
        source_id = str(
            usage_row.get("source_id") or self.api_source["source_id"]
        )
        quota_bucket = str(
            usage_row.get("physical_quota_bucket_id")
            or self.api_source["physical_quota_bucket_id"]
        )
        lookup_status = str(observation.get("lookup_status") or "")
        cache_status = (
            lookup_status
            if lookup_status in {"hit", "miss"}
            else ("bypass" if bypass_cache else "unknown")
        )
        return D2LSharedClientResult(
            text=result.response_text,
            parsed_json=parsed,
            json_error=json_error,
            model=result.observed_model_id or self.preset.requested_model_id,
            system_fingerprint=f"shared:{result.seal['seal_sha256']}",
            usage=usage,
            cost_usd=None if raw_cost is None else float(raw_cost),
            cost_status=cost_status,
            latency_ms=latency_ms,
            from_cache=not result.provider_called,
            cache_key=cache_key,
            seal_sha256=str(result.seal["seal_sha256"]),
            artifact_sha256=result.artifact_sha256,
            response_payload=result.response_payload,
            logical_request_id=logical_request_id,
            physical_attempt_index=physical_attempt_index,
            attempt_usage_id=(
                None if attempt_usage_id is None else str(attempt_usage_id)
            ),
            semantic_attempt_index=int(
                usage_row.get("semantic_attempt_index") or semantic_attempt_index
            ),
            transport_retry_ordinal=int(
                usage_row.get("transport_retry_ordinal")
                if usage_row.get("transport_retry_ordinal") is not None
                else transport_retry_ordinal
            ),
            cache_observation_id=(
                None
                if cache_observation_id is None
                else str(cache_observation_id)
            ),
            provider_called=result.provider_called,
            provider_id=source_id,
            source_id=source_id,
            source_revision=str(
                usage_row.get("source_revision")
                or result.seal["primary"]["source"]["source_revision"]
            ),
            masked_quota_bucket=_masked_bucket(quota_bucket),
            finish_reason=result.finish_reason,
            cache_status=cache_status,
            cache_mechanism=(
                "local_exact_cache"
                if cache_status in {"hit", "miss"}
                else "none"
            ),
            transport_retry_summary=retry_summary,
        )

    def _transport_retry_ordinal(self, *, logical_request_id: str) -> int:
        matching = [
            row
            for row in self.adapter.ledger.list_records("usage")
            if row.get("logical_request_id") == logical_request_id
        ]
        if not matching:
            return 0
        latest = max(
            matching,
            key=lambda row: (
                int(row["semantic_attempt_index"]),
                int(row["transport_retry_ordinal"]),
            ),
        )
        error_id = latest.get("error_id")
        if error_id is None:
            return 0
        error = self.adapter.ledger.get_record("error", str(error_id))
        if error is None or error.get("retry_disposition") != "transport_retry_allowed":
            return 0
        return int(latest["transport_retry_ordinal"]) + 1

    def _transport_failure(
        self,
        *,
        logical_request_id: str,
        semantic_attempt_index: int,
        transport_retry_ordinal: int,
    ) -> dict[str, Any]:
        matching = [
            row
            for row in self.adapter.ledger.list_records("usage")
            if row.get("logical_request_id") == logical_request_id
            and int(row.get("semantic_attempt_index") or 0)
            == semantic_attempt_index
            and int(row.get("transport_retry_ordinal") or 0)
            == transport_retry_ordinal
        ]
        if len(matching) != 1:
            raise D2LSharedLlmAdapterError(
                "transport failure lacks one authoritative usage row"
            )
        usage = matching[0]
        error_id = usage.get("error_id")
        error = (
            None
            if error_id is None
            else self.adapter.ledger.get_record("error", str(error_id))
        )
        if error is None:
            raise D2LSharedLlmAdapterError(
                "transport failure lacks its authoritative error row"
            )
        return {
            "attempt_usage_id": str(usage["attempt_usage_id"]),
            "logical_request_id": logical_request_id,
            "semantic_attempt_index": semantic_attempt_index,
            "transport_retry_ordinal": transport_retry_ordinal,
            "physical_attempt_index": int(usage["physical_attempt_index"]),
            "provider_id": str(usage["source_id"]),
            "model_id": str(usage["requested_model_id"]),
            "source_id": str(usage["source_id"]),
            "source_revision": str(usage["source_revision"]),
            "masked_quota_bucket": _masked_bucket(
                str(usage["physical_quota_bucket_id"])
            ),
            "latency_ms": int(usage["latency_ms"]),
            "prompt_tokens": usage["prompt_tokens"],
            "completion_tokens": usage["completion_tokens"],
            "cached_input_tokens": usage["cached_input_tokens"],
            "reasoning_tokens": usage["reasoning_tokens"],
            "total_tokens": usage["total_tokens"],
            "cost_usd": usage["cost_usd"],
            "cost_status": str(usage["cost_status"]),
            "reason_code": str(error["code"]),
            "retry_class": str(error["retry_class"]),
            "retry_disposition": str(error["retry_disposition"]),
        }

    def _sleep_before_retry(self, retry_index: int) -> None:
        policy = self.preset.transport_retry
        initial_ms = int(policy["initial_delay_ms"])
        maximum_ms = int(policy["max_delay_ms"])
        if policy["backoff_policy"] == "fixed":
            delay_ms = initial_ms
        elif policy["backoff_policy"] == "exponential":
            delay_ms = initial_ms * (2 ** (retry_index - 1))
        else:
            raise D2LSharedLlmAdapterError(
                "retryable transport failure has no backoff policy"
            )
        self._sleeper(min(delay_ms, maximum_ms) / 1000.0)

    def get_usage_today(self) -> dict[str, int | str]:
        rows = self.adapter.ledger.list_records("usage")
        return {
            "date": "shared_ledger",
            "total_tokens": sum(int(row.get("total_tokens") or 0) for row in rows),
            "calls": len(rows),
        }


class D2LSharedLlmClientFactory:
    """Explicit factory marker for LLMClient-compatible D2L runners."""

    uses_shared_backend = True

    def __init__(self, client: D2LSharedLlmClient) -> None:
        self.client = client

    def __call__(self, config: Any, _cache_path: Path) -> D2LSharedLlmClient:
        generation = self.client.preset.generation
        expected = {
            "model": self.client.preset.requested_model_id,
            "temperature": float(generation["temperature"]),
            "seed": generation["seed"],
            "reasoning_effort": generation["reasoning_effort"],
            "verbosity": generation["verbosity"],
            "max_output_tokens": int(generation["max_output_tokens"]),
            "prompt_token_cap": int(generation["max_input_tokens"]),
        }
        observed = {
            "model": getattr(config, "model", None),
            "temperature": float(getattr(config, "temperature", -1.0)),
            "seed": getattr(config, "seed", None),
            "reasoning_effort": getattr(config, "reasoning_effort", None),
            "verbosity": getattr(config, "verbosity", None),
            "max_output_tokens": int(
                getattr(config, "max_output_tokens", -1)
            ),
            "prompt_token_cap": getattr(config, "prompt_token_cap", None),
        }
        if observed != expected:
            raise D2LSharedLlmAdapterError(
                "legacy runner config differs from the sealed D2L role preset"
            )
        return self.client


class D2LSharedOpenAiTransportBridge:
    """Explicit parity bridge for legacy D2L call sites accepting ``**request``."""

    uses_shared_backend = True

    def __init__(self, client: D2LSharedLlmClient) -> None:
        self.client = client
        self.last_attempt_metadata: dict[str, Any] | None = None

    def __call__(self, **request: Any) -> Mapping[str, Any]:
        expected = render_openai_chat_request(
            preset=self.client.preset,
            messages=list(request.get("messages") or []),
            response_format=request.get("response_format"),
        )
        if request != expected:
            raise D2LSharedLlmAdapterError(
                "legacy OpenAI request differs from sealed shared renderer"
            )
        request_hash = sha256(
            canonical_json(request).encode("utf-8")
        ).hexdigest()
        result = self.client.call(
            list(request["messages"]),
            response_format=request.get("response_format"),
            tag=f"legacy_bridge_{request_hash[:24]}",
        )
        self.last_attempt_metadata = {
            "adapter_version": ADAPTER_VERSION,
            "seal_sha256": result.seal_sha256,
            "artifact_sha256": result.artifact_sha256,
            "cache_key_sha256": result.cache_key,
            "provider_called": not result.from_cache,
            "cost_status": result.cost_status,
        }
        return result.response_payload


def render_openai_chat_request(
    *,
    preset: D2LRolePreset,
    messages: list[Mapping[str, Any]],
    response_format: Mapping[str, Any] | None,
) -> dict[str, Any]:
    generation = preset.generation
    body = {
        "model": preset.requested_model_id,
        "messages": [dict(row) for row in messages],
        "temperature": generation["temperature"],
        "seed": generation["seed"],
        "reasoning_effort": generation["reasoning_effort"],
        "verbosity": generation["verbosity"],
        "response_format": (
            None if response_format is None else dict(response_format)
        ),
        "max_completion_tokens": generation["max_output_tokens"],
    }
    return {key: value for key, value in body.items() if value is not None}


def render_google_generate_content_request(
    *,
    preset: D2LRolePreset,
    messages: list[Mapping[str, Any]],
    response_json_schema: Mapping[str, Any] | None,
    structured_output_mode: str = "required",
) -> dict[str, Any]:
    if structured_output_mode not in {
        "required",
        "preferred",
        "prompt_validated",
        "disabled",
    }:
        raise D2LSharedLlmAdapterError(
            f"Unsupported structured-output mode: {structured_output_mode}"
        )
    if structured_output_mode in {"required", "preferred"}:
        if response_json_schema is None:
            raise D2LSharedLlmAdapterError(
                "Native Google output mode requires a response JSON schema"
            )
    elif response_json_schema is not None:
        raise D2LSharedLlmAdapterError(
            "Non-native Google output mode may not send a response JSON schema"
        )
    system_parts: list[dict[str, str]] = []
    contents: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role") or "user")
        text = str(message.get("content") or "")
        if role == "system":
            system_parts.append({"text": text})
            continue
        provider_role = "model" if role == "assistant" else "user"
        contents.append({"role": provider_role, "parts": [{"text": text}]})
    generation = preset.generation
    generation_config: dict[str, Any] = {
        "temperature": generation["temperature"],
        "seed": generation["seed"],
        "maxOutputTokens": generation["max_output_tokens"],
        "thinkingConfig": {"thinkingBudget": 0},
    }
    if structured_output_mode in {"required", "preferred"}:
        generation_config.update(
            {
                "responseMimeType": "application/json",
                "responseJsonSchema": dict(response_json_schema or {}),
            }
        )
    elif structured_output_mode == "prompt_validated":
        generation_config["responseMimeType"] = "application/json"

    body: dict[str, Any] = {
        "contents": contents,
        "generationConfig": generation_config,
    }
    if system_parts:
        body["systemInstruction"] = {"parts": system_parts}
    body["generationConfig"] = {
        key: value
        for key, value in body["generationConfig"].items()
        if value is not None
    }
    return body


def _decode_response_payload(response_bytes: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(response_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise D2LSharedLlmAdapterError(
            "shared backend returned a non-JSON provider artifact"
        ) from exc
    if not isinstance(payload, dict):
        raise D2LSharedLlmAdapterError("provider response artifact is not an object")
    return payload


def _extract_response_text(
    payload: Mapping[str, Any], *, protocol: str
) -> str:
    if protocol == "openai_chat_completions":
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise D2LSharedLlmAdapterError("OpenAI response lacks choices")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            raise D2LSharedLlmAdapterError("OpenAI response lacks text content")
        return content
    if protocol == "openai_responses":
        output = payload.get("output")
        if not isinstance(output, list):
            raise D2LSharedLlmAdapterError("Responses payload lacks output")
        parts = []
        for item in output:
            contents = item.get("content") or [] if isinstance(item, dict) else []
            for content in contents:
                text = content.get("text") if isinstance(content, dict) else None
                if isinstance(text, str):
                    parts.append(text)
        if not parts:
            raise D2LSharedLlmAdapterError("Responses payload lacks text content")
        return "".join(parts)
    if protocol == "google_genai_generate_content":
        candidates = payload.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise D2LSharedLlmAdapterError("Google response lacks candidates")
        content = (
            candidates[0].get("content")
            if isinstance(candidates[0], dict)
            else None
        )
        parts = content.get("parts") if isinstance(content, dict) else None
        texts = [
            part["text"]
            for part in parts or []
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        ]
        if not texts:
            raise D2LSharedLlmAdapterError("Google response lacks text content")
        return "".join(texts)
    if protocol == "local_in_process":
        text = payload.get("text")
        if not isinstance(text, str):
            raise D2LSharedLlmAdapterError("local response lacks text")
        return text
    raise D2LSharedLlmAdapterError(f"unsupported response protocol: {protocol}")


def _logical_request_id(
    *,
    tag: str,
    request_body: Mapping[str, Any],
    run_id: str,
    attempt_run_id: str,
    stage_id: str,
) -> str:
    digest = sha256(
        canonical_json(
            {
                "run_id": run_id,
                "attempt_run_id": attempt_run_id,
                "stage_id": stage_id,
                "tag": tag,
                "request_body_sha256": sha256(
                    canonical_json(request_body).encode("utf-8")
                ).hexdigest(),
            }
        ).encode("utf-8")
    ).hexdigest()
    return f"request_{digest[:32]}"


def _parse_json_if_requested(
    text: str, *, response_format: Mapping[str, Any] | None
) -> tuple[Any | None, str | None]:
    if response_format is None:
        return None, None
    try:
        return json.loads(text), None
    except json.JSONDecodeError as exc:
        return None, str(exc)


def _masked_bucket(value: str) -> str:
    if len(value) <= 8:
        return "***"
    return value[:8] + "...***"


__all__ = [
    "D2LTransportRetriesExhausted",
    "TRANSPORT_RETRY_EXHAUSTED_EXIT_CODE",
    "ADAPTER_VERSION",
    "D2LSharedAttemptResult",
    "D2LSharedClientResult",
    "D2LSharedLlmAdapterError",
    "D2LSharedLlmAttemptAdapter",
    "D2LSharedLlmClient",
    "D2LSharedLlmClientFactory",
    "D2LSharedOpenAiTransportBridge",
    "render_google_generate_content_request",
    "render_openai_chat_request",
]
