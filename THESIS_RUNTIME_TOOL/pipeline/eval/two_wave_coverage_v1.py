from __future__ import annotations

from collections import Counter
import hashlib
import json
from typing import Any, Mapping

from pipeline.eval.common_input_v1 import (
    CommonEvaluationInputV1,
    source_binding_to_dict,
)
from pipeline.eval.contracts_v1 import (
    CanonicalPolicy,
    ContractValidationError,
    canonicalize,
    require_commit,
    require_enum,
    require_exact_keys,
    require_int,
    require_list,
    require_mapping,
    require_rfc3339,
    require_sha256,
    require_string,
    require_unique,
    seal_payload,
    verify_payload_hash,
)
from pipeline.eval.two_wave_sampling_v1 import (
    BENCHMARK_ARM_IDS_V1,
    TWO_WAVE_CHAPTER_IDS_V1,
    validate_two_wave_sampling_manifest_v1,
)


__all__ = [
    "build_two_wave_sample_coverage_v1",
    "validate_two_wave_sample_coverage_v1",
]


COVERAGE_SCHEMA_ID = "EvaluationTwoWaveSampleCoverageV1"
COVERAGE_SCHEMA_VERSION = "1.0.0"
_COVERAGE_HASH_PATH = ("integrity", "coverage_sha256")
_COVERAGE_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(),
    semantic_sequence_paths=frozenset(
        {
            ("selected_chapter_ids",),
            ("selected_arm_ids",),
            ("sample_block_ids",),
            ("input_artifacts",),
            ("arm_coverage",),
            ("arm_coverage", "*", "unavailable_rows"),
        }
    ),
)


