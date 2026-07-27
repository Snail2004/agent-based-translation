"""Result-only projection for the Literary B4 translation output."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any, Mapping

from pipeline.literary.checkpoint import canonical_hash
from pipeline.literary.b4_translation_lint_v1 import (
    CORRECTED_TRANSLATION_SCHEMA_VERSION,
)


RESULT_SCHEMA_VERSION = "literary_b4_translation_result_v1"
TEXT_SEPARATOR = "\n\n"


class B4TranslationResultError(RuntimeError):
    pass


def build_translation_result_v1(
    translation_artifact: Mapping[str, Any],
) -> dict[str, Any]:
    """Project a sealed chapter artifact into the file-export contract.

    The source artifact remains the authority. This function only keeps the
    ordered source/target block pairs needed by a downstream normalizer and
    never edits or repairs translation text.
    """

    artifact = _verify_translation_artifact(translation_artifact)
    source_schema = str(artifact["schema_version"])
    blocks = [
        {
            "block_id": row["block_id"],
            "source_text": row["source_text"],
            "target_text": row["target_text"],
        }
        for row in artifact["blocks"]
    ]
    body: dict[str, Any] = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "book_id": artifact["book_id"],
        "chapter_id": artifact["chapter_id"],
        "chapter_order": artifact["chapter_order"],
        "source_artifact_hash": artifact["artifact_hash"],
        "source_artifact_schema_version": source_schema,
        "source_translation_artifact_hash": artifact.get(
            "source_translation_artifact_hash"
        ),
        "blocks": blocks,
        "translated_block_count": len(blocks),
        "text_separator": TEXT_SEPARATOR,
        "translated_text": TEXT_SEPARATOR.join(
            row["target_text"] for row in blocks
        ),
        "translation_performed": True,
        "semantic_record_mutation_performed": False,
    }
    for key in (
        "style_profile_version",
        "measured_arm",
        "translator_output_contract",
        "address_metadata_collected",
    ):
        if key in artifact:
            body[key] = deepcopy(artifact[key])
    return {**body, "artifact_hash": canonical_hash(body)}


def write_translation_result_v1(
    *,
    translation_artifact: Mapping[str, Any],
    out_dir: Path,
) -> dict[str, Any]:
    """Write only the normalized JSON result and plain-text chapter result."""

    output = Path(out_dir).resolve()
    if output.exists():
        raise B4TranslationResultError(
            f"translation result output directory already exists: {output}"
        )
    result = build_translation_result_v1(translation_artifact)
    output.mkdir(parents=True)
    json_path = output / f"translation_{result['chapter_id']}_result.json"
    text_path = output / f"translation_{result['chapter_id']}.txt"
    json_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    text_path.write_text(
        str(result["translated_text"]) + "\n",
        encoding="utf-8",
    )
    return {
        "schema_version": "literary_b4_translation_result_report_v1",
        "status": "complete",
        "chapter_id": result["chapter_id"],
        "translated_block_count": result["translated_block_count"],
        "json_output": str(json_path),
        "text_output": str(text_path),
        "source_artifact_hash": result["source_artifact_hash"],
        "result_artifact_hash": result["artifact_hash"],
        "provider_calls": 0,
        "result_only": True,
    }


def _verify_translation_artifact(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise B4TranslationResultError("translation artifact must be an object")
    artifact = deepcopy(dict(value))
    observed_hash = artifact.pop("artifact_hash", None)
    if observed_hash != canonical_hash(artifact):
        raise B4TranslationResultError("translation artifact hash mismatch")
    schema = artifact.get("schema_version")
    if not (
        isinstance(schema, str)
        and (
            schema.startswith("literary_b4_translation_chapter_")
            or schema == CORRECTED_TRANSLATION_SCHEMA_VERSION
            or schema == "literary_b4_translation_editorially_edited_v1"
        )
    ):
        raise B4TranslationResultError(
            "unsupported translation artifact schema"
        )
    if artifact.get("translation_performed") is not True:
        raise B4TranslationResultError(
            "translation artifact did not perform translation"
        )
    if artifact.get("semantic_record_mutation_performed") is not False:
        raise B4TranslationResultError(
            "translation artifact claims semantic mutation"
        )
    for key in ("book_id", "chapter_id"):
        if not isinstance(artifact.get(key), str) or not artifact[key].strip():
            raise B4TranslationResultError(f"translation {key} is missing")
    if (
        not isinstance(artifact.get("chapter_order"), int)
        or isinstance(artifact["chapter_order"], bool)
        or artifact["chapter_order"] < 1
    ):
        raise B4TranslationResultError("translation chapter_order is invalid")
    rows = artifact.get("blocks")
    if not isinstance(rows, list) or not rows:
        raise B4TranslationResultError("translation blocks are missing")
    seen: set[str] = set()
    normalized_rows: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise B4TranslationResultError("translation block is malformed")
        block_id = _required_text(row.get("block_id"), "block_id")
        source_text = _required_text(row.get("source_text"), "source_text")
        target_text = _required_text(row.get("target_text"), "target_text")
        if block_id in seen:
            raise B4TranslationResultError("translation repeats a block")
        seen.add(block_id)
        normalized_rows.append(
            {
                "block_id": block_id,
                "source_text": source_text,
                "target_text": target_text,
            }
        )
    artifact["blocks"] = normalized_rows
    return {**artifact, "artifact_hash": observed_hash}


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise B4TranslationResultError(f"translation {label} is missing")
    return value


__all__ = [
    "B4TranslationResultError",
    "RESULT_SCHEMA_VERSION",
    "TEXT_SEPARATOR",
    "build_translation_result_v1",
    "write_translation_result_v1",
]
