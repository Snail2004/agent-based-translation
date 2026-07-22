from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from pipeline.eval.contracts_v1 import (
    CanonicalPolicy,
    ContractValidationError,
    assert_no_forbidden_runtime_data,
    canonicalize,
    require_commit,
    require_enum,
    require_exact_keys,
    require_int,
    require_list,
    require_mapping,
    require_nullable_string,
    require_rfc3339,
    require_sha256,
    require_string,
    require_unique,
    seal_payload,
    verify_payload_hash,
)
from pipeline.eval.d2l_input_v1 import validate_d2l_evaluation_input


__all__ = [
    "AdmissionPolicyIdentityV1",
    "CanonicalComponentIdentityV1",
    "CanonicalProjectionIdentityV1",
    "CanonicalSourcePackageBindingV1",
    "CommonArmV1",
    "CommonBlockV1",
    "CommonEvaluationInputV1",
    "CommonSourceSnapshotV1",
    "CommonTranslationV1",
    "LegacyD2LSourceBindingV1",
    "SourceBindingV1",
    "TRANSLATION_ARTIFACT_SCHEMA_ID",
    "TRANSLATION_ARTIFACT_SCHEMA_VERSION",
    "build_common_evaluation_input",
    "project_d2l_source_snapshot",
    "seal_translation_artifact",
    "source_binding_to_dict",
    "validate_source_binding",
    "validate_translation_artifact",
]


TRANSLATION_ARTIFACT_SCHEMA_ID = "TranslationArtifactV1"
TRANSLATION_ARTIFACT_SCHEMA_VERSION = "1.0.0"
TRANSLATION_ARTIFACT_SELF_HASH_PATH = ("integrity", "artifact_sha256")

TRANSLATION_ARTIFACT_CANONICAL_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(),
    semantic_sequence_paths=frozenset({("translations",)}),
)

_TRANSLATION_STATUSES = frozenset(
    {"translated", "preserved", "excluded", "review_held", "missing", "failed"}
)


@dataclass(frozen=True, slots=True)
class CommonBlockV1:
    block_id: str
    chapter_id: str
    order_index: int
    block_type: str
    source_text: str
    admission: str


@dataclass(frozen=True, slots=True)
class CanonicalComponentIdentityV1:
    schema_version: str
    sha256: str


@dataclass(frozen=True, slots=True)
class CanonicalProjectionIdentityV1:
    schema_version: str
    payload_sha256: str


@dataclass(frozen=True, slots=True)
class AdmissionPolicyIdentityV1:
    policy_id: str
    policy_version: str
    policy_sha256: str


@dataclass(frozen=True, slots=True)
class CanonicalSourcePackageBindingV1:
    project_id: str
    document_id: str
    document: CanonicalComponentIdentityV1
    structure: CanonicalComponentIdentityV1
    asset_manifest: CanonicalComponentIdentityV1
    admitted_projection: CanonicalProjectionIdentityV1
    admission_policy: AdmissionPolicyIdentityV1

    @property
    def binding_kind(self) -> str:
        return "canonical_source_package_v1"


@dataclass(frozen=True, slots=True)
class LegacyD2LSourceBindingV1:
    project_id: str
    document_id: str
    source_db_sha256: str
    runtime_manifest_sha256: str

    @property
    def binding_kind(self) -> str:
        return "legacy_d2l"


SourceBindingV1 = CanonicalSourcePackageBindingV1 | LegacyD2LSourceBindingV1


@dataclass(frozen=True, slots=True)
class CommonSourceSnapshotV1:
    source_schema_id: str
    source_schema_version: str
    source_binding: SourceBindingV1
    blocks: tuple[CommonBlockV1, ...]

    @property
    def project_id(self) -> str:
        return self.source_binding.project_id

    @property
    def document_id(self) -> str:
        return self.source_binding.document_id


@dataclass(frozen=True, slots=True)
class CommonArmV1:
    artifact_id: str
    artifact_sha256: str
    logical_run_id: str
    attempt_run_id: str
    arm_id: str
    profile_id: str
    profile_config_sha256: str
    source_language: str
    target_language: str


@dataclass(frozen=True, slots=True)
class CommonTranslationV1:
    arm_id: str
    block_id: str
    status: str
    target_text: str | None
    error_code: str | None


