from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import ipaddress
import json
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit


PROFILE_SCHEMA_VERSION = "thesis_provider_profile_v1"
SUPPORTED_PROVIDERS = frozenset({"google_genai", "openai"})


class ProviderProfileError(ValueError):
    pass


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(payload).hexdigest()


@dataclass(frozen=True)
class CredentialRef:
    quota_bucket_id: str
    provider: str
    credential_revision: str
    relative_file: str
    nonempty_line: int
    enabled: bool
    base_url: str | None
    request_timeout_ms: int


@dataclass(frozen=True)
class ProviderRole:
    role_id: str
    provider: str
    model_id: str
    bucket_order: tuple[str, ...]


@dataclass(frozen=True)
class ProviderProfile:
    profile_id: str
    credentials: Mapping[str, CredentialRef]
    roles: Mapping[str, ProviderRole]
    profile_hash: str
    source_path: Path

    def provider_bucket_ids(self, provider: str) -> tuple[str, ...]:
        return tuple(
            sorted(
                bucket_id
                for bucket_id, ref in self.credentials.items()
                if ref.provider == provider
            )
        )


@dataclass(frozen=True)
class ResolvedCredential:
    quota_bucket_id: str
    provider: str
    credential_revision: str
    commitment: str
    source_path: Path
    nonempty_line: int
    base_url: str | None
    request_timeout_ms: int
    secret: str = field(repr=False)


def _expect_exact_keys(
    value: Mapping[str, Any], expected: set[str], label: str
) -> None:
    observed = set(value)
    if observed != expected:
        raise ProviderProfileError(
            f"{label} keys differ; missing={sorted(expected - observed)}, "
            f"extra={sorted(observed - expected)}"
        )


def _relative_credential_path(raw: Any, label: str) -> str:
    value = str(raw or "")
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise ProviderProfileError(f"{label} must be a contained relative path")
    return value


def load_provider_profile(path: Path) -> ProviderProfile:
    source = Path(path).resolve()
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise ProviderProfileError(f"cannot load provider profile: {source}") from exc
    if not isinstance(raw, dict):
        raise ProviderProfileError("provider profile must be a JSON object")
    _expect_exact_keys(
        raw,
        {"schema_version", "profile_id", "credentials", "roles"},
        "provider profile",
    )
    if raw["schema_version"] != PROFILE_SCHEMA_VERSION:
        raise ProviderProfileError("foreign provider profile schema")
    profile_id = str(raw["profile_id"] or "")
    if not profile_id:
        raise ProviderProfileError("provider profile id is empty")
    raw_credentials = raw["credentials"]
    raw_roles = raw["roles"]
    if not isinstance(raw_credentials, dict) or not raw_credentials:
        raise ProviderProfileError("provider profile needs credential references")
    if not isinstance(raw_roles, dict) or not raw_roles:
        raise ProviderProfileError("provider profile needs roles")

    credentials: dict[str, CredentialRef] = {}
    for bucket_id, value in raw_credentials.items():
        if not isinstance(value, dict):
            raise ProviderProfileError(f"credential {bucket_id} must be an object")
        _expect_exact_keys(
            value,
            {
                "provider",
                "credential_revision",
                "relative_file",
                "nonempty_line",
                "enabled",
                "base_url",
                "request_timeout_ms",
            },
            f"credential {bucket_id}",
        )
        provider = str(value["provider"] or "")
        if provider not in SUPPORTED_PROVIDERS:
            raise ProviderProfileError(f"credential {bucket_id} has foreign provider")
        revision = str(value["credential_revision"] or "")
        line = value["nonempty_line"]
        enabled = value["enabled"]
        base_url_value = value["base_url"]
        request_timeout_ms = value["request_timeout_ms"]
        if not revision:
            raise ProviderProfileError(f"credential {bucket_id} has empty revision")
        if not isinstance(line, int) or isinstance(line, bool) or line <= 0:
            raise ProviderProfileError(
                f"credential {bucket_id} has invalid nonempty line"
            )
        if not isinstance(enabled, bool):
            raise ProviderProfileError(f"credential {bucket_id} enabled must be bool")
        if (
            not isinstance(request_timeout_ms, int)
            or isinstance(request_timeout_ms, bool)
            or not 1_000 <= request_timeout_ms <= 900_000
        ):
            raise ProviderProfileError(
                f"credential {bucket_id} request timeout is invalid"
            )
        if base_url_value is not None:
            if (
                not isinstance(base_url_value, str)
                or not base_url_value.startswith(("https://", "http://"))
            ):
                raise ProviderProfileError(
                    f"credential {bucket_id} base URL is invalid"
                )
            base_url = base_url_value.rstrip("/")
        else:
            base_url = None
        credentials[str(bucket_id)] = CredentialRef(
            quota_bucket_id=str(bucket_id),
            provider=provider,
            credential_revision=revision,
            relative_file=_relative_credential_path(
                value["relative_file"], f"credential {bucket_id} file"
            ),
            nonempty_line=line,
            enabled=enabled,
            base_url=base_url,
            request_timeout_ms=request_timeout_ms,
        )

    roles: dict[str, ProviderRole] = {}
    for role_id, value in raw_roles.items():
        if not isinstance(value, dict):
            raise ProviderProfileError(f"role {role_id} must be an object")
        _expect_exact_keys(
            value,
            {"provider", "model_id", "bucket_order"},
            f"role {role_id}",
        )
        provider = str(value["provider"] or "")
        model_id = str(value["model_id"] or "")
        order = value["bucket_order"]
        if provider not in SUPPORTED_PROVIDERS:
            raise ProviderProfileError(f"role {role_id} has foreign provider")
        if not model_id or "latest" in model_id.lower():
            raise ProviderProfileError(f"role {role_id} model must be pinned")
        if (
            not isinstance(order, list)
            or not order
            or any(not isinstance(row, str) or not row for row in order)
            or len(set(order)) != len(order)
        ):
            raise ProviderProfileError(f"role {role_id} bucket order is invalid")
        for bucket_id in order:
            ref = credentials.get(bucket_id)
            if ref is None or not ref.enabled or ref.provider != provider:
                raise ProviderProfileError(
                    f"role {role_id} references unavailable bucket {bucket_id}"
                )
        roles[str(role_id)] = ProviderRole(
            role_id=str(role_id),
            provider=provider,
            model_id=model_id,
            bucket_order=tuple(order),
        )
    return ProviderProfile(
        profile_id=profile_id,
        credentials=credentials,
        roles=roles,
        profile_hash=_canonical_hash(raw),
        source_path=source,
    )


