from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from pipeline.eval.common_input_v1 import (
    AdmissionPolicyIdentityV1,
    CanonicalComponentIdentityV1,
    CanonicalProjectionIdentityV1,
    CanonicalSourcePackageBindingV1,
    CommonBlockV1,
    CommonEvaluationInputV1,
    CommonSourceSnapshotV1,
    build_common_evaluation_input,
    source_binding_to_dict,
    validate_translation_artifact,
)
from pipeline.eval.contracts_v1 import (
    ContractValidationError,
    require_enum,
    require_mapping,
    require_sha256,
    require_string,
    require_unique,
)
from pipeline.ingest.admitted_projection import (
    AdmissionProjectionError,
    validate_admitted_projection,
)
from pipeline.ingest.canonical_source_package import (
    CanonicalSourcePackageError,
    canonical_json_sha256,
    validate_canonical_source_package,
)


__all__ = [
    "FinalizedCanonicalSourceArtifactsV1",
    "build_canonical_d2l_common_input_v1",
    "derive_finalized_canonical_source_binding_v1",
    "load_finalized_canonical_d2l_source_v1",
]


_CHANNEL_TO_ADMISSION = {
    "semantic_text": "translate",
    "structured_translate": "translate_structured",
    "preserve_only": "preserve",
    "exclude": "exclude",
    "review_required": "review_required",
}


@dataclass(frozen=True, slots=True)
class FinalizedCanonicalSourceArtifactsV1:
    document: Path
    structure_manifest: Path
    asset_manifest: Path
    admitted_projection: Path
    package_seal: Path


def derive_finalized_canonical_source_binding_v1(
    *,
    source_artifacts: FinalizedCanonicalSourceArtifactsV1,
    project_id: str,
) -> dict[str, Any]:
    """Derive and validate the public binding from explicit finalized files."""

    document = _read_json(source_artifacts.document, label="document")
    structure = _read_json(
        source_artifacts.structure_manifest, label="structure manifest"
    )
    asset_manifest = _read_json(
        source_artifacts.asset_manifest, label="asset manifest"
    )
    projection = _read_json(
        source_artifacts.admitted_projection, label="admitted projection"
    )
    try:
        validate_canonical_source_package(document, structure, asset_manifest)
        validate_admitted_projection(
            projection, document, structure, asset_manifest
        )
    except (CanonicalSourcePackageError, AdmissionProjectionError) as exc:
        raise ContractValidationError(
            "canonical_source_package", "$.source_artifacts", str(exc)
        ) from exc
    draft = {
        "binding_kind": "canonical_source_package_v1",
        "project_id": require_string(project_id, path="$.project_id"),
        "document_id": require_string(
            document.get("doc_id"), path="$.document.doc_id"
        ),
        "document": {
            "schema_version": require_string(
                document.get("schema_version"), path="$.document.schema_version"
            ),
            "sha256": canonical_json_sha256(document),
        },
        "structure": {
            "schema_version": require_string(
                structure.get("schema_version"), path="$.structure.schema_version"
            ),
            "sha256": canonical_json_sha256(structure),
        },
        "asset_manifest": {
            "schema_version": require_string(
                asset_manifest.get("schema_version"),
                path="$.asset_manifest.schema_version",
            ),
            "sha256": canonical_json_sha256(asset_manifest),
        },
        "admitted_projection": {
            "schema_version": require_string(
                projection.get("schema_version"),
                path="$.admitted_projection.schema_version",
            ),
            "payload_sha256": require_sha256(
                require_mapping(
                    projection.get("integrity"),
                    path="$.admitted_projection.integrity",
                ).get("payload_sha256"),
                path="$.admitted_projection.integrity.payload_sha256",
            ),
        },
        "admission_policy": {
            "policy_id": require_string(
                require_mapping(
                    projection.get("policy"), path="$.admitted_projection.policy"
                ).get("policy_id"),
                path="$.admitted_projection.policy.policy_id",
            ),
            "policy_version": require_string(
                projection["policy"].get("policy_version"),
                path="$.admitted_projection.policy.policy_version",
            ),
            "policy_sha256": require_sha256(
                projection["policy"].get("policy_sha256"),
                path="$.admitted_projection.policy.policy_sha256",
            ),
        },
    }
    binding = _canonical_binding(
        draft,
        document=document,
        structure=structure,
        asset_manifest=asset_manifest,
        projection=projection,
    )
    _validate_package_seal(
        _read_json(source_artifacts.package_seal, label="package seal"),
        document=document,
        structure=structure,
        asset_manifest=asset_manifest,
        projection=projection,
        binding=binding,
    )
    return source_binding_to_dict(binding)


