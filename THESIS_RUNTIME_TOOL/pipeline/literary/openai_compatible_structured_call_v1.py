from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from pipeline.agents.llm_client import LLMClient
from pipeline.agents.llm_config import LLMConfig
from pipeline.agents.provider_profile import ResolvedCredential
from pipeline.literary.structured_output_policy_v1 import (
    StructuredOutputContract,
    openai_response_format,
)


class OpenAICompatibleStructuredCallError(RuntimeError):
    """Raised when a sealed OpenAI-compatible route cannot execute safely."""


@dataclass(frozen=True)
class OpenAICompatibleStructuredResult:
    model: str
    response_text: str
    parsed_json: Mapping[str, Any] | None
    json_error: str | None
    usage: Mapping[str, Any]
    latency_ms: int
    cost_usd: float
    from_cache: bool
    cache_key: str


def call_openai_compatible_structured_v1(
    *,
    credential: ResolvedCredential,
    model_id: str,
    messages: Sequence[Mapping[str, Any]],
    contract: StructuredOutputContract,
    schema_name: str,
    cache_path: Path,
    tag: str,
    prompt_token_cap: int,
    max_output_tokens: int,
    temperature: float,
    seed: int,
    reasoning_effort: str,
    verbosity: str | None = "low",
) -> OpenAICompatibleStructuredResult:
    if credential.provider != "openai":
        raise OpenAICompatibleStructuredCallError(
            "OpenAI-compatible call received a foreign provider"
        )
    if contract.provider != "openai" or not contract.native_enforcement:
        raise OpenAICompatibleStructuredCallError(
            "OpenAI-compatible call requires a native structured-output contract"
        )
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover - real environment only.
        raise OpenAICompatibleStructuredCallError(
            "openai package is required for OpenAI-compatible execution"
        ) from exc

    client = LLMClient(
        LLMConfig(
            model=model_id,
            temperature=temperature,
            seed=seed,
            reasoning_effort=reasoning_effort,
            verbosity=verbosity,
            max_output_tokens=max_output_tokens,
            daily_token_cap=prompt_token_cap + max_output_tokens,
            prompt_token_cap=prompt_token_cap,
            pricing={"input": 0.0, "cached_input": 0.0, "output": 0.0},
        ),
        cache_path,
        transport=OpenAI(
            api_key=credential.secret,
            base_url=credential.base_url,
            timeout=credential.request_timeout_ms / 1000,
        ).chat.completions.create,
        max_retries=0,
    )
    result = client.call(
        [dict(row) for row in messages],
        response_format=openai_response_format(contract, schema_name=schema_name),
        tag=tag,
        bypass_cache=True,
    )
    parsed = result.parsed_json
    return OpenAICompatibleStructuredResult(
        model=result.model,
        response_text=result.text,
        parsed_json=dict(parsed) if isinstance(parsed, Mapping) else None,
        json_error=result.json_error,
        usage=asdict(result.usage),
        latency_ms=result.latency_ms,
        cost_usd=result.cost_usd,
        from_cache=result.from_cache,
        cache_key=result.cache_key,
    )


__all__ = [
    "OpenAICompatibleStructuredCallError",
    "OpenAICompatibleStructuredResult",
    "call_openai_compatible_structured_v1",
]