def _read_secret(ref: CredentialRef, credential_root: Path) -> ResolvedCredential:
    root = Path(credential_root).resolve()
    source = (root / ref.relative_file).resolve()
    try:
        source.relative_to(root)
    except ValueError as exc:
        raise ProviderProfileError("credential path escapes credential root") from exc
    try:
        values = [
            row.strip()
            for row in source.read_text(encoding="utf-8").splitlines()
            if row.strip()
        ]
    except OSError as exc:
        raise ProviderProfileError(
            f"cannot read credential source: {ref.relative_file}"
        ) from exc
    if ref.nonempty_line > len(values):
        raise ProviderProfileError(
            f"credential source lacks configured row: {ref.relative_file}"
        )
    secret = values[ref.nonempty_line - 1]
    if ref.provider == "google_genai":
        allowed_prefixes = ("sk-", "AIza", "AQ.A") if ref.base_url else ("AIza", "AQ.A")
        valid = secret.startswith(allowed_prefixes) and len(secret) >= 35
    elif ref.provider == "openai" and _is_explicit_loopback_url(ref.base_url):
        valid = (
            5 <= len(secret) <= 4096
            and all(0x21 <= ord(character) <= 0x7E for character in secret)
        )
    else:
        valid = secret.startswith("sk-") and len(secret) >= 20
    if not valid:
        raise ProviderProfileError(
            f"credential source is malformed: {ref.relative_file}"
        )
    return ResolvedCredential(
        quota_bucket_id=ref.quota_bucket_id,
        provider=ref.provider,
        credential_revision=ref.credential_revision,
        commitment=_canonical_hash({"credential": secret}),
        source_path=source,
        nonempty_line=ref.nonempty_line,
        base_url=ref.base_url,
        request_timeout_ms=ref.request_timeout_ms,
        secret=secret,
    )


def _is_explicit_loopback_url(value: str | None) -> bool:
    if value is None or not all(0x21 <= ord(character) <= 0x7E for character in value):
        return False
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
        if (
            parsed.scheme not in {"http", "https"}
            or host is None
            or "%" in host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.netloc.endswith(":")
            or (port is not None and not 1 <= port <= 65535)
        ):
            return False
        if host.casefold() == "localhost":
            return True
        return ipaddress.ip_address(host).is_loopback
    except (ValueError, UnicodeError):
        return False


def resolve_role_credential(
    profile: ProviderProfile,
    *,
    role_id: str,
    credential_root: Path,
    quota_bucket_id: str | None = None,
) -> ResolvedCredential:
    role = profile.roles.get(role_id)
    if role is None:
        raise ProviderProfileError(f"provider profile lacks role {role_id}")
    selected = quota_bucket_id or role.bucket_order[0]
    if selected not in role.bucket_order:
        raise ProviderProfileError(
            f"bucket {selected} is not enabled for role {role_id}"
        )
    return _read_secret(profile.credentials[selected], credential_root)


def resolve_role_credentials(
    profile: ProviderProfile, *, role_id: str, credential_root: Path
) -> tuple[ResolvedCredential, ...]:
    role = profile.roles.get(role_id)
    if role is None:
        raise ProviderProfileError(f"provider profile lacks role {role_id}")
    return tuple(
        _read_secret(profile.credentials[bucket_id], credential_root)
        for bucket_id in role.bucket_order
    )
