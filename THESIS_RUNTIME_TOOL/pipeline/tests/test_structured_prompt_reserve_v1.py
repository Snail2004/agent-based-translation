from __future__ import annotations

import json

import pytest

from pipeline.literary.structured_prompt_reserve_v1 import (
    structured_prompt_reserve_v1,
)


def test_structured_reserve_accounts_for_response_schema_transport_bytes() -> None:
    messages = [{"role": "user", "content": "Inspect this source."}]
    response_schema = {
        "type": "object",
        "properties": {
            "rows": {
                "type": "array",
                "items": {
                    "type": "object",
                    "description": "x" * 3500,
                    "properties": {"surface": {"type": "string"}},
                },
            }
        },
    }

    reserve = structured_prompt_reserve_v1(
        messages=messages,
        response_schema=response_schema,
        output_token_cap=4096,
    )

    expected_schema_bytes = len(
        json.dumps(
            response_schema,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    assert reserve.response_schema_utf8_bytes == expected_schema_bytes
    assert reserve.prompt_token_reserve > reserve.message_token_estimate
    assert reserve.prompt_token_reserve > expected_schema_bytes // 2
    assert reserve.total_token_reserve == reserve.prompt_token_reserve + 4096


def test_structured_reserve_can_omit_unsent_schema_transport_overhead() -> None:
    messages = [{"role": "user", "content": "Inspect this source."}]
    response_schema = {
        "type": "object",
        "properties": {
            "rows": {
                "type": "array",
                "items": {
                    "type": "object",
                    "description": "x" * 3500,
                },
            }
        },
    }

    native = structured_prompt_reserve_v1(
        messages=messages,
        response_schema=response_schema,
        output_token_cap=4096,
    )
    prompt_only = structured_prompt_reserve_v1(
        messages=messages,
        response_schema=response_schema,
        output_token_cap=4096,
        include_schema_transport_overhead=False,
    )

    assert prompt_only.response_schema_utf8_bytes == native.response_schema_utf8_bytes
    assert prompt_only.message_token_estimate == native.message_token_estimate
    assert prompt_only.prompt_token_reserve < native.prompt_token_reserve
    assert prompt_only.total_token_reserve == prompt_only.prompt_token_reserve + 4096


@pytest.mark.parametrize("output_cap", [-1, True, 1.5])
def test_structured_reserve_rejects_invalid_output_cap(output_cap: object) -> None:
    with pytest.raises(ValueError):
        structured_prompt_reserve_v1(
            messages=[{"role": "user", "content": "source"}],
            response_schema={"type": "object"},
            output_token_cap=output_cap,  # type: ignore[arg-type]
        )


def test_structured_reserve_rejects_non_boolean_schema_overhead_flag() -> None:
    with pytest.raises(ValueError):
        structured_prompt_reserve_v1(
            messages=[{"role": "user", "content": "source"}],
            response_schema={"type": "object"},
            output_token_cap=32,
            include_schema_transport_overhead=1,  # type: ignore[arg-type]
        )