def build_two_wave_sample_coverage_v1(
    chapter_inputs: Mapping[str, CommonEvaluationInputV1],
    sampling_manifest: Mapping[str, Any],
    *,
    active_wave: str,
    created_at: str,
    producer_code_commit: str,
) -> dict[str, Any]:
    manifest = validate_two_wave_sampling_manifest_v1(sampling_manifest)
    wave_id = require_enum(active_wave, {"wave_a", "wave_b"}, path="$.active_wave")
    timestamp = require_rfc3339(created_at, path="$.created_at")
    commit = require_commit(producer_code_commit, path="$.producer_code_commit")
    inputs = _validate_inputs_against_manifest(chapter_inputs, manifest)
    sample_block_ids = tuple(manifest["waves"][wave_id]["block_ids"])
    sample_set = set(sample_block_ids)

    input_artifacts: list[dict[str, Any]] = []
    rows_by_arm_block: dict[tuple[str, str], Any] = {}
    for chapter_id in TWO_WAVE_CHAPTER_IDS_V1:
        common_input = inputs[chapter_id]
        arms_by_id = {arm.arm_id: arm for arm in common_input.arms}
        if tuple(arm.arm_id for arm in common_input.arms) != BENCHMARK_ARM_IDS_V1:
            raise ContractValidationError(
                "arm_order",
                f"$.chapter_inputs.{chapter_id}.arms",
                "chapter input must carry the exact ordered five benchmark arms",
            )
        for arm_id in BENCHMARK_ARM_IDS_V1:
            arm = arms_by_id[arm_id]
            input_artifacts.append(
                {
                    "chapter_id": chapter_id,
                    "arm_id": arm_id,
                    "artifact_id": require_string(
                        arm.artifact_id,
                        path=f"$.chapter_inputs.{chapter_id}.arms.{arm_id}.artifact_id",
                        maximum=240,
                    ),
                    "artifact_sha256": require_sha256(
                        arm.artifact_sha256,
                        path=(
                            f"$.chapter_inputs.{chapter_id}.arms."
                            f"{arm_id}.artifact_sha256"
                        ),
                    ),
                    "logical_run_id": require_string(
                        arm.logical_run_id,
                        path=f"$.chapter_inputs.{chapter_id}.arms.{arm_id}.logical_run_id",
                        maximum=240,
                    ),
                    "attempt_run_id": require_string(
                        arm.attempt_run_id,
                        path=f"$.chapter_inputs.{chapter_id}.arms.{arm_id}.attempt_run_id",
                        maximum=240,
                    ),
                    "profile_id": require_string(
                        arm.profile_id,
                        path=f"$.chapter_inputs.{chapter_id}.arms.{arm_id}.profile_id",
                        maximum=240,
                    ),
                    "profile_config_sha256": require_sha256(
                        arm.profile_config_sha256,
                        path=(
                            f"$.chapter_inputs.{chapter_id}.arms."
                            f"{arm_id}.profile_config_sha256"
                        ),
                    ),
                }
            )
        seen_translation_keys: set[tuple[str, str]] = set()
        for row in common_input.translations:
            key = (row.arm_id, row.block_id)
            if key in seen_translation_keys:
                raise ContractValidationError(
                    "duplicate_translation",
                    f"$.chapter_inputs.{chapter_id}.translations",
                    f"duplicate translation row {row.arm_id!r}/{row.block_id!r}",
                )
            seen_translation_keys.add(key)
            if row.block_id in sample_set:
                rows_by_arm_block[key] = row

    arm_coverage: list[dict[str, Any]] = []
    for arm_id in BENCHMARK_ARM_IDS_V1:
        unavailable: list[dict[str, Any]] = []
        status_counts: Counter[str] = Counter()
        for block_id in sample_block_ids:
            row = rows_by_arm_block.get((arm_id, block_id))
            status = "absent" if row is None else row.status
            status_counts[status] += 1
            if status != "translated":
                unavailable.append(
                    {
                        "block_id": block_id,
                        "status": status,
                        "error_code": None if row is None else row.error_code,
                    }
                )
        arm_coverage.append(
            {
                "arm_id": arm_id,
                "expected_block_count": len(sample_block_ids),
                "translated_block_count": len(sample_block_ids) - len(unavailable),
                "unavailable_block_count": len(unavailable),
                "status_counts": dict(sorted(status_counts.items())),
                "unavailable_rows": unavailable,
                "status": "ready" if not unavailable else "blocked",
            }
        )

    ready = all(row["status"] == "ready" for row in arm_coverage)
    draft = {
        "schema_id": COVERAGE_SCHEMA_ID,
        "schema_version": COVERAGE_SCHEMA_VERSION,
        "created_at": timestamp,
        "producer": {
            "workstream": "evaluation",
            "component": "two_wave_sample_coverage_v1",
            "component_version": COVERAGE_SCHEMA_VERSION,
            "code_commit": commit,
        },
        "sampling_manifest_sha256": manifest["integrity"]["manifest_sha256"],
        "active_wave": wave_id,
        "selected_chapter_ids": list(TWO_WAVE_CHAPTER_IDS_V1),
        "selected_arm_ids": list(BENCHMARK_ARM_IDS_V1),
        "sample_block_ids": list(sample_block_ids),
        "input_artifacts": input_artifacts,
        "arm_coverage": arm_coverage,
        "no_replacement_policy": "hold_comparison_no_replacement",
        "coverage_status": "ready" if ready else "blocked",
        "integrity": {"coverage_sha256": "0" * 64},
    }
    sealed = seal_payload(
        draft,
        policy=_COVERAGE_POLICY,
        hash_path=_COVERAGE_HASH_PATH,
    )
    return validate_two_wave_sample_coverage_v1(sealed)


