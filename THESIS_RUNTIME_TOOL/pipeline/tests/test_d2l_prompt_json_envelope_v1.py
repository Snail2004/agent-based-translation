from __future__ import annotations

import pytest

from pipeline.translate.d2l_prompt_json_envelope_v1 import (
    normalize_prompt_json_envelope,
)


def test_unwraps_one_whole_response_json_fence() -> None:
    source = '  ```json\n{"translations":{"T01":"Ban dich."}}\n```\n'

    normalized, changed = normalize_prompt_json_envelope(source)

    assert changed is True
    assert normalized == '{"translations":{"T01":"Ban dich."}}'


@pytest.mark.parametrize(
    "source",
    [
        '{"translations":{"T01":"Ban dich."}}',
        'Prose\n```json\n{}\n```',
        '```python\n{}\n```',
        '```json\n{}\n```\n```json\n{}\n```',
        '```json\n{"x":"```"}\n```',
        '```json\n{}',
    ],
)
def test_leaves_every_non_exact_envelope_unchanged(source: str) -> None:
    normalized, changed = normalize_prompt_json_envelope(source)

    assert changed is False
    assert normalized == source
