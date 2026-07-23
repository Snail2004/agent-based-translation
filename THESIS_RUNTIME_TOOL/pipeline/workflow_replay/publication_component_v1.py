from __future__ import annotations

import copy
import html
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from pipeline.ingest.admitted_projection import build_admitted_projection
from pipeline.ingest.canonical_source_package import (
    seal_asset_manifest,
    validate_canonical_source_package,
)
from pipeline.ingest.source_package_exporter import (
    export_source_package,
    seal_translation_overlay,
    validate_translation_overlay,
)

from .contracts_v1 import (
    canonical_json_bytes,
    canonical_sha256,
    physical_sha256,
    validate_typed_artifact_binding_v1,
)


SCHEMA_VERSION = "1.0.0"
MANIFEST_SCHEMA_ID = "PublicationComponentManifestV1"
INDEX_SCHEMA_ID = "PublicationArtifactIndexV1"
EVENT_SCHEMA_ID = "PublicationComponentEventV1"
FLOW_KIND = "publication"
COMPONENT_ID = "publication"
STAGE_ID = "export"
VALIDATOR_ID = "publication.component.validator_v1"
VALIDATOR_REVISION = "v1"

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_MARKDOWN_IMAGE_RE = re.compile(
    r"!\[(?P<alt>[^\]]*)\]\((?P<target>[^)\s]+)"
    r"(?:\s+[\"'](?P<title>.*?)[\"'])?\)"
)
_DISPLAY_MATH_RE = re.compile(r"\$\$(?P<math>.+?)\$\$", re.DOTALL)
_INLINE_MATH_RE = re.compile(
    r"(?<!\$)\$(?!\$)(?P<math>(?:\\.|[^$\\])+?)\$(?!\$)",
    re.DOTALL,
)
_PROTECTED_ASSET_KINDS = frozenset({"image", "equation", "code"})


class PublicationComponentError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class PublicationComponentResultV1:
    component_root: Path
    manifest: dict[str, Any]
    artifact_index: dict[str, Any]
    export_manifest: dict[str, Any]


