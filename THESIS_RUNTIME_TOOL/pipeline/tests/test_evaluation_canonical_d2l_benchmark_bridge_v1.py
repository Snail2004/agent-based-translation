from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from pipeline.eval.canonical_d2l_benchmark_bridge_v1 import (
    FinalizedCanonicalSourceArtifactsV1,
    build_canonical_d2l_common_input_v1,
)
from pipeline.eval.common_input_v1 import (
    seal_translation_artifact,
    source_binding_to_dict,
)
from pipeline.eval.contracts_v1 import ContractValidationError
from pipeline.ingest.admitted_projection import build_admitted_projection
from pipeline.ingest.canonical_source_package import canonical_json_sha256


FIXTURE_ROOT = (
    Path(__file__).parent / "fixtures" / "canonical_source_package_v1"
)
CHAPTER_ID = "neutral_fixture_ch01"


def _write_json(path: Path, value: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
        newline="\n",
    )
    return path


def _source_fixture(tmp_path: Path) -> tuple[FinalizedCanonicalSourceArtifactsV1, dict, dict]:
    document = json.loads((FIXTURE_ROOT / "document.json").read_text("utf-8"))
    structure = json.loads(
        (FIXTURE_ROOT / "structure_manifest.json").read_text("utf-8")
    )
    asset_manifest = json.loads(
        (FIXTURE_ROOT / "asset_manifest.json").read_text("utf-8")
    )
    projection = build_admitted_projection(document, structure, asset_manifest)
    binding = {
        "binding_kind": "canonical_source_package_v1",
        "project_id": document["doc_id"],
        "document_id": document["doc_id"],
        "document": {
            "schema_version": document["schema_version"],
            "sha256": canonical_json_sha256(document),
        },
        "structure": {
            "schema_version": structure["schema_version"],
            "sha256": canonical_json_sha256(structure),
        },
        "asset_manifest": {
            "schema_version": asset_manifest["schema_version"],
            "sha256": canonical_json_sha256(asset_manifest),
        },
        "admitted_projection": {
            "schema_version": projection["schema_version"],
            "payload_sha256": projection["integrity"]["payload_sha256"],
        },
        "admission_policy": copy.deepcopy(projection["policy"]),
    }
    body = {
        "schema_version": "source_package_finalization_v1",
        "lifecycle": "finalized_pre_run",
        "doc_id": document["doc_id"],
        "package": {
            "document": {
                "schema_version": document["schema_version"],
                "sha256": canonical_json_sha256(document),
            },
            "structure": {
                "schema_version": structure["schema_version"],
                "sha256": canonical_json_sha256(structure),
            },
            "asset_manifest": {
                "schema_version": asset_manifest["schema_version"],
                "sha256": canonical_json_sha256(asset_manifest),
            },
            "admitted_projection": {
                "schema_version": projection["schema_version"],
                "sha256": canonical_json_sha256(projection),
            },
        },
        "policies": {"admission": copy.deepcopy(projection["policy"])},
    }
    finalization = {
        **body,
        "integrity": {"payload_sha256": canonical_json_sha256(body)},
    }
    source_root = tmp_path / "source"
    paths = FinalizedCanonicalSourceArtifactsV1(
        document=_write_json(source_root / "document.json", document),
        structure_manifest=_write_json(
            source_root / "structure_manifest.json", structure
        ),
        asset_manifest=_write_json(
            source_root / "asset_manifest.json", asset_manifest
        ),
        admitted_projection=_write_json(
            source_root / "admitted_projection_v1.json", projection
        ),
        package_seal=_write_json(
            source_root / "source_package_finalization_v1.json", finalization
        ),
    )
    return paths, binding, projection