def validate_two_wave_sample_coverage_v1(
    value: Mapping[str, Any],
    *,
    sampling_manifest: Mapping[str, Any] | None = None,
    chapter_inputs: Mapping[str, CommonEvaluationInputV1] | None = None,
) -> dict[str, Any]:
    row = require_mapping(value, path="$coverage")
    require_exact_keys(
        row,
        required={
            "schema_id",
            "schema_version",
            "created_at",
            "producer",
            "sampling_manifest_sha256",
            "active_wave",
            "selected_chapter_ids",
            "selected_arm_ids",
            "sample_block_ids",
            "input_artifacts",
            "arm_coverage",
            "no_replacement_policy",
            "coverage_status",
            "integrity",
        },
        path="$coverage",
    )
    normalized = {
        "schema_id": require_enum(
            row["schema_id"], {COVERAGE_SCHEMA_ID}, path="$coverage.schema_id"
        ),
        "schema_version": require_enum(
            row["schema_version"],
            {COVERAGE_SCHEMA_VERSION},
            path="$coverage.schema_version",
        ),
        "created_at": require_rfc3339(row["created_at"], path="$coverage.created_at"),
        "producer": _validate_producer(row["producer"]),
        "sampling_manifest_sha256": require_sha256(
            row["sampling_manifest_sha256"],
            path="$coverage.sampling_manifest_sha256",
        ),
        "active_wave": require_enum(
            row["active_wave"], {"wave_a", "wave_b"}, path="$coverage.active_wave"
        ),
        "selected_chapter_ids": _exact_string_list(
            row["selected_chapter_ids"],
            expected=TWO_WAVE_CHAPTER_IDS_V1,
            path="$coverage.selected_chapter_ids",
        ),
        "selected_arm_ids": _exact_string_list(
            row["selected_arm_ids"],
            expected=BENCHMARK_ARM_IDS_V1,
            path="$coverage.selected_arm_ids",
        ),
        "sample_block_ids": _string_list(
            row["sample_block_ids"], path="$coverage.sample_block_ids"
        ),
        "input_artifacts": _validate_input_artifacts(row["input_artifacts"]),
        "arm_coverage": _validate_arm_coverage(row["arm_coverage"]),
        "no_replacement_policy": require_enum(
            row["no_replacement_policy"],
            {"hold_comparison_no_replacement"},
            path="$coverage.no_replacement_policy",
        ),
        "coverage_status": require_enum(
            row["coverage_status"],
            {"ready", "blocked"},
            path="$coverage.coverage_status",
        ),
        "integrity": _one_hash(row["integrity"]),
    }
    _validate_coverage_references(normalized)
    if not verify_payload_hash(
        normalized,
        policy=_COVERAGE_POLICY,
        hash_path=_COVERAGE_HASH_PATH,
    ):
        raise ContractValidationError(
            "coverage_hash",
            "$coverage.integrity.coverage_sha256",
            "coverage self-hash does not match canonical content",
        )
    canonical = canonicalize(normalized, policy=_COVERAGE_POLICY)
    if not isinstance(canonical, dict):
        raise AssertionError("coverage must remain an object")
    if sampling_manifest is not None:
        manifest = validate_two_wave_sampling_manifest_v1(sampling_manifest)
        if canonical["sampling_manifest_sha256"] != manifest["integrity"][
            "manifest_sha256"
        ]:
            raise ContractValidationError(
                "sampling_binding",
                "$coverage.sampling_manifest_sha256",
                "coverage is bound to a different sampling manifest",
            )
        expected_blocks = manifest["waves"][canonical["active_wave"]]["block_ids"]
        if canonical["sample_block_ids"] != expected_blocks:
            raise ContractValidationError(
                "sample_exact_cover",
                "$coverage.sample_block_ids",
                "coverage block IDs differ from the frozen sampling wave",
            )
        if chapter_inputs is not None:
            rebuilt = build_two_wave_sample_coverage_v1(
                chapter_inputs,
                manifest,
                active_wave=canonical["active_wave"],
                created_at=canonical["created_at"],
                producer_code_commit=canonical["producer"]["code_commit"],
            )
            if rebuilt != canonical:
                raise ContractValidationError(
                    "coverage_rederivation",
                    "$coverage",
                    "coverage differs from the authoritative inputs",
                )
    elif chapter_inputs is not None:
        raise ContractValidationError(
            "sampling_binding",
            "$coverage",
            "chapter inputs require the bound sampling manifest",
        )
    return canonical


def _validate_inputs_against_manifest(
    value: Mapping[str, CommonEvaluationInputV1],
    manifest: Mapping[str, Any],
) -> dict[str, CommonEvaluationInputV1]:
    if not isinstance(value, Mapping) or tuple(value.keys()) != TWO_WAVE_CHAPTER_IDS_V1:
        raise ContractValidationError(
            "chapter_order",
            "$.chapter_inputs",
            "chapter inputs must use the exact locked five-chapter order",
        )
    result: dict[str, CommonEvaluationInputV1] = {}
    for chapter_id in TWO_WAVE_CHAPTER_IDS_V1:
        common_input = value[chapter_id]
        if not isinstance(common_input, CommonEvaluationInputV1):
            raise ContractValidationError(
                "type",
                f"$.chapter_inputs.{chapter_id}",
                "expected CommonEvaluationInputV1",
            )
        source_hash = _stable_sha256(source_binding_to_dict(common_input.source_binding))
        if source_hash != manifest["source_bindings"][chapter_id]:
            raise ContractValidationError(
                "source_binding",
                f"$.chapter_inputs.{chapter_id}.source_binding",
                "chapter source binding differs from the sampling manifest",
            )
        result[chapter_id] = common_input
    return result