def publish_selected_chapters_v1(
    *,
    component_root: str | Path,
    source_package_root: str | Path,
    workflow_run_id: str,
    component_run_id: str,
    selected_chapter_ids: Sequence[str],
    translation_artifact: Mapping[str, Any] | str | Path,
    translation_artifact_binding: Mapping[str, Any],
    source_package_bindings: Sequence[Mapping[str, Any]],
    producer_code_commit: str,
    created_at: str | None = None,
) -> PublicationComponentResultV1:
    root = Path(component_root).resolve()
    package_root = Path(source_package_root).resolve()
    workflow_id = _identifier(workflow_run_id, "workflow_run_id")
    run_id = _identifier(component_run_id, "component_run_id")
    commit = _commit(producer_code_commit)
    chapters = _chapter_ids(selected_chapter_ids)
    translation_binding = validate_typed_artifact_binding_v1(
        translation_artifact_binding,
        path="$.translation_artifact_binding",
    )
    bindings = _source_bindings(source_package_bindings)
    artifact, artifact_bytes = _load_translation_artifact(translation_artifact)
    _verify_translation_binding(translation_binding, artifact_bytes)
    timestamp = _timestamp(created_at or _utc_now())

    expected_identity = {
        "workflow_run_id": workflow_id,
        "component_run_id": run_id,
        "selected_chapter_ids": chapters,
        "translation_artifact": translation_binding,
        "source_package_bindings": bindings,
        "producer_code_commit": commit,
    }
    if root.exists():
        validation = validate_publication_component_package_v1(
            root, require_terminal=True
        )
        observed = validation["manifest"]
        for key, expected in expected_identity.items():
            if observed[key] != expected:
                raise PublicationComponentError(
                    "publication_identity_drift",
                    f"Existing Publication component differs at {key}.",
                )
        return PublicationComponentResultV1(
            component_root=root,
            manifest=observed,
            artifact_index=validation["artifact_index"],
            export_manifest=validation["export_manifest"],
        )

    if root.parent.exists() and (root.parent.is_symlink() or not root.parent.is_dir()):
        raise PublicationComponentError(
            "publication_root_unsafe",
            "Publication component parent must be a real directory.",
        )
    root.parent.mkdir(parents=True, exist_ok=True)
    temporary_root = Path(
        tempfile.mkdtemp(prefix=f".{root.name}.", dir=root.parent)
    ).resolve()
    try:
        selected_package = temporary_root / "selected_source_package"
        selected_document, selected_structure, selected_assets = (
            _materialize_selected_package(
                package_root,
                selected_package,
                selected_chapter_ids=chapters,
            )
        )
        overlay = _build_translation_overlay(
            selected_document,
            selected_structure,
            selected_assets,
            selected_package,
            artifact,
        )
        overlay_path = temporary_root / "translation_overlay.json"
        overlay_bytes = canonical_json_bytes(overlay) + b"\n"
        overlay_path.write_bytes(overlay_bytes)

        export_root = temporary_root / "artifacts" / "publication"
        export_result = export_source_package(
            selected_package,
            overlay,
            export_root,
            review_mode="markers",
            pandoc_executable=None,
        )
        export_manifest = copy.deepcopy(export_result.manifest)

        events = _publication_events(
            workflow_run_id=workflow_id,
            component_run_id=run_id,
            created_at=timestamp,
            selected_chapter_ids=chapters,
            export_manifest=export_manifest,
        )
        event_bytes = b"".join(canonical_json_bytes(row) + b"\n" for row in events)
        created_event_id = events[-2]["event_id"]

        artifact_rows = []
        artifact_specs = (
            (
                "publication/document.html",
                "publication_html",
                "text/html",
                export_result.html_path,
                (),
            ),
            (
                "publication/document.md",
                "publication_markdown",
                "text/markdown",
                export_result.markdown_path,
                (),
            ),
            (
                "publication/export_manifest.json",
                "publication_export_manifest",
                export_manifest["schema_version"],
                export_result.manifest_path,
                (
                    "publication/document.html",
                    "publication/document.md",
                ),
            ),
            (
                "publication/translation_overlay.json",
                "canonical_translation_overlay_v1",
                overlay["schema_version"],
                overlay_path,
                (),
            ),
        )
        for artifact_ref, kind, schema_version, path, parents in artifact_specs:
            relative_path = (
                "artifacts/publication/translation_overlay.json"
                if path == overlay_path
                else path.relative_to(temporary_root).as_posix()
            )
            if path == overlay_path:
                destination = temporary_root / relative_path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(overlay_bytes)
                path = destination
            binding = {
                "artifact_ref": artifact_ref,
                "artifact_kind": kind,
                "schema_version": str(schema_version),
                "sha256": physical_sha256(path.read_bytes()),
                "sha256_kind": "physical",
            }
            validate_typed_artifact_binding_v1(
                binding, path=f"$.artifacts.{artifact_ref}"
            )
            artifact_rows.append(
                {
                    "artifact": binding,
                    "relative_path": relative_path,
                    "stage_id": STAGE_ID,
                    "parent_artifact_refs": list(parents),
                    "created_by_event_id": created_event_id,
                }
            )

        artifact_index = {
            "schema_id": INDEX_SCHEMA_ID,
            "schema_version": SCHEMA_VERSION,
            "workflow_run_id": workflow_id,
            "component_id": COMPONENT_ID,
            "component_run_id": run_id,
            "artifacts": artifact_rows,
            "integrity": {"artifact_index_sha256": "0" * 64},
        }
        artifact_index["integrity"]["artifact_index_sha256"] = _self_hash(
            artifact_index, "artifact_index_sha256"
        )

        manifest = {
            "schema_id": MANIFEST_SCHEMA_ID,
            "schema_version": SCHEMA_VERSION,
            "workflow_run_id": workflow_id,
            "flow_kind": FLOW_KIND,
            "component_id": COMPONENT_ID,
            "component_run_id": run_id,
            "component_attempt_id": 1,
            "component_attempt_index": 1,
            "status": "succeeded",
            "stage_id": STAGE_ID,
            "selected_chapter_ids": chapters,
            "translation_artifact": translation_binding,
            "source_package_bindings": bindings,
            "producer_code_commit": commit,
            "created_at": timestamp,
            "event_count": len(events),
            "artifact_index_sha256": artifact_index["integrity"][
                "artifact_index_sha256"
            ],
            "export_manifest_sha256": physical_sha256(
                export_result.manifest_path.read_bytes()
            ),
            "integrity": {"manifest_sha256": "0" * 64},
        }
        manifest["integrity"]["manifest_sha256"] = _self_hash(
            manifest, "manifest_sha256"
        )

        (temporary_root / "component_manifest.json").write_bytes(
            canonical_json_bytes(manifest) + b"\n"
        )
        (temporary_root / "events.jsonl").write_bytes(event_bytes)
        (temporary_root / "artifact_index.json").write_bytes(
            canonical_json_bytes(artifact_index) + b"\n"
        )
        shutil.rmtree(selected_package)
        os.replace(temporary_root, root)
    except Exception:
        shutil.rmtree(temporary_root, ignore_errors=True)
        raise

    validation = validate_publication_component_package_v1(
        root, require_terminal=True
    )
    return PublicationComponentResultV1(
        component_root=root,
        manifest=validation["manifest"],
        artifact_index=validation["artifact_index"],
        export_manifest=validation["export_manifest"],
    )