def _translation_artifact(
    document: dict,
    projection: dict,
    binding: dict,
    *,
    arm_id: str,
) -> dict:
    projection_by_id = {row["block_id"]: row for row in projection["rows"]}
    rows = []
    counts = {
        status: 0
        for status in (
            "translated",
            "preserved",
            "excluded",
            "review_held",
            "missing",
            "failed",
        )
    }
    for chapter in document["chapters"]:
        for block in chapter["blocks"]:
            channel = projection_by_id[block["block_id"]]["channel"]
            if channel in {"semantic_text", "structured_translate"}:
                status = "translated"
                target = f"{arm_id}::{block['clean_text']}"
            elif channel == "preserve_only":
                status = "preserved"
                target = block["clean_text"]
            elif channel == "exclude":
                status = "excluded"
                target = None
            else:
                status = "review_held"
                target = None
            counts[status] += 1
            rows.append(
                {
                    "block_id": block["block_id"],
                    "status": status,
                    "target_text": target,
                    "error_code": None,
                }
            )
    return seal_translation_artifact(
        {
            "schema_id": "TranslationArtifactV1",
            "schema_version": "1.0.0",
            "artifact_id": f"translation-{arm_id}",
            "created_at": "2026-07-23T00:00:00Z",
            "producer": {
                "workstream": "d2l",
                "component": "fixture_d2l_writer",
                "component_version": "1.0.0",
                "code_commit": "a" * 40,
            },
            "source_binding": binding,
            "run_identity": {
                "logical_run_id": "fixture-run",
                "attempt_run_id": f"fixture-run-{arm_id}",
                "arm_id": arm_id,
                "profile_id": "technical_d2l_v1",
                "profile_config_sha256": "9" * 64,
                "source_language": "en",
                "target_language": "vi",
            },
            "translations": rows,
            "coverage": {
                "source_block_count": len(rows),
                "eligible_count": (
                    counts["translated"] + counts["missing"] + counts["failed"]
                ),
                "translated_count": counts["translated"],
                "preserved_count": counts["preserved"],
                "excluded_count": counts["excluded"],
                "review_held_count": counts["review_held"],
                "missing_count": counts["missing"],
                "failed_count": counts["failed"],
            },
            "integrity": {"artifact_sha256": "0" * 64},
        }
    )


def test_bridge_builds_exact_common_input_from_canonical_files_and_s0_s1(
    tmp_path: Path,
) -> None:
    paths, binding, projection = _source_fixture(tmp_path)
    document = json.loads(paths.document.read_text("utf-8"))
    s0 = _translation_artifact(document, projection, binding, arm_id="s0")
    s1 = _translation_artifact(document, projection, binding, arm_id="s1")

    common = build_canonical_d2l_common_input_v1(
        source_artifacts=paths,
        s0_translation_artifact=s0,
        s1_translation_artifact=s1,
        selected_chapter_ids=(CHAPTER_ID,),
    )

    assert common.source_schema_id == "CanonicalSourcePackageV1"
    assert source_binding_to_dict(common.source_binding) == binding
    assert [row.arm_id for row in common.arms] == ["s0", "s1"]
    assert len(common.translations) == len(common.blocks) * 2


def test_bridge_rejects_foreign_s1_and_tampered_canonical_component(
    tmp_path: Path,
) -> None:
    paths, binding, projection = _source_fixture(tmp_path)
    document = json.loads(paths.document.read_text("utf-8"))
    s0 = _translation_artifact(document, projection, binding, arm_id="s0")
    foreign_binding = copy.deepcopy(binding)
    foreign_binding["document"]["sha256"] = "f" * 64
    foreign_s1 = _translation_artifact(
        document, projection, foreign_binding, arm_id="s1"
    )
    with pytest.raises(ContractValidationError, match="different canonical"):
        build_canonical_d2l_common_input_v1(
            source_artifacts=paths,
            s0_translation_artifact=s0,
            s1_translation_artifact=foreign_s1,
            selected_chapter_ids=(CHAPTER_ID,),
        )

    s1 = _translation_artifact(document, projection, binding, arm_id="s1")
    tampered_document = copy.deepcopy(document)
    tampered_document["chapters"][0]["blocks"][0]["clean_text"] += " drift"
    _write_json(paths.document, tampered_document)
    with pytest.raises(ContractValidationError, match="canonical_source_package"):
        build_canonical_d2l_common_input_v1(
            source_artifacts=paths,
            s0_translation_artifact=s0,
            s1_translation_artifact=s1,
            selected_chapter_ids=(CHAPTER_ID,),
        )
