"""Conservative pre-call token gate for rendered Literary requests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from pipeline.literary.model_ref_v1 import (
    MODEL_REF_MODE_CLASSIFIED_V1,
    model_ref_instruction_v1,
    project_model_request_v1,
)
from pipeline.literary.structured_prompt_reserve_v1 import (
    structured_prompt_reserve_v1,
)


class LiteraryRequestTokenPreflightError(ValueError):
    pass


@dataclass(frozen=True)
class LiteraryRequestTokenPreflightV1:
    prompt_token_cap: int
    message_token_estimate: int
    response_schema_utf8_bytes: int
    prompt_token_reserve: int
    output_token_cap: int
    total_token_reserve: int
    model_reference_mode: str
    projected_request_fingerprint: str

    @property
    def fits_prompt_cap(self) -> bool:
        return self.prompt_token_reserve <= self.prompt_token_cap

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": "literary_request_token_preflight_v1",
            "model_reference_mode": self.model_reference_mode,
            "message_token_estimate": self.message_token_estimate,
            "response_schema_utf8_bytes": self.response_schema_utf8_bytes,
            "prompt_token_reserve": self.prompt_token_reserve,
            "prompt_token_cap": self.prompt_token_cap,
            "output_token_cap": self.output_token_cap,
            "total_token_reserve": self.total_token_reserve,
            "fits_prompt_cap": self.fits_prompt_cap,
            "projected_request_fingerprint": self.projected_request_fingerprint,
        }


def measure_literary_request_token_preflight_v1(
    request: Mapping[str, Any],
    *,
    prompt_token_cap: int,
    output_token_cap: int,
    model_reference_mode: str | None = MODEL_REF_MODE_CLASSIFIED_V1,
) -> LiteraryRequestTokenPreflightV1:
    """Measure the exact model-visible envelope used by the selected mode."""

    if not isinstance(prompt_token_cap, int) or isinstance(prompt_token_cap, bool):
        raise LiteraryRequestTokenPreflightError("prompt_token_cap must be an integer")
    if prompt_token_cap <= 0:
        raise LiteraryRequestTokenPreflightError("prompt_token_cap must be positive")
    if not isinstance(output_token_cap, int) or isinstance(output_token_cap, bool):
        raise LiteraryRequestTokenPreflightError("output_token_cap must be an integer")
    if output_token_cap < 0:
        raise LiteraryRequestTokenPreflightError("output_token_cap cannot be negative")
    if not isinstance(request, Mapping):
        raise LiteraryRequestTokenPreflightError("request must be an object")

    if model_reference_mode is None:
        projected = dict(request)
        effective_mode = "persistent"
    elif model_reference_mode == MODEL_REF_MODE_CLASSIFIED_V1:
        try:
            projected, _ref_map = project_model_request_v1(
                request,
                instruction=model_ref_instruction_v1(),
            )
        except (TypeError, ValueError) as exc:
            raise LiteraryRequestTokenPreflightError(str(exc)) from exc
        effective_mode = MODEL_REF_MODE_CLASSIFIED_V1
    else:
        raise LiteraryRequestTokenPreflightError(
            "unsupported Literary model-reference mode"
        )

    messages = projected.get("messages")
    response_schema = projected.get("response_schema")
    fingerprint = projected.get("request_fingerprint")
    if not isinstance(messages, list) or not all(
        isinstance(row, Mapping) for row in messages
    ):
        raise LiteraryRequestTokenPreflightError("projected messages are malformed")
    if not isinstance(response_schema, Mapping):
        raise LiteraryRequestTokenPreflightError(
            "projected response schema is malformed"
        )
    if not isinstance(fingerprint, str) or len(fingerprint) != 64:
        raise LiteraryRequestTokenPreflightError(
            "projected request fingerprint is malformed"
        )

    reserve = structured_prompt_reserve_v1(
        messages=messages,
        response_schema=response_schema,
        output_token_cap=output_token_cap,
    )
    return LiteraryRequestTokenPreflightV1(
        prompt_token_cap=prompt_token_cap,
        message_token_estimate=reserve.message_token_estimate,
        response_schema_utf8_bytes=reserve.response_schema_utf8_bytes,
        prompt_token_reserve=reserve.prompt_token_reserve,
        output_token_cap=reserve.output_token_cap,
        total_token_reserve=reserve.total_token_reserve,
        model_reference_mode=effective_mode,
        projected_request_fingerprint=fingerprint,
    )


def require_literary_request_within_prompt_cap_v1(
    request: Mapping[str, Any],
    *,
    role_id: str,
    prompt_token_cap: int,
    output_token_cap: int,
    model_reference_mode: str | None = MODEL_REF_MODE_CLASSIFIED_V1,
) -> LiteraryRequestTokenPreflightV1:
    preflight = measure_literary_request_token_preflight_v1(
        request,
        prompt_token_cap=prompt_token_cap,
        output_token_cap=output_token_cap,
        model_reference_mode=model_reference_mode,
    )
    if not preflight.fits_prompt_cap:
        raise LiteraryRequestTokenPreflightError(
            f"{role_id} prompt reserve {preflight.prompt_token_reserve} exceeds "
            f"input cap {preflight.prompt_token_cap}"
        )
    return preflight


__all__ = [
    "LiteraryRequestTokenPreflightError",
    "LiteraryRequestTokenPreflightV1",
    "measure_literary_request_token_preflight_v1",
    "require_literary_request_within_prompt_cap_v1",
]
