"""Stable wire schema and prompt marker for Literary B3 temporal V2."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from pipeline.literary.b3_temporal_prompts_v1 import (
    B3_TEMPORAL_SYSTEM_PROMPT_V1,
    b3_temporal_response_schema_v1,
)


B3_TEMPORAL_PROMPT_ID_V2 = "literary_b3_temporal_state_batch_v2"

B3_TEMPORAL_SYSTEM_PROMPT_V2 = B3_TEMPORAL_SYSTEM_PROMPT_V1.replace(
    "Prompt version: literary_b3_temporal_state_batch_v1.",
    "Prompt version: literary_b3_temporal_state_batch_v2.",
    1,
) + """

The response schema is stable across batches. The supplied chapter_id,
batch_id, component IDs, referent refs, event IDs, turn IDs, block IDs, and
frame IDs remain closed by local validation. Use only values present in this
request; a syntactically valid foreign reference will be rejected.
"""


def b3_temporal_response_schema_v2() -> dict[str, Any]:
    """Return the stable transport shape; local code binds request-specific IDs."""

    return deepcopy(b3_temporal_response_schema_v1())


__all__ = [
    "B3_TEMPORAL_PROMPT_ID_V2",
    "B3_TEMPORAL_SYSTEM_PROMPT_V2",
    "b3_temporal_response_schema_v2",
]
