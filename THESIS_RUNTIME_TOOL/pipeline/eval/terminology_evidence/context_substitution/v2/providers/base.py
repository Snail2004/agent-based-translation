from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from pipeline.eval.contracts_v1 import (
    ContractValidationError,
    require_nullable_string,
)
from pipeline.eval.terminology_evidence.context_substitution.v2.contracts.common import (
    PROVIDER_ROUTE_IDS,
    nonnegative_int,
    sha256_text,
)
from pipeline.eval.terminology_evidence.context_substitution.v2.providers.ledger import (
    LEDGER_POLICY,
    ProviderResponseLedger,
)


class ContextExecutionError(RuntimeError):
    """Raised when no provider route can produce valid structured output."""


@dataclass(frozen=True)
class ProviderRawResponse:
    text: str
    payload: Mapping[str, Any] | None
    request_id: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cached: bool = False
    latency_ms: int = 0


class ContextProviderSender(Protocol):
    def __call__(
        self,
        *,
        system_prompt: str,
        user_payload_json: str,
        response_schema: Mapping[str, Any],
        max_output_tokens: int,
        tag: str,
    ) -> ProviderRawResponse:
        ...


@dataclass(frozen=True)
class ContextProviderRoute:
    route_id: str
    model_id: str
    sender: ContextProviderSender
    model_family: str | None = None
    independence_group: str | None = None

    def __post_init__(self) -> None:
        if self.route_id not in PROVIDER_ROUTE_IDS:
            raise ValueError(f"unsupported Context Judge route: {self.route_id}")
        if "latest" in self.model_id.casefold():
            raise ValueError("Context Judge model must not use a latest alias")
        family = (
            self.model_family
            or self.model_id.rsplit("/", 1)[-1]
        ).strip().casefold()
        group = (self.independence_group or family).strip().casefold()
        if not family or not group:
            raise ValueError("model family and independence group must not be empty")
        object.__setattr__(self, "model_family", family)
        object.__setattr__(self, "independence_group", group)


@dataclass
class ProviderCallCollector:
    successful_calls: list[dict[str, Any]]
    attempted_calls: list[dict[str, Any]]


