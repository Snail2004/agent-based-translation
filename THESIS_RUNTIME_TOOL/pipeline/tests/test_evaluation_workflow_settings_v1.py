from __future__ import annotations

import copy
from dataclasses import replace

import pytest

from pipeline.eval.contracts_v1 import (
    CanonicalPolicy,
    ContractValidationError,
    seal_payload,
)
from pipeline.eval.evaluation_workflow_settings_v1 import (
    EvaluationWorkflowSettingsAuthorityV1,
    build_evaluation_workflow_settings_v1,
    validate_evaluation_workflow_settings_v1,
)
from pipeline.tests.test_evaluation_workflow_component_v1 import _binding, _handoff


_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(),
    semantic_sequence_paths=frozenset(
        {
            ("selected_chapter_ids",),
            ("selected_arm_ids",),
            ("selected_scorer_ids",),
        }
    ),
)


def _authority() -> EvaluationWorkflowSettingsAuthorityV1:
    return EvaluationWorkflowSettingsAuthorityV1(
        benchmark_preset=_binding(
            "presets/narrow_five_chapter_d2l_v1.json", "evaluation_benchmark_preset_v1"
        ),
        evaluation_config=_binding(
            "configs/evaluation_config_v1.json", "evaluation_run_config_v1"
        ),
        scorer_set=_binding(
            "scorers/sf_qe_sf_bt_pj_v1.json", "evaluation_scorer_set_v1"
        ),
        evaluation_profiles=(
            _binding(
                "profiles/evaluation_gemini_v1.json", "evaluation_profile_v1"
            ),
            _binding(
                "profiles/evaluation_openai_v1.json", "evaluation_profile_v1"
            ),
        ),
        policy_profiles=(
            _binding(
                "policies/evaluation_policy_v1.json", "evaluation_policy_profile_v1"
            ),
        ),
        shared_selections=(
            _binding(
                "selections/evaluation_five_chapter_v1.json",
                "evaluation_shared_selection_v1",
            ),
        ),
    )


def _settings() -> dict:
    return build_evaluation_workflow_settings_v1(
        authority=_authority(),
        scoring_handoff=_handoff(),
        evaluation_profile_ref="profiles/evaluation_gemini_v1.json",
        policy_profile_ref="policies/evaluation_policy_v1.json",
        shared_selection_ref="selections/evaluation_five_chapter_v1.json",
        highlight_pair={"baseline_arm_id": "s0", "candidate_arm_id": "s1"},
    )


def _reseal(value: dict) -> dict:
    draft = copy.deepcopy(value)
    draft["settings_sha256"] = "0" * 64
    return seal_payload(draft, policy=_POLICY, hash_path=("settings_sha256",))


def test_settings_resolve_only_registered_selectable_refs() -> None:
    settings = _settings()
    assert settings["schema_version"] == "1.1.0"
    assert settings["selected_chapter_ids"] == [
        "d2l_preliminaries",
        "d2l_linear_networks",
        "d2l_multilayer_perceptrons",
        "d2l_deep_learning_computation",
        "d2l_convolutional_neural_networks",
    ]
    assert settings["selected_arm_ids"] == [
        "s0",
        "s1",
        "community",
        "google_nmt",
        "llm_lc",
    ]
    assert settings["selected_scorer_ids"] == ["sf_qe", "sf_bt", "pj"]
    assert settings["evaluation_profile_ref"]["artifact_ref"].endswith(
        "evaluation_gemini_v1.json"
    )
    assert settings["highlight_pair"] == {
        "baseline_arm_id": "s0",
        "candidate_arm_id": "s1",
    }
    assert settings["scoring_handoff"]["input_set_sha256"] == _handoff()[
        "input_set_sha256"
    ]


def test_unknown_or_unregistered_modal_fields_fail_closed() -> None:
    settings = _settings()
    settings["temperature"] = 0
    with pytest.raises(ContractValidationError, match="unknown"):
        validate_evaluation_workflow_settings_v1(
            settings, authority=_authority(), scoring_handoff=_handoff()
        )

    with pytest.raises(ContractValidationError, match="server-registered"):
        build_evaluation_workflow_settings_v1(
            authority=_authority(),
            scoring_handoff=_handoff(),
            evaluation_profile_ref="profiles/client_supplied.json",
            policy_profile_ref=None,
            shared_selection_ref="selections/evaluation_five_chapter_v1.json",
            highlight_pair=None,
        )


def test_resealed_foreign_profile_is_rejected_by_server_authority() -> None:
    settings = _settings()
    settings["evaluation_profile_ref"] = _binding(
        "profiles/foreign.json", "evaluation_profile_v1"
    )
    settings = _reseal(settings)
    with pytest.raises(ContractValidationError, match="server-owned|catalog"):
        validate_evaluation_workflow_settings_v1(
            settings, authority=_authority(), scoring_handoff=_handoff()
        )


def test_exact_five_chapter_and_scorer_authority_cannot_drift() -> None:
    bad_chapters = replace(
        _authority(), chapter_ids=("d2l_multilayer_perceptrons",)
    )
    with pytest.raises(ContractValidationError, match="five-chapter"):
        build_evaluation_workflow_settings_v1(
            authority=bad_chapters,
            scoring_handoff=_handoff(),
            evaluation_profile_ref="profiles/evaluation_gemini_v1.json",
            policy_profile_ref=None,
            shared_selection_ref="selections/evaluation_five_chapter_v1.json",
            highlight_pair=None,
        )

    bad_scorers = replace(_authority(), scorer_ids=("sf_qe", "pj"))
    with pytest.raises(ContractValidationError, match="SF-QE/SF-BT/PJ"):
        build_evaluation_workflow_settings_v1(
            authority=bad_scorers,
            scoring_handoff=_handoff(),
            evaluation_profile_ref="profiles/evaluation_gemini_v1.json",
            policy_profile_ref=None,
            shared_selection_ref="selections/evaluation_five_chapter_v1.json",
            highlight_pair=None,
        )