def _validate_producer(value: Any) -> dict[str, str]:
    path = "$coverage.producer"
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={"workstream", "component", "component_version", "code_commit"},
        path=path,
    )
    return {
        "workstream": require_enum(
            row["workstream"], {"evaluation"}, path=f"{path}.workstream"
        ),
        "component": require_enum(
            row["component"],
            {"two_wave_sample_coverage_v1"},
            path=f"{path}.component",
        ),
        "component_version": require_enum(
            row["component_version"],
            {COVERAGE_SCHEMA_VERSION},
            path=f"{path}.component_version",
        ),
        "code_commit": require_commit(row["code_commit"], path=f"{path}.code_commit"),
    }


def _validate_input_artifacts(value: Any) -> list[dict[str, Any]]:
    rows = require_list(value, path="$coverage.input_artifacts")
    expected_keys = [
        (chapter_id, arm_id)
        for chapter_id in TWO_WAVE_CHAPTER_IDS_V1
        for arm_id in BENCHMARK_ARM_IDS_V1
    ]
    if len(rows) != len(expected_keys):
        raise ContractValidationError(
            "artifact_exact_cover",
            "$coverage.input_artifacts",
            "input artifacts must exact-cover every chapter/arm pair",
        )
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(rows):
        path = f"$coverage.input_artifacts[{index}]"
        row = require_mapping(item, path=path)
        require_exact_keys(
            row,
            required={
                "chapter_id",
                "arm_id",
                "artifact_id",
                "artifact_sha256",
                "logical_run_id",
                "attempt_run_id",
                "profile_id",
                "profile_config_sha256",
            },
            path=path,
        )
        expected_chapter, expected_arm = expected_keys[index]
        normalized.append(
            {
                "chapter_id": require_enum(
                    row["chapter_id"], {expected_chapter}, path=f"{path}.chapter_id"
                ),
                "arm_id": require_enum(
                    row["arm_id"], {expected_arm}, path=f"{path}.arm_id"
                ),
                "artifact_id": require_string(
                    row["artifact_id"], path=f"{path}.artifact_id", maximum=240
                ),
                "artifact_sha256": require_sha256(
                    row["artifact_sha256"], path=f"{path}.artifact_sha256"
                ),
                "logical_run_id": require_string(
                    row["logical_run_id"], path=f"{path}.logical_run_id", maximum=240
                ),
                "attempt_run_id": require_string(
                    row["attempt_run_id"], path=f"{path}.attempt_run_id", maximum=240
                ),
                "profile_id": require_string(
                    row["profile_id"], path=f"{path}.profile_id", maximum=240
                ),
                "profile_config_sha256": require_sha256(
                    row["profile_config_sha256"],
                    path=f"{path}.profile_config_sha256",
                ),
            }
        )
    return normalized


def _validate_arm_coverage(value: Any) -> list[dict[str, Any]]:
    rows = require_list(value, path="$coverage.arm_coverage")
    if len(rows) != len(BENCHMARK_ARM_IDS_V1):
        raise ContractValidationError(
            "arm_exact_cover",
            "$coverage.arm_coverage",
            "coverage must contain exactly one row per benchmark arm",
        )
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(rows):
        path = f"$coverage.arm_coverage[{index}]"
        row = require_mapping(item, path=path)
        require_exact_keys(
            row,
            required={
                "arm_id",
                "expected_block_count",
                "translated_block_count",
                "unavailable_block_count",
                "status_counts",
                "unavailable_rows",
                "status",
            },
            path=path,
        )
        expected_arm = BENCHMARK_ARM_IDS_V1[index]
        status_counts_raw = require_mapping(
            row["status_counts"], path=f"{path}.status_counts"
        )
        status_counts = {
            require_enum(
                key,
                {"translated", "missing", "failed", "absent"},
                path=f"{path}.status_counts.<key>",
            ): require_int(count, path=f"{path}.status_counts.{key}", minimum=0)
            for key, count in status_counts_raw.items()
        }
        unavailable_rows = _validate_unavailable_rows(
            row["unavailable_rows"], path=f"{path}.unavailable_rows"
        )
        normalized.append(
            {
                "arm_id": require_enum(
                    row["arm_id"], {expected_arm}, path=f"{path}.arm_id"
                ),
                "expected_block_count": require_int(
                    row["expected_block_count"],
                    path=f"{path}.expected_block_count",
                    minimum=1,
                ),
                "translated_block_count": require_int(
                    row["translated_block_count"],
                    path=f"{path}.translated_block_count",
                    minimum=0,
                ),
                "unavailable_block_count": require_int(
                    row["unavailable_block_count"],
                    path=f"{path}.unavailable_block_count",
                    minimum=0,
                ),
                "status_counts": status_counts,
                "unavailable_rows": unavailable_rows,
                "status": require_enum(
                    row["status"], {"ready", "blocked"}, path=f"{path}.status"
                ),
            }
        )
    return normalized