class FailoverStructuredModel:
    """Use one schema across routes; only transport/structure failures fail over."""

    def __init__(
        self,
        routes: Sequence[ContextProviderRoute],
        *,
        response_ledger: ProviderResponseLedger | None = None,
    ) -> None:
        if not routes:
            raise ValueError("at least one Context Judge route is required")
        route_ids = [route.route_id for route in routes]
        if len(route_ids) != len(set(route_ids)):
            raise ValueError("Context Judge route IDs must be unique")
        self.routes = tuple(routes)
        self.response_ledger = response_ledger
        self.successful_calls: list[dict[str, Any]] = []
        self.attempted_calls: list[dict[str, Any]] = []
        self._active_collector: ContextVar[ProviderCallCollector | None] = (
            ContextVar(
                f"context_provider_collector_{id(self)}",
                default=None,
            )
        )

    @property
    def raw_response_ledger_policy(self) -> str:
        return (
            LEDGER_POLICY
            if self.response_ledger is not None
            else "NOT_CONFIGURED_DEVELOPMENT"
        )

    @contextmanager
    def collect_calls(self) -> Iterable[ProviderCallCollector]:
        if self._active_collector.get() is not None:
            raise RuntimeError("provider call collector is already active")
        collector = ProviderCallCollector(
            successful_calls=[],
            attempted_calls=[],
        )
        token = self._active_collector.set(collector)
        try:
            yield collector
        finally:
            self._active_collector.reset(token)

    def _record_success(self, provenance: Mapping[str, Any]) -> None:
        row = dict(provenance)
        self.successful_calls.append(row)
        collector = self._active_collector.get()
        if collector is not None:
            collector.successful_calls.append(dict(row))

    def _record_attempt(self, attempt: Mapping[str, Any]) -> None:
        row = dict(attempt)
        self.attempted_calls.append(row)
        if self.response_ledger is not None:
            self.response_ledger.record_attempt(row)
        collector = self._active_collector.get()
        if collector is not None:
            collector.attempted_calls.append(dict(row))

    def call(
        self,
        *,
        role: str,
        prompt_version: str,
        system_prompt: str,
        payload: Mapping[str, Any],
        response_schema: Mapping[str, Any],
        validator: Callable[[Any], dict[str, Any]],
        tag: str,
        max_output_tokens: int = 2_048,
        excluded_routes: Iterable[str] = (),
        excluded_independence_groups: Iterable[str] = (),
        excluded_model_families: Iterable[str] = (),
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        excluded = frozenset(excluded_routes)
        excluded_groups = {
            value.casefold() for value in excluded_independence_groups
        }
        excluded_families = {
            value.casefold() for value in excluded_model_families
        }
        user_payload_json = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        schema_json = json.dumps(
            response_schema,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        prompt_sha256 = sha256_text(
            "\n".join(
                (prompt_version, system_prompt, user_payload_json, schema_json)
            )
        )
        available = [
            route
            for route in self.routes
            if route.route_id not in excluded
            and route.independence_group not in excluded_groups
            and route.model_family not in excluded_families
        ]
        if not available:
            raise ContextExecutionError(
                f"{role}: no independent provider route remains"
            )
        failures: list[str] = []
        for route in available:
            raw: ProviderRawResponse | None = None
            raw_fields = {
                "raw_response_ref": None,
                "raw_response_sha256": None,
                "raw_response_storage_status": "UNAVAILABLE",
            }
            captured_response_text: str | None = None
            try:
                raw = route.sender(
                    system_prompt=system_prompt,
                    user_payload_json=user_payload_json,
                    response_schema=response_schema,
                    max_output_tokens=max_output_tokens,
                    tag=tag,
                )
                captured_response_text = raw.text or json.dumps(
                    dict(raw.payload or {}),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                if self.response_ledger is not None:
                    raw_fields = self.response_ledger.capture(captured_response_text)
                else:
                    raw_fields = {
                        "raw_response_ref": None,
                        "raw_response_sha256": sha256_text(captured_response_text),
                        "raw_response_storage_status": "NOT_CONFIGURED",
                    }
                parsed: Any = (
                    dict(raw.payload)
                    if raw.payload is not None
                    else json.loads(raw.text)
                )
                validated = validator(parsed)
                provenance = provider_provenance(
                    route=route,
                    role=role,
                    prompt_version=prompt_version,
                    prompt_sha256=prompt_sha256,
                    response_text=captured_response_text,
                    raw=raw,
                )
                self._record_success(provenance)
                self._record_attempt(
                    {
                        **provenance,
                        **raw_fields,
                        "accepted": True,
                        "failure_kind": None,
                    }
                )
                return validated, provenance
            except (
                ContractValidationError,
                ContextExecutionError,
                json.JSONDecodeError,
                KeyError,
                TypeError,
                ValueError,
            ) as exc:
                failures.append(f"{route.route_id}:{exc.__class__.__name__}")
                if raw is not None:
                    response_text = captured_response_text or raw.text
                    rejected = provider_provenance(
                        route=route,
                        role=role,
                        prompt_version=prompt_version,
                        prompt_sha256=prompt_sha256,
                        response_text=response_text,
                        raw=raw,
                    )
                    self._record_attempt(
                        {
                            **rejected,
                            **raw_fields,
                            "accepted": False,
                            "failure_kind": exc.__class__.__name__,
                        }
                    )
            except Exception as exc:
                if not provider_failure_is_retryable(exc):
                    raise
                failures.append(f"{route.route_id}:{exc.__class__.__name__}")
                self._record_attempt(
                    {
                        "provider_route_id": route.route_id,
                        "model_id": route.model_id,
                        "model_family": route.model_family,
                        "independence_group": route.independence_group,
                        "role": role,
                        "prompt_version": prompt_version,
                        "prompt_sha256": prompt_sha256,
                        "response_sha256": None,
                        "request_id": None,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "reasoning_tokens": 0,
                        "total_tokens": 0,
                        "cached": False,
                        "latency_ms": 0,
                        **raw_fields,
                        "accepted": False,
                        "failure_kind": exc.__class__.__name__,
                    }
                )
        rendered = ", ".join(failures) if failures else "no route attempted"
        raise ContextExecutionError(
            f"{role}: all provider routes failed local validation ({rendered})"
        )


def provider_failure_is_retryable(exc: Exception) -> bool:
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(exc, "code", None)
    if isinstance(status, int) and (
        status in {401, 402, 403, 408, 409, 429} or 500 <= status <= 599
    ):
        return True
    rendered = f"{exc.__class__.__name__} {exc}".casefold()
    return any(
        marker in rendered
        for marker in (
            "timeout",
            "timed out",
            "connection",
            "quota",
            "rate limit",
            "resource exhausted",
            "temporarily unavailable",
            "service unavailable",
            "unauthorized",
            "forbidden",
            "api key",
            "truncated",
            "malformed",
        )
    )


def provider_provenance(
    *,
    route: ContextProviderRoute,
    role: str,
    prompt_version: str,
    prompt_sha256: str,
    response_text: str,
    raw: ProviderRawResponse,
) -> dict[str, Any]:
    input_tokens = nonnegative_int(
        raw.input_tokens, path="$.provider.input_tokens"
    )
    output_tokens = nonnegative_int(
        raw.output_tokens, path="$.provider.output_tokens"
    )
    reasoning_tokens = nonnegative_int(
        raw.reasoning_tokens, path="$.provider.reasoning_tokens"
    )
    if reasoning_tokens > output_tokens:
        raise ContractValidationError(
            "usage_reasoning",
            "$.provider.reasoning_tokens",
            "reasoning_tokens must be included in output_tokens",
        )
    return {
        "provider_route_id": route.route_id,
        "model_id": route.model_id,
        "model_family": route.model_family,
        "independence_group": route.independence_group,
        "role": role,
        "prompt_version": prompt_version,
        "prompt_sha256": prompt_sha256,
        "response_sha256": sha256_text(response_text),
        "request_id": require_nullable_string(
            raw.request_id, path="$.provider.request_id", maximum=500
        ),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "total_tokens": input_tokens + output_tokens,
        "cached": bool(raw.cached),
        "latency_ms": nonnegative_int(
            raw.latency_ms, path="$.provider.latency_ms"
        ),
    }