def build_canonical_d2l_common_input_v1(
    *,
    source_artifacts: FinalizedCanonicalSourceArtifactsV1,
    s0_translation_artifact: Mapping[str, Any] | Path,
    s1_translation_artifact: Mapping[str, Any] | Path,
    selected_chapter_ids: Sequence[str],
) -> CommonEvaluationInputV1:
    """Join a finalized source package to exact public S0/S1 overlays."""

    s0 = validate_translation_artifact(
        _load_payload(s0_translation_artifact, label="S0 translation artifact")
    )
    s1 = validate_translation_artifact(
        _load_payload(s1_translation_artifact, label="S1 translation artifact")
    )
    by_arm = {s0["run_identity"]["arm_id"]: s0, s1["run_identity"]["arm_id"]: s1}
    if set(by_arm) != {"s0", "s1"}:
        raise ContractValidationError(
            "d2l_arm_scope",
            "$.translation_artifacts",
            "canonical D2L bridge requires exact lower-case s0 and s1 arms",
        )
    if s0["source_binding"] != s1["source_binding"]:
        raise ContractValidationError(
            "source_binding",
            "$.translation_artifacts",
            "S0 and S1 bind different canonical source packages",
        )

    source = load_finalized_canonical_d2l_source_v1(
        source_artifacts=source_artifacts,
        expected_source_binding=s0["source_binding"],
        selected_chapter_ids=selected_chapter_ids,
    )
    return build_common_evaluation_input(source, [s0, s1])


def load_finalized_canonical_d2l_source_v1(
    *,
    source_artifacts: FinalizedCanonicalSourceArtifactsV1,
    expected_source_binding: Mapping[str, Any],
    selected_chapter_ids: Sequence[str],
) -> CommonSourceSnapshotV1:
    """Load explicitly named canonical files without scanning producer storage."""

    document = _read_json(source_artifacts.document, label="document")
    structure = _read_json(
        source_artifacts.structure_manifest, label="structure manifest"
    )
    asset_manifest = _read_json(
        source_artifacts.asset_manifest, label="asset manifest"
    )
    projection = _read_json(
        source_artifacts.admitted_projection, label="admitted projection"
    )
    package_seal = _read_json(source_artifacts.package_seal, label="package seal")

    try:
        validate_canonical_source_package(document, structure, asset_manifest)
        validate_admitted_projection(
            projection, document, structure, asset_manifest
        )
    except (CanonicalSourcePackageError, AdmissionProjectionError) as exc:
        raise ContractValidationError(
            "canonical_source_package", "$.source_artifacts", str(exc)
        ) from exc

    binding = _canonical_binding(
        expected_source_binding,
        document=document,
        structure=structure,
        asset_manifest=asset_manifest,
        projection=projection,
    )
    _validate_package_seal(
        package_seal,
        document=document,
        structure=structure,
        asset_manifest=asset_manifest,
        projection=projection,
        binding=binding,
    )

    selected = tuple(
        require_string(value, path=f"$.selected_chapter_ids[{index}]")
        for index, value in enumerate(selected_chapter_ids)
    )
    if not selected:
        raise ContractValidationError(
            "empty_array",
            "$.selected_chapter_ids",
            "at least one chapter is required",
        )
    require_unique(selected, path="$.selected_chapter_ids")

    chapters = document.get("chapters")
    if not isinstance(chapters, list) or not chapters:
        raise ContractValidationError(
            "source_document", "$.document.chapters", "chapters are required"
        )
    chapter_order = tuple(
        require_string(
            require_mapping(row, path=f"$.document.chapters[{index}]").get(
                "chapter_id"
            ),
            path=f"$.document.chapters[{index}].chapter_id",
        )
        for index, row in enumerate(chapters)
    )
    selected_set = set(selected)
    observed_selected = tuple(
        chapter_id for chapter_id in chapter_order if chapter_id in selected_set
    )
    if observed_selected != selected:
        raise ContractValidationError(
            "chapter_scope",
            "$.selected_chapter_ids",
            "selected chapters must exist once and preserve canonical source order",
        )

    projection_rows = projection["rows"]
    projection_by_id = {
        require_string(row["block_id"], path="$.projection.rows.block_id"): row
        for row in projection_rows
    }
    if len(projection_by_id) != len(projection_rows):
        raise ContractValidationError(
            "duplicate",
            "$.projection.rows.block_id",
            "admitted projection duplicates a block",
        )

    blocks: list[CommonBlockV1] = []
    for chapter_index, raw_chapter in enumerate(chapters):
        chapter = require_mapping(
            raw_chapter, path=f"$.document.chapters[{chapter_index}]"
        )
        chapter_id = str(chapter["chapter_id"])
        if chapter_id not in selected_set:
            continue
        raw_blocks = chapter.get("blocks")
        if not isinstance(raw_blocks, list) or not raw_blocks:
            raise ContractValidationError(
                "source_document",
                f"$.document.chapters[{chapter_index}].blocks",
                "selected chapter has no blocks",
            )
        for block_index, raw_block in enumerate(raw_blocks):
            path = f"$.document.chapters[{chapter_index}].blocks[{block_index}]"
            block = require_mapping(raw_block, path=path)
            block_id = require_string(block.get("block_id"), path=f"{path}.block_id")
            projection_row = projection_by_id[block_id]
            channel = require_enum(
                projection_row["channel"],
                set(_CHANNEL_TO_ADMISSION),
                path=f"$.projection.rows[{block_id}].channel",
            )
            blocks.append(
                CommonBlockV1(
                    block_id=block_id,
                    chapter_id=chapter_id,
                    order_index=int(block["order_index"]),
                    block_type=require_string(
                        block.get("block_type"), path=f"{path}.block_type"
                    ),
                    source_text=require_string(
                        block.get("clean_text"),
                        path=f"{path}.clean_text",
                        allow_empty=True,
                    ),
                    admission=_CHANNEL_TO_ADMISSION[channel],
                )
            )

    return CommonSourceSnapshotV1(
        source_schema_id="CanonicalSourcePackageV1",
        source_schema_version=require_string(
            document.get("schema_version"), path="$.document.schema_version"
        ),
        source_binding=binding,
        blocks=tuple(blocks),
    )


