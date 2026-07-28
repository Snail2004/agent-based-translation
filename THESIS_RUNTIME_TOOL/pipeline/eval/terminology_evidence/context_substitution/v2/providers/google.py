from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from pipeline.eval.terminology_evidence.context_substitution.v2.providers.base import (
    ContextProviderRoute,
    ProviderRawResponse,
)


@dataclass(frozen=True)
class GoogleRouteSettings:
    route_id: str
    model_id: str
    api_key: str = field(repr=False)
    base_url: str | None = None
    timeout_seconds: int = 120
    model_family: str | None = None
    independence_group: str | None = None

    def build(self) -> ContextProviderRoute:
        if not self.api_key.strip():
            raise ValueError(f"{self.route_id}: API key must not be empty")
        return ContextProviderRoute(
            route_id=self.route_id,
            model_id=self.model_id,
            sender=GoogleGenAISender(self),
            model_family=self.model_family,
            independence_group=self.independence_group,
        )


class GoogleGenAISender:
    """Thin Google Gen AI adapter shared by official and compatible routes."""

    def __init__(self, settings: GoogleRouteSettings) -> None:
        self._settings = settings
        self._client: Any | None = None

    def __call__(
        self,
        *,
        system_prompt: str,
        user_payload_json: str,
        response_schema: Mapping[str, Any],
        max_output_tokens: int,
        tag: str,
    ) -> ProviderRawResponse:
        from google.genai import types

        started = time.perf_counter()
        response = self._get_client().models.generate_content(
            model=self._settings.model_id,
            contents=user_payload_json,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=0,
                response_mime_type="application/json",
                response_json_schema=dict(response_schema),
                max_output_tokens=max_output_tokens,
            ),
        )
        latency_ms = round((time.perf_counter() - started) * 1_000)
        payload = _parsed_payload(response)
        text = _response_text(response, payload)
        usage = _usage_counts(getattr(response, "usage_metadata", None))
        return ProviderRawResponse(
            text=text,
            payload=payload,
            request_id=getattr(response, "response_id", None),
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            reasoning_tokens=usage["reasoning_tokens"],
            cached=usage["cached"],
            latency_ms=latency_ms,
        )

    def _get_client(self) -> Any:
        if self._client is None:
            from google import genai
            from google.genai import types

            http_option_values: dict[str, Any] = {
                "timeout": self._settings.timeout_seconds * 1_000,
            }
            if self._settings.base_url is not None:
                http_option_values["base_url"] = self._settings.base_url
            http_options = types.HttpOptions(**http_option_values)
            self._client = genai.Client(
                api_key=self._settings.api_key,
                http_options=http_options,
            )
        return self._client


def _parsed_payload(response: Any) -> Mapping[str, Any] | None:
    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, Mapping):
        return dict(parsed)
    return None


def _response_text(
    response: Any, payload: Mapping[str, Any] | None
) -> str:
    try:
        text = response.text
    except (AttributeError, TypeError, ValueError):
        text = None
    if isinstance(text, str) and text:
        return text
    if payload is not None:
        return json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    raise ValueError("provider returned neither JSON text nor parsed JSON")


def _usage_counts(metadata: Any) -> dict[str, int | bool]:
    if metadata is None:
        return {
            "input_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
            "cached": False,
        }
    prompt = _nonnegative(getattr(metadata, "prompt_token_count", 0))
    candidate = _nonnegative(
        getattr(metadata, "candidates_token_count", 0)
    )
    reasoning = _nonnegative(getattr(metadata, "thoughts_token_count", 0))
    provider_total = _nonnegative(getattr(metadata, "total_token_count", 0))
    # The evidence contract treats reasoning as part of output usage.
    output = max(candidate + reasoning, provider_total - prompt, 0)
    cached_count = _nonnegative(
        getattr(metadata, "cached_content_token_count", 0)
    )
    return {
        "input_tokens": prompt,
        "output_tokens": output,
        "reasoning_tokens": reasoning,
        "cached": cached_count > 0,
    }


def _nonnegative(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    try:
        result = int(value)
    except (TypeError, ValueError):
        return 0
    return max(result, 0)


