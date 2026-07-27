"""Review-packet-aware Literary B3 temporal prompt and response schema."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from pipeline.literary.b3_temporal_prompts_v5 import (
    B3_TEMPORAL_SYSTEM_PROMPT_V5,
    b3_temporal_response_schema_v5,
)


B3_TEMPORAL_PROMPT_ID_V6 = "literary_b3_temporal_state_batch_v6"

B3_TEMPORAL_SYSTEM_PROMPT_V6 = B3_TEMPORAL_SYSTEM_PROMPT_V5.replace(
    "Prompt version: literary_b3_temporal_state_batch_v5.",
    "Prompt version: literary_b3_temporal_state_batch_v6.",
    1,
) + """

The request packetizes repeated B2 review context. Each
`b2_review_packets[]` row carries the shared review once and lists its
component-specific evidence in `component_bindings`. Each component names its
applicable rows in `review_ids`. While deciding a component, use only the
binding whose `component_id` matches that component; do not borrow a block or
referent from another binding. This layout changes no review meaning or
authority.
"""


def b3_temporal_response_schema_v6() -> dict[str, Any]:
    return deepcopy(b3_temporal_response_schema_v5())


__all__ = [
    "B3_TEMPORAL_PROMPT_ID_V6",
    "B3_TEMPORAL_SYSTEM_PROMPT_V6",
    "b3_temporal_response_schema_v6",
]
