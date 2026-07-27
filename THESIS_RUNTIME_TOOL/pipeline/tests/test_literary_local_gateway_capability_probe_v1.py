from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pipeline.literary.local_gateway_capability_probe_v1 import (
    LocalGatewayCapabilityProbeError,
    _response_format,
    execute_local_gateway_probe_v1,
    load_probe_profile_v1,
    prepare_local_gateway_probe_v1,
)


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
CONFIG_ROOT = RUNTIME_ROOT / "pipeline" / "configs"
PROFILE = CONFIG_ROOT / "literary_local_gateway_capability_probe_v1.json"
PROVIDER = CONFIG_ROOT / "literary_provider_profile_local_gateway_premium_v1.json"
FROZEN_DB = RUNTIME_ROOT / "data" / "jobs" / "d2l_p1" / "memory.sqlite3"
GIT_HEAD = "a" * 40


def _fixture_configs(tmp_path: Path) -> tuple[Path, Path]:
    provider = json.loads(PROVIDER.read_text(encoding="utf-8"))
    provider["credentials"]["local-gpt-gateway-v1"]["relative_file"] = "token.txt"
    provider_path = tmp_path / PROVIDER.name
    provider_path.write_text(json.dumps(provider), encoding="utf-8")
    profile = json.loads(PROFILE.read_text(encoding="utf-8"))
    profile_path = tmp_path / PROFILE.name
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    (tmp_path / "token.txt").write_text("abcde\n", encoding="utf-8")
    return profile_path, tmp_path


def _payload_for_schema(schema_id: str) -> dict:
    if schema_id == "b1_inventory_current":
        return {
            "entity_candidates": [],
            "glossary_candidates": [],
            "unresolved_referents": [],
            "chapter_priority_order": [],
        }
    if schema_id == "b2_interaction_v2":
        return {
            "schema_version": "literary_b2_interaction_response_v2",
            "chapter_id": "probe_chapter",
            "window_id": "probe_window",
            "speaker_turns": [],
            "interaction_events": [],
            "review_requests": [],
        }
    if schema_id == "local_auditor_current":
        return {
            "chapter_id": "probe_chapter",
            "component_decisions": [],
            "glossary_dispositions": [],
        }
    return {
        "schema_version": "literary_b2_frame_response_v1",
        "chapter_id": "probe_chapter",
        "chapter_orientation": {
            "chapter_gist": "Transport capability probe.",
            "narrative_mode": "unknown",
            "setting_surfaces": [],
        },
        "frame_starts": [],
        "review_requests": [],
    }


def _factory(_credential):
    def call(**kwargs):
        model = kwargs["model"]
        name = kwargs["response_format"]["json_schema"]["name"]
        schema_id_by_probe = {
            "gpt_5_4_b1_inventory_current": "b1_inventory_current",
            "gpt_5_4_b2_interaction_v2": "b2_interaction_v2",
            "gpt_5_5_local_auditor_current": "local_auditor_current",
            "gpt_5_5_b2_frame_v1": "b2_frame_v1",
        }
        return SimpleNamespace(
            model=model,
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(
                            _payload_for_schema(schema_id_by_probe[name])
                        )
                    ),
                    finish_reason="stop",
                )
            ],
            usage=SimpleNamespace(
                prompt_tokens=100,
                completion_tokens=50,
                total_tokens=150,
            ),
        )

    return call


def test_checked_in_profile_exact_covers_two_models_and_schemas() -> None:
    profile = load_probe_profile_v1(PROFILE)
    assert profile.max_calls == 4
    assert [row.role_id for row in profile.probes] == [
        "literary_b0",
        "literary_b2_interaction",
        "literary_local_conflict_auditor",
        "literary_b2_frame",
    ]
    assert {row.schema_id for row in profile.probes} == {
        "b1_inventory_current",
        "b2_interaction_v2",
        "local_auditor_current",
        "b2_frame_v1",
    }


def test_prepare_and_execute_are_sealed_and_never_persist_the_bearer(
    tmp_path: Path,
) -> None:
    profile_path, credential_root = _fixture_configs(tmp_path)
    output = tmp_path / "probe"
    seal = prepare_local_gateway_probe_v1(
        output_root=output,
        profile_path=profile_path,
        credential_root=credential_root,
        frozen_db=FROZEN_DB,
        current_git_head=GIT_HEAD,
    )
    assert seal["max_calls"] == 4
    assert seal["max_retries_per_call"] == 0
    assert seal["total_token_reserve"] <= seal["hard_visible_token_cap"]
    assert "anyOf" in seal["probes"][0]["schema_keywords"]
    assert "const" in seal["probes"][1]["schema_keywords"]
    assert "anyOf" in seal["probes"][2]["schema_keywords"]
    assert "const" in seal["probes"][3]["schema_keywords"]
    report = execute_local_gateway_probe_v1(
        output_root=output,
        profile_path=profile_path,
        credential_root=credential_root,
        frozen_db=FROZEN_DB,
        current_git_head=GIT_HEAD,
        transport_factory=_factory,
    )
    assert report["status"] == "passed_exact_model_schema_probe"
    assert report["call_count"] == 4
    assert report["total_visible_tokens"] == 600
    assert "abcde" not in "".join(
        path.read_text(encoding="utf-8")
        for path in output.rglob("*.json")
    )


def test_seal_drift_halts_before_transport(tmp_path: Path) -> None:
    profile_path, credential_root = _fixture_configs(tmp_path)
    output = tmp_path / "probe"
    prepare_local_gateway_probe_v1(
        output_root=output,
        profile_path=profile_path,
        credential_root=credential_root,
        frozen_db=FROZEN_DB,
        current_git_head=GIT_HEAD,
    )
    seal_path = output / "run_seal.json"
    seal = json.loads(seal_path.read_text(encoding="utf-8"))
    seal["hard_visible_token_cap"] += 1
    seal_path.write_text(json.dumps(seal), encoding="utf-8")
    called = False

    def forbidden(_credential):
        nonlocal called
        called = True
        return _factory(_credential)

    with pytest.raises(LocalGatewayCapabilityProbeError, match="seal hash drifted"):
        execute_local_gateway_probe_v1(
            output_root=output,
            profile_path=profile_path,
            credential_root=credential_root,
            frozen_db=FROZEN_DB,
            current_git_head=GIT_HEAD,
            transport_factory=forbidden,
        )
    assert called is False


def test_transport_projection_keeps_canonical_schema_stricter() -> None:
    canonical = {
        "type": "object",
        "additionalProperties": False,
        "required": ["name", "rows"],
        "properties": {
            "name": {"type": "string", "minLength": 1},
            "rows": {
                "type": "array",
                "minItems": 1,
                "uniqueItems": True,
                "items": {"type": "string"},
            },
        },
    }
    response_format = _response_format(deepcopy(canonical), "probe")
    transported = response_format["json_schema"]["schema"]
    assert "minLength" not in transported["properties"]["name"]
    assert "minItems" not in transported["properties"]["rows"]
    assert "uniqueItems" not in transported["properties"]["rows"]
    assert canonical["properties"]["name"]["minLength"] == 1
