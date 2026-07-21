"""In-memory credential resolution for the shared LLM backend."""

from __future__ import annotations

from hashlib import sha256
import hmac
import os
from typing import Mapping, Protocol

from .contracts_v1 import ContractValidationError, validate_api_source


class CredentialProvider(Protocol):
    def resolve(self, credential_ref: str) -> str | None: ...


class ResolvedCredential:
    """A deliberately non-serializable, redacted credential value."""

    __slots__ = ("_value", "credential_ref", "commitment")

    def __init__(self, *, value: str, credential_ref: str, commitment: str) -> None:
        self._value = value
        self.credential_ref = credential_ref
        self.commitment = commitment

    def reveal_for_transport(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return (
            "ResolvedCredential(credential_ref="
            f"{self.credential_ref!r}, commitment={self.commitment!r}, value=<redacted>)"
        )

    def __str__(self) -> str:
        return "<redacted credential>"

    def __reduce__(self):
        raise TypeError("resolved credentials may not be serialized")


class MappingCredentialProvider:
    """Test/runtime provider backed by an already in-memory mapping."""

    def __init__(self, values: Mapping[str, str]) -> None:
        self._values = dict(values)

    def resolve(self, credential_ref: str) -> str | None:
        return self._values.get(credential_ref)


class EnvironmentCredentialProvider:
    """Resolve opaque references through an explicit ref-to-env-name mapping."""

    def __init__(self, environment_names: Mapping[str, str]) -> None:
        self._environment_names = dict(environment_names)

    def resolve(self, credential_ref: str) -> str | None:
        variable = self._environment_names.get(credential_ref)
        return None if variable is None else os.environ.get(variable)


def credential_commitment(value: str) -> str:
    _validate_secret_value(value)
    return sha256(value.encode("utf-8")).hexdigest()


def resolve_source_credential(
    *, source: Mapping[str, object], provider: CredentialProvider
) -> ResolvedCredential | None:
    """Resolve exactly the credential committed by one validated API source."""

    normalized = validate_api_source(source)
    credential_ref = normalized["credential_ref"]
    if credential_ref is None:
        if normalized["source_class"] != "local_in_process":
            raise ContractValidationError("network source lacks a credential reference")
        return None
    value = provider.resolve(credential_ref)
    if value is None:
        raise ContractValidationError("configured credential reference is unavailable")
    observed = credential_commitment(value)
    expected = normalized["credential_commitment"]
    if not hmac.compare_digest(observed, expected):
        raise ContractValidationError("resolved credential commitment mismatch")
    return ResolvedCredential(
        value=value,
        credential_ref=credential_ref,
        commitment=expected,
    )


def _validate_secret_value(value: str) -> None:
    if not isinstance(value, str) or not value:
        raise ContractValidationError("credential value must be a nonempty string")
    if any(character.isspace() or ord(character) < 32 for character in value):
        raise ContractValidationError("credential value contains whitespace or controls")