@dataclass(frozen=True, slots=True)
class CommonEvaluationInputV1:
    source_schema_id: str
    source_schema_version: str
    source_binding: SourceBindingV1
    blocks: tuple[CommonBlockV1, ...]
    arms: tuple[CommonArmV1, ...]
    translations: tuple[CommonTranslationV1, ...]

    @property
    def project_id(self) -> str:
        return self.source_binding.project_id

    @property
    def document_id(self) -> str:
        return self.source_binding.document_id


def source_binding_to_dict(binding: SourceBindingV1) -> dict[str, Any]:
    if isinstance(binding, CanonicalSourcePackageBindingV1):
        return {
            "binding_kind": binding.binding_kind,
            "project_id": binding.project_id,
            "document_id": binding.document_id,
            "document": {
                "schema_version": binding.document.schema_version,
                "sha256": binding.document.sha256,
            },
            "structure": {
                "schema_version": binding.structure.schema_version,
                "sha256": binding.structure.sha256,
            },
            "asset_manifest": {
                "schema_version": binding.asset_manifest.schema_version,
                "sha256": binding.asset_manifest.sha256,
            },
            "admitted_projection": {
                "schema_version": binding.admitted_projection.schema_version,
                "payload_sha256": binding.admitted_projection.payload_sha256,
            },
            "admission_policy": {
                "policy_id": binding.admission_policy.policy_id,
                "policy_version": binding.admission_policy.policy_version,
                "policy_sha256": binding.admission_policy.policy_sha256,
            },
        }
    if isinstance(binding, LegacyD2LSourceBindingV1):
        return {
            "binding_kind": binding.binding_kind,
            "project_id": binding.project_id,
            "document_id": binding.document_id,
            "source_db_sha256": binding.source_db_sha256,
            "runtime_manifest_sha256": binding.runtime_manifest_sha256,
        }
    raise TypeError(f"unsupported source binding type: {type(binding).__name__}")


def seal_translation_artifact(payload: Mapping[str, Any]) -> dict[str, Any]:
    return seal_payload(
        payload,
        policy=TRANSLATION_ARTIFACT_CANONICAL_POLICY,
        hash_path=TRANSLATION_ARTIFACT_SELF_HASH_PATH,
    )