def _canonical_binding(
    value: Mapping[str, Any],
    *,
    document: Mapping[str, Any],
    structure: Mapping[str, Any],
    asset_manifest: Mapping[str, Any],
    projection: Mapping[str, Any],
) -> CanonicalSourcePackageBindingV1:
    row = require_mapping(value, path="$.source_binding")
    if row.get("binding_kind") != "canonical_source_package_v1":
        raise ContractValidationError(
            "source_binding",
            "$.source_binding.binding_kind",
            "public S0/S1 artifacts must bind a canonical source package",
        )
    expected = {
        "binding_kind": "canonical_source_package_v1",
        "project_id": require_string(
            row.get("project_id"), path="$.source_binding.project_id"
        ),
        "document_id": require_string(
            document.get("doc_id"), path="$.document.doc_id"
        ),
        "document": {
            "schema_version": require_string(
                document.get("schema_version"), path="$.document.schema_version"
            ),
            "sha256": canonical_json_sha256(document),
        },
        "structure": {
            "schema_version": require_string(
                structure.get("schema_version"), path="$.structure.schema_version"
            ),
            "sha256": canonical_json_sha256(structure),
        },
        "asset_manifest": {
            "schema_version": require_string(
                asset_manifest.get("schema_version"),
                path="$.asset_manifest.schema_version",
            ),
            "sha256": canonical_json_sha256(asset_manifest),
        },
        "admitted_projection": {
            "schema_version": require_string(
                projection.get("schema_version"),
                path="$.admitted_projection.schema_version",
            ),
            "payload_sha256": require_sha256(
                require_mapping(
                    projection.get("integrity"),
                    path="$.admitted_projection.integrity",
                ).get("payload_sha256"),
                path="$.admitted_projection.integrity.payload_sha256",
            ),
        },
        "admission_policy": {
            "policy_id": require_string(
                require_mapping(
                    projection.get("policy"), path="$.admitted_projection.policy"
                ).get("policy_id"),
                path="$.admitted_projection.policy.policy_id",
            ),
            "policy_version": require_string(
                projection["policy"].get("policy_version"),
                path="$.admitted_projection.policy.policy_version",
            ),
            "policy_sha256": require_sha256(
                projection["policy"].get("policy_sha256"),
                path="$.admitted_projection.policy.policy_sha256",
            ),
        },
    }
    normalized = _lower_hashes(dict(row))
    if normalized != expected:
        raise ContractValidationError(
            "source_binding",
            "$.source_binding",
            "translation source binding differs from finalized canonical components",
        )
    return CanonicalSourcePackageBindingV1(
        project_id=expected["project_id"],
        document_id=expected["document_id"],
        document=CanonicalComponentIdentityV1(**expected["document"]),
        structure=CanonicalComponentIdentityV1(**expected["structure"]),
        asset_manifest=CanonicalComponentIdentityV1(**expected["asset_manifest"]),
        admitted_projection=CanonicalProjectionIdentityV1(
            **expected["admitted_projection"]
        ),
        admission_policy=AdmissionPolicyIdentityV1(
            **expected["admission_policy"]
        ),
    )


