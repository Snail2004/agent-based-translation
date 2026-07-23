from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from pipeline.eval.common_input_v1 import (
    CommonSourceSnapshotV1,
    seal_translation_artifact,
    source_binding_to_dict,
    validate_translation_artifact,
)
from pipeline.eval.community_aligned_translation_v1 import (
    build_community_aligned_translation_v1,
    validate_community_aligned_translation_v1,
)
from pipeline.eval.contracts_v1 import (
    ContractValidationError,
    require_commit,
    require_rfc3339,
    require_sha256,
)
from pipeline.eval.google_translate_baseline_v1 import (
    validate_google_translate_capture_v1,
)
from pipeline.workflow_replay.contracts_v1 import canonical_sha256


__all__ = [
    "D2LFiveArmBaselineMaterialV1",
    "build_google_nmt_translation_artifact_v1",
    "build_llm_lc_translation_artifact_v1",
    "materialize_d2l_five_arm_baselines_v1",
]


_MARKER = re.compile(r"^\[\[B(?P<number>\d{4})\]\][ \t]*\r?$", re.MULTILINE)
_EXTERNAL_ARM_ORDER = ("community", "google_nmt", "llm_lc")


@dataclass(frozen=True, slots=True)
class D2LFiveArmBaselineMaterialV1:
    artifact_paths: Mapping[str, Path]
    external_translation_inputs: tuple[Mapping[str, Any], ...]


def build_google_nmt_translation_artifact_v1(
    source: CommonSourceSnapshotV1,
    *,
    capture_payloads: Sequence[Mapping[str, Any]],
    selected_chapter_ids: Sequence[str],
    created_at: str,
    producer_code_commit: str,
) -> dict[str, Any]:
    captures = tuple(
        validate_google_translate_capture_v1(payload) for payload in capture_payloads
    )
    expected_chapters = tuple(selected_chapter_ids)
    observed_chapters = tuple(row["source"]["chapter_id"] for row in captures)
    if observed_chapters != expected_chapters:
        raise ContractValidationError(
            "google_capture_chapters",
            "$.capture_payloads",
            "Google captures must exact-cover selected chapters in canonical order",
        )
    if (
        len({row["source"]["source_db_sha256"] for row in captures}) != 1
        or {(row["source"]["project_id"], row["source"]["document_id"]) for row in captures}
        != {("d2l", "d2l")}
    ):
        raise ContractValidationError(
            "google_capture_source",
            "$.capture_payloads",
            "Google captures do not share the pinned D2L source",
        )
    capture_rows = [
        item for capture in captures for item in capture["translations"]
    ]
    source_rows = list(source.blocks)
    if [row["block_id"] for row in capture_rows] != [
        row.block_id for row in source_rows
    ]:
        raise ContractValidationError(
            "google_capture_exact_cover",
            "$.capture_payloads.translations",
            "Google captures do not preserve canonical selected block order",
        )
    translations = [
        _project_external_row(
            block,
            capture_row,
            preserve_external_text=True,
            path=f"$.capture_payloads.translations[{index}]",
        )
        for index, (block, capture_row) in enumerate(
            zip(source_rows, capture_rows, strict=True)
        )
    ]
    capture_hashes = [
        row["integrity"]["capture_sha256"] for row in captures
    ]
    return _build_translation_artifact(
        source,
        arm_id="google_nmt",
        component="d2l_google_nmt_capture_bridge_v1",
        artifact_id=(
            "google-nmt-five-chapter-"
            + canonical_sha256(capture_hashes)[:24]
        ),
        logical_run_id="google-nmt-five-chapter-baseline-v1",
        attempt_run_id="google-nmt-five-chapter-baseline-v1-attempt-1",
        profile_id="evaluation.google_nmt.basic_v2.five_chapter.v1",
        profile_config_sha256=canonical_sha256(
            {
                "capture_sha256s": capture_hashes,
                "projection_policy": "canonical_admission_projection_v1",
            }
        ),
        translations=translations,
        created_at=created_at,
        producer_code_commit=producer_code_commit,
    )


