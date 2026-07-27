"""Shared-backend execution boundary for active Literary runner stages.

This module owns no transport, credential, scheduler, ledger, or cache.  It
binds an already rendered Literary request to an injected Shared LLM Backend
adapter, runs local semantic validation, and persists the accepted application
receipt or a non-authoritative rejection diagnostic.
"""

from __future__ import annotations

from dataclasses import dataclass
import inspect
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from pipeline.llm_backend import canonical_sha256
from pipeline.literary.checkpoint import canonical_json, write_checkpoint_atomic
from pipeline.literary.shared_llm_adapter_v1 import (
    ADAPTER_VERSION,
    LiterarySharedAttemptResult,
    LiterarySharedLlmAttemptAdapter,
    resolve_transport_response_schema,
)
from pipeline.literary.model_ref_v1 import (
    MODEL_REF_FIELDS_V1,
    MODEL_REF_MODE_CLASSIFIED_V1,
    ModelRefError,
    model_ref_fields_hash_v1,
    model_ref_instruction_v1,
    project_model_request_v1,
    resolve_model_response_v1,
)
from pipeline.literary.model_ref_transport_v1 import bind_model_ref_validator_v1
from pipeline.literary.shared_llm_profiles_v1 import (
    PROFILE_ID,
    PROFILE_REVISION,
    ROLE_PRESETS,
)
from pipeline.literary.shared_runtime_profile_v1 import (
    LiterarySharedRuntimeProfileV1,
)
from pipeline.literary.shared_runtime_profile_v2 import (
    LiterarySharedRuntimeProfileV2,
    validate_runtime_source_against_binding_v2,
)


BACKEND_MODE_LEGACY = "legacy"
BACKEND_MODE_SHARED_V1 = "shared_v1"
BACKEND_MODES = frozenset({BACKEND_MODE_LEGACY, BACKEND_MODE_SHARED_V1})
SHARED_ATTEMPT_RECEIPT_SCHEMA_VERSION = "literary_shared_attempt_receipt_v1"
SEMANTIC_REJECTION_DIAGNOSTIC_SCHEMA_VERSION = (
    "literary_semantic_rejection_diagnostic_v1"
)
SEMANTIC_REJECTION_EXCERPT_MAX_UTF8_BYTES = 16_384

SemanticValidator = Callable[[Mapping[str, Any]], Mapping[str, Any]]


class LiterarySharedRunnerError(RuntimeError):
    pass


def capability_binding_key(role_id: str, response_schema: Mapping[str, Any]) -> str:
    return f"{role_id}|{canonical_sha256(response_schema)}"


def build_literary_code_ref_v1(
    *, identifier: str, revision: str, callables: Sequence[Callable[..., Any]]
) -> dict[str, str]:
    """Bind local semantic code without assigning it transport authority."""

    rows: list[dict[str, str]] = []
    for function in callables:
        try:
            source = inspect.getsource(function)
        except (OSError, TypeError) as exc:
            raise LiterarySharedRunnerError(
                f"cannot bind semantic callable source: {function!r}"
            ) from exc
        rows.append(
            {
                "module": str(getattr(function, "__module__", "")),
                "qualname": str(getattr(function, "__qualname__", "")),
                "source_sha256": canonical_sha256(source),
            }
        )
    if not rows or any(not row["module"] or not row["qualname"] for row in rows):
        raise LiterarySharedRunnerError("semantic code reference is incomplete")
    return {
        "id": _required_text(identifier, "code ref id"),
        "revision": _required_text(revision, "code ref revision"),
        "sha256": canonical_sha256(rows),
    }


