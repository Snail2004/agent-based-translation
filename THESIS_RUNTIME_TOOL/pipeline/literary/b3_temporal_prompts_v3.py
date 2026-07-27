"""Cross-chapter prompt revision for Literary B3 temporal memory."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from pipeline.literary.b3_temporal_prompts_v2 import (
    B3_TEMPORAL_SYSTEM_PROMPT_V2,
    b3_temporal_response_schema_v2,
)


B3_TEMPORAL_PROMPT_ID_V3 = "literary_b3_temporal_state_batch_v3"

B3_TEMPORAL_SYSTEM_PROMPT_V3 = B3_TEMPORAL_SYSTEM_PROMPT_V2.replace(
    "Prompt version: literary_b3_temporal_state_batch_v2.",
    "Prompt version: literary_b3_temporal_state_batch_v3.",
    1,
) + """

Cross-chapter context has two separate authority classes:
- prior_open_states are effective states from earlier accepted batches or
  chapters. They may be reinforced, changed, or closed when current evidence
  supports that operation.
- prior_pending_cases are unresolved proposals supplied only so current evidence
  can be interpreted cautiously. They are not facts, are not predecessors, and
  must never be copied into an authoritative state merely because they appear in
  context. Current evidence may independently justify a state action or another
  pending review.

Batches from the same chapter run in sequence. The current request contains the
latest accepted prior_open_states. Do not assume an omitted state or pending case
was rejected; bounded code supplies only rows relevant to this component.
"""


def b3_temporal_response_schema_v3() -> dict[str, Any]:
    return deepcopy(b3_temporal_response_schema_v2())


__all__ = [
    "B3_TEMPORAL_PROMPT_ID_V3",
    "B3_TEMPORAL_SYSTEM_PROMPT_V3",
    "b3_temporal_response_schema_v3",
]
