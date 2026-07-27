from __future__ import annotations

from copy import deepcopy

import pytest

from pipeline.literary.checkpoint import canonical_hash
from pipeline.literary.response_normalization_v1 import (
    LiteraryResponseNormalizationError,
    attach_response_normalization_notes_v1,
    normalize_code_owned_response_echoes_v1,
)


def test_code_owned_echoes_are_replaced_without_mutating_model_response() -> None:
    response = {
        "chapter_id": "copied_example_chapter",
        "window_id": "copied_example_window",
        "semantic_rows": [{"verdict": "keep"}],
    }
    original = deepcopy(response)

    normalized, notes = normalize_code_owned_response_echoes_v1(
        response,
        expected={"chapter_id": "wh_ch01", "window_id": "wh_ch01_w01"},
    )

    assert response == original
    assert normalized["chapter_id"] == "wh_ch01"
    assert normalized["window_id"] == "wh_ch01_w01"
    assert normalized["semantic_rows"] == response["semantic_rows"]
    assert [row["field"] for row in notes] == ["chapter_id", "window_id"]
    assert notes[0]["observed_value_sha256"] == canonical_hash(
        {"value": "copied_example_chapter"}
    )


def test_missing_echo_is_not_invented_and_clean_artifact_stays_identical() -> None:
    response = {"semantic_rows": []}
    normalized, notes = normalize_code_owned_response_echoes_v1(
        response,
        expected={"chapter_id": "wh_ch01"},
    )
    artifact = {"chapter_id": "wh_ch01", "semantic_rows": []}

    assert normalized == response
    assert notes == []
    assert attach_response_normalization_notes_v1(artifact, notes) == artifact


@pytest.mark.parametrize(
    "field",
    [
        "component_id",
        "manifest_hash",
        "request_fingerprint",
        "schema_id",
        "target_ref",
    ],
)
def test_semantic_and_lineage_fields_cannot_use_echo_normalizer(field: str) -> None:
    with pytest.raises(
        LiteraryResponseNormalizationError,
        match="non-echo fields",
    ):
        normalize_code_owned_response_echoes_v1(
            {field: "model_value"},
            expected={field: "code_value"},
        )


def test_notes_are_optional_validator_metadata() -> None:
    artifact = {"chapter_id": "wh_ch01", "semantic_rows": []}
    _, notes = normalize_code_owned_response_echoes_v1(
        {"chapter_id": "wrong"},
        expected={"chapter_id": "wh_ch01"},
    )

    attached = attach_response_normalization_notes_v1(artifact, notes)

    assert "response_normalization_notes" not in artifact
    assert attached["response_normalization_notes"] == notes
    assert attached["response_normalization_notes"][0]["normalized_value"] == "wh_ch01"