def build_llm_lc_translation_artifact_v1(
    source: CommonSourceSnapshotV1,
    *,
    marked_markdown_bytes: bytes,
    expected_evidence_sha256: str,
    expected_marker_count: int,
    created_at: str,
    producer_code_commit: str,
) -> dict[str, Any]:
    expected_sha256 = require_sha256(
        expected_evidence_sha256, path="$.expected_evidence_sha256"
    )
    observed_sha256 = hashlib.sha256(marked_markdown_bytes).hexdigest()
    if observed_sha256 != expected_sha256:
        raise ContractValidationError(
            "llm_lc_evidence_hash",
            "$.marked_markdown",
            "LLM-LC evidence bytes differ from the accepted full capture",
        )
    try:
        text = marked_markdown_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ContractValidationError(
            "encoding", "$.marked_markdown", "marked Markdown must be UTF-8"
        ) from exc
    markers = list(_MARKER.finditer(text))
    numbers = [int(row.group("number")) for row in markers]
    if (
        expected_marker_count < 1
        or len(numbers) != expected_marker_count
        or numbers != list(range(1, expected_marker_count + 1))
    ):
        raise ContractValidationError(
            "llm_lc_marker_sequence",
            "$.marked_markdown",
            "LLM-LC evidence must exact-cover its declared B0001..Bnnnn sequence",
        )
    segments = {
        numbers[index]: text[
            marker.end() : (
                markers[index + 1].start()
                if index + 1 < len(markers)
                else len(text)
            )
        ].strip("\r\n")
        for index, marker in enumerate(markers)
    }
    translations = []
    for index, block in enumerate(source.blocks):
        marker_id = block.order_index + 1
        target = segments.get(marker_id)
        if target is None:
            raise ContractValidationError(
                "llm_lc_exact_cover",
                f"$.marked_markdown.B{marker_id:04d}",
                "accepted LLM-LC evidence does not cover a selected source block",
            )
        if block.admission in {"translate", "translate_structured"}:
            if not target:
                raise ContractValidationError(
                    "llm_lc_empty_translation",
                    f"$.marked_markdown.B{marker_id:04d}",
                    "translatable source block has an empty LLM-LC target",
                )
            row = {
                "block_id": block.block_id,
                "status": "translated",
                "target_text": target,
                "error_code": None,
            }
        elif block.admission == "preserve":
            # The benchmark evaluates semantic translations. Structural rows are
            # projected from canonical source rather than trusting model edits.
            row = {
                "block_id": block.block_id,
                "status": "preserved",
                "target_text": block.source_text,
                "error_code": None,
            }
        elif block.admission == "exclude":
            row = {
                "block_id": block.block_id,
                "status": "excluded",
                "target_text": None,
                "error_code": None,
            }
        elif block.admission == "review_required":
            row = {
                "block_id": block.block_id,
                "status": "review_held",
                "target_text": None,
                "error_code": None,
            }
        else:
            raise AssertionError(f"unsupported admission: {block.admission}")
        translations.append(row)
    return _build_translation_artifact(
        source,
        arm_id="llm_lc",
        component="d2l_gpt_web_capture_bridge_v1",
        artifact_id=f"gpt-web-marked-{observed_sha256[:24]}",
        logical_run_id="gpt-web-full-book-oneshot-v1",
        attempt_run_id="gpt-web-full-book-oneshot-v1-attempt-1",
        profile_id="evaluation.gpt_web.long_context_diagnostic.v1",
        profile_config_sha256=canonical_sha256(
            {
                "evidence_sha256": observed_sha256,
                "marker_count": expected_marker_count,
                "projection_policy": "canonical_preserve_and_review_projection_v1",
            }
        ),
        translations=translations,
        created_at=created_at,
        producer_code_commit=producer_code_commit,
    )


