from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.agents.provider_profile import (
    ProviderProfileError,
    load_provider_profile,
    resolve_role_credential,
    resolve_role_credentials,
)


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
PROFILE = RUNTIME_ROOT / "pipeline" / "configs" / "literary_provider_profile_v1.json"


def _credential_root(tmp_path: Path) -> Path:
    (tmp_path / "CKEY.txt").write_text("sk-" + "c" * 64, encoding="utf-8")
    (tmp_path / "GEMINI-KEY.txt").write_text("sk-" + "s" * 40, encoding="utf-8")
    (tmp_path / "GEMINI-KEY-FREE.txt").write_text(
        "\n".join("AIza" + str(index) * 35 for index in range(1, 6)),
        encoding="utf-8",
    )
    (tmp_path / "OPENAI-KEY-1.txt").write_text("sk-" + "a" * 40, encoding="utf-8")
    (tmp_path / "OPENAI-KEY-2.txt").write_text("sk-" + "b" * 40, encoding="utf-8")
    return tmp_path


def test_profile_resolves_ckey_and_openai_without_secret_repr(
    tmp_path: Path,
) -> None:
    profile = load_provider_profile(PROFILE)
    root = _credential_root(tmp_path)
    ckey = resolve_role_credential(
        profile, role_id="literary_b0", credential_root=root
    )
    openai = resolve_role_credentials(
        profile,
        role_id="literary_local_conflict_auditor",
        credential_root=root,
    )
    assert ckey.quota_bucket_id == "ckey-account-v1"
    assert ckey.source_path == root / "CKEY.txt"
    assert "sk-" not in repr(ckey)
    assert ckey.base_url == "https://api.xah.io"
    assert ckey.request_timeout_ms == 120_000
    assert profile.roles["literary_b0"].model_id == (
        "vuduythanh2023/gemini-3.5-flash"
    )
    assert all(row.request_timeout_ms == 120_000 for row in openai)
    assert [row.quota_bucket_id for row in openai] == [
        "openai-row1",
        "openai-row2",
    ]
    assert profile.roles["literary_b0"].bucket_order == ("ckey-account-v1",)
    assert "gemini-free-row5-v2" in profile.provider_bucket_ids("google_genai")


def test_profile_rejects_credential_path_escape(tmp_path: Path) -> None:
    raw = json.loads(PROFILE.read_text(encoding="utf-8"))
    raw["credentials"]["gemini-shopapi-v1"]["relative_file"] = "../secret.txt"
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ProviderProfileError, match="contained relative path"):
        load_provider_profile(path)


def _single_openai_profile(
    tmp_path: Path, *, base_url: str, token: str
) -> tuple[Path, Path]:
    credential_root = tmp_path / "credentials"
    credential_root.mkdir()
    (credential_root / "LOCAL-GATEWAY.txt").write_text(token, encoding="utf-8")
    payload = {
        "schema_version": "thesis_provider_profile_v1",
        "profile_id": "synthetic_loopback_profile",
        "credentials": {
            "synthetic-loopback-bucket": {
                "provider": "openai",
                "credential_revision": "synthetic-v1",
                "relative_file": "LOCAL-GATEWAY.txt",
                "nonempty_line": 1,
                "enabled": True,
                "base_url": base_url,
                "request_timeout_ms": 10_000,
            }
        },
        "roles": {
            "synthetic_role": {
                "provider": "openai",
                "model_id": "synthetic-model-v1",
                "bucket_order": ["synthetic-loopback-bucket"],
            }
        },
    }
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(payload), encoding="utf-8")
    return profile_path, credential_root


@pytest.mark.parametrize(
    "base_url",
    [
        "http://localhost/v1",
        "http://localhost:8317/v1",
        "http://127.0.0.1:8317/v1",
        "http://[::1]:8317/v1",
    ],
)
def test_short_opaque_openai_bearer_is_allowed_only_on_loopback(
    tmp_path: Path, base_url: str
) -> None:
    profile_path, credential_root = _single_openai_profile(
        tmp_path, base_url=base_url, token="abcde"
    )
    credential = resolve_role_credential(
        load_provider_profile(profile_path),
        role_id="synthetic_role",
        credential_root=credential_root,
    )
    assert credential.base_url == base_url
    assert credential.quota_bucket_id == "synthetic-loopback-bucket"
    assert "abcde" not in repr(credential)


@pytest.mark.parametrize(
    "base_url",
    [
        "https://api.openai.com/v1",
        "http://localhost.example:8317/v1",
        "http://user@localhost:8317/v1",
        "http://localhost:8317/v1?route=local",
        "http://localhost:8317/v1#fragment",
        "http://0.0.0.0:8317/v1",
        "http://host.docker.internal:8317/v1",
        "http://localhost:abc/v1",
        "http://localhost:99999/v1",
        "http://localhost:/v1",
        "http://localhost:0/v1",
        "http://local\thost:8317/v1",
        "http://localhost:8317/v1\n",
        "http://[::1%25evil]:8317/v1",
    ],
)
def test_short_opaque_openai_bearer_is_rejected_outside_explicit_loopback(
    tmp_path: Path, base_url: str
) -> None:
    profile_path, credential_root = _single_openai_profile(
        tmp_path, base_url=base_url, token="abcde"
    )
    with pytest.raises(ProviderProfileError, match="malformed"):
        resolve_role_credential(
            load_provider_profile(profile_path),
            role_id="synthetic_role",
            credential_root=credential_root,
        )


@pytest.mark.parametrize("token", ["abcd", "abc def", "abc\tdef", "ébcde"])
def test_loopback_openai_bearer_rejects_bad_shape(
    tmp_path: Path, token: str
) -> None:
    profile_path, credential_root = _single_openai_profile(
        tmp_path, base_url="http://localhost:8317/v1", token=token
    )
    with pytest.raises(ProviderProfileError, match="malformed"):
        resolve_role_credential(
            load_provider_profile(profile_path),
            role_id="synthetic_role",
            credential_root=credential_root,
        )


def test_remote_openai_and_google_secret_rules_are_unchanged(tmp_path: Path) -> None:
    profile = load_provider_profile(PROFILE)
    root = _credential_root(tmp_path)
    openai = resolve_role_credential(
        profile,
        role_id="literary_local_conflict_auditor",
        credential_root=root,
        quota_bucket_id="openai-row1",
    )
    google = resolve_role_credential(
        profile,
        role_id="literary_b0",
        credential_root=root,
    )
    assert openai.provider == "openai"
    assert google.provider == "google_genai"
