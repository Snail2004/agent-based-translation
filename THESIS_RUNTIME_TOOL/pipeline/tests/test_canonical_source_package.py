from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

import pytest

from pipeline.ingest.canonical_source_package import (
    CanonicalSourcePackageError,
    seal_asset_manifest,
    validate_canonical_source_package,
)
from pipeline.ingest.document_loader import load_document
from pipeline.ingest.unified_source_normalizer import validate_normalization_contract


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "canonical_source_package_v1"
SCHEMA_PATH = (
    Path(__file__).parents[1]
    / "ingest"
    / "schemas"
    / "canonical_asset_manifest_v1.schema.json"
)


def _load_fixture() -> tuple[dict, dict, dict]:
    return tuple(
        json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8"))
        for name in ("document.json", "structure_manifest.json", "asset_manifest.json")
    )


def test_conformance_fixture_validates_and_legacy_loader_still_reads_document(
    tmp_path: Path,
) -> None:
    document, structure, assets = _load_fixture()

    normalization = validate_normalization_contract(
        document,
        structure,
        expected_format="html",
    )
    package = validate_canonical_source_package(
        document,
        structure,
        assets,
        package_root=FIXTURE_ROOT,
    )
    load_report = load_document(tmp_path / "memory.sqlite3", FIXTURE_ROOT / "document.json")

    assert normalization["status"] == "ready"
    assert package["status"] == "preservation_complete"
    assert package["counts"] == {
        "blocks": 5,
        "bindings": 5,
        "assets": 3,
        "materialized_assets": 3,
        "source_reference_assets": 0,
        "missing_assets": 0,
        "review_required_bindings": 0,
    }
    assert load_report.blocks == 5
    assert load_report.warnings == []


def test_sealer_reproduces_the_reviewed_fixture_manifest() -> None:
    document, structure, expected = _load_fixture()

    actual = seal_asset_manifest(
        document,
        structure,
        assets=expected["assets"],
        block_bindings=expected["block_bindings"],
    )

    assert actual == expected


def test_json_schema_is_valid_and_accepts_the_fixture() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    _, _, assets = _load_fixture()

    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.Draft202012Validator(schema).validate(assets)


def test_json_schema_rejects_the_same_load_bearing_invalid_shapes() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    _, _, assets = _load_fixture()

    missing_without_review = copy.deepcopy(assets)
    missing_without_review["assets"][0].update(
        {
            "availability": "missing",
            "package_path": None,
            "sha256": None,
            "review_required": False,
        }
    )
    rich_without_asset = copy.deepcopy(assets)
    rich_without_asset["block_bindings"][2].update(
        {
            "asset_ids": [],
            "render_role": "placeholder",
        }
    )

    assert list(validator.iter_errors(missing_without_review))
    assert list(validator.iter_errors(rich_without_asset))


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (
            lambda payload: payload["block_bindings"].pop(),
            "cover every document block",
        ),
        (
            lambda payload: payload["block_bindings"][2]["asset_ids"].append("foreign"),
            "unknown asset_id",
        ),
        (
            lambda payload: payload["assets"].append(copy.deepcopy(payload["assets"][0])),
            "duplicate asset_id",
        ),
        (
            lambda payload: payload["document"].update({"sha256": "0" * 64}),
            "document identity is stale",
        ),
        (
            lambda payload: payload["structure"].update({"sha256": "0" * 64}),
            "structure identity is stale",
        ),
    ],
)
def test_cross_artifact_tampering_fails_closed(mutator, message: str) -> None:
    document, structure, assets = _load_fixture()
    tampered = copy.deepcopy(assets)
    mutator(tampered)

    with pytest.raises(CanonicalSourcePackageError, match=message):
        validate_canonical_source_package(document, structure, tampered)


def test_asset_paths_cannot_escape_package_root() -> None:
    document, structure, assets = _load_fixture()
    assets["assets"][0]["package_path"] = "assets/../secret.txt"

    with pytest.raises(CanonicalSourcePackageError, match="normalized relative path"):
        validate_canonical_source_package(document, structure, assets)


def test_materialized_asset_hash_is_verified_against_physical_bytes(
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "package"
    shutil.copytree(FIXTURE_ROOT, package_root)
    (package_root / "assets" / "table.html").write_text(
        "<table><tr><td>tampered</td></tr></table>\n",
        encoding="utf-8",
    )
    document = json.loads((package_root / "document.json").read_text(encoding="utf-8"))
    structure = json.loads(
        (package_root / "structure_manifest.json").read_text(encoding="utf-8")
    )
    assets = json.loads(
        (package_root / "asset_manifest.json").read_text(encoding="utf-8")
    )

    with pytest.raises(CanonicalSourcePackageError, match="asset hash differs"):
        validate_canonical_source_package(
            document,
            structure,
            assets,
            package_root=package_root,
        )


def test_missing_asset_requires_visible_review_state() -> None:
    document, structure, assets = _load_fixture()
    asset = assets["assets"][0]
    asset.update(
        {
            "availability": "missing",
            "package_path": None,
            "sha256": None,
            "review_required": False,
        }
    )

    with pytest.raises(CanonicalSourcePackageError, match="must be review_required"):
        validate_canonical_source_package(document, structure, assets)


def test_rich_block_without_asset_must_be_review_required() -> None:
    document, structure, assets = _load_fixture()
    assets["block_bindings"][2]["asset_ids"] = []
    assets["block_bindings"][2]["render_role"] = "placeholder"

    with pytest.raises(CanonicalSourcePackageError, match="require an asset"):
        validate_canonical_source_package(document, structure, assets)


def test_contract_code_is_book_neutral() -> None:
    source = (
        Path(__file__).parents[1]
        / "ingest"
        / "canonical_source_package.py"
    ).read_text(encoding="utf-8").casefold()
    for forbidden in ("canterville", "gatsby", "wuthering", "treasure", "jekyll"):
        assert forbidden not in source