def test_registered_universe_allows_sealed_ordered_subsets() -> None:
    settings = build_evaluation_workflow_settings_v1(
        authority=_authority(),
        scoring_handoff=_handoff(),
        evaluation_profile_ref="profiles/evaluation_gemini_v1.json",
        policy_profile_ref=None,
        shared_selection_ref="selections/evaluation_five_chapter_v1.json",
        selected_chapter_ids=(
            "d2l_multilayer_perceptrons",
            "d2l_deep_learning_computation",
        ),
        selected_arm_ids=("s0", "s1", "google_nmt"),
        selected_scorer_ids=("sf_qe", "pj"),
        highlight_pair={"baseline_arm_id": "s1", "candidate_arm_id": "google_nmt"},
    )
    assert settings["selected_chapter_ids"] == [
        "d2l_multilayer_perceptrons",
        "d2l_deep_learning_computation",
    ]
    assert settings["selected_arm_ids"] == ["s0", "s1", "google_nmt"]
    assert settings["selected_scorer_ids"] == ["sf_qe", "pj"]


@pytest.mark.parametrize(
    ("field", "value", "match"),
    (
        (
            "selected_chapter_ids",
            ("d2l_multilayer_perceptrons", "d2l_preliminaries"),
            "server-owned order",
        ),
        ("selected_arm_ids", ("s0",), "at least 2"),
        ("selected_scorer_ids", ("sf_qe", "sf_qe"), "unique"),
        ("selected_scorer_ids", ("sf_qe", "foreign"), "unregistered"),
    ),
)
def test_invalid_scope_selection_fails_closed(
    field: str, value: tuple[str, ...], match: str
) -> None:
    kwargs = {
        "selected_chapter_ids": None,
        "selected_arm_ids": None,
        "selected_scorer_ids": None,
    }
    kwargs[field] = value
    with pytest.raises(ContractValidationError, match=match):
        build_evaluation_workflow_settings_v1(
            authority=_authority(),
            scoring_handoff=_handoff(),
            evaluation_profile_ref="profiles/evaluation_gemini_v1.json",
            policy_profile_ref=None,
            shared_selection_ref="selections/evaluation_five_chapter_v1.json",
            highlight_pair=None,
            **kwargs,
        )


def test_highlight_pair_must_be_inside_selected_arms() -> None:
    with pytest.raises(ContractValidationError, match="enum"):
        build_evaluation_workflow_settings_v1(
            authority=_authority(),
            scoring_handoff=_handoff(),
            evaluation_profile_ref="profiles/evaluation_gemini_v1.json",
            policy_profile_ref=None,
            shared_selection_ref="selections/evaluation_five_chapter_v1.json",
            selected_arm_ids=("s0", "s1"),
            highlight_pair={
                "baseline_arm_id": "s1",
                "candidate_arm_id": "google_nmt",
            },
        )


def test_settings_bind_exact_handoff_and_reject_self_hashed_foreign_input_set() -> None:
    settings = _settings()
    settings["scoring_handoff"]["input_set_sha256"] = "f" * 64
    settings = _reseal(settings)
    with pytest.raises(ContractValidationError, match="exact parent-owned"):
        validate_evaluation_workflow_settings_v1(
            settings, authority=_authority(), scoring_handoff=_handoff()
        )


def test_highlight_pair_is_optional_but_must_be_distinct_known_arms() -> None:
    no_highlight = build_evaluation_workflow_settings_v1(
        authority=_authority(),
        scoring_handoff=_handoff(),
        evaluation_profile_ref="profiles/evaluation_openai_v1.json",
        policy_profile_ref=None,
        shared_selection_ref="selections/evaluation_five_chapter_v1.json",
        highlight_pair=None,
    )
    assert no_highlight["highlight_pair"] is None

    with pytest.raises(ContractValidationError, match="distinct"):
        build_evaluation_workflow_settings_v1(
            authority=_authority(),
            scoring_handoff=_handoff(),
            evaluation_profile_ref="profiles/evaluation_openai_v1.json",
            policy_profile_ref=None,
            shared_selection_ref="selections/evaluation_five_chapter_v1.json",
            highlight_pair={"baseline_arm_id": "s1", "candidate_arm_id": "s1"},
        )


def test_runtime_authority_cannot_smuggle_gold_or_result_callback() -> None:
    bad = replace(
        _authority(),
        evaluation_profiles=(
            _binding("profiles/gold_override.json", "evaluation_profile_v1"),
        ),
    )
    with pytest.raises(ContractValidationError, match="forbidden_runtime_authority"):
        build_evaluation_workflow_settings_v1(
            authority=bad,
            scoring_handoff=_handoff(),
            evaluation_profile_ref="profiles/gold_override.json",
            policy_profile_ref=None,
            shared_selection_ref="selections/evaluation_five_chapter_v1.json",
            highlight_pair=None,
        )


def test_validation_does_not_mutate_settings_or_authority() -> None:
    authority = _authority()
    settings = _settings()
    authority_before = copy.deepcopy(authority)
    settings_before = copy.deepcopy(settings)
    validate_evaluation_workflow_settings_v1(
        settings, authority=authority, scoring_handoff=_handoff()
    )
    assert authority == authority_before
    assert settings == settings_before
