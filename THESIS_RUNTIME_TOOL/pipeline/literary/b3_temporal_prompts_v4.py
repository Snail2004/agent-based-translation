"""Packet-deduplicated cross-chapter prompt for Literary B3 temporal memory."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from pipeline.literary.b3_temporal_prompts_v3 import (
    B3_TEMPORAL_SYSTEM_PROMPT_V3,
    b3_temporal_response_schema_v3,
)


B3_TEMPORAL_PROMPT_ID_V4 = "literary_b3_temporal_state_batch_v4"

B3_TEMPORAL_SYSTEM_PROMPT_V4 = B3_TEMPORAL_SYSTEM_PROMPT_V3.replace(
    "Prompt version: literary_b3_temporal_state_batch_v3.",
    "Prompt version: literary_b3_temporal_state_batch_v4.",
    1,
) + """

Prior temporal context is packetized once per request to avoid repeating the
same row in several components:
- prior_state_packets contain one effective state plus the component_ids where
  that state is relevant;
- prior_pending_packets contain one non-authoritative pending case plus the
  component_ids where it is relevant.

Use component_ids as an explicit pre-join. Do not infer relevance outside those
lists, and do not treat a pending packet as an effective state.
"""


def b3_temporal_response_schema_v4() -> dict[str, Any]:
    return deepcopy(b3_temporal_response_schema_v3())


__all__ = [
    "B3_TEMPORAL_PROMPT_ID_V4",
    "B3_TEMPORAL_SYSTEM_PROMPT_V4",
    "b3_temporal_response_schema_v4",
]
