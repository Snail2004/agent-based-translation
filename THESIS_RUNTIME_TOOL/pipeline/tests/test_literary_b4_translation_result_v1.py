from __future__ import annotations

from copy import deepcopy
import json

import pytest

from pipeline.literary.b4_translation_result_v1 import (
    B4TranslationResultError,
    RESULT_SCHEMA_VERSION,
    build_translation_result_v1,
    write_translation_result_v1,
)
from pipeline.literary.b4_translation_lint_v1 import (
    CORRECTED_TRANSLATION_SCHEMA_VERSION,
)
from pipeline.literary.checkpoint import canonical_hash


def _artifact() -> dict:
    body = {
        "schema_version": "literary_b4_translation_chapter_v7",
        "book_id": "fixture_book",
        "chapter_id": "bk_ch01",
        "chapter_order": 1,
        "style_profile_version": "style_v1",
        "measured_arm": False,
        "translator_output_contract": "translation_only_v1",
        "address_metadata_collected": False,
        "blocks": [
            {
                "block_id": "bk_ch01_b001",
                "source_text": "One.",
                "target_text": "Một.",
            },
            {
                "block_id": "bk_ch01_b002",
                "source_text": "Two.",
                "target_text": "Hai.",
            },
        ],
        "translation_performed": True,
        "semantic_record_mutation_performed": False,
    }
    return {**body, "artifact_hash": canonical_hash(body)}


def _duplicate_block_with_valid_hash(value: dict) -> None:
    value["blocks"].append(deepcopy(value["blocks"][0]))
    body = {
        key: item for key, item in value.items() if key != "artifact_hash"
    }
    value["artifact_hash"] = canonical_hash(body)


def test_result_projection_keeps_order_and_only_translation_fields() -> None:
    result = build_translation_result_v1(_artifact())

    assert result["schema_version"] == RESULT_SCHEMA_VERSION
    assert [row["block_id"] for row in result["blocks"]] == [
        "bk_ch01_b001",
        "bk_ch01_b002",
    ]
    assert result["translated_text"] == "Một.\n\nHai."
    assert "provider_receipts" not in result


def test_result_export_writes_json_and_plain_text_only(tmp_path) -> None:
    report = write_translation_result_v1(
        translation_artifact=_artifact(),
        out_dir=tmp_path / "result",
    )

    assert report["provider_calls"] == 0
    assert report["result_only"] is True
    result_path = tmp_path / "result" / "translation_bk_ch01_result.json"
    text_path = tmp_path / "result" / "translation_bk_ch01.txt"
    assert json.loads(result_path.read_text(encoding="utf-8"))["artifact_hash"]
    assert text_path.read_text(encoding="utf-8") == "Một.\n\nHai.\n"
    assert sorted(path.name for path in (tmp_path / "result").iterdir()) == [
        "translation_bk_ch01.txt",
        "translation_bk_ch01_result.json",
    ]


def test_result_export_accepts_editorially_edited_artifact() -> None:
    artifact = _artifact()
    edited_body = {
        **{
            key: deepcopy(value)
            for key, value in artifact.items()
            if key != "artifact_hash"
        },
        "schema_version": "literary_b4_translation_editorially_edited_v1",
        "source_translation_artifact_hash": artifact["artifact_hash"],
    }
    edited = {**edited_body, "artifact_hash": canonical_hash(edited_body)}

    result = build_translation_result_v1(edited)

    assert result["source_artifact_schema_version"] == (
        "literary_b4_translation_editorially_edited_v1"
    )
    assert result["source_translation_artifact_hash"] == artifact["artifact_hash"]


def test_result_export_accepts_mechanically_corrected_artifact() -> None:
    artifact = _artifact()
    corrected_body = {
        **{
            key: deepcopy(value)
            for key, value in artifact.items()
            if key != "artifact_hash"
        },
        "schema_version": CORRECTED_TRANSLATION_SCHEMA_VERSION,
        "source_translation_artifact_hash": artifact["artifact_hash"],
        "translation_text_mutation_performed": True,
    }
    corrected = {
        **corrected_body,
        "artifact_hash": canonical_hash(corrected_body),
    }

    result = build_translation_result_v1(corrected)

    assert result["source_artifact_schema_version"] == (
        CORRECTED_TRANSLATION_SCHEMA_VERSION
    )
    assert result["source_translation_artifact_hash"] == artifact["artifact_hash"]


@pytest.mark.parametrize(
    "mutate, message",
    [
        (
            lambda value: value.update({"artifact_hash": "0" * 64}),
            "hash mismatch",
        ),
        (
            _duplicate_block_with_valid_hash,
            "repeats a block",
        ),
    ],
)
def test_result_export_fails_closed_on_invalid_input(mutate, message) -> None:
    artifact = _artifact()
    mutate(artifact)
    with pytest.raises(B4TranslationResultError, match=message):
        build_translation_result_v1(artifact)
