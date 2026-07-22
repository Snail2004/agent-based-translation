from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from pipeline.eval.contracts_v1 import ContractValidationError
from pipeline.eval.d2l_input_v1 import seal_d2l_evaluation_input
from pipeline.eval.d2l_package_adapter_v1 import project_d2l_evaluation_package


FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "evaluation_v1"
    / "d2l_input_valid.json"
)


def _load() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_projection_is_immutable_and_maps_only_producer_facts():
    payload = _load()
    before = copy.deepcopy(payload)

    common = project_d2l_evaluation_package(payload)

    assert payload == before
    assert common.source_schema_id == "D2LEvaluationInputV1"
    assert common.project_id == payload["identity"]["project_id"]
    assert common.document_id == payload["identity"]["document_id"]
    assert len(common.arms) == 1
    arm = common.arms[0]
    assert arm.artifact_id == "artifact-s1"
    assert arm.artifact_sha256 == "5" * 64
    assert arm.logical_run_id == "run-d2l-1"
    assert arm.attempt_run_id == "exp-s0s1"
    assert arm.profile_id == "technical_d2l_v1"
    assert arm.profile_config_sha256 == "4" * 64
    assert (arm.source_language, arm.target_language) == ("en", "vi")


def test_projection_restores_source_order_and_maps_passthrough_to_preserved():
    common = project_d2l_evaluation_package(_load())

    assert [block.block_id for block in common.blocks] == ["b001", "b002"]
    assert [row.block_id for row in common.translations] == ["b001", "b002"]
    assert [row.status for row in common.translations] == ["translated", "preserved"]
    assert common.translations[0].target_text == "Tensor lưu trữ dữ liệu số."
    assert common.translations[1].target_text == "tensor.shape"


def test_excluded_source_rows_are_materialized_without_inventing_translation():
    payload = _load()
    payload["blocks"].append(
        {
            "block_id": "b003",
            "chapter_id": "chapter-intro",
            "order_index": 2,
            "block_type": "metadata",
            "source_text": "internal note",
            "admission": "exclude",
        }
    )
    payload = seal_d2l_evaluation_input(payload)

    common = project_d2l_evaluation_package(payload)

    excluded = common.translations[-1]
    assert excluded.block_id == "b003"
    assert excluded.status == "excluded"
    assert excluded.target_text is None
    assert excluded.error_code is None


@pytest.mark.parametrize(
    "mutation, expected_code",
    [
        (lambda row: row["identity"].update({"experiment_id": ""}), "empty_string"),
        (lambda row: row["runtime_profile"].update({"source_artifact_id": "missing"}), "artifact_reference"),
        (lambda row: row["arms"][0].update({"translation_sha256": "f" * 64}), "translation_hash"),
    ],
)
def test_invalid_or_drifted_package_fails_before_projection(mutation, expected_code):
    payload = _load()
    mutation(payload)
    payload = seal_d2l_evaluation_input(payload)

    with pytest.raises(ContractValidationError) as exc_info:
        project_d2l_evaluation_package(payload)

    assert exc_info.value.code == expected_code
