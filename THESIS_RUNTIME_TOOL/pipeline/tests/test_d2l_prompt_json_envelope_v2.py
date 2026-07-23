from __future__ import annotations

import pytest

from pipeline.translate.d2l_prompt_json_envelope_v2 import (
    MAX_TRAILING_COMMENT_CHARS,
    normalize_prompt_json_envelope,
)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            '{"translations":{"T01":"Ban dich."}} Converted to JSON._\n',
            '{"translations":{"T01":"Ban dich."}}',
        ),
        (
            '{"translations":{"T01":"Ban dich."}} Sungguh-sungguh._\n',
            '{"translations":{"T01":"Ban dich."}}',
        ),
        (
            '```json\n{"translations":{"T01":"Ban dich."}}\n```',
            '{"translations":{"T01":"Ban dich."}}',
        ),
    ],
)
def test_normalizes_one_object_or_one_exact_fence(
    source: str,
    expected: str,
) -> None:
    normalized, changed = normalize_prompt_json_envelope(source)

    assert changed is True
    assert normalized == expected


@pytest.mark.parametrize(
    "source",
    [
        '{"translations":{"T01":"Ban dich."}}',
        'Preamble {"translations":{"T01":"Ban dich."}}',
        '{"translations":{"T01":"Ban dich."}} {"extra":true}',
        '{"translations":{"T01":"Ban dich."}} ["extra"]',
        '{"translations":{"T01":"Ban dich."}} "extra"',
        '{"translations":{"T01":"Ban dich."}} null',
        '{"translations":{"T01":"Ban dich."}} ```',
        '{"translations":{"T01":"Ban dich."}} [[MATH_REF_0001]]',
        '{"translations":{"T01":"Ban dich."}} first line\nsecond line',
        '{"translations":{"T01":"Ban dich."}} ' + "x" * (MAX_TRAILING_COMMENT_CHARS + 1),
        '{"translations":{"T01":"Ban dich."}',
    ],
)
def test_rejects_ambiguous_or_malformed_envelopes(source: str) -> None:
    normalized, changed = normalize_prompt_json_envelope(source)

    assert changed is False
    assert normalized == source
