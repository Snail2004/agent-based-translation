"""Strict normalization for prompt-generated JSON wrapped in one Markdown fence."""

from __future__ import annotations

import re


POLICY_ID = "d2l_prompt_json_single_fence_v1"

_SINGLE_JSON_FENCE_RE = re.compile(
    r"\A[ \t\r\n]*```json[ \t]*\r?\n"
    r"(?P<body>[\s\S]*?)"
    r"\r?\n```[ \t]*[ \t\r\n]*\Z",
    re.IGNORECASE,
)


def normalize_prompt_json_envelope(text: str) -> tuple[str, bool]:
    """Unwrap exactly one whole-response JSON fence; leave every other form alone."""

    value = str(text)
    match = _SINGLE_JSON_FENCE_RE.fullmatch(value)
    if match is None:
        return value, False
    body = match.group("body")
    if "```" in body:
        return value, False
    return body, True


__all__ = ["POLICY_ID", "normalize_prompt_json_envelope"]