def validate_translation_artifact(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a bound, gold-free translation overlay without mutating input."""

    return _validate_translation_artifact(payload, allow_legacy_d2l=False)


def _validate_translation_artifact(
    payload: Mapping[str, Any], *, allow_legacy_d2l: bool
) -> dict[str, Any]:
    assert_no_forbidden_runtime_data(payload)
    root = require_mapping(payload, path="$")
    require_exact_keys(
        root,
        required={
            "schema_id",
            "schema_version",
            "artifact_id",
            "created_at",
            "producer",
            "source_binding",
            "run_identity",
            "translations",
            "coverage",
            "integrity",
        },
        path="$",
    )
    producer = _validate_translation_producer(root["producer"])
    source_binding = _validate_source_binding(
        root["source_binding"], allow_legacy_d2l=allow_legacy_d2l
    )
    if source_binding["binding_kind"] == "legacy_d2l" and producer["workstream"] != "d2l":
        raise ContractValidationError(
            "source_binding_authority",
            "$.source_binding.binding_kind",
            "legacy_d2l compatibility artifacts may only identify the D2L producer",
        )
    normalized: dict[str, Any] = {
        "schema_id": require_enum(
            root["schema_id"], {TRANSLATION_ARTIFACT_SCHEMA_ID}, path="$.schema_id"
        ),
        "schema_version": require_enum(
            root["schema_version"],
            {TRANSLATION_ARTIFACT_SCHEMA_VERSION},
            path="$.schema_version",
        ),
        "artifact_id": require_string(root["artifact_id"], path="$.artifact_id"),
        "created_at": require_rfc3339(root["created_at"], path="$.created_at"),
        "producer": producer,
        "source_binding": source_binding,
        "run_identity": _validate_run_identity(root["run_identity"]),
        "translations": _validate_translation_rows(root["translations"]),
        "coverage": _validate_coverage(root["coverage"]),
        "integrity": _validate_translation_integrity(root["integrity"]),
    }
    _validate_declared_coverage(normalized)
    if not verify_payload_hash(
        normalized,
        policy=TRANSLATION_ARTIFACT_CANONICAL_POLICY,
        hash_path=TRANSLATION_ARTIFACT_SELF_HASH_PATH,
    ):
        raise ContractValidationError(
            "artifact_hash",
            "$.integrity.artifact_sha256",
            "translation artifact self-hash does not match canonical content",
        )
    canonical = canonicalize(normalized, policy=TRANSLATION_ARTIFACT_CANONICAL_POLICY)
    if not isinstance(canonical, dict):
        raise AssertionError("canonical translation artifact must remain an object")
    return canonical


def project_d2l_source_snapshot(payload: Mapping[str, Any]) -> CommonSourceSnapshotV1:
    """Project the accepted D2L package into a source-only compatibility view.

    This path is explicitly legacy and offline-only. It preserves the source DB
    and runtime manifest identities under their honest names; public
    TranslationArtifactV1 validation rejects this binding kind.
    """

    validated = validate_d2l_evaluation_input(payload)
    identity = validated["identity"]
    return CommonSourceSnapshotV1(
        source_schema_id=validated["schema_id"],
        source_schema_version=validated["schema_version"],
        source_binding=LegacyD2LSourceBindingV1(
            project_id=identity["project_id"],
            document_id=identity["document_id"],
            source_db_sha256=identity["source_db_sha256"],
            runtime_manifest_sha256=identity["runtime_manifest_sha256"],
        ),
        blocks=tuple(
            CommonBlockV1(
                block_id=row["block_id"],
                chapter_id=row["chapter_id"],
                order_index=row["order_index"],
                block_type=row["block_type"],
                source_text=row["source_text"],
                admission=row["admission"],
            )
            for row in validated["blocks"]
        ),
    )


def build_common_evaluation_input(
    source: CommonSourceSnapshotV1,
    translation_artifacts: Sequence[Mapping[str, Any]],
) -> CommonEvaluationInputV1:
    _validate_common_source_snapshot(source)
    if not translation_artifacts:
        raise ContractValidationError(
            "empty_array", "$.translation_artifacts", "at least one arm is required"
        )

    allow_legacy_d2l = isinstance(source.source_binding, LegacyD2LSourceBindingV1)
    validated_artifacts = [
        _validate_translation_artifact(
            artifact,
            allow_legacy_d2l=allow_legacy_d2l,
        )
        for artifact in translation_artifacts
    ]
    arm_ids = [artifact["run_identity"]["arm_id"] for artifact in validated_artifacts]
    artifact_ids = [artifact["artifact_id"] for artifact in validated_artifacts]
    require_unique(arm_ids, path="$.translation_artifacts.arm_id")
    require_unique(artifact_ids, path="$.translation_artifacts.artifact_id")

    source_block_ids = [block.block_id for block in source.blocks]
    source_block_rank = {block_id: index for index, block_id in enumerate(source_block_ids)}
    source_block_set = set(source_block_ids)
    block_by_id = {block.block_id: block for block in source.blocks}
    arms: list[CommonArmV1] = []
    translations: list[CommonTranslationV1] = []

    for index, artifact in enumerate(validated_artifacts):
        artifact_path = f"$.translation_artifacts[{index}]"
        binding = artifact["source_binding"]
        expected_binding = source_binding_to_dict(source.source_binding)
        if binding != expected_binding:
            raise ContractValidationError(
                "source_binding",
                f"{artifact_path}.source_binding",
                "translation artifact does not bind the selected source and admission policy",
            )

        rows = artifact["translations"]
        artifact_block_ids = [row["block_id"] for row in rows]
        if artifact_block_ids != source_block_ids:
            foreign = sorted(set(artifact_block_ids) - source_block_set)
            missing = sorted(source_block_set - set(artifact_block_ids))
            detail = []
            if foreign:
                detail.append("foreign=" + ",".join(foreign))
            if missing:
                detail.append("missing=" + ",".join(missing))
            if not detail:
                detail.append("source order differs")
            raise ContractValidationError(
                "block_coverage",
                f"{artifact_path}.translations",
                "; ".join(detail),
            )

        for row_index, row in enumerate(rows):
            block = block_by_id[row["block_id"]]
            _validate_admission_status(
                block,
                row,
                path=f"{artifact_path}.translations[{row_index}]",
            )

        run_identity = artifact["run_identity"]
        arms.append(
            CommonArmV1(
                artifact_id=artifact["artifact_id"],
                artifact_sha256=artifact["integrity"]["artifact_sha256"],
                logical_run_id=run_identity["logical_run_id"],
                attempt_run_id=run_identity["attempt_run_id"],
                arm_id=run_identity["arm_id"],
                profile_id=run_identity["profile_id"],
                profile_config_sha256=run_identity["profile_config_sha256"],
                source_language=run_identity["source_language"],
                target_language=run_identity["target_language"],
            )
        )
        translations.extend(
            CommonTranslationV1(
                arm_id=run_identity["arm_id"],
                block_id=row["block_id"],
                status=row["status"],
                target_text=row["target_text"],
                error_code=row["error_code"],
            )
            for row in rows
        )

    language_pairs = {(arm.source_language, arm.target_language) for arm in arms}
    if len(language_pairs) != 1:
        raise ContractValidationError(
            "language_pair",
            "$.translation_artifacts",
            "all compared arms must use the same source and target languages",
        )

    return CommonEvaluationInputV1(
        source_schema_id=source.source_schema_id,
        source_schema_version=source.source_schema_version,
        source_binding=source.source_binding,
        blocks=source.blocks,
        arms=tuple(sorted(arms, key=lambda row: row.arm_id)),
        translations=tuple(
            sorted(translations, key=lambda row: (row.arm_id, source_block_rank[row.block_id]))
        ),
    )


def _validate_common_source_snapshot(source: CommonSourceSnapshotV1) -> None:
    require_string(source.source_schema_id, path="$.source.source_schema_id")
    require_string(source.source_schema_version, path="$.source.source_schema_version")
    _validate_source_binding_for_offline_planning(
        source_binding_to_dict(source.source_binding)
    )
    if (
        isinstance(source.source_binding, LegacyD2LSourceBindingV1)
        and source.source_schema_id != "D2LEvaluationInputV1"
    ):
        raise ContractValidationError(
            "legacy_source_schema",
            "$.source.source_schema_id",
            "legacy_d2l binding is restricted to D2LEvaluationInputV1 compatibility",
        )
    if not source.blocks:
        raise ContractValidationError(
            "empty_array", "$.source.blocks", "source blocks are required"
        )

    block_ids: list[str] = []
    seen_chapters: set[str] = set()
    active_chapter: str | None = None
    last_order_by_chapter: dict[str, int] = {}
    for index, block in enumerate(source.blocks):
        path = f"$.source.blocks[{index}]"
        block_id = require_string(block.block_id, path=f"{path}.block_id")
        chapter_id = require_string(block.chapter_id, path=f"{path}.chapter_id")
        order_index = require_int(block.order_index, path=f"{path}.order_index", minimum=0)
        require_string(block.block_type, path=f"{path}.block_type")
        require_string(block.source_text, path=f"{path}.source_text")
        require_enum(
            block.admission,
            {"translate", "translate_structured", "preserve", "exclude", "review_required"},
            path=f"{path}.admission",
        )
        block_ids.append(block_id)

        if chapter_id != active_chapter:
            if chapter_id in seen_chapters:
                raise ContractValidationError(
                    "block_order", path, "a chapter may not reappear after another chapter"
                )
            seen_chapters.add(chapter_id)
            active_chapter = chapter_id
        previous_order = last_order_by_chapter.get(chapter_id)
        if previous_order is not None and order_index <= previous_order:
            raise ContractValidationError(
                "block_order", f"{path}.order_index", "chapter block order must increase"
            )
        last_order_by_chapter[chapter_id] = order_index
    require_unique(block_ids, path="$.source.blocks.block_id")


def _validate_translation_producer(value: Any) -> dict[str, str]:
    path = "$.producer"
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={"workstream", "component", "component_version", "code_commit"},
        path=path,
    )
    return {
        "workstream": require_enum(
            row["workstream"],
            {"d2l", "literary"},
            path=f"{path}.workstream",
        ),
        "component": require_string(row["component"], path=f"{path}.component"),
        "component_version": require_string(
            row["component_version"], path=f"{path}.component_version"
        ),
        "code_commit": require_commit(row["code_commit"], path=f"{path}.code_commit"),
    }


def validate_source_binding(value: Any) -> dict[str, Any]:
    """Validate the public canonical DEC-017 source binding."""

    return _validate_source_binding(value, allow_legacy_d2l=False)


def _validate_source_binding_for_offline_planning(value: Any) -> dict[str, Any]:
    """Validate canonical or explicit legacy input inside Evaluation planning."""

    return _validate_source_binding(value, allow_legacy_d2l=True)


def _validate_source_binding(
    value: Any, *, allow_legacy_d2l: bool
) -> dict[str, Any]:
    path = "$.source_binding"
    row = require_mapping(value, path=path)
    binding_kind = require_enum(
        row.get("binding_kind"),
        (
            {"canonical_source_package_v1", "legacy_d2l"}
            if allow_legacy_d2l
            else {"canonical_source_package_v1"}
        ),
        path=f"{path}.binding_kind",
    )
    if binding_kind == "legacy_d2l":
        require_exact_keys(
            row,
            required={
                "binding_kind",
                "project_id",
                "document_id",
                "source_db_sha256",
                "runtime_manifest_sha256",
            },
            path=path,
        )
        return {
            "binding_kind": binding_kind,
            "project_id": require_string(row["project_id"], path=f"{path}.project_id"),
            "document_id": require_string(row["document_id"], path=f"{path}.document_id"),
            "source_db_sha256": require_sha256(
                row["source_db_sha256"], path=f"{path}.source_db_sha256"
            ),
            "runtime_manifest_sha256": require_sha256(
                row["runtime_manifest_sha256"],
                path=f"{path}.runtime_manifest_sha256",
            ),
        }

    require_exact_keys(
        row,
        required={
            "binding_kind",
            "project_id",
            "document_id",
            "document",
            "structure",
            "asset_manifest",
            "admitted_projection",
            "admission_policy",
        },
        path=path,
    )
    return {
        "binding_kind": binding_kind,
        "project_id": require_string(row["project_id"], path=f"{path}.project_id"),
        "document_id": require_string(row["document_id"], path=f"{path}.document_id"),
        "document": _validate_component_identity(
            row["document"], path=f"{path}.document"
        ),
        "structure": _validate_component_identity(
            row["structure"], path=f"{path}.structure"
        ),
        "asset_manifest": _validate_component_identity(
            row["asset_manifest"], path=f"{path}.asset_manifest"
        ),
        "admitted_projection": _validate_projection_identity(
            row["admitted_projection"], path=f"{path}.admitted_projection"
        ),
        "admission_policy": _validate_policy_identity(
            row["admission_policy"], path=f"{path}.admission_policy"
        ),
    }


def _validate_component_identity(value: Any, *, path: str) -> dict[str, str]:
    row = require_mapping(value, path=path)
    require_exact_keys(row, required={"schema_version", "sha256"}, path=path)
    return {
        "schema_version": require_string(
            row["schema_version"], path=f"{path}.schema_version"
        ),
        "sha256": require_sha256(row["sha256"], path=f"{path}.sha256"),
    }


def _validate_projection_identity(value: Any, *, path: str) -> dict[str, str]:
    row = require_mapping(value, path=path)
    require_exact_keys(
        row, required={"schema_version", "payload_sha256"}, path=path
    )
    return {
        "schema_version": require_string(
            row["schema_version"], path=f"{path}.schema_version"
        ),
        "payload_sha256": require_sha256(
            row["payload_sha256"], path=f"{path}.payload_sha256"
        ),
    }


def _validate_policy_identity(value: Any, *, path: str) -> dict[str, str]:
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={"policy_id", "policy_version", "policy_sha256"},
        path=path,
    )
    return {
        "policy_id": require_string(row["policy_id"], path=f"{path}.policy_id"),
        "policy_version": require_string(
            row["policy_version"], path=f"{path}.policy_version"
        ),
        "policy_sha256": require_sha256(
            row["policy_sha256"], path=f"{path}.policy_sha256"
        ),
    }


def _validate_run_identity(value: Any) -> dict[str, str]:
    path = "$.run_identity"
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "logical_run_id",
            "attempt_run_id",
            "arm_id",
            "profile_id",
            "profile_config_sha256",
            "source_language",
            "target_language",
        },
        path=path,
    )
    return {
        "logical_run_id": require_string(
            row["logical_run_id"], path=f"{path}.logical_run_id"
        ),
        "attempt_run_id": require_string(
            row["attempt_run_id"], path=f"{path}.attempt_run_id"
        ),
        "arm_id": require_string(row["arm_id"], path=f"{path}.arm_id"),
        "profile_id": require_string(row["profile_id"], path=f"{path}.profile_id"),
        "profile_config_sha256": require_sha256(
            row["profile_config_sha256"], path=f"{path}.profile_config_sha256"
        ),
        "source_language": require_string(
            row["source_language"], path=f"{path}.source_language"
        ),
        "target_language": require_string(
            row["target_language"], path=f"{path}.target_language"
        ),
    }


def _validate_translation_rows(value: Any) -> list[dict[str, Any]]:
    path = "$.translations"
    rows = require_list(value, path=path)
    if not rows:
        raise ContractValidationError("empty_array", path, "translation rows are required")
    result: list[dict[str, Any]] = []
    for index, value_row in enumerate(rows):
        row_path = f"{path}[{index}]"
        row = require_mapping(value_row, path=row_path)
        require_exact_keys(
            row,
            required={"block_id", "status", "target_text", "error_code"},
            path=row_path,
        )
        status = require_enum(row["status"], _TRANSLATION_STATUSES, path=f"{row_path}.status")
        target_text = require_nullable_string(
            row["target_text"], path=f"{row_path}.target_text", allow_empty=False
        )
        error_code = require_nullable_string(row["error_code"], path=f"{row_path}.error_code")
        if status in {"translated", "preserved"} and target_text is None:
            raise ContractValidationError(
                "translation_text", f"{row_path}.target_text", "successful rows need text"
            )
        if status not in {"translated", "preserved"} and target_text is not None:
            raise ContractValidationError(
                "translation_text",
                f"{row_path}.target_text",
                "non-output rows require null target text",
            )
        if status == "failed" and error_code is None:
            raise ContractValidationError(
                "translation_error", f"{row_path}.error_code", "failed rows need an error code"
            )
        if status != "failed" and error_code is not None:
            raise ContractValidationError(
                "translation_error",
                f"{row_path}.error_code",
                "only failed rows may carry an error code",
            )
        result.append(
            {
                "block_id": require_string(row["block_id"], path=f"{row_path}.block_id"),
                "status": status,
                "target_text": target_text,
                "error_code": error_code,
            }
        )
    require_unique([row["block_id"] for row in result], path=f"{path}.block_id")
    return result


def _validate_coverage(value: Any) -> dict[str, int]:
    path = "$.coverage"
    row = require_mapping(value, path=path)
    fields = {
        "source_block_count",
        "eligible_count",
        "translated_count",
        "preserved_count",
        "excluded_count",
        "review_held_count",
        "missing_count",
        "failed_count",
    }
    require_exact_keys(row, required=fields, path=path)
    return {
        field: require_int(row[field], path=f"{path}.{field}", minimum=0)
        for field in sorted(fields)
    }


def _validate_translation_integrity(value: Any) -> dict[str, str]:
    path = "$.integrity"
    row = require_mapping(value, path=path)
    require_exact_keys(row, required={"artifact_sha256"}, path=path)
    return {
        "artifact_sha256": require_sha256(
            row["artifact_sha256"], path=f"{path}.artifact_sha256"
        )
    }


def _validate_declared_coverage(payload: Mapping[str, Any]) -> None:
    counts = Counter(row["status"] for row in payload["translations"])
    expected = {
        "source_block_count": len(payload["translations"]),
        "eligible_count": counts["translated"] + counts["missing"] + counts["failed"],
        "translated_count": counts["translated"],
        "preserved_count": counts["preserved"],
        "excluded_count": counts["excluded"],
        "review_held_count": counts["review_held"],
        "missing_count": counts["missing"],
        "failed_count": counts["failed"],
    }
    if payload["coverage"] != {key: expected[key] for key in sorted(expected)}:
        raise ContractValidationError(
            "coverage_mismatch", "$.coverage", "coverage does not reconcile with rows"
        )


def _validate_admission_status(
    block: CommonBlockV1, row: Mapping[str, Any], *, path: str
) -> None:
    allowed_by_admission = {
        "translate": {"translated", "missing", "failed"},
        "translate_structured": {"translated", "missing", "failed"},
        "preserve": {"preserved"},
        "exclude": {"excluded"},
        "review_required": {"review_held"},
    }
    if row["status"] not in allowed_by_admission[block.admission]:
        raise ContractValidationError(
            "admission_status",
            f"{path}.status",
            f"status {row['status']!r} is invalid for admission {block.admission!r}",
        )
    if row["status"] == "preserved" and row["target_text"] != block.source_text:
        raise ContractValidationError(
            "preserved_text",
            f"{path}.target_text",
            "preserved target text must equal source text",
        )
