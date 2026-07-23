"""Narrow normalization for one JSON object with harmless trailing prose."""

from __future__ import annotations

import json
import re

from pipeline.translate.d2l_prompt_json_envelope_v1 import (
    normalize_prompt_json_envelope as normalize_single_fence,
)


POLICY_ID = "d2l_prompt_json_single_object_v2"
MAX_TRAILING_COMMENT_CHARS = 160

_FORBIDDEN_TRAILING_CHAR_RE = re.compile(r'[,{}\[\]"`]')
_JSON_PRIMITIVE_RE = re.compile(
    r"(?:true|false|null|-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?)\Z"
)


def normalize_prompt_json_envelope(text: str) -> tuple[str, bool]:
    """Return one unambiguous JSON object and discard only harmless tail prose.

    V1's exact whole-response JSON fence remains accepted. Outside that form, the
    response must begin with a valid JSON object. A short, single-line suffix may
    be discarded only when it cannot be another JSON value, a code fence, or a
    protected reference. The canonical local validator still decides whether the
    extracted object satisfies the Translator contract.
    """

    value = str(text)
    unwrapped, fence_changed = normalize_single_fence(value)
    if fence_changed:
        return unwrapped, True

    candidate = value.lstrip()
    if not candidate.startswith("{"):
        return value, False

    try:
        parsed, end = json.JSONDecoder().raw_decode(candidate)
    except json.JSONDecodeError:
        return value, False
    if not isinstance(parsed, dict):
        return value, False

    trailing = candidate[end:].strip()
    if not trailing:
        return value, False
    if len(trailing) > MAX_TRAILING_COMMENT_CHARS:
        return value, False
    if "\n" in trailing or "\r" in trailing:
        return value, False
    if any(_is_forbidden_control(char) for char in trailing):
        return value, False
    if _FORBIDDEN_TRAILING_CHAR_RE.search(trailing):
        return value, False
    if "[[" in trailing or "]]" in trailing:
        return value, False
    if _JSON_PRIMITIVE_RE.fullmatch(trailing):
        return value, False

    return candidate[:end], True


def _is_forbidden_control(char: str) -> bool:
    codepoint = ord(char)
    return (
        0 <= codepoint <= 8
        or codepoint in {11, 12}
        or 14 <= codepoint <= 31
        or codepoint == 127
    )


__all__ = [
    "MAX_TRAILING_COMMENT_CHARS",
    "POLICY_ID",
    "normalize_prompt_json_envelope",
]