def materialize_d2l_five_arm_baselines_v1(
    source: CommonSourceSnapshotV1,
    *,
    output_root: Path,
    source_finalization_path: Path,
    admitted_projection_path: Path,
    candidate_tree_sha256: str,
    community_alignment_root: Path,
    google_capture_paths: Sequence[Path],
    selected_chapter_ids: Sequence[str],
    llm_lc_marked_path: Path,
    llm_lc_expected_sha256: str,
    llm_lc_expected_marker_count: int,
    created_at: str,
    producer_code_commit: str,
) -> D2LFiveArmBaselineMaterialV1:
    root = Path(output_root).resolve()
    created = require_rfc3339(created_at, path="$.created_at")
    commit = require_commit(producer_code_commit, path="$.producer_code_commit")
    community = build_community_aligned_translation_v1(
        source,
        alignment_bundle_root=Path(community_alignment_root),
        source_finalization_path=Path(source_finalization_path),
        created_at=created,
        producer_code_commit=commit,
    )
    google = build_google_nmt_translation_artifact_v1(
        source,
        capture_payloads=[
            _read_json(Path(path), label="Google capture")
            for path in google_capture_paths
        ],
        selected_chapter_ids=selected_chapter_ids,
        created_at=created,
        producer_code_commit=commit,
    )
    llm_lc_path = Path(llm_lc_marked_path).resolve()
    if not llm_lc_path.is_file() or llm_lc_path.is_symlink():
        raise ContractValidationError(
            "llm_lc_evidence_path",
            "$.llm_lc_marked_path",
            "LLM-LC evidence must be a regular file",
        )
    llm_lc = build_llm_lc_translation_artifact_v1(
        source,
        marked_markdown_bytes=llm_lc_path.read_bytes(),
        expected_evidence_sha256=llm_lc_expected_sha256,
        expected_marker_count=llm_lc_expected_marker_count,
        created_at=created,
        producer_code_commit=commit,
    )
    payloads = {
        "community": validate_community_aligned_translation_v1(community),
        "google_nmt": validate_translation_artifact(google),
        "llm_lc": validate_translation_artifact(llm_lc),
    }
    paths: dict[str, Path] = {}
    for arm_id in _EXTERNAL_ARM_ORDER:
        path = root / f"{arm_id}.json"
        _write_create_or_equal(path, payloads[arm_id])
        paths[arm_id] = path
    projection_binding = _projection_binding(
        Path(admitted_projection_path),
        candidate_tree_sha256=candidate_tree_sha256,
    )
    inputs = tuple(
        _external_input(
            source,
            arm_id=arm_id,
            artifact_path=paths[arm_id],
            artifact=payloads[arm_id],
            source_binding=projection_binding,
        )
        for arm_id in _EXTERNAL_ARM_ORDER
    )
    return D2LFiveArmBaselineMaterialV1(
        artifact_paths=paths,
        external_translation_inputs=inputs,
    )


def _build_translation_artifact(
    source: CommonSourceSnapshotV1,
    *,
    arm_id: str,
    component: str,
    artifact_id: str,
    logical_run_id: str,
    attempt_run_id: str,
    profile_id: str,
    profile_config_sha256: str,
    translations: Sequence[Mapping[str, Any]],
    created_at: str,
    producer_code_commit: str,
) -> dict[str, Any]:
    counts = {
        name: sum(1 for row in translations if row["status"] == status)
        for name, status in (
            ("translated_count", "translated"),
            ("preserved_count", "preserved"),
            ("excluded_count", "excluded"),
            ("review_held_count", "review_held"),
            ("missing_count", "missing"),
            ("failed_count", "failed"),
        )
    }
    payload = {
        "schema_id": "TranslationArtifactV1",
        "schema_version": "1.0.0",
        "artifact_id": artifact_id,
        "created_at": created_at,
        "producer": {
            "workstream": "d2l",
            "component": component,
            "component_version": "1.0.0",
            "code_commit": producer_code_commit,
        },
        "source_binding": source_binding_to_dict(source.source_binding),
        "run_identity": {
            "logical_run_id": logical_run_id,
            "attempt_run_id": attempt_run_id,
            "arm_id": arm_id,
            "profile_id": profile_id,
            "profile_config_sha256": profile_config_sha256,
            "source_language": "en",
            "target_language": "vi",
        },
        "translations": [dict(row) for row in translations],
        "coverage": {
            "source_block_count": len(translations),
            "eligible_count": (
                counts["translated_count"]
                + counts["missing_count"]
                + counts["failed_count"]
            ),
            **counts,
        },
        "integrity": {"artifact_sha256": "0" * 64},
    }
    return validate_translation_artifact(seal_translation_artifact(payload))