def _validate_package_seal(
    value: Mapping[str, Any],
    *,
    document: Mapping[str, Any],
    structure: Mapping[str, Any],
    asset_manifest: Mapping[str, Any],
    projection: Mapping[str, Any],
    binding: CanonicalSourcePackageBindingV1,
) -> None:
    row = require_mapping(value, path="$.package_seal")
    if row.get("schema_version") != "source_package_finalization_v1":
        raise ContractValidationError(
            "package_seal",
            "$.package_seal.schema_version",
            "unsupported source finalization schema",
        )
    if not str(row.get("lifecycle") or "").startswith("finalized"):
        raise ContractValidationError(
            "package_seal",
            "$.package_seal.lifecycle",
            "source package is not finalized",
        )
    if row.get("doc_id") != binding.document_id:
        raise ContractValidationError(
            "package_seal",
            "$.package_seal.doc_id",
            "source finalization document identity drift",
        )
    integrity = require_mapping(
        row.get("integrity"), path="$.package_seal.integrity"
    )
    payload = dict(row)
    payload.pop("integrity", None)
    if canonical_json_sha256(payload) != require_sha256(
        integrity.get("payload_sha256"),
        path="$.package_seal.integrity.payload_sha256",
    ):
        raise ContractValidationError(
            "package_seal",
            "$.package_seal.integrity.payload_sha256",
            "source finalization self-hash drift",
        )
    package = require_mapping(row.get("package"), path="$.package_seal.package")
    expected = {
        "document": (
            document["schema_version"],
            canonical_json_sha256(document),
        ),
        "structure": (
            structure["schema_version"],
            canonical_json_sha256(structure),
        ),
        "asset_manifest": (
            asset_manifest["schema_version"],
            canonical_json_sha256(asset_manifest),
        ),
        "admitted_projection": (
            projection["schema_version"],
            canonical_json_sha256(projection),
        ),
    }
    for key, (schema_version, sha256) in expected.items():
        component = require_mapping(
            package.get(key), path=f"$.package_seal.package.{key}"
        )
        if (
            component.get("schema_version") != schema_version
            or str(component.get("sha256") or "").lower() != sha256
        ):
            raise ContractValidationError(
                "package_seal",
                f"$.package_seal.package.{key}",
                "source finalization component binding drift",
            )
    policies = require_mapping(row.get("policies"), path="$.package_seal.policies")
    admission = require_mapping(
        policies.get("admission"), path="$.package_seal.policies.admission"
    )
    if _lower_hashes(dict(admission)) != source_binding_to_dict(binding)[
        "admission_policy"
    ]:
        raise ContractValidationError(
            "package_seal",
            "$.package_seal.policies.admission",
            "source finalization admission policy drift",
        )


def _load_payload(
    value: Mapping[str, Any] | Path, *, label: str
) -> Mapping[str, Any]:
    if isinstance(value, Path):
        return _read_json(value, label=label)
    return require_mapping(value, path=f"$.{label.replace(' ', '_')}")


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    candidate = Path(path).resolve()
    if not candidate.is_file():
        raise ContractValidationError(
            "missing_artifact", f"$.{label}", f"file does not exist: {candidate}"
        )
    try:
        value = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractValidationError(
            "invalid_json", f"$.{label}", f"cannot read canonical JSON: {candidate}"
        ) from exc
    return dict(require_mapping(value, path=f"$.{label}"))


def _lower_hashes(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                item.lower()
                if isinstance(item, str)
                and (
                    key == "sha256"
                    or key == "payload_sha256"
                    or key == "policy_sha256"
                )
                else _lower_hashes(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_lower_hashes(item) for item in value]
    return value
