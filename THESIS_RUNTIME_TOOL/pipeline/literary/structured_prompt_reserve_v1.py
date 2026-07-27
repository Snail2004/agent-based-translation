"""Conservative transport reserve for structured Literary requests."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from typing import Any, Mapping, Sequence

from pipeline.literary.chapter_registry_v4 import estimate_registry_prompt_tokens


@dataclass(frozen=True)
class StructuredPromptReserve:
    message_token_estimate: int
    response_schema_utf8_bytes: int
    prompt_token_reserve: int
    output_token_cap: int

    @property
    def total_token_reserve(self) -> int:
        return self.prompt_token_reserve + self.output_token_cap

    def to_payload(self) -> dict[str, int]:
        return {
            "message_token_estimate": self.message_token_estimate,
            "response_schema_utf8_bytes": self.response_schema_utf8_bytes,
            "prompt_token_reserve": self.prompt_token_reserve,
            "output_token_cap": self.output_token_cap,
            "total_token_reserve": self.total_token_reserve,
        }


def structured_prompt_reserve_v1(
    *,
    messages: Sequence[Mapping[str, Any]],
    response_schema: Mapping[str, Any],
    output_token_cap: int,
    include_schema_transport_overhead: bool = True,
) -> StructuredPromptReserve:
    if not isinstance(output_token_cap, int) or isinstance(output_token_cap, bool):
        raise ValueError("output_token_cap must be an integer")
    if output_token_cap < 0:
        raise ValueError("output_token_cap cannot be negative")
    if not isinstance(include_schema_transport_overhead, bool):
        raise ValueError("include_schema_transport_overhead must be bool")
    message_estimate = estimate_registry_prompt_tokens(messages)
    schema_bytes = len(
        json.dumps(
            response_schema,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    # CKEY/Gemini provider accounting includes serialized structured-schema
    # overhead that the message-only estimator cannot observe. Measured B0
    # traffic used at most 0.47 provider prompt tokens per schema byte; one
    # token per two bytes plus bounded headroom stays conservative without
    # rejecting the accepted chapter-2 request (13,103 observed prompt tokens).
    schema_token_reserve = (
        math.ceil(schema_bytes / 2) if include_schema_transport_overhead else 0
    )
    transport_basis = message_estimate + schema_token_reserve
    prompt_reserve = math.ceil(transport_basis * 1.15) + 256
    return StructuredPromptReserve(
        message_token_estimate=message_estimate,
        response_schema_utf8_bytes=schema_bytes,
        prompt_token_reserve=prompt_reserve,
        output_token_cap=output_token_cap,
    )


__all__ = ["StructuredPromptReserve", "structured_prompt_reserve_v1"]