def _project_external_row(
    block: Any,
    row: Mapping[str, Any],
    *,
    preserve_external_text: bool,
    path: str,
) -> dict[str, Any]:
    status = row["status"]
    target = row["target_text"]
    if block.admission in {"translate", "translate_structured"}:
        if status != "translated" or not isinstance(target, str) or not target:
            raise ContractValidationError(
                "external_translation_status",
                path,
                "translatable block is not translated in the external capture",
            )
        return {
            "block_id": block.block_id,
            "status": "translated",
            "target_text": target,
            "error_code": None,
        }
    if block.admission == "preserve":
        if (
            status != "preserved"
            or not isinstance(target, str)
            or (
                preserve_external_text
                and target.replace("\r\n", "\n")
                != block.source_text.replace("\r\n", "\n")
            )
        ):
            raise ContractValidationError(
                "external_preserve_status",
                path,
                "preserve-only block differs from canonical source",
            )
        return {
            "block_id": block.block_id,
            "status": "preserved",
            "target_text": block.source_text,
            "error_code": None,
        }
    if block.admission == "exclude":
        return {
            "block_id": block.block_id,
            "status": "excluded",
            "target_text": None,
            "error_code": None,
        }
    if block.admission == "review_required":
        return {
            "block_id": block.block_id,
            "status": "review_held",
            "target_text": None,
            "error_code": None,
        }
    raise AssertionError(f"unsupported admission: {block.admission}")


def _external_input(
    source: CommonSourceSnapshotV1,
    *,
    arm_id: str,
    artifact_path: Path,
    artifact: Mapping[str, Any],
    source_binding: Mapping[str, str],
) -> dict[str, Any]:
    admitted = [
        row for row in source.blocks if row.admission != "review_required"
    ]
    counts = {
        "translated_block_count": sum(
            row.admission in {"translate", "translate_structured"}
            for row in admitted
        ),
        "preserved_block_count": sum(
            row.admission == "preserve" for row in admitted
        ),
        "excluded_block_count": sum(
            row.admission == "exclude" for row in admitted
        ),
        "review_held_block_count": 0,
        "missing_block_count": 0,
        "failed_block_count": 0,
    }
    return {
        "arm_id": arm_id,
        "translation_artifact": {
            "artifact_ref": f"baselines/{arm_id}.json",
            "artifact_kind": "translation_artifact",
            "schema_version": str(artifact["schema_version"]),
            "sha256": _physical_sha256(artifact_path),
            "sha256_kind": "physical",
        },
        "producer": {
            "component_id": f"baseline_{arm_id}_v1",
            "component_run_id": str(
                artifact.get("run_identity", {}).get(
                    "logical_run_id",
                    artifact.get("artifact_id"),
                )
            ),
        },
        "coverage": {
            "expected_block_count": len(admitted),
            "block_universe_sha256": canonical_sha256(
                [row.block_id for row in admitted]
            ),
            **counts,
        },
        "source_binding": dict(source_binding),
    }


def _projection_binding(
    path: Path,
    *,
    candidate_tree_sha256: str,
) -> dict[str, str]:
    candidate = require_sha256(
        candidate_tree_sha256, path="$.candidate_tree_sha256"
    )
    resolved = Path(path).resolve()
    if not resolved.is_file() or resolved.is_symlink():
        raise ContractValidationError(
            "admitted_projection_path",
            "$.admitted_projection_path",
            "admitted projection must be a regular file",
        )
    payload = _read_json(resolved, label="admitted projection")
    return {
        "artifact_ref": f"srcpkg_{candidate[:16]}_projection",
        "artifact_kind": "admitted_projection",
        "schema_version": str(payload["schema_version"]),
        "sha256": _physical_sha256(resolved),
        "sha256_kind": "physical",
    }


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    resolved = Path(path).resolve()
    if not resolved.is_file() or resolved.is_symlink():
        raise ContractValidationError(
            "missing_artifact", f"$.{label}", f"file does not exist: {resolved}"
        )
    try:
        value = json.loads(resolved.read_text("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractValidationError(
            "invalid_json", f"$.{label}", f"{label} is not strict UTF-8 JSON"
        ) from exc
    if not isinstance(value, dict):
        raise ContractValidationError(
            "type", f"$.{label}", f"{label} must be a JSON object"
        )
    return value


def _write_create_or_equal(path: Path, payload: Mapping[str, Any]) -> None:
    data = (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != data:
            raise ContractValidationError(
                "immutable_output",
                str(path),
                "existing baseline artifact differs from deterministic output",
            )
        return
    path.write_bytes(data)


def _physical_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
