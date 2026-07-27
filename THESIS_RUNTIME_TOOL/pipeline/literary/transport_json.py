from __future__ import annotations

import json
from typing import Any


class LiteraryTransportJsonError(ValueError):
    """Raised when a structured response is not one complete JSON payload."""


def parse_structured_response(text: str) -> tuple[Any, str]:
    """Parse strict JSON or one whole-response Markdown JSON fence."""

    try:
        return json.loads(text), "strict_json"
    except (TypeError, ValueError) as strict_error:
        stripped = str(text).strip()
        lines = stripped.splitlines()
        if (
            len(lines) < 3
            or lines[0].strip().lower() not in {"```json", "```"}
            or lines[-1].strip() != "```"
        ):
            raise LiteraryTransportJsonError(
                "response is neither strict JSON nor one complete JSON fence"
            ) from strict_error
        body = "\n".join(lines[1:-1])
        try:
            return json.loads(body), "single_json_fence"
        except (TypeError, ValueError) as fenced_error:
            raise LiteraryTransportJsonError(
                "single JSON fence contains invalid JSON"
            ) from fenced_error