def _validate_unavailable_rows(value: Any, *, path: str) -> list[dict[str, Any]]:
    rows = require_list(value, path=path)
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(rows):
        row_path = f"{path}[{index}]"
        row = require_mapping(item, path=row_path)
        require_exact_keys(
            row,
            required={"block_id", "status", "error_code"},
            path=row_path,
        )
        error_code = row["error_code"]
        if error_code is not None:
            error_code = require_string(
                error_code, path=f"{row_path}.error_code", maximum=240
            )
        normalized.append(
            {
                "block_id": require_string(
                    row["block_id"], path=f"{row_path}.block_id", maximum=240
                ),
                "status": require_enum(
                    row["status"],
                    {"missing", "failed", "absent"},
                    path=f"{row_path}.status",
                ),
                "error_code": error_code,
            }
        )
    require_unique(
        [row["block_id"] for row in normalized],
        path=path,
    )
    return normalized


def _validate_coverage_references(value: Mapping[str, Any]) -> None:
    expected_count = len(value["sample_block_ids"])
    expected_set = set(value["sample_block_ids"])
    ready = True
    for row in value["arm_coverage"]:
        unavailable_ids = [item["block_id"] for item in row["unavailable_rows"]]
        if any(block_id not in expected_set for block_id in unavailable_ids):
            raise ContractValidationError(
                "foreign_block",
                "$coverage.arm_coverage",
                "unavailable row references a block outside the frozen sample",
            )
        if row["expected_block_count"] != expected_count:
            raise ContractValidationError(
                "coverage_count",
                "$coverage.arm_coverage",
                "arm expected count differs from the frozen sample",
            )
        if row["unavailable_block_count"] != len(unavailable_ids):
            raise ContractValidationError(
                "coverage_count",
                "$coverage.arm_coverage",
                "unavailable count differs from unavailable rows",
            )
        if row["translated_block_count"] + row["unavailable_block_count"] != expected_count:
            raise ContractValidationError(
                "coverage_count",
                "$coverage.arm_coverage",
                "translated and unavailable counts do not exact-cover the sample",
            )
        if sum(row["status_counts"].values()) != expected_count:
            raise ContractValidationError(
                "coverage_count",
                "$coverage.arm_coverage.status_counts",
                "status counts do not exact-cover the sample",
            )
        should_be_ready = not unavailable_ids
        if row["status"] != ("ready" if should_be_ready else "blocked"):
            raise ContractValidationError(
                "coverage_status",
                "$coverage.arm_coverage",
                "arm status contradicts its unavailable rows",
            )
        ready = ready and should_be_ready
    expected_status = "ready" if ready else "blocked"
    if value["coverage_status"] != expected_status:
        raise ContractValidationError(
            "coverage_status",
            "$coverage.coverage_status",
            "overall status contradicts arm coverage",
        )


def _exact_string_list(
    value: Any, *, expected: tuple[str, ...], path: str
) -> list[str]:
    result = _string_list(value, path=path)
    if tuple(result) != expected:
        raise ContractValidationError(
            "sequence", path, f"expected exact sequence {list(expected)!r}"
        )
    return result


def _string_list(value: Any, *, path: str) -> list[str]:
    rows = require_list(value, path=path)
    result = [
        require_string(item, path=f"{path}[{index}]", maximum=240)
        for index, item in enumerate(rows)
    ]
    require_unique(result, path=path)
    return result


def _one_hash(value: Any) -> dict[str, str]:
    path = "$coverage.integrity"
    row = require_mapping(value, path=path)
    require_exact_keys(row, required={"coverage_sha256"}, path=path)
    return {
        "coverage_sha256": require_sha256(
            row["coverage_sha256"], path=f"{path}.coverage_sha256"
        )
    }


def _stable_sha256(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
