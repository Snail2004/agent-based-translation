from __future__ import annotations

import copy
import json
import math
from pathlib import Path

import pytest

from pipeline.eval.contracts_v1 import ContractValidationError, canonical_sha256
from pipeline.eval.d2l_input_v1 import (
    D2L_CANONICAL_POLICY,
    seal_d2l_evaluation_input,
    validate_d2l_evaluation_input,
)


FIXTURES = Path(__file__).parent / "fixtures" / "evaluation_v1"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _reseal(payload: dict) -> dict:
    payload["integrity"]["artifact_set_sha256"] = canonical_sha256(
        {"artifacts": payload["artifacts"]}, policy=D2L_CANONICAL_POLICY
    )
    return seal_d2l_evaluation_input(payload)


def test_valid_fixture_is_closed_gold_free_and_input_is_immutable():
    payload = _load("d2l_input_valid.json")
    before = copy.deepcopy(payload)

    normalized = validate_d2l_evaluation_input(payload)

    assert payload == before
    assert normalized is not payload
    assert normalized["schema_id"] == "D2LEvaluationInputV1"
    assert [row["block_id"] for row in normalized["blocks"]] == ["b001", "b002"]
    assert [row["block_id"] for row in normalized["translations"]] == ["b001", "b002"]


def test_recursive_forbidden_fixture_is_rejected_before_unknown_key_handling():
    with pytest.raises(ContractValidationError) as exc_info:
        validate_d2l_evaluation_input(_load("d2l_input_forbidden_gold.json"))

    assert exc_info.value.code == "forbidden_runtime_data"
    assert "gold_label" in exc_info.value.path


@pytest.mark.parametrize(
    "forbidden_key",
    ["oracle", "human_reference", "eval_override", "score", "threshold", "result_callback"],
)
def test_recursive_negative_list_covers_every_eval_authority_class(forbidden_key):
    payload = _load("d2l_input_valid.json")
    payload["runtime_profile"]["nested"] = {forbidden_key: "forbidden"}
    with pytest.raises(ContractValidationError, match="forbidden_runtime_data"):
        validate_d2l_evaluation_input(payload)


def test_forbidden_words_inside_source_prose_are_not_false_positive():
    payload = _load("d2l_input_valid.json")
    payload["blocks"][0]["source_text"] = "A gold score threshold appears in source prose."
    normalized = validate_d2l_evaluation_input(_reseal(payload))
    assert "gold score threshold" in normalized["blocks"][0]["source_text"]


def test_unknown_keys_are_rejected_at_nested_levels():
    payload = _load("d2l_input_valid.json")
    payload["runtime_terms"][0]["unexpected"] = "value"
    with pytest.raises(ContractValidationError, match="unknown_keys"):
        validate_d2l_evaluation_input(payload)


def test_set_like_reordering_keeps_hash_but_block_sequence_does_not():
    payload = _load("d2l_input_valid.json")
    reordered = copy.deepcopy(payload)
    reordered["artifacts"].reverse()
    reordered["translations"].reverse()
    assert seal_d2l_evaluation_input(reordered)["integrity"]["package_sha256"] == payload[
        "integrity"
    ]["package_sha256"]

    reversed_blocks = copy.deepcopy(payload)
    reversed_blocks["blocks"].reverse()
    assert seal_d2l_evaluation_input(reversed_blocks)["integrity"]["package_sha256"] != payload[
        "integrity"
    ]["package_sha256"]
    with pytest.raises(ContractValidationError, match="block_order"):
        validate_d2l_evaluation_input(seal_d2l_evaluation_input(reversed_blocks))


