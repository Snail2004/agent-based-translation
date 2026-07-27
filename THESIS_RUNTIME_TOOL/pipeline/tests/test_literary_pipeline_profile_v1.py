from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.agents.provider_profile import load_provider_profile
from pipeline.literary.chapter_cycle_profile_v1 import (
    load_chapter_cycle_profile,
    verify_profile_roles,
)
from pipeline.literary.literary_pipeline_profile_v1 import (
    LiteraryPipelineProfileError,
    load_literary_pipeline_profile,
    public_stage_plan,
)
from pipeline.scripts.run_b0_entity_inventory_experiment import (
    INPUT_TOKEN_CAP as INITIAL_B1_INPUT_TOKEN_CAP,
)
from pipeline.scripts.run_b0_prior_challenge_experiment import (
    INPUT_TOKEN_CAP as PRIOR_B1_INPUT_TOKEN_CAP,
)
from pipeline.scripts.run_b0_inventory_gemini_comparison import STAGE_CAPS
from pipeline.scripts.run_b0_entity_conflict_auditor_experiment import (
    _transport_config as local_auditor_transport_config,
)


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
PROFILE = RUNTIME_ROOT / "pipeline" / "configs" / "literary_pipeline_profile_v1.json"
PROFILE_V2 = RUNTIME_ROOT / "pipeline" / "configs" / "literary_pipeline_profile_v2.json"
PROVIDER = RUNTIME_ROOT / "pipeline" / "configs" / "literary_provider_profile_v2.json"
CHAPTER_PROFILE = (
    RUNTIME_ROOT / "pipeline" / "configs" / "literary_chapter_cycle_profile_v2.json"
)
PREMIUM_PROFILE = (
    RUNTIME_ROOT
    / "pipeline"
    / "configs"
    / "literary_pipeline_profile_local_gateway_premium_v1.json"
)


def test_console_profile_exposes_b1_without_renaming_implementation() -> None:
    profile = load_literary_pipeline_profile(PROFILE_V2)
    projected = public_stage_plan(
        profile,
        [
            {"stage_name": "b0", "stage_id": "ch001_b0"},
            {"stage_name": "b0_prior", "stage_id": "ch002_b0_prior"},
            {"stage_name": "local_auditor", "stage_id": "ch001_local_auditor"},
        ],
    )

    assert profile.public_stages["b2"].enabled is False
    assert projected[0]["public_stage_name"] == "b1"
    assert projected[0]["implementation_stage_name"] == "b0"
    assert projected[1]["public_stage_name"] == "b1"
    assert projected[1]["implementation_stage_name"] == "b0_prior"
    assert projected[2]["public_stage_name"] == "local_auditor"
    assert profile.structured_output_policy is not None
    assert profile.console_controls["expose_structured_output_mode"] is True


def test_legacy_console_profile_remains_loadable() -> None:
    profile = load_literary_pipeline_profile(PROFILE)

    assert profile.structured_output_policy is None


def test_literary_provider_v2_contains_only_ckey_and_openai_key1() -> None:
    payload = json.loads(PROVIDER.read_text(encoding="utf-8"))
    assert set(payload["credentials"]) == {"ckey-account-v1", "openai-row1"}
    assert payload["credentials"]["openai-row1"]["relative_file"] == (
        "OPENAI-KEY-1.txt"
    )
    assert "OPENAI-KEY-2" not in PROVIDER.read_text(encoding="utf-8")
    for role in payload["roles"].values():
        if role["provider"] == "openai":
            assert role["bucket_order"] == ["openai-row1"]


def test_console_chapter_profile_exposes_measured_b1_reserve_cap() -> None:
    payload = json.loads(CHAPTER_PROFILE.read_text(encoding="utf-8"))
    assert payload["stage_limits"]["b0"]["prompt_token_cap"] == 20_000
    assert INITIAL_B1_INPUT_TOKEN_CAP == 20_000
    assert PRIOR_B1_INPUT_TOKEN_CAP == 20_000
    assert STAGE_CAPS["b0"]["input"] == 20_000


def test_local_gateway_premium_profile_seals_expected_model_split() -> None:
    pipeline_profile = load_literary_pipeline_profile(PREMIUM_PROFILE)
    cycle = load_chapter_cycle_profile(pipeline_profile.chapter_cycle_profile_path)
    provider = load_provider_profile(cycle.provider_profile_path())
    verify_profile_roles(cycle, provider_profile=provider)

    assert pipeline_profile.usage_baseline.quota_bucket_id == (
        "local-gpt-gateway-v1"
    )
    assert pipeline_profile.structured_output_policy is not None
    assert cycle.orchestration["default_stop_after_chapter_count"] == 1
    assert cycle.resilience["transport_retries_per_request"] == 0
    assert provider.roles[cycle.role_bindings["b0"]].model_id == "gpt-5.4"
    for stage in ("local_auditor", "stable_claim_auditor", "identity_auditor"):
        role = provider.roles[cycle.role_bindings[stage]]
        assert role.model_id == "gpt-5.5"
        assert role.bucket_order == ("local-gpt-gateway-v1",)

    transport = local_auditor_transport_config(
        ("local-gpt-gateway-v1",), model_id="gpt-5.5"
    )
    auditor_gate = transport.role_quota_gate_ids["auditor"]
    assert len(auditor_gate) == 1
    assert transport.quota_gates[auditor_gate[0]]["model_id"] == "gpt-5.5"


def test_profile_rejects_enabled_b2_before_implementation(tmp_path: Path) -> None:
    payload = json.loads(PROFILE_V2.read_text(encoding="utf-8"))
    payload["public_stage_contract"]["b2"] = {
        "enabled": True,
        "implementation_role": "literary_b2",
        "implementation_stage_names": ["b2"],
    }
    for relative in (
        "literary_chapter_cycle_profile_v2.json",
        "literary_provider_profile_v2.json",
        "literary_openai_usage_baseline_v1.json",
        "literary_structured_output_policy_v1.json",
    ):
        source = PROFILE_V2.parent / relative
        (tmp_path / relative).write_bytes(source.read_bytes())
    design_dir = tmp_path.parent / "design"
    design_dir.mkdir(exist_ok=True)
    (design_dir / "LITERARY_PROMPT_DESIGN.md").write_text("fixture", encoding="utf-8")
    payload["design_doc"] = "../design/LITERARY_PROMPT_DESIGN.md"
    target = tmp_path / "profile.json"
    target.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(LiteraryPipelineProfileError, match="B2 is not implemented"):
        load_literary_pipeline_profile(target)
