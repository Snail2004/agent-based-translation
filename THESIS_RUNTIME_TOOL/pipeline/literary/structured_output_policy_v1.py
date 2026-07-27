"""Provider-aware Structured Output policy for the Literary pipeline.

The canonical JSON Schema remains the local authority. Provider-native schema
enforcement is an optional transport capability, never a replacement for
local validation or the stage-specific semantic validator.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from jsonschema import Draft202012Validator


POLICY_SCHEMA_VERSION = "literary_structured_output_policy_v1"
STRUCTURED_OUTPUT_MODES = frozenset({"auto", "required", "prompt_validated"})
CAPABILITY_STATUSES = frozenset(
    {"verified_native", "prompt_validated_only", "unavailable"}
)
SUPPORTED_PROVIDERS = frozenset({"google_genai", "openai"})
SUPPORTED_SCHEMA_DIALECTS = frozenset(
    {
        "gemini_response_json_schema_subset_v1",
        "openai_strict_json_schema_subset_v1",
        "prompt_json_local_validation_v1",
    }
)
TRANSPORT_ONLY_OMISSIONS = frozenset({"minItems", "minLength", "uniqueItems"})


class LiteraryStructuredOutputError(ValueError):
    pass


class LiteraryStructuredOutputValidationError(LiteraryStructuredOutputError):
    pass


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _canonical_hash(value: Any) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LiteraryStructuredOutputError(f"{label} must be a non-empty string")
    return value.strip()


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise LiteraryStructuredOutputError(
            f"{label} keys differ; missing={sorted(expected - set(value))}, "
            f"extra={sorted(set(value) - expected)}"
        )


def _normalized_base_url(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.startswith(("https://", "http://")):
        raise LiteraryStructuredOutputError("capability base_url is invalid")
    return value.rstrip("/")


@dataclass(frozen=True)
class StructuredOutputRolePolicy:
    role_id: str
    mode: str
    adapter_ids_by_provider: Mapping[str, str]
    format_repair_cap: int


@dataclass(frozen=True)
class StructuredOutputCapability:
    capability_id: str
    provider: str
    base_url: str | None
    model_id: str
    adapter_id: str
    status: str
    schema_dialect: str
    evidence_id: str


@dataclass(frozen=True)
class LiteraryStructuredOutputPolicy:
    policy_id: str
    role_policies: Mapping[str, StructuredOutputRolePolicy]
    capabilities: tuple[StructuredOutputCapability, ...]
    policy_hash: str
    source_path: Path
    source_sha256: str

    def role(self, role_id: str) -> StructuredOutputRolePolicy:
        try:
            return self.role_policies[role_id]
        except KeyError as exc:
            raise LiteraryStructuredOutputError(
                f"structured-output policy has no role: {role_id}"
            ) from exc


@dataclass(frozen=True)
class StructuredOutputContract:
    policy_id: str
    role_id: str
    provider: str
    base_url: str | None
    model_id: str
    adapter_id: str
    requested_mode: str
    effective_mode: str
    capability_status: str
    capability_id: str | None
    schema_dialect: str
    canonical_schema_hash: str
    transport_schema: Mapping[str, Any] | None
    transport_schema_hash: str | None
    omitted_transport_constraints: tuple[Mapping[str, Any], ...]
    format_repair_cap: int
    evidence_id: str | None

    @property
    def native_enforcement(self) -> bool:
        return self.effective_mode == "native_schema"

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": "literary_structured_output_contract_v1",
            "policy_id": self.policy_id,
            "role_id": self.role_id,
            "provider": self.provider,
            "base_url_class": "official" if self.base_url is None else self.base_url,
            "model_id": self.model_id,
            "adapter_id": self.adapter_id,
            "requested_mode": self.requested_mode,
            "effective_mode": self.effective_mode,
            "capability_status": self.capability_status,
            "capability_id": self.capability_id,
            "schema_dialect": self.schema_dialect,
            "canonical_schema_hash": self.canonical_schema_hash,
            "transport_schema_hash": self.transport_schema_hash,
            "omitted_transport_constraints": [
                dict(row) for row in self.omitted_transport_constraints
            ],
            "format_repair_cap": self.format_repair_cap,
            "local_validation_required": True,
            "evidence_id": self.evidence_id,
        }


def load_literary_structured_output_policy(
    path: Path,
) -> LiteraryStructuredOutputPolicy:
    source = Path(path).resolve()
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise LiteraryStructuredOutputError(
            f"cannot load structured-output policy: {source}"
        ) from exc
    if not isinstance(raw, dict):
        raise LiteraryStructuredOutputError("structured-output policy must be an object")
    _exact_keys(
        raw,
        {"schema_version", "policy_id", "role_policies", "capabilities"},
        "structured-output policy",
    )
    if raw["schema_version"] != POLICY_SCHEMA_VERSION:
        raise LiteraryStructuredOutputError("foreign structured-output policy schema")
    policy_id = _required_string(raw["policy_id"], "policy_id")

    raw_roles = raw["role_policies"]
    if not isinstance(raw_roles, dict) or not raw_roles:
        raise LiteraryStructuredOutputError("role_policies must be a non-empty object")
    roles: dict[str, StructuredOutputRolePolicy] = {}
    for raw_role_id, raw_role in raw_roles.items():
        role_id = _required_string(raw_role_id, "role id")
        if not isinstance(raw_role, dict):
            raise LiteraryStructuredOutputError(f"role {role_id} must be an object")
        _exact_keys(
            raw_role,
            {"mode", "adapter_ids_by_provider", "format_repair_cap"},
            f"role {role_id}",
        )
        mode = _required_string(raw_role["mode"], f"role {role_id} mode")
        if mode not in STRUCTURED_OUTPUT_MODES:
            raise LiteraryStructuredOutputError(f"role {role_id} has invalid mode")
        raw_adapters = raw_role["adapter_ids_by_provider"]
        if not isinstance(raw_adapters, dict) or not raw_adapters:
            raise LiteraryStructuredOutputError(
                f"role {role_id} needs provider adapter ids"
            )
        adapters: dict[str, str] = {}
        for provider, adapter_id in raw_adapters.items():
            if provider not in SUPPORTED_PROVIDERS:
                raise LiteraryStructuredOutputError(
                    f"role {role_id} has unsupported provider {provider}"
                )
            adapters[provider] = _required_string(
                adapter_id, f"role {role_id} adapter"
            )
        repair_cap = raw_role["format_repair_cap"]
        if (
            not isinstance(repair_cap, int)
            or isinstance(repair_cap, bool)
            or not 0 <= repair_cap <= 1
        ):
            raise LiteraryStructuredOutputError(
                f"role {role_id} format repair cap must be 0 or 1"
            )
        roles[role_id] = StructuredOutputRolePolicy(
            role_id=role_id,
            mode=mode,
            adapter_ids_by_provider=adapters,
            format_repair_cap=repair_cap,
        )

    raw_capabilities = raw["capabilities"]
    if not isinstance(raw_capabilities, list) or not raw_capabilities:
        raise LiteraryStructuredOutputError("capabilities must be a non-empty array")
    capabilities: list[StructuredOutputCapability] = []
    seen_ids: set[str] = set()
    seen_keys: set[tuple[str, str | None, str, str]] = set()
    for index, raw_capability in enumerate(raw_capabilities):
        if not isinstance(raw_capability, dict):
            raise LiteraryStructuredOutputError(
                f"capability {index} must be an object"
            )
        _exact_keys(
            raw_capability,
            {
                "capability_id",
                "provider",
                "base_url",
                "model_id",
                "adapter_id",
                "status",
                "schema_dialect",
                "evidence_id",
            },
            f"capability {index}",
        )
        capability_id = _required_string(
            raw_capability["capability_id"], f"capability {index} id"
        )
        if capability_id in seen_ids:
            raise LiteraryStructuredOutputError("capability id is duplicated")
        seen_ids.add(capability_id)
        provider = _required_string(
            raw_capability["provider"], f"capability {index} provider"
        )
        if provider not in SUPPORTED_PROVIDERS:
            raise LiteraryStructuredOutputError("capability provider is unsupported")
        status = _required_string(
            raw_capability["status"], f"capability {index} status"
        )
        if status not in CAPABILITY_STATUSES:
            raise LiteraryStructuredOutputError("capability status is invalid")
        dialect = _required_string(
            raw_capability["schema_dialect"], f"capability {index} dialect"
        )
        if dialect not in SUPPORTED_SCHEMA_DIALECTS:
            raise LiteraryStructuredOutputError("schema dialect is unsupported")
        capability = StructuredOutputCapability(
            capability_id=capability_id,
            provider=provider,
            base_url=_normalized_base_url(raw_capability["base_url"]),
            model_id=_required_string(
                raw_capability["model_id"], f"capability {index} model"
            ),
            adapter_id=_required_string(
                raw_capability["adapter_id"], f"capability {index} adapter"
            ),
            status=status,
            schema_dialect=dialect,
            evidence_id=_required_string(
                raw_capability["evidence_id"], f"capability {index} evidence"
            ),
        )
        key = (
            capability.provider,
            capability.base_url,
            capability.model_id,
            capability.adapter_id,
        )
        if key in seen_keys:
            raise LiteraryStructuredOutputError("capability match key is duplicated")
        seen_keys.add(key)
        capabilities.append(capability)

    return LiteraryStructuredOutputPolicy(
        policy_id=policy_id,
        role_policies=roles,
        capabilities=tuple(capabilities),
        policy_hash=_canonical_hash(raw),
        source_path=source,
        source_sha256=sha256(source.read_bytes()).hexdigest(),
    )


def _pointer_token(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _project_transport_schema(
    value: Any,
    *,
    pointer: str = "",
) -> tuple[Any, list[dict[str, Any]]]:
    omissions: list[dict[str, Any]] = []
    if isinstance(value, Mapping):
        projected: dict[str, Any] = {}
        for key, child in value.items():
            child_pointer = pointer + "/" + _pointer_token(str(key))
            if key in TRANSPORT_ONLY_OMISSIONS:
                omissions.append(
                    {
                        "json_pointer": child_pointer,
                        "keyword": str(key),
                        "canonical_value": deepcopy(child),
                    }
                )
                continue
            lowered, nested = _project_transport_schema(
                child, pointer=child_pointer
            )
            projected[str(key)] = lowered
            omissions.extend(nested)
        return projected, omissions
    if isinstance(value, list):
        projected_rows: list[Any] = []
        for index, child in enumerate(value):
            lowered, nested = _project_transport_schema(
                child, pointer=pointer + f"/{index}"
            )
            projected_rows.append(lowered)
            omissions.extend(nested)
        return projected_rows, omissions
    return deepcopy(value), omissions


def project_transport_schema_v1(
    canonical_schema: Mapping[str, Any],
) -> tuple[dict[str, Any], tuple[Mapping[str, Any], ...]]:
    """Project provider-safe wire schema while preserving local authority."""

    if not isinstance(canonical_schema, Mapping):
        raise LiteraryStructuredOutputError("canonical schema must be an object")
    Draft202012Validator.check_schema(dict(canonical_schema))
    projected, omissions = _project_transport_schema(canonical_schema)
    if not isinstance(projected, dict):
        raise LiteraryStructuredOutputError("projected transport schema must be an object")
    if any(row["keyword"] not in TRANSPORT_ONLY_OMISSIONS for row in omissions):
        raise LiteraryStructuredOutputError("transport projection omitted a forbidden keyword")
    return deepcopy(projected), tuple(deepcopy(omissions))


def resolve_structured_output_contract(
    policy: LiteraryStructuredOutputPolicy,
    *,
    role_id: str,
    provider: str,
    base_url: str | None,
    model_id: str,
    canonical_schema: Mapping[str, Any],
) -> StructuredOutputContract:
    if provider not in SUPPORTED_PROVIDERS:
        raise LiteraryStructuredOutputError("structured-output provider is unsupported")
    Draft202012Validator.check_schema(dict(canonical_schema))
    role = policy.role(role_id)
    adapter_id = role.adapter_ids_by_provider.get(provider)
    if adapter_id is None:
        raise LiteraryStructuredOutputError(
            f"role {role_id} has no adapter for provider {provider}"
        )
    normalized_base_url = _normalized_base_url(base_url)
    matches = [
        row
        for row in policy.capabilities
        if (
            row.provider,
            row.base_url,
            row.model_id,
            row.adapter_id,
        )
        == (provider, normalized_base_url, model_id, adapter_id)
    ]
    if len(matches) > 1:
        raise LiteraryStructuredOutputError("capability lookup is ambiguous")
    capability = matches[0] if matches else None
    status = capability.status if capability else "unknown"
    if status == "unavailable":
        raise LiteraryStructuredOutputError("sealed structured-output route is unavailable")
    if role.mode == "required" and status != "verified_native":
        raise LiteraryStructuredOutputError(
            f"role {role_id} requires verified native Structured Output"
        )
    native = status == "verified_native" and role.mode != "prompt_validated"
    effective_mode = "native_schema" if native else "prompt_plus_local_validation"
    if native:
        transport_schema, omissions = _project_transport_schema(canonical_schema)
        transport_hash = _canonical_hash(transport_schema)
        dialect = capability.schema_dialect if capability else "unknown"
    else:
        transport_schema = None
        transport_hash = None
        omissions = []
        dialect = "prompt_json_local_validation_v1"
    return StructuredOutputContract(
        policy_id=policy.policy_id,
        role_id=role_id,
        provider=provider,
        base_url=normalized_base_url,
        model_id=model_id,
        adapter_id=adapter_id,
        requested_mode=role.mode,
        effective_mode=effective_mode,
        capability_status=status,
        capability_id=capability.capability_id if capability else None,
        schema_dialect=dialect,
        canonical_schema_hash=_canonical_hash(canonical_schema),
        transport_schema=transport_schema,
        transport_schema_hash=transport_hash,
        omitted_transport_constraints=tuple(omissions),
        format_repair_cap=role.format_repair_cap,
        evidence_id=capability.evidence_id if capability else None,
    )


def openai_response_format(
    contract: StructuredOutputContract,
    *,
    schema_name: str,
) -> dict[str, Any]:
    if contract.provider != "openai":
        raise LiteraryStructuredOutputError("OpenAI format needs an OpenAI contract")
    if contract.native_enforcement:
        if contract.transport_schema is None:
            raise LiteraryStructuredOutputError("native OpenAI contract lacks schema")
        return {
            "type": "json_schema",
            "json_schema": {
                "name": _required_string(schema_name, "schema_name"),
                "strict": True,
                "schema": deepcopy(dict(contract.transport_schema)),
            },
        }
    return {"type": "json_object"}


def gemini_response_json_schema(
    contract: StructuredOutputContract,
) -> dict[str, Any] | None:
    if contract.provider != "google_genai":
        raise LiteraryStructuredOutputError("Gemini schema needs a Gemini contract")
    return (
        deepcopy(dict(contract.transport_schema))
        if contract.native_enforcement and contract.transport_schema is not None
        else None
    )


def validate_structured_payload(
    payload: Any,
    *,
    canonical_schema: Mapping[str, Any],
) -> None:
    errors = sorted(
        Draft202012Validator(dict(canonical_schema)).iter_errors(payload),
        key=lambda row: tuple(str(part) for part in row.absolute_path),
    )
    if not errors:
        return
    first = errors[0]
    location = "/" + "/".join(str(part) for part in first.absolute_path)
    if location == "/":
        location = "<root>"
    raise LiteraryStructuredOutputValidationError(
        f"structured response violates canonical schema at {location}: {first.message}"
    )


def validate_contract_payload(
    contract_payload: Mapping[str, Any],
    *,
    contract: StructuredOutputContract,
) -> None:
    expected = contract.to_payload()
    if _canonical_json(contract_payload) != _canonical_json(expected):
        raise LiteraryStructuredOutputError("structured-output contract payload drifted")


def capability_matrix(
    policy: LiteraryStructuredOutputPolicy,
    *,
    role_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    selected = tuple(role_ids or policy.role_policies)
    for role_id in selected:
        policy.role(role_id)
    return {
        "schema_version": "literary_structured_output_capability_matrix_v1",
        "policy_id": policy.policy_id,
        "policy_hash": policy.policy_hash,
        "roles": {
            role_id: {
                "mode": policy.role_policies[role_id].mode,
                "format_repair_cap": policy.role_policies[role_id].format_repair_cap,
                "adapter_ids_by_provider": dict(
                    policy.role_policies[role_id].adapter_ids_by_provider
                ),
            }
            for role_id in selected
        },
        "capabilities": [
            {
                "capability_id": row.capability_id,
                "provider": row.provider,
                "base_url_class": "official" if row.base_url is None else row.base_url,
                "model_id": row.model_id,
                "adapter_id": row.adapter_id,
                "status": row.status,
                "schema_dialect": row.schema_dialect,
                "evidence_id": row.evidence_id,
            }
            for row in policy.capabilities
        ],
    }


__all__ = [
    "CAPABILITY_STATUSES",
    "LiteraryStructuredOutputError",
    "LiteraryStructuredOutputPolicy",
    "LiteraryStructuredOutputValidationError",
    "POLICY_SCHEMA_VERSION",
    "STRUCTURED_OUTPUT_MODES",
    "StructuredOutputCapability",
    "StructuredOutputContract",
    "StructuredOutputRolePolicy",
    "capability_matrix",
    "gemini_response_json_schema",
    "load_literary_structured_output_policy",
    "openai_response_format",
    "project_transport_schema_v1",
    "resolve_structured_output_contract",
    "validate_contract_payload",
    "validate_structured_payload",
]
