"""Pipeline-owned semantic identity separated from per-attempt API transport."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from pipeline.literary.checkpoint import canonical_hash


SEMANTIC_STAGE_IDENTITY_SCHEMA_VERSION_V1 = "literary_semantic_stage_identity_v1"


class LiterarySemanticIdentityError(RuntimeError):
    pass


def build_literary_semantic_stage_identity_v1(
    *,
    shared_runtime: Any,
    role_id: str,
    prompt_id: str,
    prompt_sha256: str,
    response_schema_sha256: str,
    validator_ref: Mapping[str, str],
    application_contract_id: str,
    application_contract_revision: str,
    context_contract: Mapping[str, Any],
) -> dict[str, Any]:
    """Seal model and semantic behavior while deliberately excluding API source."""

    preset = shared_runtime.role_preset_for(role_id)
    body = {
        "schema_version": SEMANTIC_STAGE_IDENTITY_SCHEMA_VERSION_V1,
        "role_id": _text(role_id, "role_id"),
        "preset_id": _text(preset.preset_id, "preset_id"),
        "preset_revision": _text(preset.preset_revision, "preset_revision"),
        "requested_model_id": _text(
            preset.requested_model_id, "requested_model_id"
        ),
        "generation": deepcopy(dict(preset.generation)),
        "limits": deepcopy(dict(preset.limits)),
        "transport_retry_policy": deepcopy(dict(preset.transport_retry)),
        "semantic_retry_policy": deepcopy(dict(preset.semantic_retry)),
        "prompt": {
            "id": _text(prompt_id, "prompt_id"),
            "sha256": _sha(prompt_sha256, "prompt_sha256"),
        },
        "response_contract": {
            "canonical_schema_sha256": _sha(
                response_schema_sha256, "response_schema_sha256"
            ),
            "validator": _validator(validator_ref),
        },
        "application_contract": {
            "id": _text(application_contract_id, "application_contract_id"),
            "revision": _text(
                application_contract_revision, "application_contract_revision"
            ),
        },
        "context_contract": deepcopy(dict(context_contract)),
        "source_or_credential_bound": False,
        "resume_transport_policy": (
            "replacement_source_must_expose_sealed_model_and_compatible_capability"
        ),
    }
    return {**body, "semantic_identity_hash": canonical_hash(body)}


def verify_literary_semantic_stage_identity_v1(
    *, expected: Mapping[str, Any], observed: Mapping[str, Any]
) -> None:
    left = deepcopy(dict(expected))
    right = deepcopy(dict(observed))
    for row, label in ((left, "expected"), (right, "observed")):
        claimed = row.pop("semantic_identity_hash", None)
        if claimed != canonical_hash(row):
            raise LiterarySemanticIdentityError(
                f"{label} Literary semantic identity hash mismatch"
            )
    if left != right:
        raise LiterarySemanticIdentityError(
            "Literary semantic stage changed across resume"
        )


def _validator(value: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise LiterarySemanticIdentityError("validator_ref must be an object")
    if set(value) != {"id", "revision", "sha256"}:
        raise LiterarySemanticIdentityError("validator_ref keys differ")
    return {
        "id": _text(value["id"], "validator id"),
        "revision": _text(value["revision"], "validator revision"),
        "sha256": _sha(value["sha256"], "validator sha256"),
    }


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LiterarySemanticIdentityError(f"{label} must be non-empty text")
    return value


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise LiterarySemanticIdentityError(f"{label} must be SHA-256 text")
    try:
        int(value, 16)
    except ValueError as exc:
        raise LiterarySemanticIdentityError(f"{label} must be SHA-256 text") from exc
    return value.lower()


__all__ = [
    "LiterarySemanticIdentityError",
    "SEMANTIC_STAGE_IDENTITY_SCHEMA_VERSION_V1",
    "build_literary_semantic_stage_identity_v1",
    "verify_literary_semantic_stage_identity_v1",
]
