"""Shared request-local reference transport for Literary model calls.

Production requests and capability probes must project opaque persistent IDs
to the same request-local labels, resolve those labels before semantic
validation, and bind capability evidence to that exact wire transform.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping, Sequence

from pipeline.literary import model_ref_v1 as model_ref_module
from pipeline.literary.checkpoint import canonical_hash, file_sha256
from pipeline.literary.model_ref_v1 import (
    MODEL_REF_FIELDS_V1,
    MODEL_REF_MAP_SCHEMA_VERSION,
    MODEL_REF_MODE_CLASSIFIED_V1,
    model_ref_fields_hash_v1,
    model_ref_instruction_v1,
    project_model_request_v1,
    resolve_model_response_v1,
)


MODEL_REF_VALIDATOR_BINDING_REVISION = "literary_model_ref_validator_binding_v1"


def project_capability_probe_request_v1(
    request: Mapping[str, Any],
    *,
    field_names_by_namespace: Mapping[str, Sequence[str]] = MODEL_REF_FIELDS_V1,
) -> dict[str, Any]:
    """Return the exact model-facing envelope used by production runtime."""

    projected, _ref_map = project_model_request_v1(
        request,
        field_names_by_namespace=field_names_by_namespace,
        instruction=model_ref_instruction_v1(),
    )
    return projected


def resolve_capability_probe_response_v1(
    *,
    projected_request: Mapping[str, Any],
    response: Mapping[str, Any],
    field_names_by_namespace: Mapping[str, Sequence[str]] = MODEL_REF_FIELDS_V1,
) -> dict[str, Any]:
    """Restore persistent IDs and code-owned echoes before local validation."""

    return resolve_model_response_v1(
        projected_request,
        deepcopy(dict(response)),
        field_names_by_namespace=field_names_by_namespace,
    )


def bind_model_ref_validator_v1(
    validator_ref: Mapping[str, Any],
) -> dict[str, str]:
    """Bind semantic validation to the exact request-local wire transform."""

    identifier = _required_text(validator_ref.get("id"), "validator id")
    revision = _required_text(validator_ref.get("revision"), "validator revision")
    validator_sha256 = _required_sha256(
        validator_ref.get("sha256"), "validator sha256"
    )
    model_ref_path = Path(str(model_ref_module.__file__)).resolve()
    transport_path = Path(__file__).resolve()
    binding = {
        "schema_version": MODEL_REF_VALIDATOR_BINDING_REVISION,
        "semantic_validator": {
            "id": identifier,
            "revision": revision,
            "sha256": validator_sha256,
        },
        "model_reference_mode": MODEL_REF_MODE_CLASSIFIED_V1,
        "model_ref_map_schema_version": MODEL_REF_MAP_SCHEMA_VERSION,
        "model_ref_fields_sha256": model_ref_fields_hash_v1(),
        "model_ref_implementation_sha256": file_sha256(model_ref_path),
        "model_ref_transport_implementation_sha256": file_sha256(transport_path),
    }
    return {
        "id": identifier,
        "revision": f"{revision}.model_ref_v1",
        "sha256": canonical_hash(binding),
    }


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value


def _required_sha256(value: Any, label: str) -> str:
    text = _required_text(value, label)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ValueError(f"{label} must be lowercase SHA-256")
    return text


__all__ = [
    "MODEL_REF_VALIDATOR_BINDING_REVISION",
    "bind_model_ref_validator_v1",
    "project_capability_probe_request_v1",
    "resolve_capability_probe_response_v1",
]
