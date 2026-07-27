"""Many-parked-identity Literary B3 temporal prompt and response schema."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from pipeline.literary.b3_temporal_prompts_v6 import (
    B3_TEMPORAL_SYSTEM_PROMPT_V6,
    b3_temporal_response_schema_v6,
)


B3_TEMPORAL_PROMPT_ID_V7 = "literary_b3_temporal_state_batch_v7"

B3_TEMPORAL_SYSTEM_PROMPT_V7 = B3_TEMPORAL_SYSTEM_PROMPT_V6.replace(
    "Prompt version: literary_b3_temporal_state_batch_v6.",
    "Prompt version: literary_b3_temporal_state_batch_v7.",
    1,
) + """

Some supplied referents may carry `parked_identities`, a list of unresolved
cross-chapter identity hearings. These hearings are distinct questions and
must all be preserved. Never reopen, merge, split, or adjudicate any parked
identity question. Record durable state about a referent as usual. Only when
the state genuinely depends on one or more parked identity questions, attach
`inherited_parked_identities` containing exactly the applicable supplied
hearing_component_id and unchanged resolution_condition pairs. An empty list
means the proposed state does not depend on parked identity.
"""


def b3_temporal_response_schema_v7() -> dict[str, Any]:
    schema = deepcopy(b3_temporal_response_schema_v6())
    schema["properties"]["schema_version"]["const"] = (
        "literary_b3_temporal_response_v3"
    )
    item = schema["properties"]["component_results"]["items"]
    required = list(item.get("required") or [])
    required = [
        value
        for value in required
        if value != "inherited_parked_identity"
    ]
    if "inherited_parked_identities" not in required:
        required.append("inherited_parked_identities")
    item["required"] = required
    properties = item["properties"]
    properties.pop("inherited_parked_identity", None)
    properties["inherited_parked_identities"] = {
        "type": "array",
        "maxItems": 32,
        "items": {
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
    }
    return schema


__all__ = [
    "B3_TEMPORAL_PROMPT_ID_V7",
    "B3_TEMPORAL_SYSTEM_PROMPT_V7",
    "b3_temporal_response_schema_v7",
]
