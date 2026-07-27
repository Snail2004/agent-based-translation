"""Test-only helpers for model-facing Literary capability-probe payloads."""

from __future__ import annotations

from typing import Any, Mapping

from pipeline.literary.model_ref_v1 import (
    MODEL_REF_FIELDS_V1,
    project_id_fields,
)


def model_facing_probe_payload_v1(
    plan: Any,
    persistent_payload: Mapping[str, Any],
) -> dict[str, Any]:
    projected = project_id_fields(
        persistent_payload,
        ref_map=plan.request["model_ref_map"],
        field_names_by_namespace=MODEL_REF_FIELDS_V1,
    )
    return _strip_code_owned_fields(projected)


def _strip_code_owned_fields(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _strip_code_owned_fields(child)
            for key, child in value.items()
            if not _is_code_owned_field(str(key))
        }
    if isinstance(value, list):
        return [_strip_code_owned_fields(child) for child in value]
    return value


def _is_code_owned_field(name: str) -> bool:
    return (
        name == "request_fingerprint"
        or name.endswith("_fingerprint")
        or name.endswith("_hash")
        or name.endswith("_sha256")
    )


__all__ = ["model_facing_probe_payload_v1"]
