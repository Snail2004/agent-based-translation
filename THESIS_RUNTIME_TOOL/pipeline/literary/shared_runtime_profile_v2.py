"""Per-role Literary runtime profiles for the neutral Shared LLM Backend.

V2 keeps semantic values outside runner code and makes source selection and the
output envelope role-specific.  The profile contains references only; secrets
and physical transport remain owned by the Shared LLM Backend host.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any, Collection, Mapping
from urllib.parse import urlsplit

from pipeline.llm_backend import canonical_sha256
from pipeline.literary.shared_llm_profiles_v1 import LiterarySharedRolePreset
from pipeline.literary.shared_runtime_profile_v1 import (
    EXPECTED_ROLE_IDS,
    literary_role_preset_payload_v1,
    parse_literary_role_preset_v1,
)


PROFILE_SCHEMA_VERSION_V2 = "literary_shared_llm_runtime_profile_v2"
DEFAULT_PROFILE_V2_PATH = (
    Path(__file__).resolve().parents[1]
    / "configs"
    / "literary_shared_llm_runtime_openai_official_v2.json"
)
OUTPUT_ENVELOPE_MODES = frozenset(
    {"native_schema", "json_object", "prompt_json"}
)
SOURCE_AUTHORITY_CLASSES = frozenset(
    {"direct_official_openai", "direct_official_google", "third_party"}
)
PROMPT_JSON_INSTRUCTION_ID = "literary.json_only_output_instruction"
PROMPT_JSON_INSTRUCTION_REVISION = "v2"
OPENAI_NATIVE_SCHEMA_DIALECT = "openai_strict_json_schema_subset_v1"
GOOGLE_NATIVE_SCHEMA_DIALECT = "gemini_response_json_schema_subset_v1"
LOCAL_VALIDATION_SCHEMA_DIALECT = "json_schema_2020_12"


class LiterarySharedRuntimeProfileV2Error(ValueError):
    pass


@dataclass(frozen=True)
class LiteraryRuntimeRoleBindingV2:
    preset: LiterarySharedRolePreset
    source_alias: str
    output_envelope: Mapping[str, Any]


@dataclass(frozen=True)
class LiterarySharedRuntimeProfileV2:
    source_path: Path
    profile_id: str
    profile_revision: str
    backend_mode: str
    sources: Mapping[str, Mapping[str, Any]]
    role_bindings: Mapping[str, LiteraryRuntimeRoleBindingV2]
    safety: Mapping[str, Any]
    profile_sha256: str

    @property
    def role_presets(self) -> Mapping[str, LiterarySharedRolePreset]:
        return MappingProxyType(
            {
                role_id: binding.preset
                for role_id, binding in self.role_bindings.items()
            }
        )

    def source_binding_for(self, role_id: str) -> Mapping[str, Any]:
        try:
            binding = self.role_bindings[role_id]
            return self.sources[binding.source_alias]
        except KeyError as exc:
            raise LiterarySharedRuntimeProfileV2Error(
                f"runtime profile lacks source binding for role: {role_id}"
            ) from exc

    def output_envelope_for(self, role_id: str) -> Mapping[str, Any]:
        try:
            return self.role_bindings[role_id].output_envelope
        except KeyError as exc:
            raise LiterarySharedRuntimeProfileV2Error(
                f"runtime profile lacks output envelope for role: {role_id}"
            ) from exc

    def shared_structured_output_for(self, role_id: str) -> dict[str, Any]:
        return shared_structured_output_for_envelope(
            self.output_envelope_for(role_id)
        )

    def public_payload(self) -> dict[str, Any]:
        body = {
            "schema_version": PROFILE_SCHEMA_VERSION_V2,
            "profile_id": self.profile_id,
            "profile_revision": self.profile_revision,
            "backend_mode": self.backend_mode,
            "sources": [
                deepcopy(dict(self.sources[alias]))
                for alias in sorted(self.sources)
            ],
            "roles": [
                _role_payload(self.role_bindings[role_id])
                for role_id in sorted(self.role_bindings)
            ],
            "safety": deepcopy(dict(self.safety)),
        }
        return {**body, "profile_sha256": self.profile_sha256}


def load_literary_shared_runtime_profile_v2(
    path: Path = DEFAULT_PROFILE_V2_PATH,
    *,
    expected_role_ids: Collection[str] | None = None,
) -> LiterarySharedRuntimeProfileV2:
    source_path = Path(path).resolve()
    try:
        payload = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise LiterarySharedRuntimeProfileV2Error(
            f"cannot load Literary shared runtime profile: {source_path}"
        ) from exc
    _exact_keys(
        payload,
        {
            "schema_version",
            "profile_id",
            "profile_revision",
            "backend_mode",
            "sources",
            "roles",
            "safety",
        },
        "runtime profile",
    )
    if payload["schema_version"] != PROFILE_SCHEMA_VERSION_V2:
        raise LiterarySharedRuntimeProfileV2Error("foreign runtime profile schema")
    if payload["backend_mode"] != "shared_v1":
        raise LiterarySharedRuntimeProfileV2Error(
            "runtime profile must use shared_v1"
        )

    sources = _parse_sources(payload["sources"])
    roles = _parse_roles(payload["roles"], sources=sources)
    expected_roles = (
        EXPECTED_ROLE_IDS
        if expected_role_ids is None
        else frozenset(expected_role_ids)
    )
    if not expected_roles or any(
        not isinstance(role_id, str) or not role_id
        for role_id in expected_roles
    ):
        raise LiterarySharedRuntimeProfileV2Error(
            "expected Literary role IDs must be nonempty strings"
        )
    if set(roles) != expected_roles:
        raise LiterarySharedRuntimeProfileV2Error(
            "runtime profile does not exact-cover expected Literary roles"
        )

    safety = _object(payload["safety"], "safety")
    expected_safety = {
        "provider_fallback_allowed": False,
        "application_response_cache_enabled": False,
        "production_publish_enabled": False,
    }
    if safety != expected_safety:
        raise LiterarySharedRuntimeProfileV2Error("runtime safety policy drifted")

    profile_id = _text(payload["profile_id"], "profile_id")
    profile_revision = _text(payload["profile_revision"], "profile_revision")
    normalized = {
        "schema_version": PROFILE_SCHEMA_VERSION_V2,
        "profile_id": profile_id,
        "profile_revision": profile_revision,
        "backend_mode": "shared_v1",
        "sources": [deepcopy(dict(sources[alias])) for alias in sorted(sources)],
        "roles": [_role_payload(roles[role_id]) for role_id in sorted(roles)],
        "safety": deepcopy(safety),
    }
    return LiterarySharedRuntimeProfileV2(
        source_path=source_path,
        profile_id=profile_id,
        profile_revision=profile_revision,
        backend_mode="shared_v1",
        sources=MappingProxyType(sources),
        role_bindings=MappingProxyType(roles),
        safety=MappingProxyType(safety),
        profile_sha256=canonical_sha256(normalized),
    )


def shared_structured_output_for_envelope(
    output_envelope: Mapping[str, Any],
) -> dict[str, Any]:
    mode = output_envelope.get("mode")
    dialect = output_envelope.get("schema_dialect")
    if mode == "native_schema":
        return {"mode": "required", "schema_dialect": dialect}
    if mode == "json_object":
        return {"mode": "prompt_validated", "schema_dialect": dialect}
    if mode == "prompt_json":
        return {"mode": "disabled", "schema_dialect": None}
    raise LiterarySharedRuntimeProfileV2Error("unknown output envelope mode")


def validate_runtime_source_against_binding_v2(
    *, source: Mapping[str, Any], binding: Mapping[str, Any]
) -> None:
    comparisons = {
        "source_id": "source_id",
        "source_revision": "source_revision",
        "source_class": "source_class",
        "adapter_id": "adapter_id",
        "protocol": "protocol",
        "route_id": "route_id",
        "endpoint_class": "endpoint_class",
        "base_url": "base_url",
        "credential_ref": "credential_ref",
        "physical_quota_bucket_id": "physical_quota_bucket_id",
    }
    for source_field, binding_field in comparisons.items():
        if source.get(source_field) != binding.get(binding_field):
            raise LiterarySharedRuntimeProfileV2Error(
                f"runtime source {source_field} differs from its profile binding"
            )
    if source.get("enabled") is not True:
        raise LiterarySharedRuntimeProfileV2Error("runtime source is disabled")


def _parse_sources(value: Any) -> dict[str, Mapping[str, Any]]:
    if not isinstance(value, list) or not value:
        raise LiterarySharedRuntimeProfileV2Error("sources must be a nonempty list")
    result: dict[str, Mapping[str, Any]] = {}
    for index, raw in enumerate(value, 1):
        row = _object(raw, f"source {index}")
        _exact_keys(
            row,
            {
                "source_alias",
                "source_id",
                "source_revision",
                "authority_class",
                "source_class",
                "adapter_id",
                "protocol",
                "route_id",
                "endpoint_class",
                "base_url",
                "credential_ref",
                "physical_quota_bucket_id",
                "selection_mode",
                "fallback_enabled",
            },
            f"source {index}",
        )
        alias = _text(row["source_alias"], "source_alias")
        if alias in result:
            raise LiterarySharedRuntimeProfileV2Error("source_alias is duplicated")
        for field in (
            "source_id",
            "source_revision",
            "source_class",
            "adapter_id",
            "protocol",
            "route_id",
            "endpoint_class",
            "credential_ref",
            "physical_quota_bucket_id",
        ):
            row[field] = _text(row[field], field)
        authority = _text(row["authority_class"], "authority_class")
        if authority not in SOURCE_AUTHORITY_CLASSES:
            raise LiterarySharedRuntimeProfileV2Error(
                "source authority_class is invalid"
            )
        row["authority_class"] = authority
        row["base_url"] = _normalized_url(row["base_url"])
        if row["selection_mode"] != "host_resolved_exact_source":
            raise LiterarySharedRuntimeProfileV2Error(
                "source selection must be host_resolved_exact_source"
            )
        if row["fallback_enabled"] is not False:
            raise LiterarySharedRuntimeProfileV2Error(
                "source fallback must remain disabled"
            )
        if authority == "direct_official_openai":
            if (
                row["base_url"] != "https://api.openai.com/v1"
                or row["protocol"] != "openai_chat_completions"
            ):
                raise LiterarySharedRuntimeProfileV2Error(
                    "official OpenAI source binding is not direct"
                )
        if authority == "direct_official_google":
            if (
                row["base_url"]
                != "https://generativelanguage.googleapis.com/v1beta"
                or row["protocol"] != "google_genai_generate_content"
            ):
                raise LiterarySharedRuntimeProfileV2Error(
                    "official Google source binding is not direct"
                )
        result[alias] = MappingProxyType(deepcopy(row))
    return result


def _parse_roles(
    value: Any, *, sources: Mapping[str, Mapping[str, Any]]
) -> dict[str, LiteraryRuntimeRoleBindingV2]:
    if not isinstance(value, list):
        raise LiterarySharedRuntimeProfileV2Error("roles must be a list")
    result: dict[str, LiteraryRuntimeRoleBindingV2] = {}
    namespaces: set[str] = set()
    for index, raw in enumerate(value, 1):
        row = _object(raw, f"role {index}")
        source_alias = _text(row.pop("source_alias", None), "source_alias")
        envelope = _parse_output_envelope(
            row.pop("output_envelope", None), index=index
        )
        if source_alias not in sources:
            raise LiterarySharedRuntimeProfileV2Error(
                f"role {index} references an unknown source_alias"
            )
        try:
            preset = parse_literary_role_preset_v1(row, index=index)
        except ValueError as exc:
            raise LiterarySharedRuntimeProfileV2Error(str(exc)) from exc
        if preset.role_id in result:
            raise LiterarySharedRuntimeProfileV2Error("role_id is duplicated")
        for namespace in preset.namespaces.values():
            if namespace in namespaces:
                raise LiterarySharedRuntimeProfileV2Error(
                    "runtime namespaces must be unique across roles"
                )
            namespaces.add(namespace)
        authority = sources[source_alias]["authority_class"]
        if envelope["mode"] == "native_schema" and authority == "third_party":
            raise LiterarySharedRuntimeProfileV2Error(
                "third-party source cannot claim native Structured Output"
            )
        if envelope["mode"] == "native_schema":
            expected_dialect = {
                "direct_official_openai": OPENAI_NATIVE_SCHEMA_DIALECT,
                "direct_official_google": GOOGLE_NATIVE_SCHEMA_DIALECT,
            }.get(authority)
            if envelope["schema_dialect"] != expected_dialect:
                raise LiterarySharedRuntimeProfileV2Error(
                    "native schema dialect differs from source authority"
                )
        if (
            envelope["mode"] == "json_object"
            and envelope["schema_dialect"] != LOCAL_VALIDATION_SCHEMA_DIALECT
        ):
            raise LiterarySharedRuntimeProfileV2Error(
                "json_object must declare local-validation schema authority"
            )
        result[preset.role_id] = LiteraryRuntimeRoleBindingV2(
            preset=preset,
            source_alias=source_alias,
            output_envelope=MappingProxyType(envelope),
        )
    return result


def _parse_output_envelope(value: Any, *, index: int) -> dict[str, Any]:
    row = _object(value, f"role {index} output_envelope")
    _exact_keys(
        row,
        {"mode", "schema_dialect", "instruction_id", "instruction_revision"},
        f"role {index} output_envelope",
    )
    mode = _text(row["mode"], "output envelope mode")
    if mode not in OUTPUT_ENVELOPE_MODES:
        raise LiterarySharedRuntimeProfileV2Error("output envelope mode is invalid")
    dialect = row["schema_dialect"]
    instruction_id = row["instruction_id"]
    instruction_revision = row["instruction_revision"]
    if mode in {"native_schema", "json_object"}:
        dialect = _text(dialect, "schema_dialect")
    elif dialect is not None:
        raise LiterarySharedRuntimeProfileV2Error(
            "prompt_json may not claim a provider schema dialect"
        )
    if mode == "native_schema":
        if instruction_id is not None or instruction_revision is not None:
            raise LiterarySharedRuntimeProfileV2Error(
                "native_schema does not use a prompt envelope instruction"
            )
    else:
        if (
            instruction_id != PROMPT_JSON_INSTRUCTION_ID
            or instruction_revision != PROMPT_JSON_INSTRUCTION_REVISION
        ):
            raise LiterarySharedRuntimeProfileV2Error(
                "non-native output must use the sealed JSON-only instruction"
            )
    return {
        "mode": mode,
        "schema_dialect": dialect,
        "instruction_id": instruction_id,
        "instruction_revision": instruction_revision,
    }


def _role_payload(binding: LiteraryRuntimeRoleBindingV2) -> dict[str, Any]:
    return {
        **literary_role_preset_payload_v1(binding.preset),
        "source_alias": binding.source_alias,
        "output_envelope": deepcopy(dict(binding.output_envelope)),
    }


def _normalized_url(value: Any) -> str:
    text = _text(value, "base_url")
    if not all(0x21 <= ord(character) <= 0x7E for character in text):
        raise LiterarySharedRuntimeProfileV2Error("base_url contains unsafe bytes")
    try:
        parsed = urlsplit(text)
        _ = parsed.port
    except (ValueError, UnicodeError) as exc:
        raise LiterarySharedRuntimeProfileV2Error("base_url is malformed") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise LiterarySharedRuntimeProfileV2Error("base_url is not a safe HTTPS URL")
    return text.rstrip("/")


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise LiterarySharedRuntimeProfileV2Error(f"{label} must be an object")
    return deepcopy(dict(value))


def _exact_keys(value: Any, keys: set[str], label: str) -> None:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise LiterarySharedRuntimeProfileV2Error(f"{label} keys differ")


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LiterarySharedRuntimeProfileV2Error(f"{label} is empty")
    return value.strip()


__all__ = [
    "DEFAULT_PROFILE_V2_PATH",
    "LiteraryRuntimeRoleBindingV2",
    "LiterarySharedRuntimeProfileV2",
    "LiterarySharedRuntimeProfileV2Error",
    "GOOGLE_NATIVE_SCHEMA_DIALECT",
    "LOCAL_VALIDATION_SCHEMA_DIALECT",
    "OPENAI_NATIVE_SCHEMA_DIALECT",
    "OUTPUT_ENVELOPE_MODES",
    "PROFILE_SCHEMA_VERSION_V2",
    "PROMPT_JSON_INSTRUCTION_ID",
    "PROMPT_JSON_INSTRUCTION_REVISION",
    "SOURCE_AUTHORITY_CLASSES",
    "load_literary_shared_runtime_profile_v2",
    "shared_structured_output_for_envelope",
    "validate_runtime_source_against_binding_v2",
]
