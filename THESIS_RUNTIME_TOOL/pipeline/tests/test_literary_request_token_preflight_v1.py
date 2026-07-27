from __future__ import annotations

from copy import deepcopy

import pytest

from pipeline.literary.request_token_preflight_v1 import (
    LiteraryRequestTokenPreflightError,
    measure_literary_request_token_preflight_v1,
    require_literary_request_within_prompt_cap_v1,
)


def _request() -> dict:
    return {
        "messages": [
            {"role": "system", "content": "Return only the requested JSON."},
            {
                "role": "user",
                "content": '{"chapter_id":"bk_ch01","value":"North House"}',
            },
        ],
        "response_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["value"],
            "properties": {"value": {"type": "string"}},
        },
        "request_fingerprint": "a" * 64,
    }


def test_preflight_measures_projected_request_without_mutating_input() -> None:
    request = _request()
    before = deepcopy(request)
    preflight = measure_literary_request_token_preflight_v1(
        request,
        prompt_token_cap=20_000,
        output_token_cap=512,
    )

    assert request == before
    assert preflight.fits_prompt_cap is True
    assert preflight.prompt_token_reserve > preflight.message_token_estimate
    assert preflight.total_token_reserve == (
        preflight.prompt_token_reserve + 512
    )
    assert len(preflight.projected_request_fingerprint) == 64


def test_preflight_rejects_before_call_with_both_numbers() -> None:
    measured = measure_literary_request_token_preflight_v1(
        _request(), prompt_token_cap=20_000, output_token_cap=512
    )
    cap = measured.prompt_token_reserve - 1

    with pytest.raises(
        LiteraryRequestTokenPreflightError,
        match=rf"reserve {measured.prompt_token_reserve} exceeds input cap {cap}",
    ):
        require_literary_request_within_prompt_cap_v1(
            _request(),
            role_id="literary.test.role",
            prompt_token_cap=cap,
            output_token_cap=512,
        )


def test_preflight_rejects_unknown_reference_mode() -> None:
    with pytest.raises(
        LiteraryRequestTokenPreflightError,
        match="unsupported Literary model-reference mode",
    ):
        measure_literary_request_token_preflight_v1(
            _request(),
            prompt_token_cap=20_000,
            output_token_cap=512,
            model_reference_mode="silent_fallback",
        )