def validate_publication_component_package_v1(
    component_root: str | Path,
    *,
    require_terminal: bool = False,
) -> dict[str, Any]:
    root = Path(component_root).resolve()
    if not root.is_dir() or root.is_symlink():
        raise PublicationComponentError(
            "publication_component_root_invalid",
            "Publication component root must be a real directory.",
        )
    manifest = _read_json(root / "component_manifest.json")
    required_manifest = {
        "schema_id",
        "schema_version",
        "workflow_run_id",
        "flow_kind",
        "component_id",
        "component_run_id",
        "component_attempt_id",
        "component_attempt_index",
        "status",
        "stage_id",
        "selected_chapter_ids",
        "translation_artifact",
        "source_package_bindings",
        "producer_code_commit",
        "created_at",
        "event_count",
        "artifact_index_sha256",
        "export_manifest_sha256",
        "integrity",
    }
    _exact_keys(manifest, required_manifest, "component_manifest")
    if (
        manifest["schema_id"] != MANIFEST_SCHEMA_ID
        or manifest["schema_version"] != SCHEMA_VERSION
        or manifest["flow_kind"] != FLOW_KIND
        or manifest["component_id"] != COMPONENT_ID
        or manifest["stage_id"] != STAGE_ID
    ):
        raise PublicationComponentError(
            "publication_manifest_schema",
            "Publication component manifest identity is unsupported.",
        )
    _identifier(manifest["workflow_run_id"], "workflow_run_id")
    _identifier(manifest["component_run_id"], "component_run_id")
    if manifest["component_attempt_id"] != 1 or manifest["component_attempt_index"] != 1:
        raise PublicationComponentError(
            "publication_attempt_identity",
            "Publication V1 is a deterministic one-attempt component.",
        )
    if manifest["status"] != "succeeded":
        raise PublicationComponentError(
            "publication_status",
            "Publication component must be succeeded.",
        )
    if require_terminal and manifest["status"] != "succeeded":
        raise PublicationComponentError(
            "publication_terminal",
            "Terminal Publication evidence is required.",
        )
    _chapter_ids(manifest["selected_chapter_ids"])
    validate_typed_artifact_binding_v1(
        manifest["translation_artifact"],
        path="$.manifest.translation_artifact",
    )
    _source_bindings(manifest["source_package_bindings"])
    _commit(manifest["producer_code_commit"])
    _timestamp(manifest["created_at"])
    _sha256(manifest["artifact_index_sha256"], "artifact_index_sha256")
    _sha256(manifest["export_manifest_sha256"], "export_manifest_sha256")
    integrity = manifest["integrity"]
    if not isinstance(integrity, Mapping) or set(integrity) != {"manifest_sha256"}:
        raise PublicationComponentError(
            "publication_manifest_integrity",
            "Publication manifest integrity shape is invalid.",
        )
    if integrity["manifest_sha256"] != _self_hash(
        manifest, "manifest_sha256"
    ):
        raise PublicationComponentError(
            "publication_manifest_hash",
            "Publication component manifest hash drifted.",
        )

    events = _read_jsonl(root / "events.jsonl")
    _validate_events(events, manifest)
    if manifest["event_count"] != len(events):
        raise PublicationComponentError(
            "publication_event_count",
            "Publication manifest event count differs.",
        )

    index = _read_json(root / "artifact_index.json")
    required_index = {
        "schema_id",
        "schema_version",
        "workflow_run_id",
        "component_id",
        "component_run_id",
        "artifacts",
        "integrity",
    }
    _exact_keys(index, required_index, "artifact_index")
    if (
        index["schema_id"] != INDEX_SCHEMA_ID
        or index["schema_version"] != SCHEMA_VERSION
        or index["workflow_run_id"] != manifest["workflow_run_id"]
        or index["component_id"] != COMPONENT_ID
        or index["component_run_id"] != manifest["component_run_id"]
    ):
        raise PublicationComponentError(
            "publication_index_identity",
            "Publication artifact index identity differs.",
        )
    index_integrity = index["integrity"]
    if not isinstance(index_integrity, Mapping) or set(index_integrity) != {
        "artifact_index_sha256"
    }:
        raise PublicationComponentError(
            "publication_index_integrity",
            "Publication artifact index integrity shape is invalid.",
        )
    if index_integrity["artifact_index_sha256"] != _self_hash(
        index, "artifact_index_sha256"
    ):
        raise PublicationComponentError(
            "publication_index_hash",
            "Publication artifact index hash drifted.",
        )
    if manifest["artifact_index_sha256"] != index_integrity[
        "artifact_index_sha256"
    ]:
        raise PublicationComponentError(
            "publication_index_binding",
            "Publication manifest binds another artifact index.",
        )
    _validate_artifacts(root, index, events)
    export_row = next(
        (
            row
            for row in index["artifacts"]
            if row["artifact"]["artifact_kind"]
            == "publication_export_manifest"
        ),
        None,
    )
    if export_row is None:
        raise PublicationComponentError(
            "publication_export_manifest_missing",
            "Publication export manifest artifact is missing.",
        )
    export_path = _resolve_file(root, export_row["relative_path"])
    if physical_sha256(export_path.read_bytes()) != manifest[
        "export_manifest_sha256"
    ]:
        raise PublicationComponentError(
            "publication_export_manifest_binding",
            "Publication manifest binds different export bytes.",
        )
    export_manifest = _read_json(export_path)
    export_integrity = export_manifest.get("integrity")
    if (
        not isinstance(export_integrity, Mapping)
        or set(export_integrity) != {"export_payload_sha256"}
    ):
        raise PublicationComponentError(
            "publication_export_integrity",
            "Export manifest integrity shape is invalid.",
        )
    expected_export = copy.deepcopy(export_manifest)
    expected_export.pop("integrity")
    if export_integrity["export_payload_sha256"] != canonical_sha256(
        expected_export
    ):
        raise PublicationComponentError(
            "publication_export_hash",
            "Export manifest payload hash drifted.",
        )
    return {
        "manifest": manifest,
        "events": events,
        "artifact_index": index,
        "export_manifest": export_manifest,
        "validation_receipt_sha256": canonical_sha256(
            {
                "manifest_sha256": integrity["manifest_sha256"],
                "artifact_index_sha256": index_integrity[
                    "artifact_index_sha256"
                ],
                "export_manifest_sha256": manifest[
                    "export_manifest_sha256"
                ],
            }
        ),
    }