@dataclass(frozen=True)
class LiterarySharedRunnerBindingsV1:
    adapter: LiterarySharedLlmAttemptAdapter
    api_source: Mapping[str, Any] | None
    capabilities: Mapping[str, Mapping[str, Any]]
    run_id: str
    attempt_run_id: str
    structured_output: Mapping[str, Any] | None
    runtime_profile: (
        LiterarySharedRuntimeProfileV1 | LiterarySharedRuntimeProfileV2 | None
    ) = None
    api_sources_by_alias: Mapping[str, Mapping[str, Any]] | None = None

    def _profile_values(
        self,
    ) -> tuple[str, str, str, Mapping[str, Any]]:
        if self.runtime_profile is None:
            manifest = {
                role_id: {
                    "role_id": preset.role_id,
                    "preset_id": preset.preset_id,
                    "preset_revision": preset.preset_revision,
                    "legacy_role_ids": list(preset.legacy_role_ids),
                    "requested_model_id": preset.requested_model_id,
                    "generation": dict(preset.generation),
                    "limits": dict(preset.limits),
                    "transport_retry": dict(preset.transport_retry),
                    "semantic_retry": dict(preset.semantic_retry),
                    "namespaces": dict(preset.namespaces),
                }
                for role_id, preset in sorted(ROLE_PRESETS.items())
            }
            return PROFILE_ID, PROFILE_REVISION, canonical_sha256(manifest), ROLE_PRESETS
        if isinstance(self.runtime_profile, LiterarySharedRuntimeProfileV2):
            return (
                self.runtime_profile.profile_id,
                self.runtime_profile.profile_revision,
                self.runtime_profile.profile_sha256,
                self.runtime_profile.role_presets,
            )
        if self.structured_output is None or dict(self.structured_output) != dict(
            self.runtime_profile.structured_output
        ):
            raise LiterarySharedRunnerError(
                "shared runtime Structured Output differs from its profile"
            )
        return (
            self.runtime_profile.profile_id,
            self.runtime_profile.profile_revision,
            self.runtime_profile.profile_sha256,
            self.runtime_profile.role_presets,
        )

    def api_source_for(self, role_id: str) -> Mapping[str, Any]:
        if isinstance(self.runtime_profile, LiterarySharedRuntimeProfileV2):
            role_binding = self.runtime_profile.role_bindings.get(role_id)
            if role_binding is None:
                raise LiterarySharedRunnerError(
                    f"runtime profile lacks Literary role: {role_id}"
                )
            if self.api_sources_by_alias is None:
                raise LiterarySharedRunnerError(
                    "v2 runtime requires exact api_sources_by_alias"
                )
            source = self.api_sources_by_alias.get(role_binding.source_alias)
            if source is None:
                raise LiterarySharedRunnerError(
                    f"runtime source alias is absent: {role_binding.source_alias}"
                )
            try:
                validate_runtime_source_against_binding_v2(
                    source=source,
                    binding=self.runtime_profile.sources[role_binding.source_alias],
                )
            except ValueError as exc:
                raise LiterarySharedRunnerError(str(exc)) from exc
            return source
        if self.api_source is None:
            raise LiterarySharedRunnerError("shared runtime API source is absent")
        return self.api_source

    def role_preset_for(self, role_id: str):
        _profile_id, _profile_revision, _profile_sha256, presets = (
            self._profile_values()
        )
        try:
            return presets[role_id]
        except KeyError as exc:
            raise LiterarySharedRunnerError(
                f"runtime profile lacks Literary role: {role_id}"
            ) from exc

    def output_envelope_for(self, role_id: str) -> Mapping[str, Any] | None:
        if isinstance(self.runtime_profile, LiterarySharedRuntimeProfileV2):
            try:
                return self.runtime_profile.output_envelope_for(role_id)
            except ValueError as exc:
                raise LiterarySharedRunnerError(str(exc)) from exc
        return None

    def structured_output_for(self, role_id: str) -> Mapping[str, Any]:
        if isinstance(self.runtime_profile, LiterarySharedRuntimeProfileV2):
            try:
                return self.runtime_profile.shared_structured_output_for(role_id)
            except ValueError as exc:
                raise LiterarySharedRunnerError(str(exc)) from exc
        if self.structured_output is None:
            raise LiterarySharedRunnerError(
                "shared runtime Structured Output binding is absent"
            )
        return self.structured_output

    def identity_payload(self) -> dict[str, Any]:
        profile_id, profile_revision, profile_sha256, _presets = (
            self._profile_values()
        )
        capability_hashes = {
            key: canonical_sha256(value)
            for key, value in sorted(self.capabilities.items())
        }
        if isinstance(self.runtime_profile, LiterarySharedRuntimeProfileV2):
            source_hashes = {
                role_id: canonical_sha256(self.api_source_for(role_id))
                for role_id in sorted(self.runtime_profile.role_bindings)
            }
            structured_by_role = {
                role_id: dict(self.structured_output_for(role_id))
                for role_id in sorted(self.runtime_profile.role_bindings)
            }
            envelopes_by_role = {
                role_id: dict(self.output_envelope_for(role_id) or {})
                for role_id in sorted(self.runtime_profile.role_bindings)
            }
            body = {
                "schema_version": "literary_shared_runner_identity_v2",
                "backend_mode": BACKEND_MODE_SHARED_V1,
                "adapter_version": ADAPTER_VERSION,
                "pipeline_profile_id": profile_id,
                "pipeline_profile_revision": profile_revision,
                "pipeline_profile_sha256": profile_sha256,
                "api_source_sha256_by_role": source_hashes,
                "capability_record_sha256_by_binding": capability_hashes,
                "run_id": _required_text(self.run_id, "run_id"),
                "attempt_run_id": _required_text(
                    self.attempt_run_id, "attempt_run_id"
                ),
                "structured_output_by_role": structured_by_role,
                "output_envelope_by_role": envelopes_by_role,
                "application_response_cache": "disabled",
            }
            return {**body, "identity_sha256": canonical_sha256(body)}
        source = self.api_source_for(next(iter(sorted(_presets))))
        structured_output = self.structured_output_for(next(iter(sorted(_presets))))
        body = {
            "schema_version": "literary_shared_runner_identity_v1",
            "backend_mode": BACKEND_MODE_SHARED_V1,
            "adapter_version": ADAPTER_VERSION,
            "pipeline_profile_id": profile_id,
            "pipeline_profile_revision": profile_revision,
            "pipeline_profile_sha256": profile_sha256,
            "api_source_sha256": canonical_sha256(source),
            "capability_record_sha256_by_binding": capability_hashes,
            "run_id": _required_text(self.run_id, "run_id"),
            "attempt_run_id": _required_text(self.attempt_run_id, "attempt_run_id"),
            "structured_output": dict(structured_output),
            "application_response_cache": "disabled",
        }
        return {**body, "identity_sha256": canonical_sha256(body)}

    def capability_for(
        self,
        *,
        role_id: str,
        response_schema: Mapping[str, Any],
        binding_schema: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        key = capability_binding_key(
            role_id, binding_schema if binding_schema is not None else response_schema
        )
        capability = self.capabilities.get(key)
        if capability is None:
            raise LiterarySharedRunnerError(
                f"shared capability is absent for exact role/schema binding: {key}"
            )
        if isinstance(self.runtime_profile, LiterarySharedRuntimeProfileV2):
            envelope = self.output_envelope_for(role_id)
            mode = envelope.get("mode") if envelope is not None else None
            expected_kind = {
                "native_schema": "native_structured_output",
                "json_object": "json_object",
                "prompt_json": "text_generation",
            }.get(mode)
            if capability.get("capability_kind") != expected_kind:
                raise LiterarySharedRunnerError(
                    "shared capability kind differs from the role output envelope"
                )
            if mode in {"native_schema", "json_object"}:
                source = self.api_source_for(role_id)
                transport_schema, _omissions = resolve_transport_response_schema(
                    response_schema=response_schema,
                    protocol=str(source["protocol"]),
                    output_envelope=envelope,
                )
                if capability.get("schema_sha256") != canonical_sha256(
                    transport_schema
                ):
                    raise LiterarySharedRunnerError(
                        "shared capability schema binding drifted"
                    )
            elif any(
                capability.get(field) is not None
                for field in (
                    "schema_dialect",
                    "schema_sha256",
                    "local_validator_id",
                    "local_validator_sha256",
                )
            ):
                raise LiterarySharedRunnerError(
                    "text-generation capability claims schema authority"
                )
        elif capability.get("schema_sha256") != canonical_sha256(response_schema):
            raise LiterarySharedRunnerError("shared capability schema binding drifted")
        return capability

    def execute_accepted_request(
        self,
        *,
        role_id: str,
        stage_id: str,
        logical_request_id: str,
        request: Mapping[str, Any],
        schema_name: str,
        semantic_validator: SemanticValidator,
        validator_ref: Mapping[str, str],
        application_contract_id: str,
        application_contract_revision: str,
        output_dir: Path,
        additional_input_bindings: Sequence[Mapping[str, str]] = (),
        semantic_attempt_index: int = 1,
        model_reference_mode: str | None = None,
        model_reference_fields: Mapping[str, Sequence[str]] = MODEL_REF_FIELDS_V1,
    ) -> LiterarySharedAttemptResult:
        original_request = request
        projected_request = request
        ref_map: Mapping[str, Any] | None = None
        effective_validator_ref = validator_ref
        if model_reference_mode is not None:
            if model_reference_mode != MODEL_REF_MODE_CLASSIFIED_V1:
                raise LiterarySharedRunnerError(
                    "unsupported Literary model-reference transport mode"
                )
            try:
                projected_request, ref_map = project_model_request_v1(
                    request,
                    field_names_by_namespace=model_reference_fields,
                    instruction=model_ref_instruction_v1(),
                )
                effective_validator_ref = bind_model_ref_validator_v1(validator_ref)
            except (ModelRefError, ValueError) as exc:
                raise LiterarySharedRunnerError(str(exc)) from exc
        messages = projected_request.get("messages")
        response_schema = projected_request.get("response_schema")
        original_response_schema = original_request.get("response_schema")
        request_fingerprint = original_request.get("request_fingerprint")
        projected_request_fingerprint = projected_request.get("request_fingerprint")
        if not isinstance(messages, list) or not all(
            isinstance(row, Mapping) for row in messages
        ):
            raise LiterarySharedRunnerError("rendered request messages are malformed")
        if not isinstance(response_schema, Mapping):
            raise LiterarySharedRunnerError("rendered response schema is malformed")
        if not isinstance(original_response_schema, Mapping):
            raise LiterarySharedRunnerError(
                "original rendered response schema is malformed"
            )
        if not isinstance(request_fingerprint, str) or len(request_fingerprint) != 64:
            raise LiterarySharedRunnerError("rendered request fingerprint is malformed")
        if not isinstance(projected_request_fingerprint, str) or len(
            projected_request_fingerprint
        ) != 64:
            raise LiterarySharedRunnerError(
                "projected request fingerprint is malformed"
            )
        profile_id, profile_revision, _profile_sha256, _presets = (
            self._profile_values()
        )
        preset = self.role_preset_for(role_id)
        capability = self.capability_for(
            role_id=role_id,
            response_schema=response_schema,
            binding_schema=original_response_schema,
        )
        api_source = self.api_source_for(role_id)
        structured_output = self.structured_output_for(role_id)
        output_envelope = self.output_envelope_for(role_id)
        prompt_ref = {
            "id": f"{role_id}.rendered_prompt",
            "revision": "runtime_v1",
            "sha256": canonical_sha256(messages),
        }
        response_schema_ref = {
            "id": f"{role_id}.response_schema",
            "revision": "runtime_v1",
            "sha256": canonical_sha256(response_schema),
        }
        semantic_extension_body = {
            "backend_mode": BACKEND_MODE_SHARED_V1,
            "role_id": role_id,
            "application_contract_id": _required_text(
                application_contract_id, "application contract id"
            ),
            "application_contract_revision": _required_text(
                application_contract_revision, "application contract revision"
            ),
        }
        semantic_extension_ref = {
            "id": semantic_extension_body["application_contract_id"],
            "schema_version": "literary_semantic_authority_v1",
            "sha256": canonical_sha256(semantic_extension_body),
        }
        bindings = [
            {
                "name": "literary_rendered_request",
                "sha256": canonical_sha256(original_request),
            },
            {
                "name": "literary_request_fingerprint",
                "sha256": canonical_sha256(request_fingerprint),
            },
            {
                "name": "literary_model_request",
                "sha256": canonical_sha256(projected_request),
            },
            *(
                [
                    {
                        "name": "literary_model_ref_map",
                        "sha256": ref_map["map_hash"],
                    },
                    {
                        "name": "literary_model_ref_fields",
                        "sha256": model_ref_fields_hash_v1(model_reference_fields),
                    },
                ]
                if ref_map is not None
                else []
            ),
            *[dict(row) for row in additional_input_bindings],
        ]

        def response_transformer(raw: Mapping[str, Any]) -> Mapping[str, Any]:
            if ref_map is None:
                return raw
            try:
                return resolve_model_response_v1(
                    projected_request,
                    raw,
                    field_names_by_namespace=model_reference_fields,
                )
            except ModelRefError as exc:
                raise LiterarySharedRunnerError(str(exc)) from exc

        def diagnostic_semantic_validator(
            payload: Mapping[str, Any],
        ) -> Mapping[str, Any]:
            try:
                return semantic_validator(payload)
            except Exception as exc:
                diagnostic = _build_semantic_rejection_diagnostic_v1(
                    role_id=role_id,
                    stage_id=stage_id,
                    logical_request_id=logical_request_id,
                    semantic_attempt_index=semantic_attempt_index,
                    request_fingerprint=request_fingerprint,
                    projected_request_fingerprint=projected_request_fingerprint,
                    validator_ref=effective_validator_ref,
                    semantic_payload=payload,
                    validator_error=exc,
                )
                _write_immutable_json(
                    Path(output_dir) / "semantic_rejection.json", diagnostic
                )
                raise

        result = self.adapter.execute(
            preset=preset,
            api_source=api_source,
            capability=capability,
            messages=messages,
            response_schema=response_schema,
            schema_name=schema_name,
            prompt_ref=prompt_ref,
            response_schema_ref=response_schema_ref,
            validator_ref=effective_validator_ref,
            semantic_extension_ref=semantic_extension_ref,
            structured_output=structured_output,
            output_envelope=output_envelope,
            semantic_validator=diagnostic_semantic_validator,
            response_transformer=(response_transformer if ref_map is not None else None),
            run_id=self.run_id,
            attempt_run_id=self.attempt_run_id,
            stage_id=stage_id,
            logical_request_id=logical_request_id,
            semantic_attempt_index=semantic_attempt_index,
            transport_retry_ordinal=0,
            additional_input_bindings=bindings,
            allow_response_cache_read=False,
            allow_response_cache_write=False,
            pipeline_profile_id=profile_id,
            pipeline_profile_revision=profile_revision,
        )
        receipt_body = {
            "schema_version": SHARED_ATTEMPT_RECEIPT_SCHEMA_VERSION,
            "backend_mode": BACKEND_MODE_SHARED_V1,
            "role_id": role_id,
            "stage_id": stage_id,
            "logical_request_id": logical_request_id,
            "semantic_attempt_index": semantic_attempt_index,
            "transport_retry_ordinal": 0,
            "request_fingerprint": request_fingerprint,
            "projected_request_fingerprint": projected_request_fingerprint,
            "request_sha256": canonical_sha256(original_request),
            "projected_request_sha256": canonical_sha256(projected_request),
            "model_reference_mode": (
                MODEL_REF_MODE_CLASSIFIED_V1 if ref_map is not None else "persistent"
            ),
            "model_ref_map_hash": ref_map["map_hash"] if ref_map is not None else None,
            "model_code_owned_echoes_hash": (
                projected_request.get("model_code_owned_echoes_hash")
                if ref_map is not None
                else None
            ),
            "semantic_output_sha256": canonical_sha256(result.semantic_payload),
            "provider_artifact_sha256": result.artifact_sha256,
            "seal": dict(result.seal),
            "seal_sha256": result.seal["seal_sha256"],
            "usage": dict(result.usage) if result.usage is not None else None,
            "cache_observation": result.cache_observation,
            "application_response_cache": "disabled",
            "semantic_status": result.status,
            "production_publish_performed": False,
        }
        receipt = {
            **receipt_body,
            "receipt_sha256": canonical_sha256(receipt_body),
        }
        _write_immutable_json(Path(output_dir) / "shared_attempt_receipt.json", receipt)
        return result


def _write_immutable_json(path: Path, payload: Mapping[str, Any]) -> None:
    target = Path(path)
    if target.is_file():
        try:
            import json

            existing = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise LiterarySharedRunnerError(
                f"cannot load shared runtime artifact: {target}"
            ) from exc
        if canonical_json(existing) != canonical_json(payload):
            raise LiterarySharedRunnerError(
                f"immutable shared runtime artifact differs: {target}"
            )
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    write_checkpoint_atomic(target, dict(payload))


def _build_semantic_rejection_diagnostic_v1(
    *,
    role_id: str,
    stage_id: str,
    logical_request_id: str,
    semantic_attempt_index: int,
    request_fingerprint: str,
    projected_request_fingerprint: str,
    validator_ref: Mapping[str, Any],
    semantic_payload: Mapping[str, Any],
    validator_error: Exception,
) -> dict[str, Any]:
    payload_bytes = canonical_json(dict(semantic_payload)).encode("utf-8")
    excerpt_bytes = payload_bytes[:SEMANTIC_REJECTION_EXCERPT_MAX_UTF8_BYTES]
    error_message = str(validator_error).strip() or (
        "validator supplied no error message"
    )
    if "sk-" in error_message or "Bearer " in error_message:
        error_message = "credential-like material was redacted"
    body = {
        "schema_version": SEMANTIC_REJECTION_DIAGNOSTIC_SCHEMA_VERSION,
        "semantic_status": "rejected",
        "role_id": _required_text(role_id, "role id"),
        "stage_id": _required_text(stage_id, "stage id"),
        "logical_request_id": _required_text(
            logical_request_id, "logical request id"
        ),
        "semantic_attempt_index": semantic_attempt_index,
        "request_fingerprint": request_fingerprint,
        "projected_request_fingerprint": projected_request_fingerprint,
        "validator_ref": dict(validator_ref),
        "validator_error": {
            "error_type": type(validator_error).__name__,
            "message": error_message[:4000],
        },
        "semantic_payload": {
            "sha256": canonical_sha256(dict(semantic_payload)),
            "utf8_bytes": len(payload_bytes),
            "excerpt": excerpt_bytes.decode("utf-8", errors="replace"),
            "excerpt_utf8_bytes": len(excerpt_bytes),
            "excerpt_truncated": len(excerpt_bytes) < len(payload_bytes),
        },
        "semantic_authority_granted": False,
        "application_response_cache": "disabled",
        "accepted_application_receipt_written": False,
        "production_publish_performed": False,
    }
    return {**body, "diagnostic_sha256": canonical_sha256(body)}


def _required_text(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise LiterarySharedRunnerError(f"{label} is empty")
    return text


__all__ = [
    "BACKEND_MODE_LEGACY",
    "BACKEND_MODE_SHARED_V1",
    "BACKEND_MODES",
    "LiterarySharedRunnerBindingsV1",
    "LiterarySharedRunnerError",
    "SHARED_ATTEMPT_RECEIPT_SCHEMA_VERSION",
    "build_literary_code_ref_v1",
    "capability_binding_key",
]
