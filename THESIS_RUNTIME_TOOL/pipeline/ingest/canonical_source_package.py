from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any

from pipeline.ingest.document_contract import (
    DocumentContractError,
    validate_locked_document,
)


PACKAGE_VERSION = "canonical_source_package_v1"
ASSET_MANIFEST_VERSION = "canonical_asset_manifest_v1"
VALIDATION_REPORT_VERSION = "canonical_source_package_validation_v1"

TRANSLATION_POLICIES = {
    "translate",
    "preserve",
    "translate_structured",
    "exclude",
    "review",
}
SEMANTIC_KINDS = {
    "text",
    "caption",
    "table",
    "image",
    "equation",
    "code",
    "structural",
    "unknown",
}
ASSET_KINDS = {
    "image",
    "table",
    "equation",
    "code",
    "raw_fragment",
    "embedded_file",
}
ASSET_AVAILABILITY = {"materialized", "source_reference", "missing"}
RENDER_ROLES = {"text", "asset", "caption", "placeholder", "structural"}
RICH_SEMANTIC_KINDS = {"table", "image", "equation", "code"}

_TOP_LEVEL_FIELDS = {
    "schema_version",
    "package_version",
    "doc_id",
    "source",
    "document",
    "structure",
    "assets_root",
    "assets",
    "block_bindings",
    "integrity",
}
_ASSET_FIELDS = {
    "asset_id",
    "kind",
    "media_type",
    "translation_policy",
    "availability",
    "package_path",
    "sha256",
    "source_locator",
    "metadata",
    "review_required",
}
_BINDING_FIELDS = {
    "block_id",
    "source_kind",
    "semantic_kind",
    "semantic_subtype",
    "translation_policy",
    "asset_ids",
    "render_role",
    "review_required",
}


class CanonicalSourcePackageError(ValueError):
    pass


def canonical_json_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _require_exact_fields(payload: dict[str, Any], expected: set[str], *, owner: str) -> None:
    actual = set(payload)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise CanonicalSourcePackageError(
            f"{owner} fields differ; missing={missing}, extra={extra}"
        )