def _materialize_selected_package(
    source_root: Path,
    destination: Path,
    *,
    selected_chapter_ids: Sequence[str],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    if not source_root.is_dir() or source_root.is_symlink():
        raise PublicationComponentError(
            "source_package_root_invalid",
            "Source Package root must be a real directory.",
        )
    document = _read_json(source_root / "document.json")
    structure = _read_json(source_root / "structure_manifest.json")
    asset_manifest = _read_json(source_root / "asset_manifest.json")
    validate_canonical_source_package(
        document,
        structure,
        asset_manifest,
        package_root=source_root,
    )
    selected = list(selected_chapter_ids)
    document_chapters = document.get("chapters")
    if not isinstance(document_chapters, list):
        raise PublicationComponentError(
            "source_document_invalid",
            "Source document chapters are required.",
        )
    available = [str(row.get("chapter_id") or "") for row in document_chapters]
    expected = [chapter_id for chapter_id in available if chapter_id in set(selected)]
    if expected != selected:
        raise PublicationComponentError(
            "publication_chapter_order",
            "Selected chapters must be an ordered source-package subset.",
        )
    selected_document = copy.deepcopy(document)
    selected_document["chapters"] = [
        copy.deepcopy(row)
        for row in document_chapters
        if row["chapter_id"] in set(selected)
    ]
    selected_block_ids = [
        block["block_id"]
        for chapter in selected_document["chapters"]
        for block in chapter["blocks"]
    ]
    block_set = set(selected_block_ids)
    selected_structure = copy.deepcopy(structure)
    selected_structure["units"] = [
        copy.deepcopy(row)
        for row in structure["units"]
        if row["chapter_id"] in set(selected)
    ]
    selected_structure["source_map"] = [
        copy.deepcopy(row)
        for row in structure["source_map"]
        if row["block_id"] in block_set
    ]
    selected_structure["block_policies"] = [
        copy.deepcopy(row)
        for row in structure["block_policies"]
        if row["block_id"] in block_set
    ]
    selected_structure["translatable_chapter_ids"] = [
        chapter_id
        for chapter_id in structure.get("translatable_chapter_ids", [])
        if chapter_id in set(selected)
    ]
    selected_structure["review_required_unit_ids"] = [
        unit_id
        for unit_id in structure.get("review_required_unit_ids", [])
        if unit_id in set(selected)
    ]
    selected_structure["review_required_chapter_ids"] = [
        chapter_id
        for chapter_id in structure.get("review_required_chapter_ids", [])
        if chapter_id in set(selected)
    ]
    selected_structure["exact_cover"] = {
        "expected_blocks": len(selected_block_ids),
        "covered_blocks": len(selected_block_ids),
        "overlap_count": 0,
        "missing_count": 0,
        "coverage": 1.0,
    }
    selected_structure.pop("structure_sha256", None)
    selected_structure["structure_sha256"] = canonical_sha256(
        selected_structure
    )

    selected_bindings = [
        copy.deepcopy(row)
        for row in asset_manifest["block_bindings"]
        if row["block_id"] in block_set
    ]
    if [row["block_id"] for row in selected_bindings] != selected_block_ids:
        raise PublicationComponentError(
            "publication_asset_cover",
            "Selected asset bindings do not exact-cover selected blocks.",
        )
    selected_asset_ids = {
        asset_id
        for binding in selected_bindings
        for asset_id in binding["asset_ids"]
    }
    selected_asset_rows = [
        copy.deepcopy(row)
        for row in asset_manifest["assets"]
        if row["asset_id"] in selected_asset_ids
    ]
    selected_asset_manifest = seal_asset_manifest(
        selected_document,
        selected_structure,
        assets=selected_asset_rows,
        block_bindings=selected_bindings,
    )
    destination.mkdir(parents=True)
    _write_json(destination / "document.json", selected_document)
    _write_json(destination / "structure_manifest.json", selected_structure)
    _write_json(destination / "asset_manifest.json", selected_asset_manifest)
    projection = build_admitted_projection(
        selected_document,
        selected_structure,
        selected_asset_manifest,
    )
    _write_json(destination / "admitted_projection_v1.json", projection)
    for asset in selected_asset_rows:
        if asset["availability"] != "materialized":
            continue
        relative = _safe_relative(asset["package_path"])
        source = _resolve_file(source_root, relative)
        target = destination / Path(*PurePosixPath(relative).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    validate_canonical_source_package(
        selected_document,
        selected_structure,
        selected_asset_manifest,
        package_root=destination,
    )
    return selected_document, selected_structure, selected_asset_manifest


def _build_translation_overlay(
    document: Mapping[str, Any],
    structure: Mapping[str, Any],
    asset_manifest: Mapping[str, Any],
    package_root: Path,
    translation_artifact: Mapping[str, Any],
) -> dict[str, Any]:
    translations = translation_artifact.get("translations")
    if not isinstance(translations, list):
        raise PublicationComponentError(
            "translation_artifact_shape",
            "Translation artifact must contain translations.",
        )
    document_rows = [
        block
        for chapter in document["chapters"]
        for block in chapter["blocks"]
    ]
    document_ids = [row["block_id"] for row in document_rows]
    artifact_ids = [
        row.get("block_id") if isinstance(row, Mapping) else None
        for row in translations
    ]
    if artifact_ids != document_ids:
        raise PublicationComponentError(
            "translation_artifact_cover",
            "Translation artifact must exact-cover selected blocks in order.",
        )
    binding_by_id = {
        row["block_id"]: row for row in asset_manifest["block_bindings"]
    }
    unit_by_chapter = {
        row["chapter_id"]: row for row in structure["units"]
    }
    chapter_by_block = {
        block["block_id"]: chapter["chapter_id"]
        for chapter in document["chapters"]
        for block in chapter["blocks"]
    }
    asset_by_id = {
        row["asset_id"]: row for row in asset_manifest["assets"]
    }
    source_block_by_id = {row["block_id"]: row for row in document_rows}
    overlay_rows = []
    for row in translations:
        if not isinstance(row, Mapping) or set(row) != {
            "block_id",
            "error_code",
            "status",
            "target_text",
        }:
            raise PublicationComponentError(
                "translation_artifact_row_shape",
                "Translation rows must match TranslationArtifactV1.",
            )
        block_id = row["block_id"]
        binding = binding_by_id[block_id]
        unit = unit_by_chapter[chapter_by_block[block_id]]
        action = _effective_action(unit, binding)
        if action not in {"translate", "translate_structured"}:
            continue
        if row["status"] != "translated" or not isinstance(
            row["target_text"], str
        ) or not row["target_text"]:
            raise PublicationComponentError(
                "translation_artifact_not_publishable",
                f"Required block {block_id} is not translated.",
            )
        target_text = row["target_text"]
        if action == "translate":
            overlay_rows.append(
                {
                    "block_id": block_id,
                    "text": target_text,
                    "html": None,
                    "markdown": None,
                }
            )
            continue
        tokenized = _tokenize_protected_assets(
            source_block_by_id[block_id],
            binding,
            asset_by_id,
            package_root,
            target_text,
        )
        overlay_rows.append(
            {
                "block_id": block_id,
                "text": target_text,
                "html": html.escape(tokenized).replace("\n", "<br>\n"),
                "markdown": tokenized,
            }
        )
    overlay = seal_translation_overlay(dict(document), overlay_rows)
    validate_translation_overlay(
        dict(document),
        dict(structure),
        dict(asset_manifest),
        overlay,
    )
    return overlay


def _tokenize_protected_assets(
    block: Mapping[str, Any],
    binding: Mapping[str, Any],
    asset_by_id: Mapping[str, Mapping[str, Any]],
    package_root: Path,
    target_text: str,
) -> str:
    source_text = str(block.get("source_text") or "")
    fragments: list[tuple[int, int, str, str]] = []
    display_matches = list(_DISPLAY_MATH_RE.finditer(source_text))
    display_spans = [match.span() for match in display_matches]
    inline_matches = [
        match
        for match in _INLINE_MATH_RE.finditer(source_text)
        if not any(
            start <= match.start() and match.end() <= end
            for start, end in display_spans
        )
    ]
    image_matches = list(_MARKDOWN_IMAGE_RE.finditer(source_text))
    for asset_id in binding["asset_ids"]:
        asset = asset_by_id[asset_id]
        if asset["kind"] not in _PROTECTED_ASSET_KINDS:
            continue
        locator = asset["source_locator"]
        if asset["kind"] == "equation":
            if "display_math_ordinal" in locator:
                matches = display_matches
                ordinal = locator["display_math_ordinal"]
            elif "inline_math_ordinal" in locator:
                matches = inline_matches
                ordinal = locator["inline_math_ordinal"]
            else:
                raise PublicationComponentError(
                    "publication_asset_locator",
                    f"Equation asset {asset_id} has no supported ordinal.",
                )
        elif asset["kind"] == "image":
            matches = image_matches
            ordinal = locator.get("image_ordinal")
        else:
            raise PublicationComponentError(
                "publication_asset_kind",
                f"Structured code asset {asset_id} requires a dedicated projection.",
            )
        if (
            isinstance(ordinal, bool)
            or not isinstance(ordinal, int)
            or ordinal < 0
            or ordinal >= len(matches)
        ):
            raise PublicationComponentError(
                "publication_asset_locator",
                f"Protected asset {asset_id} ordinal is outside the source block.",
            )
        match = matches[ordinal]
        fragments.append((match.start(), match.end(), asset_id, match.group(0)))
        if asset["availability"] == "materialized":
            _resolve_file(package_root, asset["package_path"])
    fragments.sort()
    for previous, current in zip(fragments, fragments[1:]):
        if previous[1] > current[0]:
            raise PublicationComponentError(
                "publication_asset_overlap",
                "Protected source asset spans overlap.",
            )
    cursor = 0
    parts = []
    for _start, _end, asset_id, fragment in fragments:
        position = target_text.find(fragment, cursor)
        if position < 0:
            raise PublicationComponentError(
                "publication_asset_restore",
                f"Translated block {block['block_id']} changed protected asset {asset_id}.",
            )
        parts.append(target_text[cursor:position])
        parts.append(f"{{{{asset:{asset_id}}}}}")
        cursor = position + len(fragment)
    parts.append(target_text[cursor:])
    return "".join(parts)


def _effective_action(
    unit: Mapping[str, Any], binding: Mapping[str, Any]
) -> str:
    unit_policy = str(unit["translation_policy"])
    if unit["review_required"] or unit_policy == "review":
        return "review"
    if unit_policy in {"exclude", "preserve"}:
        return unit_policy
    if binding["review_required"] or binding["translation_policy"] == "review":
        return "review"
    return str(binding["translation_policy"])


def _publication_events(
    *,
    workflow_run_id: str,
    component_run_id: str,
    created_at: str,
    selected_chapter_ids: Sequence[str],
    export_manifest: Mapping[str, Any],
) -> list[dict[str, Any]]:
    payloads = (
        (
            "component_started",
            None,
            {"selected_chapter_ids": list(selected_chapter_ids)},
        ),
        (
            "stage_started",
            STAGE_ID,
            {
                "current_work_id": "selected_chapter_publication",
                "progress": {"completed": 0, "total": 1, "unit": "export"},
            },
        ),
        (
            "stage_completed",
            STAGE_ID,
            {
                "outcome": "succeeded",
                "progress": {"completed": 1, "total": 1, "unit": "export"},
                "detail_kind": "publication_export",
                "counts": copy.deepcopy(export_manifest["counts"]),
            },
        ),
        (
            "component_done",
            None,
            {"status": "succeeded"},
        ),
    )
    events = []
    for index, (event_name, stage_id, payload) in enumerate(payloads, start=1):
        events.append(
            {
                "schema": EVENT_SCHEMA_ID,
                "event_id": f"evt_{component_run_id}_{index:08d}",
                "workflow_run_id": workflow_run_id,
                "flow_kind": FLOW_KIND,
                "component_id": COMPONENT_ID,
                "component_run_id": component_run_id,
                "component_attempt_id": 1,
                "component_attempt_index": 1,
                "component_seq": index,
                "ts": created_at,
                "stage_id": stage_id,
                "agent": "publication_exporter",
                "event": event_name,
                "severity": "info",
                "payload": payload,
            }
        )
    return events


def _validate_events(
    events: Sequence[Mapping[str, Any]],
    manifest: Mapping[str, Any],
) -> None:
    expected_names = (
        "component_started",
        "stage_started",
        "stage_completed",
        "component_done",
    )
    if len(events) != len(expected_names):
        raise PublicationComponentError(
            "publication_event_cover",
            "Publication event stream must contain four events.",
        )
    for index, (row, expected_name) in enumerate(
        zip(events, expected_names, strict=True), start=1
    ):
        required = {
            "schema",
            "event_id",
            "workflow_run_id",
            "flow_kind",
            "component_id",
            "component_run_id",
            "component_attempt_id",
            "component_attempt_index",
            "component_seq",
            "ts",
            "stage_id",
            "agent",
            "event",
            "severity",
            "payload",
        }
        _exact_keys(row, required, f"events[{index - 1}]")
        if (
            row["schema"] != EVENT_SCHEMA_ID
            or row["workflow_run_id"] != manifest["workflow_run_id"]
            or row["component_id"] != COMPONENT_ID
            or row["component_run_id"] != manifest["component_run_id"]
            or row["component_attempt_id"] != 1
            or row["component_attempt_index"] != 1
            or row["component_seq"] != index
            or row["event"] != expected_name
        ):
            raise PublicationComponentError(
                "publication_event_identity",
                "Publication event identity or order differs.",
            )
        _timestamp(row["ts"])
        if not isinstance(row["payload"], Mapping):
            raise PublicationComponentError(
                "publication_event_payload",
                "Publication event payload must be an object.",
            )


def _validate_artifacts(
    root: Path,
    index: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
) -> None:
    rows = index["artifacts"]
    if not isinstance(rows, list) or not rows:
        raise PublicationComponentError(
            "publication_artifact_cover",
            "Publication artifact index must not be empty.",
        )
    event_ids = {row["event_id"] for row in events}
    refs = set()
    for position, row in enumerate(rows):
        required = {
            "artifact",
            "relative_path",
            "stage_id",
            "parent_artifact_refs",
            "created_by_event_id",
        }
        if not isinstance(row, Mapping):
            raise PublicationComponentError(
                "publication_artifact_shape",
                "Publication artifact rows must be objects.",
            )
        _exact_keys(row, required, f"artifacts[{position}]")
        binding = validate_typed_artifact_binding_v1(
            row["artifact"], path=f"$.artifacts[{position}].artifact"
        )
        if binding["artifact_ref"] in refs:
            raise PublicationComponentError(
                "publication_artifact_duplicate",
                "Publication artifact reference repeats.",
            )
        refs.add(binding["artifact_ref"])
        if row["stage_id"] != STAGE_ID or row["created_by_event_id"] not in event_ids:
            raise PublicationComponentError(
                "publication_artifact_lineage",
                "Publication artifact has foreign stage/event lineage.",
            )
        path = _resolve_file(root, row["relative_path"])
        if physical_sha256(path.read_bytes()) != binding["sha256"]:
            raise PublicationComponentError(
                "publication_artifact_hash",
                f"Publication artifact bytes drifted: {binding['artifact_ref']}.",
            )
        parents = row["parent_artifact_refs"]
        if (
            not isinstance(parents, list)
            or any(not isinstance(value, str) for value in parents)
        ):
            raise PublicationComponentError(
                "publication_artifact_parent",
                "Publication artifact parents must be string references.",
            )
    for row in rows:
        if not set(row["parent_artifact_refs"]).issubset(refs):
            raise PublicationComponentError(
                "publication_artifact_parent",
                "Publication artifact references an unknown parent.",
            )


def _load_translation_artifact(
    value: Mapping[str, Any] | str | Path,
) -> tuple[dict[str, Any], bytes]:
    if isinstance(value, Mapping):
        artifact = copy.deepcopy(dict(value))
        return artifact, canonical_json_bytes(artifact) + b"\n"
    path = Path(value).resolve()
    if not path.is_file() or path.is_symlink():
        raise PublicationComponentError(
            "translation_artifact_path",
            "Translation artifact must be a regular file.",
        )
    payload = path.read_bytes()
    try:
        artifact = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PublicationComponentError(
            "translation_artifact_json",
            "Translation artifact must be UTF-8 JSON.",
        ) from exc
    if not isinstance(artifact, dict):
        raise PublicationComponentError(
            "translation_artifact_shape",
            "Translation artifact must be an object.",
        )
    return artifact, payload


def _verify_translation_binding(
    binding: Mapping[str, Any], payload: bytes
) -> None:
    if binding["sha256_kind"] != "physical":
        raise PublicationComponentError(
            "translation_artifact_hash_kind",
            "Publication requires a physical translation artifact binding.",
        )
    if binding["sha256"].lower() != physical_sha256(payload):
        raise PublicationComponentError(
            "translation_artifact_hash",
            "Translation artifact bytes differ from the sealed binding.",
        )


def _source_bindings(
    rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise PublicationComponentError(
            "source_package_bindings",
            "Source Package bindings must be an array.",
        )
    normalized = []
    roles = set()
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping) or set(row) != {"role", "binding"}:
            raise PublicationComponentError(
                "source_package_binding_shape",
                f"Source Package binding {index} has invalid keys.",
            )
        role = _identifier(row["role"], f"source_package_bindings[{index}].role")
        if role in roles:
            raise PublicationComponentError(
                "source_package_binding_duplicate",
                f"Source Package role repeats: {role}.",
            )
        roles.add(role)
        normalized.append(
            {
                "role": role,
                "binding": validate_typed_artifact_binding_v1(
                    row["binding"],
                    path=f"$.source_package_bindings[{index}].binding",
                ),
            }
        )
    return normalized


def _chapter_ids(values: Sequence[str]) -> list[str]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise PublicationComponentError(
            "publication_chapters",
            "Selected chapter IDs must be an array.",
        )
    result = [
        _identifier(value, f"selected_chapter_ids[{index}]")
        for index, value in enumerate(values)
    ]
    if not result or len(result) != len(set(result)):
        raise PublicationComponentError(
            "publication_chapters",
            "Selected chapter IDs must be non-empty and duplicate-free.",
        )
    return result


def _self_hash(value: Mapping[str, Any], field: str) -> str:
    payload = copy.deepcopy(dict(value))
    integrity = payload.get("integrity")
    if not isinstance(integrity, dict):
        raise PublicationComponentError(
            "publication_integrity",
            "Expected an integrity object.",
        )
    integrity.pop(field, None)
    return canonical_sha256(payload)


def _read_json(path: Path) -> dict[str, Any]:
    payload = _resolve_file(path.parent, path.name).read_bytes()
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PublicationComponentError(
            "publication_json",
            f"Invalid UTF-8 JSON: {path}.",
        ) from exc
    if not isinstance(value, dict):
        raise PublicationComponentError(
            "publication_json",
            f"Expected JSON object: {path}.",
        )
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    payload = _resolve_file(path.parent, path.name).read_bytes()
    rows = []
    for line in payload.splitlines():
        if not line:
            continue
        try:
            row = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PublicationComponentError(
                "publication_event_json",
                "Publication event stream contains invalid JSON.",
            ) from exc
        if not isinstance(row, dict):
            raise PublicationComponentError(
                "publication_event_json",
                "Publication event rows must be objects.",
            )
        rows.append(row)
    return rows


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value) + b"\n")


