from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.agents.provider_profile import load_provider_profile
from pipeline.literary.chapter_cycle_profile_v1 import (
    ChapterCycleProfileError,
    load_chapter_cycle_profile,
    verify_profile_roles,
)


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
PROFILE_PATH = (
    RUNTIME_ROOT
    / "pipeline"
    / "configs"
    / "literary_chapter_cycle_profile_v1.json"
)


def test_recommended_profile_loads_and_resolves_roles() -> None:
    profile = load_chapter_cycle_profile(PROFILE_PATH)
    provider = load_provider_profile(profile.provider_profile_path())
    verify_profile_roles(profile, provider_profile=provider)
    policy = profile.to_resilience_policy(provider_profile=provider)

    assert profile.profile_id == "literary_context_cycle_recommended_v1"
    assert profile.stage_limits["b0"].prompt_token_cap == 18_000
    assert profile.stage_limits["identity_auditor"].max_calls_per_chapter == 4
    assert policy.max_transport_retries_per_request == 2
    assert policy.max_contract_repairs == 1
    assert policy.b0_contract_fallback_enabled is True
    assert policy.b0_fallback_model_id == "gpt-5.4"


def test_console_can_lower_retry_without_changing_safety_contract(
    tmp_path: Path,
) -> None:
    payload = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    payload["resilience"]["transport_retries_per_request"] = 0
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    profile = load_chapter_cycle_profile(path)
    assert profile.resilience["transport_retries_per_request"] == 0
    assert profile.locked_safety["chapter_skip_allowed"] is False


def test_profile_rejects_attempt_to_enable_chapter_skip(tmp_path: Path) -> None:
    payload = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    payload["orchestration"]["allow_chapter_skip"] = True
    path = tmp_path / "unsafe.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ChapterCycleProfileError, match="skipping"):
        load_chapter_cycle_profile(path)


def test_profile_rejects_attempt_to_weaken_locked_safety(
    tmp_path: Path,
) -> None:
    payload = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    payload["locked_safety"]["unknown_failure_action"] = "continue"
    path = tmp_path / "unsafe.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ChapterCycleProfileError, match="locked safety"):
        load_chapter_cycle_profile(path)


def test_profile_rejects_secret_or_absolute_provider_path(
    tmp_path: Path,
) -> None:
    payload = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    payload["provider_profile"] = "C:/secrets/provider.json"
    path = tmp_path / "unsafe.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ChapterCycleProfileError, match="neighboring file"):
        load_chapter_cycle_profile(path)


def test_profile_allows_explicit_primary_model_change(tmp_path: Path) -> None:
    profile = load_chapter_cycle_profile(PROFILE_PATH)
    provider_payload = json.loads(
        profile.provider_profile_path().read_text(encoding="utf-8")
    )
    provider_payload["roles"]["literary_local_conflict_auditor"][
        "model_id"
    ] = "gpt-5.4-mini"
    provider_path = tmp_path / "provider.json"
    provider_path.write_text(json.dumps(provider_payload), encoding="utf-8")
    provider = load_provider_profile(provider_path)

    verify_profile_roles(profile, provider_profile=provider)
    role_id = profile.role_bindings["local_auditor"]
    assert provider.roles[role_id].model_id == "gpt-5.4-mini"
