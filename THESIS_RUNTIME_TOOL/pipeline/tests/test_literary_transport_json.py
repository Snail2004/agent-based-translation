from __future__ import annotations

import pytest

from pipeline.literary.transport_json import (
    LiteraryTransportJsonError,
    parse_structured_response,
)


def test_accepts_strict_json_without_normalization() -> None:
    payload, mode = parse_structured_response('{"value":1}')
    assert payload == {"value": 1}
    assert mode == "strict_json"


def test_accepts_one_json_fence_covering_the_whole_response() -> None:
    payload, mode = parse_structured_response('```json\n{"value":1}\n```')
    assert payload == {"value": 1}
    assert mode == "single_json_fence"


@pytest.mark.parametrize(
    "text",
    (
        'Here is the result:\n```json\n{"value":1}\n```',
        '```json\n{"value":1}\n```\n```json\n{"value":2}\n```',
        '```json\n{"value":\n```',
    ),
)
def test_rejects_prose_multiple_blocks_and_invalid_fenced_json(text: str) -> None:
    with pytest.raises(LiteraryTransportJsonError):
        parse_structured_response(text)
