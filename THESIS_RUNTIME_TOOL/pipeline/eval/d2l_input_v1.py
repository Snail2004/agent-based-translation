from __future__ import annotations

from typing import Any, Mapping

from pipeline.eval.contracts_v1 import (
    CanonicalPolicy,
    ContractValidationError,
    assert_no_forbidden_runtime_data,
    canonical_sha256,
    canonicalize,
    require_enum,
    require_exact_keys,
    require_int,
    require_list,
    require_mapping,
    require_nullable_string,
    require_relative_path,
    require_rfc3339,
    require_sha256,
    require_string,
    require_unique,
    seal_payload,
    validate_producer,
    verify_payload_hash,
)


SCHEMA_ID = "D2LEvaluationInputV1"
SCHEMA_VERSION = "1.0.0"
SELF_HASH_PATH = ("integrity", "package_sha256")


D2L_CANONICAL_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(
        {
            ("arms",),
            ("translations",),
            ("runtime_terms",),
            ("runtime_terms", "*", "accepted_variants"),
            ("runtime_terms", "*", "provenance_artifact_ids"),
            ("injection_rows",),
            ("injection_rows", "*", "source_artifact_ids"),
            ("artifacts",),
        }
    ),
    semantic_sequence_paths=frozenset(
        {
            ("identity", "selected_chapter_ids"),
            ("blocks",),
            ("runtime_terms", "*", "source_block_ids"),
        }
    ),
)


def seal_d2l_evaluation_input(payload: Mapping[str, Any]) -> dict[str, Any]:
    return seal_payload(payload, policy=D2L_CANONICAL_POLICY, hash_path=SELF_HASH_PATH)