def test_tampered_package_and_artifact_set_hashes_fail_closed():
    package_tamper = _load("d2l_input_valid.json")
    package_tamper["runtime_profile"]["domain"] = "changed"
    with pytest.raises(ContractValidationError, match="package_hash"):
        validate_d2l_evaluation_input(package_tamper)

    artifact_tamper = _load("d2l_input_valid.json")
    artifact_tamper["artifacts"][0]["size_bytes"] += 1
    artifact_tamper = seal_d2l_evaluation_input(artifact_tamper)
    with pytest.raises(ContractValidationError, match="artifact_set_hash"):
        validate_d2l_evaluation_input(artifact_tamper)


def test_translation_rows_must_exact_cover_all_non_excluded_blocks():
    payload = _load("d2l_input_valid.json")
    payload["translations"].pop()
    with pytest.raises(ContractValidationError, match="translation_exact_cover"):
        validate_d2l_evaluation_input(_reseal(payload))


def test_selected_chapter_and_arm_artifact_identity_are_exact():
    missing_chapter = _load("d2l_input_valid.json")
    missing_chapter["identity"]["selected_chapter_ids"].append("chapter-missing")
    with pytest.raises(ContractValidationError, match="chapter_coverage"):
        validate_d2l_evaluation_input(_reseal(missing_chapter))

    duplicate_artifact = _load("d2l_input_valid.json")
    duplicate_artifact["arms"].append(
        {
            "arm_id": "s0",
            "role": "baseline",
            "label": "S0",
            "translation_artifact_id": "artifact-s1",
            "translation_sha256": duplicate_artifact["arms"][0]["translation_sha256"],
        }
    )
    with pytest.raises(ContractValidationError, match="duplicate"):
        validate_d2l_evaluation_input(_reseal(duplicate_artifact))


def test_reference_and_path_guards_are_load_bearing():
    bad_reference = _load("d2l_input_valid.json")
    bad_reference["runtime_terms"][0]["source_block_ids"] = ["missing-block"]
    with pytest.raises(ContractValidationError, match="term_block_reference"):
        validate_d2l_evaluation_input(_reseal(bad_reference))

    bad_path = _load("d2l_input_valid.json")
    bad_path["artifacts"][0]["relative_path"] = "../escape.json"
    with pytest.raises(ContractValidationError, match="unsafe_path"):
        validate_d2l_evaluation_input(bad_path)

    wrong_arm_artifact = _load("d2l_input_valid.json")
    wrong_arm_artifact["artifacts"].append(
        {
            "artifact_id": "artifact-other-translation",
            "kind": "translation",
            "relative_path": "translations/other.json",
            "sha256": "8888888888888888888888888888888888888888888888888888888888888888",
            "size_bytes": 10,
        }
    )
    wrong_arm_artifact["translations"][0]["source_artifact_id"] = "artifact-other-translation"
    with pytest.raises(ContractValidationError, match="translation_artifact_scope"):
        validate_d2l_evaluation_input(_reseal(wrong_arm_artifact))


def test_passthrough_and_inactive_term_rules_are_mechanical():
    bad_passthrough = _load("d2l_input_valid.json")
    row = next(item for item in bad_passthrough["translations"] if item["block_id"] == "b002")
    row["target_text"] = "changed"
    with pytest.raises(ContractValidationError, match="passthrough_text"):
        validate_d2l_evaluation_input(_reseal(bad_passthrough))

    inactive = _load("d2l_input_valid.json")
    inactive["runtime_terms"][0]["status"] = "inactive"
    with pytest.raises(ContractValidationError, match="inactive_term_injected"):
        validate_d2l_evaluation_input(_reseal(inactive))


def test_human_reference_arm_and_non_finite_values_are_rejected():
    human_reference = _load("d2l_input_valid.json")
    human_reference["arms"][0]["role"] = "human_reference"
    with pytest.raises(ContractValidationError, match="forbidden_runtime_data"):
        validate_d2l_evaluation_input(human_reference)

    non_finite = _load("d2l_input_valid.json")
    non_finite["artifacts"][0]["size_bytes"] = math.inf
    with pytest.raises(ContractValidationError):
        validate_d2l_evaluation_input(non_finite)