def _resolve_file(root: Path, relative: str | Path) -> Path:
    relative_string = str(relative).replace("\\", "/")
    normalized = _safe_relative(relative_string)
    path = (root / Path(*PurePosixPath(normalized).parts)).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise PublicationComponentError(
            "publication_path_escape",
            f"Path escapes the owning root: {relative}.",
        ) from exc
    if not path.is_file() or path.is_symlink():
        raise PublicationComponentError(
            "publication_file_missing",
            f"Expected regular file: {relative}.",
        )
    return path


def _safe_relative(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or "." in path.parts
        or ".." in path.parts
        or "\\" in value
    ):
        raise PublicationComponentError(
            "publication_relative_path",
            f"Unsafe relative path: {value}.",
        )
    return path.as_posix()


def _exact_keys(
    value: Mapping[str, Any], required: set[str], owner: str
) -> None:
    if not isinstance(value, Mapping) or set(value) != required:
        raise PublicationComponentError(
            "publication_shape",
            f"{owner} has missing or unsupported fields.",
        )


def _identifier(value: Any, owner: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 160
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", value)
    ):
        raise PublicationComponentError(
            "publication_identifier",
            f"{owner} must be a bounded identifier.",
        )
    return value


def _sha256(value: Any, owner: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise PublicationComponentError(
            "publication_sha256",
            f"{owner} must be SHA-256.",
        )
    return value.lower()


def _commit(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", value):
        raise PublicationComponentError(
            "publication_commit",
            "producer_code_commit must be a 40-character commit.",
        )
    return value.lower()


def _timestamp(value: Any) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise PublicationComponentError(
            "publication_timestamp",
            "Publication timestamps must be RFC3339 UTC.",
        )
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise PublicationComponentError(
            "publication_timestamp",
            "Publication timestamp is invalid.",
        ) from exc
    return value


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


__all__ = [
    "COMPONENT_ID",
    "FLOW_KIND",
    "MANIFEST_SCHEMA_ID",
    "PublicationComponentError",
    "PublicationComponentResultV1",
    "SCHEMA_VERSION",
    "STAGE_ID",
    "VALIDATOR_ID",
    "VALIDATOR_REVISION",
    "publish_selected_chapters_v1",
    "validate_publication_component_package_v1",
]