def validate_d2l_evaluation_input(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and return a canonical copy of a gold-free D2L runtime package."""

    assert_no_forbidden_runtime_data(payload)
    root = require_mapping(payload, path="$")
    require_exact_keys(
        root,
        required={
            "schema_id",
            "schema_version",
            "package_id",
            "created_at",
            "producer",
            "identity",
            "runtime_profile",
            "arms",
            "blocks",
            "translations",
            "runtime_terms",
            "injection_rows",
            "artifacts",
            "integrity",
        },
        path="$",
    )
    normalized: dict[str, Any] = {
        "schema_id": require_enum(root["schema_id"], {SCHEMA_ID}, path="$.schema_id"),
        "schema_version": require_enum(
            root["schema_version"], {SCHEMA_VERSION}, path="$.schema_version"
        ),
        "package_id": require_string(root["package_id"], path="$.package_id"),
        "created_at": require_rfc3339(root["created_at"], path="$.created_at"),
        "producer": validate_producer(root["producer"], path="$.producer", workstream="d2l"),
    }
    normalized["identity"] = _validate_identity(root["identity"])
    normalized["runtime_profile"] = _validate_runtime_profile(root["runtime_profile"])
    normalized["arms"] = _validate_arms(root["arms"])
    normalized["blocks"] = _validate_blocks(
        root["blocks"], selected_chapter_ids=normalized["identity"]["selected_chapter_ids"]
    )
    normalized["translations"] = _validate_translations(root["translations"])
    normalized["runtime_terms"] = _validate_runtime_terms(root["runtime_terms"])
    normalized["injection_rows"] = _validate_injection_rows(root["injection_rows"])
    normalized["artifacts"] = _validate_artifacts(root["artifacts"])
    normalized["integrity"] = _validate_integrity(root["integrity"])
    _validate_references(normalized)
    expected_artifact_set = canonical_sha256(
        {"artifacts": normalized["artifacts"]}, policy=D2L_CANONICAL_POLICY
    )
    if normalized["integrity"]["artifact_set_sha256"] != expected_artifact_set:
        raise ContractValidationError(
            "artifact_set_hash",
            "$.integrity.artifact_set_sha256",
            "artifact set hash does not match artifacts",
        )
    if not verify_payload_hash(
        normalized, policy=D2L_CANONICAL_POLICY, hash_path=SELF_HASH_PATH
    ):
        raise ContractValidationError(
            "package_hash",
            "$.integrity.package_sha256",
            "package self-hash does not match canonical content",
        )
    canonical = canonicalize(normalized, policy=D2L_CANONICAL_POLICY)
    if not isinstance(canonical, dict):
        raise AssertionError("canonical D2L input must remain an object")
    return canonical


def _validate_identity(value: Any) -> dict[str, Any]:
    path = "$.identity"
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "project_id",
            "logical_run_id",
            "document_id",
            "profile_id",
            "experiment_id",
            "selected_chapter_ids",
            "source_db_sha256",
            "runtime_manifest_sha256",
            "source_manifest_artifact_id",
        },
        path=path,
    )
    chapters = [
        require_string(item, path=f"{path}.selected_chapter_ids[{index}]")
        for index, item in enumerate(
            require_list(row["selected_chapter_ids"], path=f"{path}.selected_chapter_ids")
        )
    ]
    if not chapters:
        raise ContractValidationError(
            "empty_array", f"{path}.selected_chapter_ids", "at least one chapter is required"
        )
    require_unique(chapters, path=f"{path}.selected_chapter_ids")
    return {
        "project_id": require_string(row["project_id"], path=f"{path}.project_id"),
        "logical_run_id": require_string(
            row["logical_run_id"], path=f"{path}.logical_run_id"
        ),
        "document_id": require_string(row["document_id"], path=f"{path}.document_id"),
        "profile_id": require_string(row["profile_id"], path=f"{path}.profile_id"),
        "experiment_id": require_string(
            row["experiment_id"], path=f"{path}.experiment_id"
        ),
        "selected_chapter_ids": chapters,
        "source_db_sha256": require_sha256(
            row["source_db_sha256"], path=f"{path}.source_db_sha256"
        ),
        "runtime_manifest_sha256": require_sha256(
            row["runtime_manifest_sha256"], path=f"{path}.runtime_manifest_sha256"
        ),
        "source_manifest_artifact_id": require_string(
            row["source_manifest_artifact_id"],
            path=f"{path}.source_manifest_artifact_id",
        ),
    }


def _validate_runtime_profile(value: Any) -> dict[str, Any]:
    path = "$.runtime_profile"
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "profile_id",
            "profile_version",
            "source_language",
            "target_language",
            "domain",
            "source_artifact_id",
        },
        path=path,
    )
    return {
        "profile_id": require_string(row["profile_id"], path=f"{path}.profile_id"),
        "profile_version": require_string(
            row["profile_version"], path=f"{path}.profile_version"
        ),
        "source_language": require_string(
            row["source_language"], path=f"{path}.source_language"
        ),
        "target_language": require_string(
            row["target_language"], path=f"{path}.target_language"
        ),
        "domain": require_string(row["domain"], path=f"{path}.domain"),
        "source_artifact_id": require_string(
            row["source_artifact_id"], path=f"{path}.source_artifact_id"
        ),
    }


def _validate_arms(value: Any) -> list[dict[str, Any]]:
    path = "$.arms"
    rows = require_list(value, path=path)
    if not rows:
        raise ContractValidationError("empty_array", path, "at least one arm is required")
    result: list[dict[str, Any]] = []
    for index, value_row in enumerate(rows):
        row_path = f"{path}[{index}]"
        row = require_mapping(value_row, path=row_path)
        require_exact_keys(
            row,
            required={
                "arm_id",
                "role",
                "label",
                "translation_artifact_id",
                "translation_sha256",
            },
            path=row_path,
        )
        result.append(
            {
                "arm_id": require_string(row["arm_id"], path=f"{row_path}.arm_id"),
                "role": require_enum(
                    row["role"], {"baseline", "candidate"}, path=f"{row_path}.role"
                ),
                "label": require_string(row["label"], path=f"{row_path}.label"),
                "translation_artifact_id": require_string(
                    row["translation_artifact_id"],
                    path=f"{row_path}.translation_artifact_id",
                ),
                "translation_sha256": require_sha256(
                    row["translation_sha256"], path=f"{row_path}.translation_sha256"
                ),
            }
        )
    require_unique([row["arm_id"] for row in result], path=path)
    require_unique(
        [row["translation_artifact_id"] for row in result],
        path=f"{path}.translation_artifact_id",
    )
    roles = [row["role"] for row in result]
    if roles.count("baseline") > 1 or roles.count("candidate") > 1:
        raise ContractValidationError(
            "arm_roles", path, "baseline and candidate roles may appear at most once"
        )
    return result


def _validate_blocks(value: Any, *, selected_chapter_ids: list[str]) -> list[dict[str, Any]]:
    path = "$.blocks"
    rows = require_list(value, path=path)
    if not rows:
        raise ContractValidationError("empty_array", path, "source blocks are required")
    result: list[dict[str, Any]] = []
    chapter_rank = {chapter_id: index for index, chapter_id in enumerate(selected_chapter_ids)}
    seen_positions: set[tuple[str, int]] = set()
    for index, value_row in enumerate(rows):
        row_path = f"{path}[{index}]"
        row = require_mapping(value_row, path=row_path)
        require_exact_keys(
            row,
            required={
                "block_id",
                "chapter_id",
                "order_index",
                "block_type",
                "source_text",
                "admission",
            },
            path=row_path,
        )
        chapter_id = require_string(row["chapter_id"], path=f"{row_path}.chapter_id")
        if chapter_id not in chapter_rank:
            raise ContractValidationError(
                "chapter_reference",
                f"{row_path}.chapter_id",
                "block chapter is not selected",
            )
        order_index = require_int(
            row["order_index"], path=f"{row_path}.order_index", minimum=0
        )
        position = (chapter_id, order_index)
        if position in seen_positions:
            raise ContractValidationError(
                "duplicate_position", row_path, "chapter/order position is duplicated"
            )
        seen_positions.add(position)
        result.append(
            {
                "block_id": require_string(row["block_id"], path=f"{row_path}.block_id"),
                "chapter_id": chapter_id,
                "order_index": order_index,
                "block_type": require_string(
                    row["block_type"], path=f"{row_path}.block_type"
                ),
                "source_text": require_string(
                    row["source_text"], path=f"{row_path}.source_text"
                ),
                "admission": require_enum(
                    row["admission"],
                    {
                        "translate",
                        "translate_structured",
                        "preserve",
                        "exclude",
                        "review_required",
                    },
                    path=f"{row_path}.admission",
                ),
            }
        )
    require_unique([row["block_id"] for row in result], path=path)
    represented_chapters = {row["chapter_id"] for row in result}
    missing_chapters = [
        chapter_id
        for chapter_id in selected_chapter_ids
        if chapter_id not in represented_chapters
    ]
    if missing_chapters:
        raise ContractValidationError(
            "chapter_coverage",
            path,
            "selected chapters without source blocks: " + ", ".join(missing_chapters),
        )
    expected_order = sorted(
        result, key=lambda row: (chapter_rank[row["chapter_id"]], row["order_index"])
    )
    if result != expected_order:
        raise ContractValidationError(
            "block_order", path, "blocks must preserve selected chapter and source order"
        )
    return result


def _validate_translations(value: Any) -> list[dict[str, Any]]:
    path = "$.translations"
    rows = require_list(value, path=path)
    result: list[dict[str, Any]] = []
    for index, value_row in enumerate(rows):
        row_path = f"{path}[{index}]"
        row = require_mapping(value_row, path=row_path)
        require_exact_keys(
            row,
            required={
                "arm_id",
                "block_id",
                "status",
                "target_text",
                "error_code",
                "source_artifact_id",
            },
            path=row_path,
        )
        status = require_enum(
            row["status"],
            {"translated", "passthrough", "missing", "failed"},
            path=f"{row_path}.status",
        )
        target_text = require_nullable_string(
            row["target_text"], path=f"{row_path}.target_text", allow_empty=False
        )
        error_code = require_nullable_string(
            row["error_code"], path=f"{row_path}.error_code"
        )
        if status in {"translated", "passthrough"} and target_text is None:
            raise ContractValidationError(
                "translation_text", f"{row_path}.target_text", "successful rows need text"
            )
        if status in {"missing", "failed"} and target_text is not None:
            raise ContractValidationError(
                "translation_text", f"{row_path}.target_text", "missing/failed rows need null text"
            )
        if status == "failed" and error_code is None:
            raise ContractValidationError(
                "translation_error", f"{row_path}.error_code", "failed rows need an error code"
            )
        result.append(
            {
                "arm_id": require_string(row["arm_id"], path=f"{row_path}.arm_id"),
                "block_id": require_string(row["block_id"], path=f"{row_path}.block_id"),
                "status": status,
                "target_text": target_text,
                "error_code": error_code,
                "source_artifact_id": require_string(
                    row["source_artifact_id"], path=f"{row_path}.source_artifact_id"
                ),
            }
        )
    pairs = [(row["arm_id"], row["block_id"]) for row in result]
    if len(pairs) != len(set(pairs)):
        raise ContractValidationError(
            "duplicate", path, "arm/block translation rows must be unique"
        )
    return result


def _validate_runtime_terms(value: Any) -> list[dict[str, Any]]:
    path = "$.runtime_terms"
    rows = require_list(value, path=path)
    result: list[dict[str, Any]] = []
    for index, value_row in enumerate(rows):
        row_path = f"{path}[{index}]"
        row = require_mapping(value_row, path=row_path)
        require_exact_keys(
            row,
            required={
                "term_id",
                "source_term",
                "target_term",
                "accepted_variants",
                "constraint_strength",
                "status",
                "source_block_ids",
                "provenance_artifact_ids",
            },
            path=row_path,
        )
        variants = [
            require_string(item, path=f"{row_path}.accepted_variants[{item_index}]")
            for item_index, item in enumerate(
                require_list(
                    row["accepted_variants"], path=f"{row_path}.accepted_variants"
                )
            )
        ]
        require_unique(variants, path=f"{row_path}.accepted_variants")
        source_blocks = [
            require_string(item, path=f"{row_path}.source_block_ids[{item_index}]")
            for item_index, item in enumerate(
                require_list(row["source_block_ids"], path=f"{row_path}.source_block_ids")
            )
        ]
        if not source_blocks:
            raise ContractValidationError(
                "empty_array", f"{row_path}.source_block_ids", "term provenance is required"
            )
        require_unique(source_blocks, path=f"{row_path}.source_block_ids")
        provenance = [
            require_string(item, path=f"{row_path}.provenance_artifact_ids[{item_index}]")
            for item_index, item in enumerate(
                require_list(
                    row["provenance_artifact_ids"],
                    path=f"{row_path}.provenance_artifact_ids",
                )
            )
        ]
        require_unique(provenance, path=f"{row_path}.provenance_artifact_ids")
        result.append(
            {
                "term_id": require_string(row["term_id"], path=f"{row_path}.term_id"),
                "source_term": require_string(
                    row["source_term"], path=f"{row_path}.source_term"
                ),
                "target_term": require_string(
                    row["target_term"], path=f"{row_path}.target_term"
                ),
                "accepted_variants": variants,
                "constraint_strength": require_enum(
                    row["constraint_strength"],
                    {"mandatory", "preferred", "preserve"},
                    path=f"{row_path}.constraint_strength",
                ),
                "status": require_enum(
                    row["status"], {"active", "inactive"}, path=f"{row_path}.status"
                ),
                "source_block_ids": source_blocks,
                "provenance_artifact_ids": provenance,
            }
        )
    require_unique([row["term_id"] for row in result], path=path)
    return result


def _validate_injection_rows(value: Any) -> list[dict[str, Any]]:
    path = "$.injection_rows"
    rows = require_list(value, path=path)
    result: list[dict[str, Any]] = []
    for index, value_row in enumerate(rows):
        row_path = f"{path}[{index}]"
        row = require_mapping(value_row, path=row_path)
        require_exact_keys(
            row,
            required={
                "arm_id",
                "block_id",
                "term_id",
                "action",
                "reason_code",
                "source_artifact_ids",
            },
            path=row_path,
        )
        source_artifacts = [
            require_string(item, path=f"{row_path}.source_artifact_ids[{item_index}]")
            for item_index, item in enumerate(
                require_list(
                    row["source_artifact_ids"], path=f"{row_path}.source_artifact_ids"
                )
            )
        ]
        require_unique(source_artifacts, path=f"{row_path}.source_artifact_ids")
        result.append(
            {
                "arm_id": require_string(row["arm_id"], path=f"{row_path}.arm_id"),
                "block_id": require_string(row["block_id"], path=f"{row_path}.block_id"),
                "term_id": require_string(row["term_id"], path=f"{row_path}.term_id"),
                "action": require_enum(
                    row["action"],
                    {"injected", "eligible_not_injected", "suppressed"},
                    path=f"{row_path}.action",
                ),
                "reason_code": require_string(
                    row["reason_code"], path=f"{row_path}.reason_code"
                ),
                "source_artifact_ids": source_artifacts,
            }
        )
    keys = [(row["arm_id"], row["block_id"], row["term_id"]) for row in result]
    if len(keys) != len(set(keys)):
        raise ContractValidationError(
            "duplicate", path, "arm/block/term injection rows must be unique"
        )
    return result


def _validate_artifacts(value: Any) -> list[dict[str, Any]]:
    path = "$.artifacts"
    rows = require_list(value, path=path)
    if not rows:
        raise ContractValidationError("empty_array", path, "artifact manifest is required")
    result: list[dict[str, Any]] = []
    for index, value_row in enumerate(rows):
        row_path = f"{path}[{index}]"
        row = require_mapping(value_row, path=row_path)
        require_exact_keys(
            row,
            required={"artifact_id", "kind", "relative_path", "sha256", "size_bytes"},
            path=row_path,
        )
        result.append(
            {
                "artifact_id": require_string(
                    row["artifact_id"], path=f"{row_path}.artifact_id"
                ),
                "kind": require_enum(
                    row["kind"],
                    {
                        "source_manifest",
                        "translation",
                        "runtime_glossary",
                        "runtime_profile",
                        "injection_manifest",
                        "usage_ledger",
                    },
                    path=f"{row_path}.kind",
                ),
                "relative_path": require_relative_path(
                    row["relative_path"], path=f"{row_path}.relative_path"
                ),
                "sha256": require_sha256(row["sha256"], path=f"{row_path}.sha256"),
                "size_bytes": require_int(
                    row["size_bytes"], path=f"{row_path}.size_bytes", minimum=0
                ),
            }
        )
    require_unique([row["artifact_id"] for row in result], path=path)
    require_unique([row["relative_path"] for row in result], path=path)
    return result


def _validate_integrity(value: Any) -> dict[str, str]:
    path = "$.integrity"
    row = require_mapping(value, path=path)
    require_exact_keys(
        row, required={"artifact_set_sha256", "package_sha256"}, path=path
    )
    return {
        "artifact_set_sha256": require_sha256(
            row["artifact_set_sha256"], path=f"{path}.artifact_set_sha256"
        ),
        "package_sha256": require_sha256(
            row["package_sha256"], path=f"{path}.package_sha256"
        ),
    }


def _validate_references(payload: Mapping[str, Any]) -> None:
    arms = {row["arm_id"]: row for row in payload["arms"]}
    blocks = {row["block_id"]: row for row in payload["blocks"]}
    terms = {row["term_id"]: row for row in payload["runtime_terms"]}
    artifacts = {row["artifact_id"]: row for row in payload["artifacts"]}

    identity = payload["identity"]
    source_manifest_id = identity["source_manifest_artifact_id"]
    _require_artifact_kind(
        artifacts, source_manifest_id, "source_manifest", "$.identity.source_manifest_artifact_id"
    )
    profile = payload["runtime_profile"]
    if profile["profile_id"] != identity["profile_id"]:
        raise ContractValidationError(
            "profile_identity", "$.runtime_profile.profile_id", "profile IDs do not match"
        )
    _require_artifact_kind(
        artifacts,
        profile["source_artifact_id"],
        "runtime_profile",
        "$.runtime_profile.source_artifact_id",
    )

    for index, arm in enumerate(payload["arms"]):
        artifact = _require_artifact_kind(
            artifacts,
            arm["translation_artifact_id"],
            "translation",
            f"$.arms[{index}].translation_artifact_id",
        )
        if artifact["sha256"] != arm["translation_sha256"]:
            raise ContractValidationError(
                "translation_hash",
                f"$.arms[{index}].translation_sha256",
                "arm hash does not match translation artifact",
            )

    expected_pairs = {
        (arm_id, block_id)
        for arm_id in arms
        for block_id, block in blocks.items()
        if block["admission"] != "exclude"
    }
    actual_pairs = {(row["arm_id"], row["block_id"]) for row in payload["translations"]}
    if actual_pairs != expected_pairs:
        raise ContractValidationError(
            "translation_exact_cover",
            "$.translations",
            "translations must exact-cover every non-excluded block for every arm",
        )
    for index, row in enumerate(payload["translations"]):
        if row["arm_id"] not in arms or row["block_id"] not in blocks:
            raise ContractValidationError(
                "translation_reference", f"$.translations[{index}]", "unknown arm or block"
            )
        _require_artifact_kind(
            artifacts,
            row["source_artifact_id"],
            "translation",
            f"$.translations[{index}].source_artifact_id",
        )
        expected_artifact_id = arms[row["arm_id"]]["translation_artifact_id"]
        if row["source_artifact_id"] != expected_artifact_id:
            raise ContractValidationError(
                "translation_artifact_scope",
                f"$.translations[{index}].source_artifact_id",
                "translation row must reference its own arm artifact",
            )
        admission = blocks[row["block_id"]]["admission"]
        if admission == "preserve" and row["status"] != "passthrough":
            raise ContractValidationError(
                "admission_status",
                f"$.translations[{index}].status",
                "preserved blocks must be passthrough",
            )
        if (
            admission == "preserve"
            and row["target_text"] != blocks[row["block_id"]]["source_text"]
        ):
            raise ContractValidationError(
                "passthrough_text",
                f"$.translations[{index}].target_text",
                "passthrough text must equal source text",
            )
        if admission != "preserve" and row["status"] == "passthrough":
            raise ContractValidationError(
                "admission_status",
                f"$.translations[{index}].status",
                "only preserved blocks may be passthrough",
            )

    block_position = {
        row["block_id"]: index for index, row in enumerate(payload["blocks"])
    }
    for index, row in enumerate(payload["runtime_terms"]):
        if any(block_id not in blocks for block_id in row["source_block_ids"]):
            raise ContractValidationError(
                "term_block_reference",
                f"$.runtime_terms[{index}].source_block_ids",
                "term references an unknown block",
            )
        if row["source_block_ids"] != sorted(
            row["source_block_ids"], key=block_position.__getitem__
        ):
            raise ContractValidationError(
                "term_block_order",
                f"$.runtime_terms[{index}].source_block_ids",
                "term support blocks must preserve source order",
            )
        for artifact_id in row["provenance_artifact_ids"]:
            if artifact_id not in artifacts:
                raise ContractValidationError(
                    "artifact_reference",
                    f"$.runtime_terms[{index}].provenance_artifact_ids",
                    "term references an unknown artifact",
                )

    for index, row in enumerate(payload["injection_rows"]):
        if (
            row["arm_id"] not in arms
            or row["block_id"] not in blocks
            or row["term_id"] not in terms
        ):
            raise ContractValidationError(
                "injection_reference",
                f"$.injection_rows[{index}]",
                "injection row references an unknown arm, block, or term",
            )
        for artifact_id in row["source_artifact_ids"]:
            if artifact_id not in artifacts:
                raise ContractValidationError(
                    "artifact_reference",
                    f"$.injection_rows[{index}].source_artifact_ids",
                    "injection row references an unknown artifact",
                )
        if row["action"] == "injected" and terms[row["term_id"]]["status"] != "active":
            raise ContractValidationError(
                "inactive_term_injected",
                f"$.injection_rows[{index}].action",
                "inactive terms cannot be injected",
            )


def _require_artifact_kind(
    artifacts: Mapping[str, Mapping[str, Any]],
    artifact_id: str,
    kind: str,
    path: str,
) -> Mapping[str, Any]:
    artifact = artifacts.get(artifact_id)
    if artifact is None:
        raise ContractValidationError(
            "artifact_reference", path, f"unknown artifact: {artifact_id}"
        )
    if artifact["kind"] != kind:
        raise ContractValidationError(
            "artifact_kind", path, f"expected {kind} artifact"
        )
    return artifact