def _require_nonempty_string(payload: dict[str, Any], field: str, *, owner: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise CanonicalSourcePackageError(f"{owner}.{field} must be a non-empty string")
    return value


def _require_sha256(value: Any, *, owner: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise CanonicalSourcePackageError(f"{owner} must be a lowercase SHA-256 hex digest")
    return value


def _flatten_blocks(document: dict[str, Any]) -> list[dict[str, Any]]:
    chapters = document.get("chapters")
    if not isinstance(chapters, list) or not chapters:
        raise CanonicalSourcePackageError("document.chapters must be a non-empty list")
    blocks: list[dict[str, Any]] = []
    for chapter in chapters:
        if not isinstance(chapter, dict):
            raise CanonicalSourcePackageError("every document chapter must be an object")
        chapter_blocks = chapter.get("blocks")
        if not isinstance(chapter_blocks, list):
            raise CanonicalSourcePackageError("every document chapter must contain a blocks list")
        blocks.extend(chapter_blocks)
    if not blocks:
        raise CanonicalSourcePackageError("document contains no blocks")
    return blocks


def _manifest_payload(asset_manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(value)
        for key, value in asset_manifest.items()
        if key != "integrity"
    }


def _safe_package_path(package_path: str, *, assets_root: str) -> PurePosixPath:
    path = PurePosixPath(package_path)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise CanonicalSourcePackageError(
            f"asset package_path must be a normalized relative path: {package_path}"
        )
    if not path.parts or path.parts[0] != assets_root:
        raise CanonicalSourcePackageError(
            f"asset package_path must live under {assets_root}/: {package_path}"
        )
    return path


def seal_asset_manifest(
    document: dict[str, Any],
    structure_manifest: dict[str, Any],
    *,
    assets: list[dict[str, Any]],
    block_bindings: list[dict[str, Any]],
) -> dict[str, Any]:
    doc_id = _require_nonempty_string(document, "doc_id", owner="document")
    source = structure_manifest.get("source")
    if not isinstance(source, dict):
        raise CanonicalSourcePackageError("structure.source must be an object")
    source_format = _require_nonempty_string(source, "format", owner="structure.source")
    source_sha256 = _require_sha256(
        source.get("sha256"),
        owner="structure.source.sha256",
    )
    structure_schema = _require_nonempty_string(
        structure_manifest,
        "schema_version",
        owner="structure",
    )
    manifest: dict[str, Any] = {
        "schema_version": ASSET_MANIFEST_VERSION,
        "package_version": PACKAGE_VERSION,
        "doc_id": doc_id,
        "source": {
            "format": source_format,
            "sha256": source_sha256,
        },
        "document": {
            "schema_version": document.get("schema_version"),
            "sha256": canonical_json_sha256(document),
        },
        "structure": {
            "schema_version": structure_schema,
            "sha256": canonical_json_sha256(structure_manifest),
        },
        "assets_root": "assets",
        "assets": copy.deepcopy(assets),
        "block_bindings": copy.deepcopy(block_bindings),
    }
    manifest["integrity"] = {
        "asset_count": len(assets),
        "binding_count": len(block_bindings),
        "manifest_payload_sha256": canonical_json_sha256(manifest),
    }
    return manifest


def validate_canonical_source_package(
    document: dict[str, Any],
    structure_manifest: dict[str, Any],
    asset_manifest: dict[str, Any],
    *,
    package_root: str | Path | None = None,
) -> dict[str, Any]:
    try:
        validate_locked_document(document)
    except DocumentContractError as exc:
        raise CanonicalSourcePackageError(str(exc)) from exc
    doc_id = _require_nonempty_string(document, "doc_id", owner="document")
    if structure_manifest.get("doc_id") != doc_id:
        raise CanonicalSourcePackageError("document and structure doc_id values differ")

    _require_exact_fields(asset_manifest, _TOP_LEVEL_FIELDS, owner="asset_manifest")
    if asset_manifest.get("schema_version") != ASSET_MANIFEST_VERSION:
        raise CanonicalSourcePackageError(
            f"asset_manifest.schema_version must be {ASSET_MANIFEST_VERSION}"
        )
    if asset_manifest.get("package_version") != PACKAGE_VERSION:
        raise CanonicalSourcePackageError(
            f"asset_manifest.package_version must be {PACKAGE_VERSION}"
        )
    if asset_manifest.get("doc_id") != doc_id:
        raise CanonicalSourcePackageError("document and asset manifest doc_id values differ")

    structure_source = structure_manifest.get("source")
    manifest_source = asset_manifest.get("source")
    if not isinstance(structure_source, dict) or not isinstance(manifest_source, dict):
        raise CanonicalSourcePackageError("source identities must be objects")
    source_format = _require_nonempty_string(
        structure_source,
        "format",
        owner="structure.source",
    )
    source_sha256 = _require_sha256(
        structure_source.get("sha256"),
        owner="structure.source.sha256",
    )
    if manifest_source != {"format": source_format, "sha256": source_sha256}:
        raise CanonicalSourcePackageError("asset manifest source identity is stale")

    document_identity = asset_manifest.get("document")
    structure_identity = asset_manifest.get("structure")
    if not isinstance(document_identity, dict) or not isinstance(structure_identity, dict):
        raise CanonicalSourcePackageError("document and structure identities must be objects")
    if document_identity != {
        "schema_version": "1.5.0",
        "sha256": canonical_json_sha256(document),
    }:
        raise CanonicalSourcePackageError("asset manifest document identity is stale")
    if structure_identity != {
        "schema_version": structure_manifest.get("schema_version"),
        "sha256": canonical_json_sha256(structure_manifest),
    }:
        raise CanonicalSourcePackageError("asset manifest structure identity is stale")

    assets_root = asset_manifest.get("assets_root")
    if assets_root != "assets":
        raise CanonicalSourcePackageError("asset_manifest.assets_root must be 'assets'")

    blocks = _flatten_blocks(document)
    block_ids = [
        _require_nonempty_string(block, "block_id", owner="document.block")
        for block in blocks
    ]
    if len(block_ids) != len(set(block_ids)):
        raise CanonicalSourcePackageError("document contains duplicate block_id values")

    assets = asset_manifest.get("assets")
    if not isinstance(assets, list):
        raise CanonicalSourcePackageError("asset_manifest.assets must be a list")
    asset_by_id: dict[str, dict[str, Any]] = {}
    root = Path(package_root).resolve() if package_root is not None else None
    for index, asset in enumerate(assets):
        owner = f"asset_manifest.assets[{index}]"
        if not isinstance(asset, dict):
            raise CanonicalSourcePackageError(f"{owner} must be an object")
        _require_exact_fields(asset, _ASSET_FIELDS, owner=owner)
        asset_id = _require_nonempty_string(asset, "asset_id", owner=owner)
        if asset_id in asset_by_id:
            raise CanonicalSourcePackageError(f"duplicate asset_id: {asset_id}")
        asset_by_id[asset_id] = asset
        if asset.get("kind") not in ASSET_KINDS:
            raise CanonicalSourcePackageError(f"{owner}.kind is not allowed")
        _require_nonempty_string(asset, "media_type", owner=owner)
        if asset.get("translation_policy") not in TRANSLATION_POLICIES:
            raise CanonicalSourcePackageError(f"{owner}.translation_policy is not allowed")
        availability = asset.get("availability")
        if availability not in ASSET_AVAILABILITY:
            raise CanonicalSourcePackageError(f"{owner}.availability is not allowed")
        if not isinstance(asset.get("source_locator"), dict) or not asset["source_locator"]:
            raise CanonicalSourcePackageError(f"{owner}.source_locator must be non-empty")
        if not isinstance(asset.get("metadata"), dict):
            raise CanonicalSourcePackageError(f"{owner}.metadata must be an object")
        if not isinstance(asset.get("review_required"), bool):
            raise CanonicalSourcePackageError(f"{owner}.review_required must be boolean")

        package_path = asset.get("package_path")
        content_sha256 = asset.get("sha256")
        if availability == "materialized":
            if not isinstance(package_path, str) or not package_path:
                raise CanonicalSourcePackageError(
                    f"{owner}.package_path is required for materialized assets"
                )
            relative_path = _safe_package_path(package_path, assets_root=assets_root)
            expected_sha256 = _require_sha256(content_sha256, owner=f"{owner}.sha256")
            if root is not None:
                candidate = (root / Path(*relative_path.parts)).resolve()
                try:
                    candidate.relative_to(root)
                except ValueError as exc:
                    raise CanonicalSourcePackageError(
                        f"{owner}.package_path escapes the package root"
                    ) from exc
                if not candidate.is_file():
                    raise CanonicalSourcePackageError(
                        f"materialized asset is missing: {package_path}"
                    )
                actual_sha256 = hashlib.sha256(candidate.read_bytes()).hexdigest()
                if actual_sha256 != expected_sha256:
                    raise CanonicalSourcePackageError(
                        f"materialized asset hash differs: {package_path}"
                    )
        elif package_path is not None or content_sha256 is not None:
            raise CanonicalSourcePackageError(
                f"{owner} may only carry package_path/sha256 when materialized"
            )
        if availability == "missing" and not asset["review_required"]:
            raise CanonicalSourcePackageError(
                f"{owner} missing assets must be review_required"
            )

    bindings = asset_manifest.get("block_bindings")
    if not isinstance(bindings, list):
        raise CanonicalSourcePackageError("asset_manifest.block_bindings must be a list")
    bound_block_ids: list[str] = []
    review_binding_count = 0
    for index, binding in enumerate(bindings):
        owner = f"asset_manifest.block_bindings[{index}]"
        if not isinstance(binding, dict):
            raise CanonicalSourcePackageError(f"{owner} must be an object")
        _require_exact_fields(binding, _BINDING_FIELDS, owner=owner)
        block_id = _require_nonempty_string(binding, "block_id", owner=owner)
        bound_block_ids.append(block_id)
        _require_nonempty_string(binding, "source_kind", owner=owner)
        semantic_kind = binding.get("semantic_kind")
        if semantic_kind not in SEMANTIC_KINDS:
            raise CanonicalSourcePackageError(f"{owner}.semantic_kind is not allowed")
        semantic_subtype = binding.get("semantic_subtype")
        if semantic_subtype is not None and (
            not isinstance(semantic_subtype, str) or not semantic_subtype
        ):
            raise CanonicalSourcePackageError(
                f"{owner}.semantic_subtype must be null or a non-empty string"
            )
        policy = binding.get("translation_policy")
        if policy not in TRANSLATION_POLICIES:
            raise CanonicalSourcePackageError(f"{owner}.translation_policy is not allowed")
        render_role = binding.get("render_role")
        if render_role not in RENDER_ROLES:
            raise CanonicalSourcePackageError(f"{owner}.render_role is not allowed")
        if not isinstance(binding.get("review_required"), bool):
            raise CanonicalSourcePackageError(f"{owner}.review_required must be boolean")
        if binding["review_required"]:
            review_binding_count += 1
        asset_ids = binding.get("asset_ids")
        if (
            not isinstance(asset_ids, list)
            or any(not isinstance(asset_id, str) or not asset_id for asset_id in asset_ids)
            or len(asset_ids) != len(set(asset_ids))
        ):
            raise CanonicalSourcePackageError(
                f"{owner}.asset_ids must be a unique string list"
            )
        for asset_id in asset_ids:
            asset = asset_by_id.get(asset_id)
            if asset is None:
                raise CanonicalSourcePackageError(
                    f"{owner} references unknown asset_id: {asset_id}"
                )
            if asset["translation_policy"] != policy:
                raise CanonicalSourcePackageError(
                    f"{owner} and asset {asset_id} translation policies differ"
                )
        if render_role == "asset" and not asset_ids:
            raise CanonicalSourcePackageError(f"{owner} asset render role requires an asset")
        if (
            semantic_kind in RICH_SEMANTIC_KINDS
            and not asset_ids
            and not binding["review_required"]
        ):
            raise CanonicalSourcePackageError(
                f"{owner} rich semantic blocks require an asset or review_required"
            )

    if bound_block_ids != block_ids:
        raise CanonicalSourcePackageError(
            "block_bindings must cover every document block once in document order"
        )

    integrity = asset_manifest.get("integrity")
    if not isinstance(integrity, dict) or set(integrity) != {
        "asset_count",
        "binding_count",
        "manifest_payload_sha256",
    }:
        raise CanonicalSourcePackageError("asset_manifest.integrity has an invalid shape")
    if integrity.get("asset_count") != len(assets):
        raise CanonicalSourcePackageError("asset_manifest integrity asset_count differs")
    if integrity.get("binding_count") != len(bindings):
        raise CanonicalSourcePackageError("asset_manifest integrity binding_count differs")
    expected_payload_sha256 = canonical_json_sha256(_manifest_payload(asset_manifest))
    if integrity.get("manifest_payload_sha256") != expected_payload_sha256:
        raise CanonicalSourcePackageError("asset_manifest payload hash differs")

    missing_assets = sum(asset["availability"] == "missing" for asset in assets)
    source_reference_assets = sum(
        asset["availability"] == "source_reference" for asset in assets
    )
    if missing_assets or review_binding_count:
        status = "review_required"
    elif assets:
        status = "preservation_complete"
    else:
        status = "text_only"
    return {
        "schema_version": VALIDATION_REPORT_VERSION,
        "package_version": PACKAGE_VERSION,
        "doc_id": doc_id,
        "source_format": source_format,
        "status": status,
        "counts": {
            "blocks": len(blocks),
            "bindings": len(bindings),
            "assets": len(assets),
            "materialized_assets": sum(
                asset["availability"] == "materialized" for asset in assets
            ),
            "source_reference_assets": source_reference_assets,
            "missing_assets": missing_assets,
            "review_required_bindings": review_binding_count,
        },
        "hashes": {
            "document": canonical_json_sha256(document),
            "structure": canonical_json_sha256(structure_manifest),
            "asset_manifest_payload": expected_payload_sha256,
        },
    }


__all__ = [
    "ASSET_MANIFEST_VERSION",
    "PACKAGE_VERSION",
    "VALIDATION_REPORT_VERSION",
    "CanonicalSourcePackageError",
    "canonical_json_sha256",
    "seal_asset_manifest",
    "validate_canonical_source_package",
]
