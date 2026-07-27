"""Narrow normalization for request-bound Literary response echoes.

Only routing coordinates whose value is uniquely owned by code may be
replaced here. Semantic references, schema identifiers, hashes, fingerprints,
and lineage fields remain the responsibility of their existing validators.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from pipeline.literary.checkpoint import canonical_hash


CODE_OWNED_RESPONSE_ECHO_FIELDS_V1 = frozenset(
    {"chapter_id", "window_id", "batch_id"}
)
NORMALIZATION_KIND_V1 = "code_owned_response_echo_replaced"


class LiteraryResponseNormalizationError(ValueError):
    pass


def normalize_code_owned_response_echoes_v1(
    response: Mapping[str, Any],
    *,
    expected: Mapping[str, str],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Replace only present, mismatched, unambiguous code-owned echoes.

    Missing fields are not invented: the unchanged response schema remains
    responsible for required-field validation. Callers must explicitly name
    every field they want normalized, and the closed allowlist prevents this
    helper from being used for semantic IDs or lineage material.
    """

    if not isinstance(response, Mapping):
        raise LiteraryResponseNormalizationError("response must be an object")
    unknown = set(expected) - CODE_OWNED_RESPONSE_ECHO_FIELDS_V1
    if unknown:
        raise LiteraryResponseNormalizationError(
            "response normalization requested non-echo fields: "
            + ", ".join(sorted(unknown))
        )

    normalized = deepcopy(dict(response))
    notes: list[dict[str, Any]] = []
    for field in sorted(expected):
        expected_value = expected[field]
        if not isinstance(expected_value, str) or not expected_value.strip():
            raise LiteraryResponseNormalizationError(
                f"expected {field} must be a non-empty string"
            )
        if field not in normalized or normalized[field] == expected_value:
            continue
        observed = normalized[field]
        normalized[field] = expected_value
        notes.append(
            {
                "normalization_kind": NORMALIZATION_KIND_V1,
                "field": field,
                "observed_value_sha256": canonical_hash({"value": observed}),
                "normalized_value": expected_value,
                "reason": "request_bound_code_owned_echo",
            }
        )
    return normalized, notes


def attach_response_normalization_notes_v1(
    artifact_body: Mapping[str, Any],
    notes: list[dict[str, Any]],
) -> dict[str, Any]:
    """Attach optional validator metadata without changing clean artifacts."""

    body = deepcopy(dict(artifact_body))
    if notes:
        body["response_normalization_notes"] = deepcopy(notes)
    return body


def split_validated_response_normalization_notes_v1(
    artifact_body: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Separate optional notes after validating their non-semantic shape."""

    core = deepcopy(dict(artifact_body))
    raw_notes = core.pop("response_normalization_notes", None)
    if raw_notes is None:
        return core, []
    if not isinstance(raw_notes, list) or not raw_notes:
        raise LiteraryResponseNormalizationError(
            "response normalization notes must be a non-empty list"
        )

    notes: list[dict[str, Any]] = []
    seen: set[tuple[str, str | None]] = set()
    required = {
        "normalization_kind",
        "field",
        "observed_value_sha256",
        "normalized_value",
        "reason",
    }
    for raw_note in raw_notes:
        if not isinstance(raw_note, Mapping):
            raise LiteraryResponseNormalizationError(
                "response normalization note must be an object"
            )
        note = deepcopy(dict(raw_note))
        if set(note) not in (required, required | {"field_path"}):
            raise LiteraryResponseNormalizationError(
                "response normalization note keys differ"
            )
        field = note["field"]
        if field not in CODE_OWNED_RESPONSE_ECHO_FIELDS_V1:
            raise LiteraryResponseNormalizationError(
                "response normalization note names a non-echo field"
            )
        if (
            note["normalization_kind"] != NORMALIZATION_KIND_V1
            or note["reason"] != "request_bound_code_owned_echo"
            or note["normalized_value"] != core.get(field)
        ):
            raise LiteraryResponseNormalizationError(
                "response normalization note does not match artifact identity"
            )
        observed_hash = note["observed_value_sha256"]
        if not (
            isinstance(observed_hash, str)
            and len(observed_hash) == 64
            and all(character in "0123456789abcdef" for character in observed_hash)
        ):
            raise LiteraryResponseNormalizationError(
                "response normalization note has an invalid observed hash"
            )
        field_path = note.get("field_path")
        if field_path is not None and not (
            isinstance(field_path, str)
            and field_path.startswith("/")
            and field_path.endswith(f"/{field}")
        ):
            raise LiteraryResponseNormalizationError(
                "response normalization note has an invalid field path"
            )
        identity = (field, field_path)
        if identity in seen:
            raise LiteraryResponseNormalizationError(
                "response normalization notes repeat a field path"
            )
        seen.add(identity)
        notes.append(note)
    return core, notes


__all__ = [
    "CODE_OWNED_RESPONSE_ECHO_FIELDS_V1",
    "LiteraryResponseNormalizationError",
    "NORMALIZATION_KIND_V1",
    "attach_response_normalization_notes_v1",
    "normalize_code_owned_response_echoes_v1",
    "split_validated_response_normalization_notes_v1",
]
