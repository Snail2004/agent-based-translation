"""Parked-identity-aware Literary B3 temporal prompt and response schema."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from pipeline.literary.b3_temporal_prompts_v4 import (
    B3_TEMPORAL_SYSTEM_PROMPT_V4,
    b3_temporal_response_schema_v4,
)


B3_TEMPORAL_PROMPT_ID_V5 = "literary_b3_temporal_state_batch_v5"

B3_TEMPORAL_SYSTEM_PROMPT_V5 = B3_TEMPORAL_SYSTEM_PROMPT_V4.replace(
    "Prompt version: literary_b3_temporal_state_batch_v4.",
    "Prompt version: literary_b3_temporal_state_batch_v5.",
    1,
) + """

Some supplied referents carry a `parked_identity`: a cross-chapter identity
question a prior hearing already heard and could not settle, with a stated
`resolution_condition`. You must not re-open that question. Do not raise an
`identity_review` for a referent whose ambiguity is that parked identity.
Record the durable state you observe about the referent as usual; if the state
genuinely depends on the parked identity, attach `inherited_parked_identity`
with the hearing_component_id and carry its resolution_condition forward
unchanged. Never re-adjudicate, merge, or split a parked identity.
"""


def b3_temporal_response_schema_v5() -> dict[str, Any]:
    schema = deepcopy(b3_temporal_response_schema_v4())
    schema["properties"]["schema_version"]["const"] = (
        "literary_b3_temporal_response_v2"
    )
    result = schema["properties"]["component_results"]["items"]
    result["required"].append("inherited_parked_identity")
    result["properties"]["pending_route"]["enum"].append(
        "inherited_identity_block"
    )
    result["properties"]["inherited_parked_identity"] = {
        "oneOf": [
            {"type": "null"},
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["hearing_component_id", "resolution_condition"],
                "properties": {
                    "hearing_component_id": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 200,
                    },
                    "resolution_condition": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 1200,
                    },
                },
            },
        ]
    }
    return schema


__all__ = [
    "B3_TEMPORAL_PROMPT_ID_V5",
    "B3_TEMPORAL_SYSTEM_PROMPT_V5",
    "b3_temporal_response_schema_v5",
]
