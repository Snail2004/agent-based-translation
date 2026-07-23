from __future__ import annotations

import json

from pipeline.llm_backend import canonical_sha256
from pipeline.prepass.d2l_shared_llm_profiles_v1 import (
    ROLE_PRESETS,
    get_role_preset,
    role_manifest,
)


def test_role_manifest_keeps_current_and_legacy_authority_distinct() -> None:
    manifest = role_manifest()
    body = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    assert manifest["manifest_sha256"] == canonical_sha256(body)
    expected = {
        "d2l.candidate_discovery",
        "d2l.b2.admission",
        "d2l.b2.morphology",
        "d2l.b2.target_collision",
        "d2l.b2.multi_target",
        "d2l.translator.s0",
        "d2l.translator.s1",
        "d2l.legacy.builder_v2",
        "d2l.legacy.term_auditor",
        "d2l.legacy.target_decollision",
        "d2l.legacy.canonical_reelection",
    }
    assert set(ROLE_PRESETS) == expected
    assert len({row.preset_id for row in ROLE_PRESETS.values()}) == len(expected)
    assert (
        get_role_preset("d2l.translator.s0").namespaces
        != get_role_preset("d2l.translator.s1").namespaces
    )
    assert {
        row.lifecycle for row in ROLE_PRESETS.values()
    } == {"active", "legacy_parity"}


def test_manifest_contains_no_secret_or_evaluation_authority() -> None:
    rendered = json.dumps(role_manifest(), sort_keys=True).casefold()
    for forbidden in (
        "api_key",
        "bearer",
        "gold",
        "oracle",
        "human_reference",
        "expected_answer",
        "score_override",
    ):
        assert forbidden not in rendered


def test_current_model_split_is_explicit() -> None:
    assert (
        get_role_preset("d2l.candidate_discovery").requested_model_id
        == "gemini-3.5-flash"
    )
    for role_id in (
        "d2l.b2.admission",
        "d2l.b2.morphology",
        "d2l.b2.target_collision",
        "d2l.b2.multi_target",
    ):
        assert get_role_preset(role_id).requested_model_id == "gpt-5.5"
    assert get_role_preset("d2l.translator.s0").requested_model_id == "gpt-5.4-mini"
